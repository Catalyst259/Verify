"""HTTP integration with the real Fact graph and synthetic online-source actions."""

from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
import pytest

from backend import main
from backend.extraction import agent
from backend.extraction.models import ClaimExtractionResult
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import Evidence
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.facts import search
from test_fact_workflow import assessment, make_plan


INPUT_ID = "f" * 24


async def extract_claim(*args):
    return ClaimExtractionResult(target_place="公园", claims=[{
        "claim_id": "original", "type": "FACT", "content": "公园有停车场。",
        "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "公园有停车场。"}],
    }])


async def fact_model(prompt, task):
    state = json.loads(task)
    if prompt.startswith("# Fact Plan"):
        return json.dumps([make_plan(identity) for identity in state["active_claim_ids"]])
    return json.dumps([{"claim_id": identity, "assessment": assessment(
        state["claim_states"][identity], sufficient=True,
    )} for identity in state["active_claim_ids"]])


class OnlineSource:
    """Exercise the injected action controller without a site or browser account."""

    def __init__(self, *, per_query=10, fail_after=None):
        self.per_query = per_query
        self.fail_after = fail_after
        self.calls = []
        self.closed = False

    async def search(self, query, *, execute, excluded_ids, deadline_at):
        self.calls.append((query, excluded_ids, deadline_at))
        offset = (len(self.calls) - 1) * self.per_query
        rows = [{"note_id": f"{offset + number:024x}"} for number in range(1, self.per_query + 1)]

        async def candidates():
            return rows

        result = await execute(candidates, query=True)
        evidence = []
        for index, row in enumerate(result.get("data", [])):
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("采集部分结果后需要手动登录")
            if row["note_id"] in excluded_ids:
                continue

            async def read(row=row):
                return {"source": "小红书 · 合成作者", "content": "停车场体验 " + row["note_id"],
                        "url": "https://www.xiaohongshu.com/explore/" + row["note_id"],
                        "source_type": "WEB", "published_at": None}

            result = await execute(read)
            if result.get("stopped"):
                break
            if "data" in result and not result["data"].get("duplicate"):
                evidence.append(Evidence.model_validate(result["data"]))
        return evidence

    async def aclose(self):
        self.closed = True


def run_api(tmp_path, monkeypatch, source, *, queries=("公园",)):
    sessions = []

    async def page_data(browser, url, script, **kwargs):
        if "duckduckgo.com" in url:
            return {"results": [{"title": "公园公告", "url": "https://example.com/notice"}], "empty": False}
        return {"source": "公园公告", "content": "停车场对游客开放。", "url": "https://example.com/notice",
                "published_at": "2026-10-06"}

    monkeypatch.setattr(search, "page_data", page_data)

    async def runner(session, prompt, task):
        sessions.append(session)
        tools = search.create_tools(session, object())
        for query in queries:
            result = await tools.registry.registry.actions["search_web"].function(query=query)
            assert json.loads(result.extracted_content)["data"][0]["url"] == "https://example.com/notice"
        await tools.registry.registry.actions["read_page"].function(
            url="https://example.com/notice", source_type="OFFICIAL",
        )
        return session.result().model_dump_json()

    capabilities = VerificationCapabilities(
        evidence_sources={"xiaohongshu": source}, llm=fact_model,
        search=runner, subgraph_timeout_seconds=20,
    )
    payload = {"target_place": "公园", "text": "公园有停车场。", "image": [],
               "link": [f"http://www.xiaohongshu.com:80/explore/{INPUT_ID}?xsec_token=synthetic"]}
    with TestClient(main.create_app(tmp_path, extract_claim, subgraphs={"fact": build_fact_subgraph()},
                                   capabilities=capabilities)) as client:
        schema = client.get("/openapi.json").json()["components"]["schemas"]["VerificationRequest"]
        assert set(schema["properties"]) == {"target_place", "text", "link", "image"}
        response = client.post("/api/verifications", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"run_id", "context", "claims", "subgraph_results", "conflicts", "status"}
    assert body["status"] == "completed"
    assert body["claims"][0]["sources"][0]["source_type"] == "TEXT"
    assert capabilities.input_urls == () and not source.closed
    finding = body["subgraph_results"]["fact"]["findings"][0]
    assert finding["error"] is None and finding["assessment"]["verdict"] == "SUPPORTED"
    assert finding["assessment"]["supporting_evidence"] == [finding["evidence"][-1]["evidence_id"]]
    assert set(finding["assessment"]["context_evidence"]) == {item["evidence_id"] for item in finding["evidence"]}
    assert all(excluded == frozenset({INPUT_ID}) and deadline.tzinfo is not None
               for _, excluded, deadline in source.calls)
    return body, sessions[0]


