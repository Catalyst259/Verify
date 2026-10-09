"""Confirmed platform captcha route and run-local stopping do not relax source guards."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.sources import xiaohongshu as xhs
from test_xiaohongshu_source import FakePage, fake_source, fast_polling


@pytest.mark.parametrize("url", ["https://www.xiaohongshu.com/website-login/captcha?xsec_token=PRIVATE",
                               "https://xiaohongshu.com/website-login/captcha/"])
def test_exact_platform_captcha_route_stops_before_dom_guard(tmp_path, monkeypatch, url):
    async def exercise():
        source, _, _ = fake_source(tmp_path, monkeypatch)

        async def no_dom(*args):
            raise AssertionError("Captcha route must stop before inspecting stale visible feed DOM")

        monkeypatch.setattr(source, "_evaluate", no_dom)
        with pytest.raises(xhs.XiaohongshuAccessRestricted) as error:
            await source._guard(SimpleNamespace(url=url))
        assert "PRIVATE" not in str(error.value)
        await source.aclose()
    asyncio.run(exercise())


def test_ordinary_note_search_and_home_routes_keep_dom_access_guard(tmp_path, monkeypatch):
    async def exercise():
        source, _, _ = fake_source(tmp_path, monkeypatch)
        calls = []

        async def dom_guard(page, script):
            calls.append(script)
            return ""

        monkeypatch.setattr(source, "_evaluate", dom_guard)
        for url in [xhs.HOME, "https://www.xiaohongshu.com/search_result?keyword=test",
                    "https://www.xiaohongshu.com/explore/" + "a" * 24]:
            await source._guard(SimpleNamespace(url=url))
        assert calls == [xhs.GUARD_JS] * 3
        await source.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["login", "verify"])
def test_run_terminal_gate_stops_queued_claims_but_new_run_can_retry(tmp_path, monkeypatch, kind):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=1)
        context = await source._ensure_context()
        context.guards[None] = kind
        current = source.for_run()
        error_type = xhs.XiaohongshuLoginRequired if kind == "login" else xhs.XiaohongshuAccessRestricted
        results = await asyncio.gather(current.search("第一主张"), current.search("第二主张"), return_exceptions=True)
        assert all(isinstance(error, error_type) for error in results)
        assert len(contexts) == 1 and context.visits == [xhs.HOME]
        assert all(error.partial_evidence == [] for error in results)
        with pytest.raises(error_type):
            await current.search("第三主张")
        assert len(contexts) == 1
        assert len(await source.for_run().search("新的核验请求")) == 1 and len(contexts) == 2
        await source.aclose()
    asyncio.run(exercise())


def test_run_gate_retains_first_partial_evidence_without_sharing_claim_ids(tmp_path, monkeypatch):
    async def exercise():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, max_results=2)
        context = await source._ensure_context()
        context.guards[f"{2:024x}"] = "verify"
        current = source.for_run()
        with pytest.raises(xhs.XiaohongshuAccessRestricted) as first:
            await current.search("第一主张")
        assert len(first.value.partial_evidence) == 1
        visits = len(context.visits)
        with pytest.raises(xhs.XiaohongshuAccessRestricted) as second:
            await current.search("第二主张")
        assert second.value.partial_evidence == [] and len(context.visits) == visits
        assert len(contexts) == 1
        await source.aclose()
    asyncio.run(exercise())
