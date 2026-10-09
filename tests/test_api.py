from io import BytesIO
import sqlite3

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from PIL import Image

from backend import main
from backend.api import routes as api
from backend.extraction import agent
from backend.extraction.models import ClaimExtractionResult
from backend.extraction.materials import LinkMaterial
from backend.storage.repository import StorageRepository
from backend.verification.models import ClaimFinding, Evidence, SubgraphResult
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.state import SubgraphState


def png(color="blue"):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_persists_and_submission_recovers_in_request_order(tmp_path):
    captured = {}

    async def skip_fact(*args):
        return "[]"

    class Reader:
        async def read_note(self, url, **kwargs):
            return LinkMaterial(url, url, '标题', '正文')

    capabilities = VerificationCapabilities(llm=skip_fact, evidence_sources={'xiaohongshu': Reader()})

    async def extract(target_place, description_text, links, images, *, link_materials):
        assert [item.original_url for item in link_materials] == links
        captured.update(target_place=target_place, text=description_text, links=links, images=images)
        return ClaimExtractionResult(target_place="Agent 改写的地点", claims=[{
            "claim_id": "wrong-id", "type": "CROWD", "content": "周末人少。",
            "sources": [{"source_type": "TEXT", "source_ref": "description", "source_text": description_text}],
        }])

    with TestClient(main.create_app(tmp_path, extract, capabilities=capabilities)) as client:
        codes = []
        for color in ("red", "blue"):
            response = client.post("/api/files", files={"file": ("../test.png", png(color), "image/jpeg")})
            assert response.status_code == 201
            data = response.json()
            assert data["file_code"] == data["file_id"]
            assert data["mime_type"] == "image/png"
            codes.append(data["file_code"])
        assert codes[0] != codes[1]
    # 重启应用，确认数据不只是保存在内存。
    with TestClient(main.create_app(tmp_path, extract, capabilities=capabilities)) as client:
        payload = {"target_place": " 上海迪士尼乐园 ", "text": "周末人少", "image": codes[::-1],
                   "link": [f"https://www.xiaohongshu.com/explore/{number:024x}" for number in (1, 2)]}
        response = client.post("/api/verifications", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert set(data) == {"run_id", "context", "claims", "subgraph_results", "conflicts", "status"}
        assert data["run_id"]
        assert data["context"]["target_place"] == payload["target_place"]
        assert data["context"]["checked_at"]
        assert data["context"]["resolved_place"] is None
        # status 描述执行情况，不代表主张真实性；四类子图都跑完即为 completed。
        assert data["status"] == "completed"
        assert set(data["subgraph_results"]) == {"fact", "route", "crowd", "experience"}
        assert all(data["subgraph_results"][name]["status"] == "skipped"
                   for name in ("fact", "route", "crowd", "experience"))
        assert data["claims"] == [{
            "claim_id": "claim_001", "type": "CROWD", "content": "周末人少。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": payload["text"]}],
        }]
        assert captured["target_place"] == payload["target_place"]
        assert captured["text"] == payload["text"]
        assert captured["links"] == payload["link"]
        assert [image.file_code for image in captured["images"]] == codes[::-1]
        assert [image.data for image in captured["images"]] == [png("blue"), png("red")]
        assert client.get("/").status_code == 200
        operation = client.get("/openapi.json").json()["paths"]["/api/verifications"]["post"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/VerificationRun",
        }
    with sqlite3.connect(tmp_path / "files.sqlite3") as db:
        rows = db.execute('SELECT file_code, size, "order" FROM files ORDER BY "order"').fetchall()
    assert rows == [(codes[0], len(png("red")), 1), (codes[1], len(png("blue")), 2)]