def test_one_http_search_registers_ten_online_notes_and_web_evidence(tmp_path, monkeypatch):
    source = OnlineSource()
    before = datetime.now(timezone.utc)
    body, session = run_api(tmp_path, monkeypatch, source)
    evidence = body["subgraph_results"]["fact"]["findings"][0]["evidence"]
    assert [query for query, _, _ in source.calls] == ["公园"]
    assert len(evidence) == 11 and len({item["evidence_id"] for item in evidence}) == 11
    assert all(item["source_type"] == "WEB" and datetime.fromisoformat(item["retrieved_at"]) >= before
               for item in evidence[:-1])
    assert evidence[-1]["source_type"] == "OFFICIAL"
    assert session.round.queries == 2 and session.round.results_per_query == [1, 10]
    assert session.round.tool_calls == 13 and session.round.new_evidence_count == 11


def test_each_http_web_query_automatically_searches_the_injected_source(tmp_path, monkeypatch):
    source = OnlineSource(per_query=1)
    body, session = run_api(tmp_path, monkeypatch, source, queries=("公园", "公园 停车场"))
    assert [query for query, _, _ in source.calls] == ["公园", "公园 停车场"]
    assert len(body["subgraph_results"]["fact"]["findings"][0]["evidence"]) == 3
    assert session.round.tool_calls == 7 and session.round.queries == 4


def test_full_dual_source_queries_leave_room_for_web_body(tmp_path, monkeypatch):
    source = OnlineSource()
    body, session = run_api(tmp_path, monkeypatch, source, queries=("公园", "公园 停车场"))
    evidence = body["subgraph_results"]["fact"]["findings"][0]["evidence"]
    assert [query for query, _, _ in source.calls] == ["公园", "公园 停车场"]
    assert len(evidence) == 15 and len({item["evidence_id"] for item in evidence}) == 15
    assert all(item["source_type"] == "WEB" for item in evidence[:-1])
    assert evidence[-1]["source_type"] == "OFFICIAL"
    assert session.round.tool_calls == 19 and session.round.new_evidence_count == 15


def test_partial_online_failure_keeps_notes_and_continues_web_evidence(tmp_path, monkeypatch):
    source = OnlineSource(fail_after=2)
    body, session = run_api(tmp_path, monkeypatch, source)
    fact = body["subgraph_results"]["fact"]
    assert len(fact["findings"][0]["evidence"]) == 3
    assert fact["findings"][0]["evidence"][-1]["source_type"] == "OFFICIAL"
    assert "手动登录" in session.round.search_error
    assert any("手动登录" in note for note in fact["notes"])


def test_factory_can_start_without_model_key_and_closes_its_owned_source(tmp_path, monkeypatch):
    source, captured = OnlineSource(), {}
    config = {"api_key": "", "xiaohongshu": {"max_results": 10}}
    monkeypatch.setattr(main, "read_config", lambda: config)
    original_service = main.VerificationService

    def service(*args, **kwargs):
        captured["capabilities"] = kwargs["capabilities"]
        return original_service(*args, **kwargs)

    def factory(actual, *, root, data_directory):
        captured.update(config=actual, root=root, data_directory=data_directory)
        return source

    monkeypatch.setattr(main.XiaohongshuSource, "from_config", factory)
    monkeypatch.setattr(main, "VerificationService", service)
    monkeypatch.setattr(agent, "read_config", lambda: config)
    with TestClient(main.create_app(tmp_path)) as client:
        assert client.get("/").status_code == 200 and not source.closed
        response = client.post("/api/verifications", json={"target_place": "公园"})
        assert response.status_code == 503 and "api_key" in response.json()["detail"]
    assert source.closed and not source.calls
    capabilities = captured.pop("capabilities")
    assert set(capabilities.evidence_sources) == {"web_search", "xiaohongshu"}
    assert capabilities.evidence_sources["xiaohongshu"] is source
    assert captured == {"config": config, "root": main.ROOT, "data_directory": tmp_path}


def test_explicit_capabilities_skip_source_configuration_and_keep_external_owner(tmp_path, monkeypatch):
    source = OnlineSource()

    def unexpected(*args, **kwargs):
        pytest.fail("显式注入 capabilities 时不应读取配置或构造其他来源")

    monkeypatch.setattr(main, "read_config", unexpected)
    monkeypatch.setattr(main.XiaohongshuSource, "from_config", unexpected)

    async def empty(*args):
        return ClaimExtractionResult(target_place="公园", claims=[])

    capabilities = VerificationCapabilities(evidence_sources={"xiaohongshu": source}, search=None)
    with TestClient(main.create_app(tmp_path, empty, capabilities=capabilities)) as client:
        response = client.post("/api/verifications", json={"target_place": "公园"})
        assert response.status_code == 200 and response.json()["status"] == "no_claims"
    assert not source.closed and not source.calls
