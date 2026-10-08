"""真实 Chromium 展示四类判定词表、路线数值对比与冲突标记；响应仅由模型校验生成。"""
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from test_frontend_status import serve

from backend.verification.models import VerificationRun

pytestmark = pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"
)

# 每类词表与它自己的字面标签；跨类别同名词（UNVERIFIED）必须按各自类别显示。
CATEGORIES = {
    "fact": ("FACT", {
        "SUPPORTED": "有证据支持", "CONTRADICTED": "与证据矛盾",
        "CONDITIONAL": "有条件成立", "UNVERIFIED": "证据不足，未能确认",
    }),
    "route": ("ROUTE", {
        "MATCHED": "数值吻合", "MISMATCHED": "数值冲突",
        "CONDITION_MISMATCH": "条件不符", "UNVERIFIED": "证据不足，未能确认",
    }),
    "crowd": ("CROWD", {
        "SUPPORTED": "当前证据支持", "NOT_SUPPORTED": "当前证据不支持",
        "SCENARIO_ONLY": "仅特定场景成立", "UNVERIFIED": "证据不足，未能确认",
    }),
    "experience": ("EXPERIENCE", {
        "CONSISTENT": "体验较一致", "DIVERGENT": "存在明显分化",
        "SCENARIO_DEPENDENT": "高度依赖场景", "UNVERIFIED": "证据不足，未能确认",
    }),
}
ALL_LABELS = {label for _, vocab in CATEGORIES.values() for label in vocab.values()}


def assessment(name, verdict, *, sufficient=True):
    fields = {
        "target": "测试目标", "time_scope": "本次核验时间", "verdict": verdict,
        "confidence": 0.8 if sufficient else None, "evidence_sufficient": sufficient,
        "reason": f"{verdict} 的本地测试判定理由。", "conditions": [],
        "supporting_evidence": ["e0"], "counter_evidence": ["e1"], "context_evidence": [],
        "kind": name,
    }
    if name == "fact":
        fields["dimensions"] = {key: None for key in (
            "authority", "directness", "recency", "context_match", "independence")}
        fields["remaining_gaps"] = []
        # 事实判定要求 CONDITIONAL 说明成立条件，模型校验会拒绝空条件。
        if verdict == "CONDITIONAL":
            fields["conditions"] = ["仅限工作日白天开放。"]
    if name == "route":
        fields.update(claimed_seconds=300, measured_seconds=1080, transport_mode="步行",
                      distance_meters=1200, tolerance_seconds=120)
    if name == "crowd":
        fields.update(scenario="节假日白天", evidence_time_coverage="仅覆盖工作日公告")
    if name == "experience":
        fields["source_agreement"] = 0.35
    return fields


def finding(name, verdict, claim_id="c0", *, sufficient=True):
    return {
        "claim_id": claim_id, "summary": f"{name} 的本地测试摘要。",
        "evidence": [{"evidence_id": "e0", "source": "本地测试来源", "source_type": "WEB",
                      "content": "本地测试证据正文。", "url": "https://example.com/notice"}],
        "assessment": assessment(name, verdict, sufficient=sufficient),
    }


def subgraph(name, findings, status="completed"):
    return {"graph_name": name, "status": status,
            "selected_claim_ids": [item["claim_id"] for item in findings], "findings": findings}


