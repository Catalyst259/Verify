"""搜索诊断必须保留失败现场，并区分页面错误与正常空结果。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from test_fact_workflow import assessment, make_plan, new_session, run

from backend.verification.subgraphs.facts import search


@pytest.mark.parametrize("status,challenge,ready,empty,category", [
    (429, False, "complete", False, "rate_limited"),
    (403, False, "complete", False, "access_denied"),
    (200, True, "complete", False, "challenge"),
    (503, False, "complete", False, "http_error"),
    (200, False, "loading", False, "load_incomplete"),
    (200, False, "complete", False, "parse_error"),
    (None, False, "complete", False, "parse_error"),
    (200, False, "complete", True, None),
])
def test_search_failure_classification_and_metadata(monkeypatch, status, challenge, ready, empty, category):
    async def page(browser, url, script, *, diagnostic):
        diagnostic.update(http_status=status, final_url="https://search.test/final", title="搜索页",
                          page_excerpt="页面现场", ready_state=ready, challenge_detected=challenge)
        return {"results": [], "empty": empty}

    monkeypatch.setattr(search, "page_data", page)
    session = new_session()
    action = search.create_tools(session, object()).registry.registry.actions["search_web"]
    result = json.loads(asyncio.run(action.function(query="票价")).extracted_content)
    assert bool(result.get("error")) == bool(category)
    assert session.round.tool_calls == session.round.queries == 1
    if category:
        diagnostic = result["diagnostic"]
        assert diagnostic["category"] == category
        assert diagnostic["http_status"] == status
        assert diagnostic["final_url"] == "https://search.test/final"
        assert diagnostic["title"] == "搜索页" and diagnostic["page_excerpt"] == "页面现场"
        assert diagnostic["elapsed_ms"] >= 0
        assert json.loads(session.diagnostics[0])["claim_id"] == session.claim_id
    else:
        assert result["data"] == [] and not session.diagnostics


def test_navigation_timeout_preserves_partial_page_and_timings():
    class Page:
        async def evaluate(self, script):
            if script == "() => performance.timeOrigin":
                return "1"
            return json.dumps({"time_origin": 2, "http_status": 200,
                               "final_url": "https://search.test/slow", "title": "正在加载",
                               "page_excerpt": "部分正文", "ready_state": "loading",
                               "challenge_detected": False})

    class Browser:
        async def get_current_page(self):
            return Page()

        async def navigate_to(self, url):
            await asyncio.sleep(1)

    async def exercise():
        diagnostic = {}
        session = new_session()
        session.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=0.02)

        async def operation():
            return await search.page_data(Browser(), "https://search.test/slow", "() => ({})", diagnostic=diagnostic)

        result = await session.execute(operation, query=True, diagnostic=diagnostic)
        assert result["error"].startswith("TimeoutError:")
        assert diagnostic["category"] == "load_timeout"
        assert diagnostic["title"] == "正在加载"
        assert diagnostic["page_excerpt"] == "部分正文"
        assert diagnostic["navigation_ms"] > 0

    asyncio.run(exercise())


def test_failed_navigation_does_not_attribute_previous_page_to_new_request():
    class Page:
        async def evaluate(self, script):
            if script == "() => performance.timeOrigin":
                return "1"
            return json.dumps({"time_origin": 1, "http_status": 200,
                               "final_url": "https://search.test/previous", "title": "旧页面",
                               "page_excerpt": "旧正文", "ready_state": "complete"})

    class Browser:
        async def get_current_page(self):
            return Page()

        async def navigate_to(self, url):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    diagnostic = {}
    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        asyncio.run(search.page_data(Browser(), "https://missing.test/", "() => ({})", diagnostic=diagnostic))
    assert diagnostic["category"] == "load_error"
    assert diagnostic["http_status"] is None and diagnostic["final_url"] is None
    assert diagnostic["page_excerpt"] is None


def test_stage_timings_and_failure_diagnostics_survive_final_result(monkeypatch):
    async def page(browser, url, script, *, diagnostic):
        diagnostic.update(http_status=429, final_url=url, title="Too many requests", page_excerpt="稍后重试",
                          ready_state="complete", challenge_detected=False)
        return {"results": [], "empty": False}

    monkeypatch.setattr(search, "page_data", page)

    async def model(prompt, task):
        state = json.loads(task)
        assert "diagnostics" not in state
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        return json.dumps([{"claim_id": "c0", "assessment": assessment(state["claim_states"]["c0"])}])

    async def runner(session, *args):
        action = search.create_tools(session, object()).registry.registry.actions["search_web"]
        await action.function(query="票价")
        raise RuntimeError("Search Agent 未完成取证")

    result = run(model, runner)
    records = [json.loads(note) for note in result.notes if note.startswith('{"event":')]
    page_record = next(record for record in records if record["event"] == "page_failure")
    assert page_record["http_status"] == 429 and page_record["claim_id"] == "c0"
    timings = {record["stage"]: record for record in records if record["event"] == "stage_timing"}
    assert {"plan", "search", "validate", "search_claim"} <= timings.keys()
    assert timings["search_claim"]["queue_ms"] >= 0
    assert timings["search_claim"]["outcome"] == "error"
    assert timings["validate"]["outcome"] == "success"
    assert all(record["elapsed_ms"] >= 0 for record in timings.values())
    assert result.status == "partial"


def test_http_error_page_is_diagnosed_without_saving_error_body_as_evidence(monkeypatch):
    async def page(browser, url, script, *, diagnostic):
        diagnostic.update(http_status=404, final_url=url, title="Not found", page_excerpt="不存在")
        return {"source": "Not found", "url": url, "content": "不存在", "published_at": None}

    monkeypatch.setattr(search, "page_data", page)
    session = new_session()
    action = search.create_tools(session, object()).registry.registry.actions["read_page"]
    result = json.loads(asyncio.run(action.function(url="https://example.org/404")).extracted_content)
    assert result["diagnostic"]["category"] == "http_error"
    assert not session.evidence
    assert json.loads(session.diagnostics[0])["operation"] == "read_page"


def test_snapshot_failure_does_not_hide_successful_dom_read():
    class Page:
        async def evaluate(self, script):
            if script == "() => performance.timeOrigin":
                return "1"
            if script == search.PAGE_SNAPSHOT:
                raise RuntimeError("诊断快照失败")
            return '{"results": [], "empty": true}'

    class Browser:
        async def get_current_page(self):
            return Page()

        async def navigate_to(self, url):
            pass

    diagnostic = {}
    data = asyncio.run(search.page_data(Browser(), "https://search.test/", "() => ({})", diagnostic=diagnostic))
    assert data == {"results": [], "empty": True}
    assert diagnostic["http_status"] is None
    assert "诊断快照失败" in diagnostic["snapshot_error"]


def test_validate_timeout_has_elapsed_time_and_keeps_prior_stage_timings():
    async def model(prompt, task):
        if prompt.startswith("# Fact Plan"):
            return json.dumps([make_plan("c0")])
        await asyncio.sleep(1)

    result = run(model, timeout=0.05)
    records = [json.loads(note) for note in result.notes if note.startswith('{"event":')]
    timings = {record["stage"]: record for record in records if record["event"] == "stage_timing"}
    assert {"plan", "search", "validate"} <= timings.keys()
    assert timings["validate"]["outcome"] == "timeout"
    assert timings["validate"]["elapsed_ms"] > 0
    assert "阶段执行超时" in timings["validate"]["error"]
