"""Run-local XHS body reuse uses browser doubles and never calls public sites."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.sources.xiaohongshu import XiaohongshuAccessRestricted, canonical_note_id
from backend.sources.xiaohongshu import XiaohongshuError
from backend.verification.subgraphs.facts.model import FactEvidence, FactPlan
from backend.verification.subgraphs.facts.search import SearchSession
from backend.verification.subgraphs.facts.state import FactClaimState
from test_xiaohongshu_source import fake_source, fast_polling


def session(claim_id):
    plan = FactPlan(claim_id=claim_id, fact_type="FACILITY", target="公园", time_scope="当前",
                    questions=["水域是否存在？"], evidence_strategy=[
                        {"priority": 1, "source_type": "WEB", "purpose": "核对水域"}])
    return SearchSession(FactClaimState(plan=plan), datetime.now(timezone.utc) + timedelta(seconds=30))


def detail_visits(context):
    return [url for url in context.visits if canonical_note_id(url) is not None]


def test_overlapping_queries_read_each_body_once_but_register_each_claim(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current = source.for_run()
        first, second = session("fact"), session("experience")
        try:
            left = await current.search("公园 水域", execute=first.execute)
            right = await current.search("公园 倒影", execute=second.execute)
            assert len(left) == len(right) == 2
            assert len(detail_visits(contexts[0])) == 2
            assert len([url for url in contexts[0].visits if "/search_result?" in url]) == 2
            for original, reused in zip(left, right):
                assert original.evidence_id != reused.evidence_id
                assert original.retrieved_at == reused.retrieved_at
                assert original.content == reused.content and original.url == reused.url
                assert "PRIVATE" not in reused.model_dump_json()
            assert first.round.tool_calls == second.round.tool_calls == 3
            assert first.round.queries == second.round.queries == 1
            assert left == first.evidence and right == second.evidence
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_a_new_run_reads_fresh_bodies_with_the_same_shared_profile(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        original_read = source._read

        async def changed_body(*args):
            data = await original_read(*args)
            return data | {"content": data["content"] + f" 读取版本 {len(detail_visits(contexts[0]))}"}

        monkeypatch.setattr(source, "_read", changed_body)
        try:
            first = await source.for_run().search("公园", execute=session("first").execute)
            second = await source.for_run().search("公园", execute=session("second").execute)
            assert len(detail_visits(contexts[0])) == 4
            assert all(right.content != left.content and right.retrieved_at >= left.retrieved_at
                       for left, right in zip(first, second))
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_cached_bodies_still_respect_input_exclusions_and_tool_budget(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current = source.for_run()
        first, second = session("first"), session("second")
        try:
            await current.search("公园", execute=first.execute)
            result = await current.search("公园 水域", execute=second.execute,
                                          excluded_ids=frozenset({f"{1:024x}"}))
            assert len(result) == 1 and canonical_note_id(result[0].url) == f"{2:024x}"
            assert len(detail_visits(contexts[0])) == 2
            assert second.round.tool_calls == 2 and second.round.queries == 1
            second.update_round(tool_calls=20)
            assert await current.search("公园", execute=second.execute) == []
            assert len(detail_visits(contexts[0])) == 2
            assert second.round.tool_calls == 20
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_cached_bodies_do_not_skip_the_live_access_guard(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current = source.for_run()
        try:
            await current.search("公园", execute=session("first").execute)
            contexts[0].guards[None] = "verify"
            with pytest.raises(XiaohongshuAccessRestricted):
                await current.search("公园 倒影", execute=session("second").execute)
            assert len(detail_visits(contexts[0])) == 2
            assert source._context is None
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_cached_bodies_are_deduplicated_by_the_caller_ledger(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current, ledger = source.for_run(), session("first")
        try:
            result = await current.search("公园", execute=ledger.execute)
            assert len(result) == len(ledger.evidence) == 2
            assert await current.search("公园 水域", execute=ledger.execute) == []
            assert len(ledger.evidence) == len(detail_visits(contexts[0])) == 2
            assert ledger.round.tool_calls == 6 and ledger.round.queries == 2
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_parallel_claims_share_bodies_under_the_existing_profile_lock(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current = source.for_run()
        try:
            results = await asyncio.gather(*(current.search(query, execute=session(query).execute)
                                             for query in ("水域", "倒影")))
            assert [len(result) for result in results] == [2, 2]
            assert len(detail_visits(contexts[0])) == 2
            assert not source._lock.locked()
        finally:
            await source.aclose()
    asyncio.run(scenario())


def test_an_unrelated_registered_note_is_rejected_and_not_cached(tmp_path, monkeypatch):
    async def scenario():
        source, contexts, _ = fake_source(tmp_path, monkeypatch, count=2, max_results=2)
        current = source.for_run()

        async def mismatched(operation, *, query=False):
            data = await operation()
            if query:
                return {"data": data}
            data = data | {"url": f"https://www.xiaohongshu.com/explore/{99:024x}",
                           "evidence_id": "invalid-note", "retrieved_at": datetime.now(timezone.utc)}
            return {"data": FactEvidence.model_validate(data).model_dump()}

        try:
            with pytest.raises(XiaohongshuError, match="与候选笔记不一致"):
                await current.search("水域", execute=mismatched)
            results = await current.search("水域", execute=session("valid").execute)
            assert len(results) == 2
            assert sum(len(detail_visits(context)) for context in contexts) == 3
            assert all(canonical_note_id(item.url) != f"{99:024x}" for item in results)
        finally:
            await source.aclose()
    asyncio.run(scenario())
