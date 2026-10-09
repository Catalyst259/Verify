"""Same-Claim replay uses registered run-local bodies, never account/network requests."""

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from backend.extraction.materials import LinkMaterial
from backend.sources import xiaohongshu as xhs
from backend.verification.subgraphs.facts import search
from backend.verification.subgraphs.facts.model import FactEvidence
from backend.verification.subgraphs.facts.state import FactClaimState
from test_source_batch_budget import session
from test_xiaohongshu_source import fake_source, fast_polling


def physical_queries(contexts):
    return [url for context in contexts for url in context.visits
            if urlsplit(url).path in {"/search_result", "/search_result_ai"}]


async def collect(current, query="公园 停车场"):
    return await search._search_source(current, query)


def test_different_category_queries_replay_same_claim_with_new_ids_and_original_time(tmp_path, monkeypatch):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        graphs = [session(shared, limit=1, claim_id="claim021") for _ in range(3)]
        try:
            assert await collect(graphs[0], "公园 停车场政策") is None
            clock = owner._last_detail_navigation
            graphs[0].update_round(search_error="第一子图自己的取证错误")
            for current, query in zip(graphs[1:], ("公园 游客体验", "公园 停车场拥挤")):
                assert await collect(current, query) is None
                assert current.round.search_error is None and current.completed_result is not None
            bodies = [current.evidence[0] for current in graphs]
            assert len(physical_queries(contexts)) == 1
            assert len({body.evidence_id for body in bodies}) == 3
            assert len({body.retrieved_at for body in bodies}) == 1
            assert len({body.url for body in bodies}) == 1 and len({body.content for body in bodies}) == 1
            assert [current.round.tool_calls for current in graphs] == [2, 1, 1]
            assert [current.round.queries for current in graphs] == [1, 0, 0]
            assert [current.round.results_per_query for current in graphs] == [[1], [], []]
            assert owner._last_detail_navigation == clock and owner._owner_task is None
            assert all(page.closed for context in contexts for page in context.pages)
        finally:
            await owner.aclose()
    asyncio.run(exercise())


def test_different_claims_and_runs_still_query_the_platform(tmp_path, monkeypatch):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        try:
            first, different = session(shared, limit=1, claim_id="c0"), session(shared, limit=1, claim_id="c1")
            await collect(first)
            await collect(different)
            assert len(physical_queries(contexts)) == 2
            another_run = owner.for_run()
            assert not another_run._claim_notes and not another_run._body_cache
            fresh = session(another_run, limit=1, claim_id="c0")
            await collect(fresh)
            assert len(physical_queries(contexts)) == 3
            assert fresh.evidence[0].evidence_id != first.evidence[0].evidence_id
            assert fresh.evidence[0].retrieved_at > first.evidence[0].retrieved_at
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("excluded_by", ["prior_round", "input"])
def test_seen_or_input_note_alias_requires_new_physical_search(tmp_path, monkeypatch, excluded_by):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first = session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            if excluded_by == "prior_round":
                current = search.SearchSession(FactClaimState(plan=first_plan, evidence=first.evidence, rounds=[first.round]),
                    first.deadline_at, evidence_sources={"xiaohongshu": shared}, source_result_limit=1)
            else:
                current = session(shared, limit=1, claim_id="c0", input_urls=(first.evidence[0].url,))
            await collect(current, "公园 补充开放证据")
            assert len(physical_queries(contexts)) == 2 and len(current.evidence) == 1
            assert current.evidence[0].url != first.evidence[0].url
            assert shared._claim_notes["c0"] == xhs.canonical_note_id(current.evidence[0].url)
        finally:
            await owner.aclose()
    from backend.verification.subgraphs.facts.model import FactPlan
    first_plan = FactPlan(claim_id="c0", fact_type="FACILITY", target="公园", time_scope="当前",
        questions=["是否开放？"], evidence_strategy=[{"priority": 1, "source_type": "WEB", "purpose": "补充取证"}])
    asyncio.run(exercise())