def claim(claim_id, text, claim_type):
    return {"claim_id": claim_id, "type": claim_type, "content": text,
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": text}]}


def run_output(run_id, claims, results, conflicts=None):
    return VerificationRun.model_validate({
        "run_id": run_id, "status": "completed",
        "context": {"target_place": "测试公园", "checked_at": "2026-10-06T08:00:00+00:00"},
        "claims": claims, "subgraph_results": results, "conflicts": conflicts or [],
    }).model_dump(mode="json")


def single_category_output(run_id, name, verdict):
    claim_type = CATEGORIES[name][0]
    content = f"{claim_type} 主张：{verdict}。"
    return run_output(
        run_id, [claim("c0", content, claim_type)],
        {name: subgraph(name, [finding(name, verdict, sufficient=verdict != "UNVERIFIED")])},
    )


@pytest.fixture(scope="module")
def verdict_page(browser):
    app = FastAPI()
    outputs = []

    @app.post("/api/verifications")
    async def verification(request: Request):
        return outputs.pop(0)

    app.mount("/", StaticFiles(directory=Path(__file__).parents[1] / "frontend", html=True))
    with serve(app) as base_url:
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            yield page, base_url, outputs
            assert not errors
        finally:
            page.close()


def show(page, base_url, outputs, output):
    outputs[:] = [output]
    page.goto(base_url)
    page.locator("#place").fill("测试公园")
    page.locator("#description").fill("本地测试材料")
    page.locator("#submit").click()
    page.wait_for_function(
        """runId => !document.querySelector('#fields').disabled &&
          JSON.parse(document.querySelector('#claims').textContent).run_id === runId""",
        arg=output["run_id"],
    )


@pytest.mark.parametrize("name,verdict", [
    (name, verdict) for name, (_, vocab) in CATEGORIES.items() for verdict in vocab
])
def test_each_category_renders_its_own_verdict_words(verdict_page, name, verdict):
    page, base_url, outputs = verdict_page
    output = single_category_output(f"verdict-{name}-{verdict}", name, verdict)
    label = CATEGORIES[name][1][verdict]
    show(page, base_url, outputs, output)
    card = page.locator("#findings article.claim-result").first
    assert page.locator("#findings article.claim-result").count() == 1
    verdict_node = card.locator(".verdict")
    assert verdict_node.inner_text() == label
    assert verdict_node.get_attribute("data-verdict") == verdict
    text = card.inner_text()
    assert f"{verdict} 的本地测试判定理由。" in text
    assert "本地测试来源" in text
    # 显示别类词表的标签即为误映射，比空白更隐蔽。
    for other in ALL_LABELS - {label}:
        assert other not in text, f"{name}/{verdict} 显示了别类词表的标签：{other}"


@pytest.mark.parametrize("name", list(CATEGORIES))
def test_unverified_reads_as_insufficient_evidence(verdict_page, name):
    page, base_url, outputs = verdict_page
    show(page, base_url, outputs, single_category_output(f"unverified-{name}", name, "UNVERIFIED"))
    card = page.locator("#findings article.claim-result").first
    assert card.locator(".verdict").inner_text() == "证据不足，未能确认"
    text = card.inner_text()
    assert "证据不足" in text
    for passed in ("有证据支持", "数值吻合", "当前证据支持", "体验较一致", "核验失败"):
        assert passed not in text


@pytest.mark.parametrize("name", ["route", "crowd", "experience"])
def test_insufficient_evidence_in_any_category_is_reported_at_top_level(verdict_page, name):
    page, base_url, outputs = verdict_page
    show(page, base_url, outputs, single_category_output(f"insufficient-{name}", name, "UNVERIFIED"))
    message = page.locator("#status").inner_text()
    assert "证据不足，未能确认" in message
    assert "这不表示这些说法是假的" in message
    assert page.locator("#status").get_attribute("class") == "error"


@pytest.mark.parametrize("verdict", ["MATCHED", "MISMATCHED", "CONDITION_MISMATCH", "UNVERIFIED"])
def test_route_comparison_shows_claimed_measured_distance_and_mode(verdict_page, verdict):
    page, base_url, outputs = verdict_page
    show(page, base_url, outputs, single_category_output(f"route-compare-{verdict}", "route", verdict))
    comparison = page.locator("#findings article.claim-result .route-compare").first.inner_text()
    assert "主张 5 分钟" in comparison
    assert "实测 18 分钟" in comparison
    assert "步行" in comparison
    assert "1.2 公里" in comparison


def test_conflicting_verdicts_are_flagged_with_both_sides(verdict_page):
    page, base_url, outputs = verdict_page
    content = "地铁站步行 5 分钟。"
    detail = "route 判「MISMATCHED」：实测 18 分钟；fact 判「SUPPORTED」：步行 5 分钟属实"
    fact, route = finding("fact", "SUPPORTED"), finding("route", "MISMATCHED")
    output = run_output(
        "conflict-run", [claim("c0", content, "ROUTE")],
        {"fact": subgraph("fact", [fact]), "route": subgraph("route", [route])},
        conflicts=[{"claim_id": "c0", "by_graph": {"fact": fact, "route": route}, "detail": detail}],
    )
    show(page, base_url, outputs, output)
    section = page.locator("#conflicts")
    assert section.is_visible()
    text = section.inner_text()
    assert "同一条说法的结论互相矛盾" in text
    assert content in text
    assert detail in text
    assert "事实核验：有证据支持" in text
    assert "路线核验：数值冲突" in text
    assert "fact 的本地测试摘要。" in text and "route 的本地测试摘要。" in text
    # 冲突只是额外标记，两侧发现本身仍各自成卡。
    assert page.locator("#findings article.claim-result").count() == 2


def test_clean_run_renders_no_conflict_section(verdict_page):
    page, base_url, outputs = verdict_page
    show(page, base_url, outputs, single_category_output("clean-run", "route", "MATCHED"))
    assert not page.locator("#conflicts").is_visible()
    assert page.locator("#conflicts .claim-conflict").count() == 0
    assert page.locator("#conflicts").inner_text() == ""
    assert "互相矛盾" not in page.locator("body").inner_text()
