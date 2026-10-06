"""真实 Chromium 展示 API 状态；本地服务只返回经过契约验证的模拟结果。"""
import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
import pytest

from backend.verification.models import VerificationRun

# 复用浏览器测试服务器时，默认 app 不读取开发者的模型配置。
with patch("backend.extraction.agent.read_config", return_value={}):
    from test_browser import serve


pytestmark = pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"
)


def run_result(status, fact_status=None, verdict="SUPPORTED", *, unavailable=True, empty_registry=False):
    claim = {"claim_id": "c0", "type": "FACT", "content": "测试公园停车场开放。",
             "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": "停车场开放"}]}
    results = {}
    if not empty_registry and status != "no_claims":
        for name in ("fact", "route", "crowd", "experience"):
            state = fact_status if name == "fact" else ("not_implemented" if unavailable else "skipped")
            results[name] = {"graph_name": name, "status": state or "not_implemented",
                             "selected_claim_ids": ["c0"] if name == "fact" else [], "findings": []}
        if fact_status in {"completed", "partial"}:
            sufficient = verdict != "UNVERIFIED"
            finding = {
                "claim_id": "c0", "summary": "停车场开放",
                "evidence": [{"source": "本地测试材料", "content": "停车场开放",
                              "url": "https://example.com/notice", "evidence_id": "e0"}],
                "assessment": {
                    "target": "测试公园停车场", "time_scope": "本次核验时间", "verdict": verdict,
                    "confidence": 0.8 if sufficient else None, "evidence_sufficient": sufficient,
                    "reason": "本地测试判定", "supporting_evidence": ["e0"] if sufficient else [],
                    "dimensions": {key: None for key in (
                        "authority", "directness", "recency", "context_match", "independence"
                    )},
                },
            }
            if fact_status == "partial":
                finding["error"] = "Search: 本地测试模拟未完成"
            results["fact"]["findings"] = [finding]
        elif fact_status == "failed":
            results["fact"]["error"] = "Plan: 本地测试模拟失败"
        if status == "failed":
            # 仅注册 Fact 时，其失败会汇总为全局 failed；其他默认子图未实现时是 partial。
            results = {"fact": results["fact"]}
    return VerificationRun.model_validate({
        "run_id": "frontend-status-test", "status": status,
        "context": {"target_place": "测试公园", "checked_at": "2026-10-06T08:00:00+00:00"},
        "claims": [] if status == "no_claims" else [claim], "subgraph_results": results,
    }).model_dump(mode="json")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path
        instance = playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.mark.parametrize("output,expected,absent,attention", [
    pytest.param(run_result("partial", "completed"),
                 ["事实核验流程已完成", "路线、人流、体验核验尚未实现", "本次未核验这些内容"],
                 ["核验失败", "事实核验仅完成一部分"], False, id="fact-completed-unavailable-features"),
    pytest.param(run_result("partial", "partial"),
                 ["事实核验仅完成一部分", "已返回的结果和未完成项", "核验尚未实现"],
                 ["事实核验流程已完成", "事实核验失败"], True, id="fact-partial"),
    pytest.param(run_result("partial", "failed"),
                 ["事实核验失败", "请查看错误说明", "核验尚未实现"],
                 ["事实核验流程已完成"], True, id="fact-failed-with-unavailable-features"),
    pytest.param(run_result("failed", "failed", unavailable=False),
                 ["事实核验失败", "请查看错误说明"],
                 ["核验流程已完成", "核验尚未实现"], True, id="fact-failed"),
    pytest.param(run_result("completed", "completed", "UNVERIFIED", unavailable=False),
                 ["事实核验流程已完成", "证据不足，未能确认", "这不表示这些说法是假的"],
                 ["核验失败", "核验仅完成一部分"], True, id="completed-but-insufficient-evidence"),
    pytest.param(run_result("completed", "completed", unavailable=False),
                 ["事实核验流程已完成"],
                 ["证据不足", "未能确认", "核验失败", "核验尚未实现"], False, id="fact-supported"),
    pytest.param(run_result("no_claims"),
                 ["未提取到明确主张，未进行核验", "请补充包含具体说法的文字、图片或链接"],
                 ["核验流程已完成", "证据不足"], False, id="no-claims"),
    pytest.param(run_result("not_implemented", "not_implemented"),
                 ["已提取 1 条主张", "事实、路线、人流、体验核验尚未实现", "本次未核验这些内容"],
                 ["核验流程已完成", "核验失败"], False, id="all-features-unavailable"),
    pytest.param(run_result("not_implemented", empty_registry=True),
                 ["本次核验功能尚未实现", "尚未对这些说法作出核验结论"],
                 ["核验流程已完成", "核验失败"], False, id="no-registered-verification"),
    pytest.param(run_result("partial", "skipped"),
                 ["本次未执行事实核验", "路线、人流、体验核验尚未实现"],
                 ["事实核验流程已完成", "核验失败"], False, id="fact-skipped"),
])
def test_frontend_explains_execution_and_evidence_separately(browser, output, expected, absent, attention):
    app = FastAPI()
    submissions = []

    @app.post("/api/verifications")
    async def verification(request: Request):
        submissions.append(await request.json())
        return output

    app.mount("/", StaticFiles(directory=Path(__file__).parents[1] / "frontend", html=True))
    with serve(app) as base_url:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(base_url)
            page.locator("#place").fill("测试公园")
            page.locator("#description").fill("停车场开放")
            page.locator("#submit").click()
            page.wait_for_function(
                "!document.querySelector('#fields').disabled && !document.querySelector('#result').hidden"
            )
            message = page.locator("#status").inner_text()
            assert all(text in message for text in expected), message
            assert all(text not in message for text in absent), message
            assert (page.locator("#status").get_attribute("class") == "error") == attention
            # 展示说明不改变后端的 partial/not_implemented/UNVERIFIED 或材料内容。
            assert json.loads(page.locator("#claims").text_content()) == output
            assert submissions == [{"target_place": "测试公园", "text": "停车场开放", "link": [], "image": []}]
            assert not errors
        finally:
            page.close()