def test_input_read_note_material_never_creates_a_body_or_claim_alias(tmp_path, monkeypatch):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        input_url = "https://www.xiaohongshu.com/explore/000000000000000000000001"
        async def read_note(url, **kwargs):
            return LinkMaterial(url, url, "输入标题", "仅输入材料，不是独立正文")
        monkeypatch.setattr(owner, "read_note", read_note)
        try:
            await shared.read_note(input_url)
            assert not shared._body_cache and not shared._claim_notes
            current = session(shared, limit=1, claim_id="c0", input_urls=(input_url,))
            await collect(current)
            assert len(physical_queries(contexts)) == 1
            assert current.evidence[0].url != input_url
            assert all("仅输入材料" not in raw[0]["content"] for raw in shared._body_cache.values())
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("gate", ["closed", "login", "access"])
def test_alias_replay_does_not_bypass_closed_or_current_terminal_gate(tmp_path, monkeypatch, gate):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first, second = session(shared, limit=1, claim_id="c0"), session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            if gate == "closed":
                await owner.aclose()
                kind = xhs.XiaohongshuError
            else:
                kind = xhs.XiaohongshuLoginRequired if gate == "login" else xhs.XiaohongshuAccessRestricted
                shared._access_failures.append(kind("当前运行平台限制"))
            with pytest.raises(kind) as failure:
                await shared.search("同主张另一类别", claim_id="c0", execute=second.execute,
                                    deadline_at=second.deadline_at, result_limit=1)
            assert not second.evidence and not failure.value.partial_evidence
            assert len(physical_queries(contexts)) == 1
            if gate != "closed":
                await collect(session(owner.for_run(), limit=1, claim_id="c0"))
                assert len(physical_queries(contexts)) == 2
        finally:
            await owner.aclose()
    asyncio.run(exercise())


def test_queued_cache_replay_rechecks_terminal_gate_inside_profile_lock(tmp_path, monkeypatch):
    async def exercise():
        owner, _, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first, second = session(shared, limit=1, claim_id="c0"), session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            async with owner._lock:
                waiting = asyncio.create_task(shared.search("同主张", claim_id="c0", execute=second.execute, result_limit=1))
                await asyncio.sleep(0)
                assert not waiting.done() and owner._owner_task is None
                shared._access_failures.append(xhs.XiaohongshuAccessRestricted("锁等待期间出现限制"))
            with pytest.raises(xhs.XiaohongshuAccessRestricted):
                await waiting
            assert not second.evidence and not owner._lock.locked()
        finally:
            await owner.aclose()
    asyncio.run(exercise())


def test_without_claim_id_keeps_original_physical_search_interface(tmp_path, monkeypatch):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        try:
            for query in ("公园", "公园 倒影"):
                current = session(shared, limit=1, claim_id="c0")
                assert len(await shared.search(query, execute=current.execute, result_limit=1)) == 1
            assert not shared._claim_notes and len(physical_queries(contexts)) == 2
        finally:
            await owner.aclose()
    asyncio.run(exercise())


def test_inline_registration_uses_new_ids_but_unregistered_returns_cannot_alias(tmp_path, monkeypatch):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        try:
            original = await shared.search("公园", claim_id="c0", result_limit=1)
            reused = await shared.search("公园 倒影", claim_id="c0", result_limit=1)
            assert len(original) == len(reused) == 1 and len(physical_queries(contexts)) == 1
            assert reused[0].evidence_id != original[0].evidence_id
            assert reused[0].retrieved_at == original[0].retrieved_at
            async def unregistered(query, **kwargs):
                return original
            monkeypatch.setattr(owner, "search", unregistered)
            await shared.search("另一个主张", claim_id="c1", result_limit=1)
            assert "c1" not in shared._claim_notes
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("limit,count", [(None, 3), (2, 2)])
def test_unlimited_and_multi_body_batches_do_not_create_or_use_single_body_alias(tmp_path, monkeypatch, limit, count):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch, count=3, max_results=3)
        shared = owner.for_run()
        try:
            first = session(shared, limit=limit, claim_id="c0")
            await collect(first)
            assert len(first.evidence) == count and not shared._claim_notes
            single = session(shared, limit=1, claim_id="c0")
            await collect(single)
            assert shared._claim_notes and len(physical_queries(contexts)) == 2
            multi = session(shared, limit=limit, claim_id="c0")
            await collect(multi)
            assert len(multi.evidence) == count and len(physical_queries(contexts)) == 3
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("limit", [None, 1, 2])
def test_helper_passes_claim_id_only_for_declared_single_body_sources(limit):
    class Source:
        async def search(self, query, *, execute, excluded_ids, deadline_at, result_limit=None, claim_id=None):
            self.received = (result_limit, claim_id)
    async def exercise():
        source = Source()
        await collect(session(source, limit=limit, claim_id="c0"))
        assert source.received == (limit, "c0" if limit == 1 else None)
    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["empty", "error", "partial_error", "unregistered"])
