"""真实 Chromium 展示结构化核验结果；仅返回经模型验证的本地合成响应。"""
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from test_frontend_status import run_result, serve

from backend.verification.models import VerificationRun

pytestmark = pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"
)


def readable_result(verdict="SUPPORTED", *, run_id="readable-result"):
    output = run_result("partial", "completed")
    output["run_id"] = run_id
    finding = output["subgraph_results"]["fact"]["findings"][0]
    finding["evidence"] = [
        {"evidence_id": f"e{index}", "source": f"测试来源 {index}", "source_type": "WEB",
         "content": f"证据正文 {index}：停车场开放情况。", "url": f"https://example.com/notice/{index}",
         "published_at": "2026-10-05", "retrieved_at": "2026-10-06T08:00:00+00:00"}
        for index in range(4)
    ]
    assessment = finding["assessment"]
    assessment.update(
        verdict=verdict, reason=f"{verdict}：本地测试的明确判定理由。e0 支持，e1 反证。",
        supporting_evidence=["e0"], counter_evidence=["e1"], context_evidence=["e2"],
        evidence_sufficient=verdict != "UNVERIFIED", confidence=None if verdict == "UNVERIFIED" else 0.8,
        conditions=["仅限工作日白天开放。"] if verdict == "CONDITIONAL" else [],
        remaining_gaps=[{"question": "节假日是否同样开放？", "preferred_source": "OFFICIAL",
                         "reason": "尚缺节假日的正式公告。"}],
    )
    return VerificationRun.model_validate(output).model_dump(mode="json")


@contextmanager
def result_page(browser, outputs):
    app = FastAPI()
    submissions = []

    @app.post("/api/verifications")
    async def verification(request: Request):
        submissions.append(await request.json())
        return outputs[len(submissions) - 1]

    app.mount("/", StaticFiles(directory=Path(__file__).parents[1] / "frontend", html=True))
    with serve(app) as base_url:
        page = browser.new_page(viewport={"width": 1000, "height": 720})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(base_url)
            yield page, submissions
            assert not errors
        finally:
            page.close()


def submit_result(page, output):
    page.locator("#place").fill("测试公园")
    page.locator("#description").fill("停车场开放")
    page.locator("#submit").click()
    page.wait_for_function(
        """runId => !document.querySelector('#fields').disabled &&
          !document.querySelector('#result').hidden &&
          JSON.parse(document.querySelector('#claims').textContent).run_id === runId""",
        arg=output["run_id"],
    )


@pytest.mark.parametrize("verdict,label", [
    ("SUPPORTED", "有证据支持"),
    ("CONTRADICTED", "与证据矛盾"),
    ("CONDITIONAL", "有条件成立"),
    ("UNVERIFIED", "证据不足，未能确认"),
])
def test_readable_verdict_reasons_citations_and_raw_response(browser, verdict, label):
    output = readable_result(verdict)
    finding = output["subgraph_results"]["fact"]["findings"][0]
    with result_page(browser, [output]) as (page, submissions):
        submit_result(page, output)
        cards = page.locator("#findings article.claim-result")
        assert cards.count() == 1
        card = cards.first
        assert card.locator(".verdict").inner_text() == label
        text = card.inner_text()
        assert output["claims"][0]["content"] in text
        assert card.locator(".reason").inner_text() == (
            f"{verdict}：本地测试的明确判定理由。【证据 1】 支持，【证据 2】 反证。"
        )
        for condition in finding["assessment"]["conditions"]:
            assert condition in text
        for gap in finding["assessment"]["remaining_gaps"]:
            assert gap["question"] in text
            assert gap["reason"] in text

        evidence = card.locator("details.evidence")
        assert evidence.count() == 4
        for index, role in enumerate(("支持", "反证", "背景", "未被本次判定引用")):
            item = evidence.nth(index)
            assert role in item.locator(":scope > summary").inner_text()
            assert f"证据 {index + 1}" in item.locator(":scope > summary").inner_text()
            assert (item.get_attribute("open") is not None) == (index < 3)
            assert f"测试来源 {index}" in item.text_content()
            assert finding["evidence"][index]["content"] in item.text_content()
            assert item.locator("a").get_attribute("href") == finding["evidence"][index]["url"]
            original = item.locator("details")
            assert original.count() == 1
            assert original.get_attribute("open") is None
            assert "查看证据原文" in original.locator("summary").text_content()

        raw = page.locator("details#raw-result")
        assert raw.get_attribute("open") is None
        assert not page.locator("#claims").is_visible()
        assert json.loads(page.locator("#claims").text_content()) == output
        assert page.evaluate("document.activeElement.id") == "result"
        assert page.locator("#result").evaluate(
            "node => { const box = node.getBoundingClientRect(); return box.top < innerHeight && box.bottom > 0; }"
        )
        assert submissions == [{"target_place": "测试公园", "text": "停车场开放", "link": [], "image": []}]


