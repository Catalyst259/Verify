"""Exercise the actual nested DeepSeek wrappers without a browser or network."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import browser_use
import pytest
from pydantic import BaseModel

from backend.extraction import agent
from backend.verification.subgraphs.facts import search
from backend.verification.subgraphs.facts.model import FactPlan
from backend.verification.subgraphs.facts.state import FactClaimState


@pytest.mark.parametrize("entry", ["extraction", "search"])
@pytest.mark.parametrize("finish_reason,content", [
    ("length", '{"value":"complete-looking"}'),
    ("content_filter", '{"value":"complete-looking"}'),
    ("stop", None),
    ("stop", ""),
])
def test_deepseek_wrappers_reject_incomplete_or_empty_responses(monkeypatch, entry, finish_reason, content):
    class Payload(BaseModel):
        value: str

    class Browser:
        def __init__(self, **kwargs):
            self.closed = False
            browsers.append(self)

        async def kill(self):
            self.closed = True

    class Agent:
        def __init__(self, *, llm, **kwargs):
            self.llm = llm
            self.settings = SimpleNamespace(use_vision=False)

        async def run(self, **kwargs):
            await self.llm.ainvoke([], output_format=Payload)
            pytest.fail("Incomplete model response reached the action boundary")

    async def create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason, message=SimpleNamespace(content=content),
        )], usage=None)

    @asynccontextmanager
    async def client(self):
        yield SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    browsers = []
    monkeypatch.setattr(browser_use, "Agent", Agent)
    monkeypatch.setattr(browser_use, "Browser", Browser)
    monkeypatch.setattr(browser_use.ChatOpenAI, "get_client", client)
    config = {"model": "deepseek-flash", "api_key": "test-only", "base_url": "https://model.test/v1",
              "browser_executable_path": "", "timeout_seconds": 10, "max_steps": 2}
    monkeypatch.setattr(agent, "load_config", lambda: config)
    monkeypatch.setattr(search, "load_config", lambda: config)

    async def exercise():
        if entry == "extraction":
            await agent.extract_claims("公园", "停车场开放。", [], [])
        else:
            plan = FactPlan(claim_id="c0", fact_type="FACILITY", target="公园", time_scope="当前",
                            questions=["停车场开放吗？"],
                            evidence_strategy=[{"priority": 1, "source_type": "WEB", "purpose": "确认开放"}])
            session = search.SearchSession(FactClaimState(plan=plan),
                                           datetime.now(timezone.utc) + timedelta(seconds=10))
            await search.run_search(session, "Fact Search", "test-only task")

    with pytest.raises(ValueError, match="完整"):
        asyncio.run(exercise())
    assert browsers and all(browser.closed for browser in browsers)
