"""真实 Fact 图、browser-use 和 Chromium；网页与模型响应由本地 HTTP 服务提供。"""

import asyncio
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from playwright.sync_api import sync_playwright
from test_browser import serve
from test_fact_workflow import assessment, inputs, make_plan, new_session

from backend.verification.capabilities import VerificationCapabilities
from backend.verification.subgraphs.facts import llm, search
from backend.verification.subgraphs.facts.graph import build_fact_subgraph

pytestmark = pytest.mark.skipif(os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试")


@pytest.mark.parametrize("model,mode", [("gpt-4.1", "valid"), ("deepseek-flash", "valid"),
                                      ("deepseek-flash", "malformed"),
                                      ("deepseek-flash", "missing_action_once"),
                                      ("deepseek-flash", "missing_action_always")])
def test_fact_search_reads_real_page_with_bounded_tools(monkeypatch, model, mode):
    from browser_use.agent.views import AgentOutput
    from pydantic import ValidationError

    app = FastAPI()
    calls, visits, sessions, queries, browsers = [], [], [], [], []
    literal_control_responses, missing_action_responses, finish_attempts = [], [], []

    @app.get("/search", response_class=HTMLResponse)
    def candidates():
        visits.append("search")
        return "<html><body>" + "".join(
            f'<a class="result__a" href="{base_url}/notice?id={i}">公园公告 {i}</a>' for i in range(12)
        ) + "</body></html>"

    @app.get("/notice", response_class=HTMLResponse)
    def notice():
        visits.append("notice")
        return ('<html><head><title>公园公告</title><meta property="article:published_time" content="2026-10-05">'
                '</head><body><h1>停车场开放说明</h1><p>停车场对游客开放，夜间关闭。</p></body></html>')

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        calls.append(body)
        system = body["messages"][0]["content"]
        if system.startswith("# Fact Plan"):
            assert "# PlanSkill" in system
            content = [make_plan("c0")]
        elif system.startswith("# Fact Validate"):
            assert "# PlanSkill" not in system
            state = json.loads(body["messages"][-1]["content"])
            content = [{"claim_id": "c0", "assessment": assessment(state["claim_states"]["c0"], sufficient=True)}]
        else:
            assert "# Fact Search" in system and "# PlanSkill" not in system
            if model == "deepseek-flash":
                instruction = body["messages"][-1]["content"]
                assert "必须包含非空 action 数组" in instruction
                assert "不得只有 thinking" in instruction
                assert 'action: [{"done": {}}]' in instruction
            session = sessions[-1]
            if session.round.tool_calls == 0:
                action = {"search_web": {"query": "公园停车场开放"}}
            elif session.round.tool_calls == 1:
                action = {"read_page": {"url": base_url + "/notice", "source_type": "WEB"}}
            else:
                finish_attempts.append(1)
                action = {"done": {}}
            content = {"evaluation_previous_goal": "Success", "memory": "本地链路测试",
                       "next_goal": "搜索、读取或完成", "thinking": "Scripted response for integration test.",
                       "action": [action]}
            if mode == "malformed" and session.round.tool_calls == 2:
                content = "损坏的 Agent 输出"
            elif session.round.tool_calls == 2 and (mode == "missing_action_always" or
                                                    (mode == "missing_action_once" and not missing_action_responses)):
                del content["action"]
        serialized = json.dumps(content)
        if model == "deepseek-flash" and isinstance(content, dict) and "action" in content:
            thinking = "Scripted response\nSecond line\rThird line\tEnd."
            content["thinking"] = thinking
            encoded_thinking = json.dumps(thinking)
            literal_thinking = encoded_thinking.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
            serialized = json.dumps(content).replace(encoded_thinking, literal_thinking, 1)
            with pytest.raises(json.JSONDecodeError):
                json.loads(serialized)
            assert json.loads(serialized, strict=False)["thinking"] == thinking
            literal_control_responses.append(1)
        if isinstance(content, dict) and "action" not in content:
            assert json.loads(serialized) == content
            with pytest.raises(ValidationError) as error:
                AgentOutput.model_validate_json(serialized)
            assert [(item["loc"], item["type"]) for item in error.value.errors()] == [(("action",), "missing")]
            missing_action_responses.append(serialized)
        return {"id": "test", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": serialized}}]}

    original_page_data = search.page_data

    async def local_search(browser, url, script, **kwargs):
        browsers.append(browser)
        if url.startswith("https://html.duckduckgo.com/"):
            queries.append(url)
            url = base_url + "/search"
        return await original_page_data(browser, url, script, **kwargs)

    async def tracked_search(session, prompt, task):
        sessions.append(session)
        return await search.run_search(session, prompt, task)

    monkeypatch.setattr(search, "page_data", local_search)
    with sync_playwright() as playwright:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path
    with serve(app) as base_url:
        config = {"api_key": "test-only", "base_url": base_url + "/v1", "model": model,
                  "browser_executable_path": executable,
                  "timeout_seconds": 60, "max_steps": 6}
        monkeypatch.setattr(llm, "load_config", lambda: config)
        monkeypatch.setattr(search, "load_config", lambda: config)
        result = asyncio.run(build_fact_subgraph().ainvoke(inputs(), context=VerificationCapabilities(
            search=tracked_search, subgraph_timeout_seconds=90,
        )))["result"]

    failed = mode in {"malformed", "missing_action_always"}
    assert result.status == ("partial" if failed else "completed"), result.model_dump_json()
    assert len(calls) == (5 if mode == "valid" else 6) and len(queries) == 1
    assert len(finish_attempts) == (1 if mode == "valid" else 2)
    assert len(missing_action_responses) == {"missing_action_once": 1, "missing_action_always": 2}.get(mode, 0)
    expected_controls = (2 if failed else 3) if model == "deepseek-flash" else 0
    assert len(literal_control_responses) == expected_controls
    assert visits == ["search", "notice"]
    assert sessions[0].round.tool_calls == 2 and sessions[0].round.results_per_query == [10]
    assert sessions[0].round.queries == 1 and sessions[0].round.new_evidence_count == 1
    finding = result.findings[0]
    assert finding.assessment.verdict == "SUPPORTED" and bool(finding.error) == failed
    assert len(finding.evidence) == 1
    assert finding.evidence[0].content == "停车场开放说明\n\n停车场对游客开放，夜间关闭。"
    assert finding.evidence[0].model_dump(mode="json")["published_at"] == "2026-10-05"
    assert finding.assessment.supporting_evidence == [finding.evidence[0].evidence_id]
    assert all(browser.session_manager is None for browser in browsers)


def test_search_diagnostics_capture_real_http_status_redirect_and_page(monkeypatch):
    from browser_use import Browser

    app = FastAPI()
    pages = {
        "limited": (429, "Too many requests", "稍后重试", "rate_limited"),
        "denied": (403, "Forbidden", "禁止访问", "access_denied"),
        "challenge": (200, "Human check", '<form id="challenge-form">Verify you are human</form>', "challenge"),
        "missing": (404, "Not found", "页面不存在", "http_error"),
        "changed": (200, "新搜索布局", '<a class="new-result">页面结构已更新</a>' + "内容" * 800, "parse_error"),
        "empty": (200, "无搜索结果", '<div class="no-results">没有找到结果</div>', None),
        "iframe": (200, "主文档", '<iframe src="/limited"></iframe>', "parse_error"),
    }

    @app.get("/{kind}")
    def page(kind: str):
        if kind == "redirect":
            return RedirectResponse("/limited")
        status, title, body, _ = pages[kind]
        return HTMLResponse(f"<html><head><title>{title}</title></head><body>{body}</body></html>",
                            status_code=status)

    original_page_data = search.page_data

    async def local_search(browser, url, script, **kwargs):
        from urllib.parse import parse_qs, urlsplit
        kind = parse_qs(urlsplit(url).query)["q"][0]
        return await original_page_data(browser, base_url + "/" + kind, script, **kwargs)

    monkeypatch.setattr(search, "page_data", local_search)
    with sync_playwright() as playwright:
        executable = os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path

    async def exercise():
        browser = Browser(headless=True, use_cloud=False, enable_default_extensions=False, executable_path=executable)
        await browser.start()
        try:
            for kind in [*pages, "redirect"]:
                session = new_session()
                action = search.create_tools(session, browser).registry.registry.actions["search_web"]
                result = json.loads((await action.function(query=kind)).extracted_content)
                status, title, _, category = pages["limited" if kind == "redirect" else kind]
                if category is None:
                    assert result["data"] == [] and not session.diagnostics
                    continue
                diagnostic = result["diagnostic"]
                assert diagnostic["http_status"] == status, diagnostic
                assert diagnostic["category"] == category
                assert diagnostic["title"] == title
                assert diagnostic["final_url"] == base_url + "/" + ("limited" if kind == "redirect" else kind)
                assert diagnostic["requested_url"] == base_url + "/" + kind
                assert diagnostic["ready_state"] == "complete"
                assert len(diagnostic["page_excerpt"]) <= 1000
                assert diagnostic["navigation_ms"] > 0 and diagnostic["dom_read_ms"] > 0
        finally:
            await browser.kill()

    with serve(app) as base_url:
        asyncio.run(exercise())