def test_assessments_and_execution_failures_remain_distinct(browser):
    missing = run_result("partial", "partial")
    missing["run_id"] = "readable-no-assessment"
    missing["subgraph_results"]["fact"]["findings"][0].update(
        assessment=None, error="Validate: 测试材料尚未形成判定。"
    )
    failed = run_result("failed", "failed", unavailable=False)
    failed["run_id"] = "readable-failed"
    unavailable = run_result("not_implemented", "not_implemented")
    unavailable["run_id"] = "readable-unimplemented"
    retained = readable_result(run_id="readable-retained-assessment")
    retained["status"] = "failed"
    retained["subgraph_results"] = {"fact": retained["subgraph_results"]["fact"]}
    retained["subgraph_results"]["fact"].update(status="failed", error="Search: 后续补充查询失败。")
    route_fact = readable_result(run_id="readable-route-fact")
    route_fact["claims"][0]["type"] = "ROUTE"
    route_fact["subgraph_results"]["fact"].update(status="partial", error="Search: 补充路线事实证据超时。")
    route_fact["subgraph_results"]["fact"]["findings"][0]["error"] = None
    outputs = [VerificationRun.model_validate(item).model_dump(mode="json")
               for item in (missing, failed, unavailable, retained, route_fact)]
    with result_page(browser, outputs) as (page, submissions):
        for output, expected in zip(outputs, (
            "核验仅完成一部分", "核验失败", "核验尚未实现", "核验失败", "事实核验仅完成一部分"
        ), strict=True):
            submit_result(page, output)
            assert expected in page.locator("#status").inner_text()
            assert page.locator("#findings article.claim-result").count() == 1
            assert output["claims"][0]["content"] in page.locator("#findings").inner_text()
            if output["run_id"] in {retained["run_id"], route_fact["run_id"]}:
                assert "有证据支持" in page.locator("#findings").inner_text()
                assert "当前展示已返回的判定" in page.locator("#findings").inner_text()
                assert output["subgraph_results"]["fact"]["error"] in page.locator("#findings").inner_text()
                assert "此类核验尚未实现" not in page.locator("#findings").inner_text()
            else:
                assert "有证据支持" not in page.locator("#findings").inner_text()
            assert json.loads(page.locator("#claims").text_content()) == output
            for finding in output["subgraph_results"]["fact"]["findings"]:
                if finding["error"]:
                    assert finding["error"] in page.locator("#findings").inner_text()
        assert len(submissions) == 5


def test_untrusted_result_text_and_unsafe_links_are_not_executed(browser):
    output = readable_result(run_id="readable-untrusted")
    claim_markup = '<img src=x onerror="window.__injected = 1">'
    reason_markup = '<script>window.__injected = 2</script>'
    evidence_markup = '<svg onload="window.__injected = 3"></svg>'
    output["claims"][0]["content"] = claim_markup
    finding = output["subgraph_results"]["fact"]["findings"][0]
    finding["assessment"]["reason"] = reason_markup
    finding["evidence"][0].update(content=evidence_markup, url="javascript:window.__injected=4")
    finding["evidence"][1]["source"] = '<img src=x onerror="window.__injected = 5">'
    output = VerificationRun.model_validate(output).model_dump(mode="json")
    with result_page(browser, [output]) as (page, _):
        submit_result(page, output)
        findings = page.locator("#findings")
        assert claim_markup in findings.text_content()
        assert reason_markup in findings.text_content()
        assert evidence_markup in findings.text_content()
        assert findings.locator("img, script, svg").count() == 0
        assert page.evaluate("window.__injected ?? null") is None
        assert findings.locator('a[href^="javascript:"]').count() == 0
        assert findings.locator("a").evaluate_all(
            "links => links.every(link => ['http:', 'https:'].includes(new URL(link.href).protocol))"
        )
        assert json.loads(page.locator("#claims").text_content()) == output


def test_second_submission_replaces_findings_and_recollapses_raw_response(browser):
    first = readable_result(run_id="readable-first")
    second = readable_result("CONTRADICTED", run_id="readable-second")
    second["claims"][0]["content"] = "第二次提交：测试停车场已关闭。"
    second["subgraph_results"]["fact"]["findings"][0]["assessment"]["reason"] = "第二次提交的反证理由。"
    second = VerificationRun.model_validate(second).model_dump(mode="json")
    with result_page(browser, [first, second]) as (page, submissions):
        submit_result(page, first)
        page.locator("#raw-result > summary").click()
        assert page.locator("#claims").is_visible()
        submit_result(page, second)
        cards = page.locator("#findings article.claim-result")
        assert cards.count() == 1
        assert cards.locator(".verdict").inner_text() == "与证据矛盾"
        text = cards.inner_text()
        assert second["claims"][0]["content"] in text
        assert "第二次提交的反证理由。" in text
        assert first["claims"][0]["content"] not in text
        assert "SUPPORTED：本地测试的明确判定理由。" not in text
        assert page.locator("#raw-result").get_attribute("open") is None
        assert json.loads(page.locator("#claims").text_content()) == second
        assert page.evaluate("document.activeElement.id") == "result"
        assert len(submissions) == 2