@pytest.mark.parametrize("failed_graphs,status", [
    (set(), "completed"),
    ({"second"}, "partial"),
    ({"first", "second"}, "failed"),
])
def test_verification_response_preserves_subgraph_results(tmp_path, failed_graphs, status):
    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[{
            "claim_id": "original", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "免费开放"}],
        }])

    def subgraph(name):
        async def check(state: SubgraphState):
            if name in failed_graphs:
                raise RuntimeError("测试证据来源不可用")
            claim_id = state["claims"][0].claim_id
            return {"result": SubgraphResult(
                graph_name=name, status="completed", selected_claim_ids=[claim_id],
                findings=[ClaimFinding(claim_id=claim_id, summary="已找到开放信息", evidence=[
                    Evidence(source="test", content="免费开放", url="https://example.com/park"),
                ])],
            )}

        builder = StateGraph(SubgraphState)
        builder.add_node("check", check)
        builder.add_edge(START, "check")
        builder.add_edge("check", END)
        return builder.compile()

    subgraphs = {name: subgraph(name) for name in ("first", "second")}
    with TestClient(main.create_app(tmp_path, extract, subgraphs=subgraphs)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园", "text": "免费开放"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == status
    assert data["context"]["target_place"] == "公园"
    assert data["claims"][0]["claim_id"] == "claim_001"
    assert set(data["subgraph_results"]) == set(subgraphs)
    for name, result in data["subgraph_results"].items():
        assert result["graph_name"] == name
        if name in failed_graphs:
            assert result["status"] == "failed"
            assert "测试证据来源不可用" in result["error"]
        else:
            assert result["status"] == "completed"
            assert result["selected_claim_ids"] == ["claim_001"]
            assert result["findings"] == [{
                "claim_id": "claim_001", "summary": "已找到开放信息",
                "evidence": [{
                    "source": "test", "content": "免费开放", "url": "https://example.com/park",
                    "evidence_id": None, "source_type": None, "published_at": None, "retrieved_at": None,
                }],
                "assessment": None, "error": None,
            }]


def test_verification_with_no_claims_returns_full_run(tmp_path):
    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[])

    with TestClient(main.create_app(tmp_path, extract)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园"})

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"]
    assert data["context"]["target_place"] == "公园"
    assert data["status"] == "no_claims"
    assert data["claims"] == []
    assert data["subgraph_results"] == {}


def test_invalid_material_is_rejected_before_calling_agent(tmp_path, monkeypatch):
    async def unexpected_call(*args):
        pytest.fail("无效材料不应进入 Agent")

    with TestClient(main.create_app(tmp_path, unexpected_call)) as client:
        assert client.post("/api/verifications", json={"target_place": " "}).status_code == 422
        assert client.post("/api/verifications", json={"target_place": "公园", "link": ["file:///etc/passwd"]}).status_code == 422
        assert client.post("/api/files", files={"file": ("x.png", b"not an image", "image/png")}).status_code == 415
        monkeypatch.setattr(api, "MAX_FILE_SIZE", 10)
        assert client.post("/api/files", files={"file": ("x.png", png(), "image/png")}).status_code == 413
        response = client.post("/api/verifications", json={"target_place": "公园", "image": ["unknown"]})
        assert response.status_code == 404
        assert response.json() == {"detail": "图片不存在：unknown"}

        storage = StorageRepository(tmp_path)
        code = storage.save("image.png", "image/png", png())
        (storage.uploads / code).unlink()
        response = client.post("/api/verifications", json={"target_place": "公园", "image": [code]})
        assert response.status_code == 404
        assert response.json() == {"detail": f"图片不存在：{code}"}


def test_missing_model_configuration_returns_503(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "__file__", str(tmp_path / "extraction" / "agent.py"))
    (tmp_path / "config.example.toml").write_text('api_key = ""', encoding="utf-8")

    with TestClient(main.create_app(tmp_path)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园"})

    assert response.status_code == 503
    assert response.json() == {"detail": "请先填写 backend/config.toml 中的 api_key、model 和 base_url"}


@pytest.mark.parametrize("timeout,status,detail", [
    (True, 504, "提取超时，请减少材料后重试"),
    (False, 502, "模型返回的材料来源与本次提交不一致，来源校验未通过；请重试"),
])
def test_extraction_errors_return_expected_responses(tmp_path, timeout, status, detail):
    async def extract(*args):
        if timeout:
            raise TimeoutError("模型请求超时")
        return ClaimExtractionResult(target_place="公园", claims=[{
            "claim_id": "claim_001", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "IMAGE", "source_ref": "unsubmitted", "source_text": None}],
        }])

    with TestClient(main.create_app(tmp_path, extract)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园"})

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_unsubmitted_source_failure_does_not_expose_reference(tmp_path, caplog):
    unknown_ref = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=test-sensitive"

    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=[{
            "claim_id": "claim_001", "type": "FACT", "content": "免费开放。",
            "sources": [{"source_type": "LINK", "source_ref": unknown_ref, "source_text": None}],
        }])

    with TestClient(main.create_app(tmp_path, extract)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园", "text": "免费开放。"})

    assert response.status_code == 502
    assert "本次提交不一致" in response.json()["detail"]
    assert unknown_ref not in response.text and "test-sensitive" not in caplog.text
    error = next(record.exc_info[1] for record in caplog.records if record.exc_info)
    assert all(set(issue) == {"claim_index", "source_index", "source_type", "reason"} for issue in error.issues)


def test_generic_agent_failure_keeps_502_response(tmp_path):
    from backend.common.errors import ExtractionFailed

    async def extract(*args):
        raise ExtractionFailed("Agent 未完成提取")

    with TestClient(main.create_app(tmp_path, extract)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园", "text": "免费开放。"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Agent 提取失败，请检查模型配置、浏览器及链接；详情见后端日志"}


@pytest.mark.parametrize("error", [
    FileNotFoundError("prompt.md"),
    PermissionError("配置文件无法读取"),
    sqlite3.OperationalError("数据库不可用"),
    TypeError("内部类型错误"),
])
def test_internal_errors_return_500_and_log_cause(tmp_path, caplog, error):
    async def extract(*args):
        raise error

    with TestClient(main.create_app(tmp_path, extract), raise_server_exceptions=False) as client:
        response = client.post("/api/verifications", json={"target_place": "公园"})

    assert response.status_code == 500
    assert response.json() == {"detail": "服务内部错误，请稍后重试"}
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