def test_empty_error_and_unregistered_physical_returns_do_not_create_alias(tmp_path, monkeypatch, outcome):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch, count=0 if outcome == "empty" else 2, max_results=2)
        shared = owner.for_run()
        current = session(shared, limit=1, claim_id="c0")
        try:
            if outcome == "error":
                context = await owner._ensure_context()
                context.fail_navigation.add(f"{1:024x}")
            elif outcome == "partial_error":
                from test_xiaohongshu_source import FakePage
                async def failed_close(page):
                    raise RuntimeError("模拟正文后清理失败")
                monkeypatch.setattr(FakePage, "close", failed_close)
            elif outcome == "unregistered":
                async def fake_return(query, *, _body_cache, **kwargs):
                    item = FactEvidence(source="伪造返回", content="没有通过 execute", source_type="WEB",
                        url="https://www.xiaohongshu.com/explore/" + f"{1:024x}",
                        evidence_id="unregistered", retrieved_at=datetime.now(timezone.utc))
                    _body_cache[f"{1:024x}"] = (item.model_dump(exclude={"evidence_id", "retrieved_at"}), item.retrieved_at)
                    return [item]
                monkeypatch.setattr(owner, "search", fake_return)
            message = await collect(current)
            assert not shared._claim_notes
            if outcome == "partial_error":
                assert message and len(current.evidence) == len(shared._body_cache) == 1
            elif outcome == "error":
                assert message and not current.evidence
            else:
                assert message is None and not current.evidence
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("receipt", ["empty", "stopped", "duplicate"])
def test_cached_empty_stopped_or_duplicate_receipt_is_not_a_body(tmp_path, monkeypatch, receipt):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first = session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            calls = []
            async def execute(operation, *, query=False, retrieved_at=None):
                calls.append((query, retrieved_at))
                return {"empty": {}, "stopped": {"stopped": True}, "duplicate": {"data": {"duplicate": True}}}[receipt]
            assert await shared.search("另一查询", execute=execute, claim_id="c0", result_limit=1) == []
            assert calls == [(False, first.evidence[0].retrieved_at)]
            assert len(physical_queries(contexts)) == 1 and owner._owner_task is None
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", ["empty_query", "long_query", "bool_limit", "bad_limit", "naive_deadline", "expired"])
def test_cache_hit_preserves_input_and_deadline_validation(tmp_path, monkeypatch, invalid):
    async def exercise():
        owner, contexts, _ = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first, second = session(shared, limit=1, claim_id="c0"), session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            query, options = "公园", {"claim_id": "c0", "execute": second.execute, "result_limit": 1}
            if invalid == "empty_query": query = "  "
            if invalid == "long_query": query = "a" * 101
            if invalid == "bool_limit": options["result_limit"] = True
            if invalid == "bad_limit": options["result_limit"] = 11
            if invalid == "naive_deadline": options["deadline_at"] = datetime.now()
            if invalid == "expired": options["deadline_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            with pytest.raises(xhs.XiaohongshuError if invalid == "expired" else ValueError):
                await shared.search(query, **options)
            assert not second.evidence and len(physical_queries(contexts)) == 1
            assert owner._owner_task is None and not owner._lock.locked()
        finally:
            await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["deadline", "close"])
def test_cached_hook_wait_is_bounded_and_aclose_can_cancel_its_owner(tmp_path, monkeypatch, ending):
    async def exercise():
        owner, contexts, drivers = fake_source(tmp_path, monkeypatch)
        shared = owner.for_run()
        first = session(shared, limit=1, claim_id="c0")
        try:
            await collect(first)
            entered = asyncio.Event()
            async def execute(operation, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            deadline = datetime.now(timezone.utc) + timedelta(seconds=.08 if ending == "deadline" else 30)
            running = asyncio.create_task(shared.search("另一类别", claim_id="c0", execute=execute, deadline_at=deadline,
                                                         result_limit=1))
            await entered.wait()
            assert owner._owner_task is running and owner._lock.locked()
            if ending == "close":
                await owner.aclose()
                with pytest.raises(asyncio.CancelledError):
                    await running
                assert contexts[0].closed and drivers[0].stopped
            else:
                with pytest.raises(xhs.XiaohongshuError, match="缓存正文登记超过限定时间"):
                    await running
            assert owner._owner_task is None and not owner._lock.locked()
            assert len(physical_queries(contexts)) == 1
        finally:
            await owner.aclose()
    asyncio.run(exercise())
