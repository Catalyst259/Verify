"""Image requests must retain completed work when the shared search budget expires."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

from fastapi.testclient import TestClient
import pytest

from backend.main import create_app
from backend.extraction.models import ClaimExtractionResult
from backend.verification import service as service_module
from backend.verification.budget import RunBudget, remaining
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.models import VerificationInput
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from test_api import png
from test_fact_workflow import assessment, inputs, make_plan, read
from test_graph import extracted, service, subgraph


async def fact_model(prompt, task):
    state = json.loads(task)
    if prompt.startswith("# Fact Plan"):
        return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
    if prompt.startswith("# Fact Validate"):
        return json.dumps([
            {"claim_id": cid, "assessment": assessment(
                state["claim_states"][cid], sufficient=bool(state["claim_states"][cid]["evidence"]))}
            for cid in state["active_claim_ids"]
        ])
    return "[]"


@pytest.mark.parametrize("frontend", [False, pytest.param(True, marks=pytest.mark.skipif(
    os.getenv("VERIFY_BROWSER_TESTS") != "1", reason="显式启用 Chromium 集成测试"))])
def test_image_request_keeps_evidence_and_completed_claim_before_shared_timeout(tmp_path, monkeypatch, frontend):
    budgets, deadlines, cancelled = [], [], []

    def budget():
        result = RunBudget(timeout_seconds=0.8)
        budgets.append(result)
        return result

    monkeypatch.setattr(service_module, "RunBudget", budget)
    original = png()

    async def extract(place, text, links, images):
        assert images[0].data == original
        await asyncio.sleep(0.08)  # Image extraction consumes part of the same run budget.
        return ClaimExtractionResult(target_place=place, claims=[
            {"claim_id": str(i), "type": "FACT", "content": "The park has public parking.",
             "sources": [{"source_type": "IMAGE", "source_ref": images[0].file_code,
                          "source_text": "Public parking"}]}
            for i in range(2)
        ])

    async def search(session, *args):
        deadlines.append(session.deadline_at)
        await read(session)
        if session.claim_id == "claim_002":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(session.claim_id)
        return session.result().model_dump_json()

    app = create_app(tmp_path, extract, capabilities=VerificationCapabilities(llm=fact_model, search=search))
    if frontend:
        from playwright.sync_api import sync_playwright
        from test_browser import serve

        with sync_playwright() as playwright, serve(app) as base_url:
            browser = playwright.chromium.launch(
                executable_path=os.getenv("VERIFY_CHROMIUM") or playwright.chromium.executable_path, headless=True)
            try:
                page = browser.new_page()
                page.goto(base_url)
                page.locator("#place").fill("公园")
                page.locator("#images").set_input_files(
                    {"name": "claims.png", "mimeType": "image/png", "buffer": original})
                page.wait_for_function("document.querySelectorAll('#uploads li').length === 1 && !document.querySelector('#submit').disabled")
                with page.expect_response("**/api/verifications") as pending:
                    page.locator("#submit").click()
                response = pending.value
                assert response.status == 200
                code = response.request.post_data_json["image"][0]
                page.wait_for_function("!document.querySelector('#fields').disabled")
                result = json.loads(page.locator("#claims").text_content())
                assert "已返回的结果和未完成项" in page.locator("#status").inner_text()
                assert "Search: TimeoutError" in page.locator("#result").inner_text()
            finally:
                browser.close()
    else:
        with TestClient(app) as client:
            upload = client.post("/api/files", files={"file": ("claims.png", original, "image/png")})
            assert upload.status_code == 201
            code = upload.json()["file_code"]
            response = client.post("/api/verifications", json={"target_place": "公园", "image": [code]})
        assert response.status_code == 200
        result = response.json()
    fact = result["subgraph_results"]["fact"]
    assert fact["status"] == "partial" and fact["error"] is None
    first, second = fact["findings"]
    assert first["error"] is None and first["assessment"]["verdict"] == "SUPPORTED"
    assert "Search: TimeoutError" in second["error"]
    assert len(first["evidence"]) == len(second["evidence"]) == 1
    assert all(item["sources"][0]["source_ref"] == code for item in result["claims"])
    assert all(deadline < budgets[0].deadline_at for deadline in deadlines)
    assert cancelled == ["claim_002"]


def test_queue_wait_expires_without_starting_browser_or_hanging_validation():
    async def exercise():
        budget = RunBudget(timeout_seconds=0.5, concurrency=1)
        await budget.browser_slots.acquire()

        async def unexpected(*args):
            pytest.fail("A timed-out queue entry must not start a browser")

        try:
            output = await asyncio.wait_for(build_fact_subgraph().ainvoke(inputs(2), context=
                VerificationCapabilities(llm=fact_model, search=unexpected, run_budget=budget)), timeout=1)
        finally:
            budget.browser_slots.release()
        result = output["result"]
        assert result.status == "partial" and result.error is None
        assert len(result.findings) == 2
        assert all("TimeoutError" in item.error and not item.evidence for item in result.findings)
        assert all(item.assessment.verdict == "UNVERIFIED" for item in result.findings)
        diagnostics = [json.loads(note) for note in result.notes if note.startswith("{")]
        searches = [item for item in diagnostics if item.get("stage") == "search_claim"]
        assert len(searches) == 2
        assert all(item["outcome"] == "timeout" and item["queue_ms"] > 0 for item in searches)
        assert not budget.browser_slots.locked()

    asyncio.run(exercise())


def test_outer_timeout_uses_remaining_budget_without_extending_deadline(tmp_path):
    async def exercise():
        budget = RunBudget(timeout_seconds=0.8,
                           started_at=datetime.now(timezone.utc) - timedelta(seconds=0.6))
        original_deadline = budget.deadline_at
        stopped = asyncio.Event()

        async def extract(*args):
            return extracted()

        async def stalled(*args):
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

        svc = service(tmp_path, extract, subgraphs={"stalled": subgraph("stalled", stalled)})
        output = await asyncio.wait_for(svc.graph.ainvoke(
            {"request": VerificationInput(target_place="公园", text="免费开放，周末游客少")},
            context=VerificationCapabilities(run_budget=budget)), timeout=0.6)
        assert output["result"].subgraph_results["stalled"].status == "failed"
        assert "TimeoutError" in output["result"].subgraph_results["stalled"].error
        assert stopped.is_set()
        assert budget.deadline_at == original_deadline

    asyncio.run(exercise())


def test_expired_shared_budget_does_not_start_model_or_search():
    async def unexpected(*args):
        pytest.fail("The shared run budget has already expired")

    async def exercise():
        budget = RunBudget(timeout_seconds=0.1,
                           started_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        result = (await build_fact_subgraph().ainvoke(inputs(), context=VerificationCapabilities(
            llm=unexpected, search=unexpected, run_budget=budget)))["result"]
        assert result.status == "failed" and "TimeoutError" in result.error

    asyncio.run(exercise())


def test_browser_cleanup_before_shared_deadline_keeps_registered_evidence(tmp_path, monkeypatch):
    """A late Search cancellation must not let browser cleanup erase its evidence."""
    import browser_use
    from backend.verification.subgraphs.facts import search as search_module

    sessions, events = [], []

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            events.append("cleanup_started")
            try:
                # This is within run_search's actual three-second cleanup bound,
                # but longer than the short remaining run can afford after Search.
                await asyncio.sleep(0.7)
            except asyncio.CancelledError:
                events.append("cleanup_cancelled")
                raise
            finally:
                events.append("cleanup_finished")

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            await read(sessions[-1])
            events.append("evidence_registered")
            await asyncio.sleep(10)

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(search_module, "create_tools", lambda *args: object())
    monkeypatch.setattr(search_module, "load_config", lambda: {
        "model": "compatible-model", "api_key": "test-only", "base_url": "http://model.test/v1",
        "timeout_seconds": 10, "max_steps": 1, "browser_executable_path": "",
    })

    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=inputs()["claims"])

    async def search(session, *args):
        sessions.append(session)
        return await search_module.run_search(session, *args)

    svc = service(tmp_path, extract, subgraphs={"fact": build_fact_subgraph()})

    async def exercise():
        # Keep the production 240-second budget; extraction/POI resolution may
        # leave less than one second by the time the branch is dispatched.
        budget = RunBudget(timeout_seconds=240,
                           started_at=datetime.now(timezone.utc) - timedelta(seconds=239.2))
        result = (await svc.graph.ainvoke(
            {"request": VerificationInput(target_place="公园", text="公园有停车场。")},
            context=VerificationCapabilities(run_budget=budget, llm=fact_model, search=search),
        ))["result"].subgraph_results["fact"]
        print(json.dumps({"events": events, "result": result.model_dump(mode="json")}, ensure_ascii=False))
        assert "evidence_registered" in events and "cleanup_finished" in events
        acquired = 0
        try:
            # Both production slots must be available; locked() alone would
            # miss a single leaked slot while concurrency remains two.
            async with asyncio.timeout(0.1):
                for _ in range(2):
                    await budget.browser_slots.acquire()
                    acquired += 1
        finally:
            for _ in range(acquired):
                budget.browser_slots.release()
        assert len(result.findings) == 1, "Outer cancellation discarded registered Search evidence"
        assert len(result.findings[0].evidence) == 1
        assert "子图超过运行预算" not in (result.error or "")
        assert any("Search: TimeoutError" in note for note in result.notes)

    asyncio.run(exercise())


@pytest.mark.parametrize("agent_done", [True, False])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_successful_search_keeps_valid_result_when_browser_cleanup_times_out(tmp_path, monkeypatch, agent_done, cleanup_failure):
    """A valid done before Search's deadline remains valid if only cleanup expires."""
    import browser_use
    from backend.verification.subgraphs.facts import search as search_module

    sessions, events, successful_raw = [], [], []

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            events.append("cleanup_started")
            try:
                await asyncio.sleep(0.2)
                if cleanup_failure:
                    raise TimeoutError("browser cleanup failed")
            except asyncio.CancelledError:
                events.append("cleanup_cancelled")
                raise
            finally:
                events.append("cleanup_finished")

    class History:
        def is_successful(self):
            return agent_done

        def final_result(self):
            raw = sessions[-1].result().model_dump_json()
            successful_raw.append(raw)
            return raw

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            await read(sessions[-1])
            if not agent_done:
                sessions[-1].update_round(tool_calls=20)
            await asyncio.sleep(max(0, remaining(sessions[-1].deadline_at) - 0.08))
            events.append({"agent_done_remaining": remaining(sessions[-1].deadline_at)})
            return History()

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(search_module, "create_tools", lambda *args: object())
    monkeypatch.setattr(search_module, "load_config", lambda: {
        "model": "compatible-model", "api_key": "test-only", "base_url": "http://model.test/v1",
        "timeout_seconds": 10, "max_steps": 1, "browser_executable_path": "",
    })

    async def extract(*args):
        return ClaimExtractionResult(target_place="公园", claims=inputs()["claims"])

    async def search(session, *args):
        sessions.append(session)
        return await search_module.run_search(session, *args)

    svc = service(tmp_path, extract, subgraphs={"fact": build_fact_subgraph()})

    async def exercise():
        budget = RunBudget(timeout_seconds=240,
                           started_at=datetime.now(timezone.utc) - timedelta(seconds=238))
        result = (await svc.graph.ainvoke(
            {"request": VerificationInput(target_place="公园", text="公园有停车场。")},
            context=VerificationCapabilities(run_budget=budget, llm=fact_model, search=search),
        ))["result"].subgraph_results["fact"]
        print(json.dumps({"events": events, "result": result.model_dump(mode="json")}, ensure_ascii=False))
        done = next(item for item in events if isinstance(item, dict))
        assert done["agent_done_remaining"] > 0
        assert "cleanup_cancelled" not in events and "cleanup_finished" in events
        assert len(successful_raw) == int(agent_done)
        validated = sessions[0].result(sessions[0].completed_result)
        assert len(validated.evidence) == 1 and validated.error is None
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.assessment.verdict == "SUPPORTED" and len(finding.evidence) == 1
        assert finding.error is None, "Browser cleanup overwrote a validated, completed Search"
        assert result.status == "completed" and result.error is None
        diagnostics = [json.loads(note) for note in result.notes if note.startswith("{")]
        cleanup = [item for item in diagnostics if item.get("stage") == "browser_cleanup"]
        assert len(cleanup) == 1
        assert cleanup[0]["outcome"] == ("timeout" if cleanup_failure else "success")
        search_timings = [item for item in diagnostics if item.get("stage") == "search_claim"]
        assert search_timings[0]["outcome"] == "cleanup_error"
        assert not any("Search: TimeoutError" in note for note in result.notes)

    asyncio.run(exercise())
