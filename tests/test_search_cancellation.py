"""Browser cleanup must preserve cancellation already received by the search caller."""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.verification.subgraphs.facts import search as search_module
from test_fact_workflow import new_session, read


def test_external_cancellation_survives_browser_cleanup_timeout(monkeypatch):
    import browser_use

    events = []
    current = new_session()

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            events.append("cleanup_started")
            try:
                await asyncio.sleep(10)
            finally:
                events.append("cleanup_finished")

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def run(self, **kwargs):
            await read(current)
            entered.set()
            await asyncio.sleep(10)

    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(search_module, "create_tools", lambda *args: object())
    monkeypatch.setattr(search_module, "load_config", lambda: {
        "model": "compatible-model", "api_key": "test-only", "base_url": "http://model.test/v1",
        "timeout_seconds": 10, "max_steps": 1, "browser_executable_path": "",
    })

    async def exercise():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(search_module.run_search(current, "scripted instructions", "memory task"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            current.cleanup_deadline_at = datetime.now(timezone.utc) + timedelta(seconds=0.1)
            task.cancel("external caller cancelled")
            with pytest.raises(asyncio.CancelledError, match="external caller cancelled"):
                try:
                    await task
                except BaseException as cause:
                    print(json.dumps({"raised": type(cause).__name__, "detail": str(cause),
                                      "task_cancelled": task.cancelled(), "events": events,
                                      "diagnostics": current.diagnostics}, ensure_ascii=False))
                    raise
            assert task.cancelled()
            assert events == ["cleanup_started", "cleanup_finished"]
            diagnostics = [json.loads(note) for note in current.diagnostics if note.startswith("{")]
            cleanup = [item for item in diagnostics if item.get("stage") == "browser_cleanup"]
            assert len(cleanup) == 1 and cleanup[0]["outcome"] == "timeout"
            assert len(current.evidence) == 1 and current.completed_result is None
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    entered = None
    asyncio.run(exercise())
