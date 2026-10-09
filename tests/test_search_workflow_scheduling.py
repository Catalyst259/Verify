"""Admission and time slicing must reach actual category workflows, not only the resource pool."""

import asyncio
import json
import pytest

from backend.verification.budget import RunBudget
from backend.verification.capabilities import VerificationCapabilities
from backend.verification.subgraphs.facts.graph import build_fact_subgraph
from backend.verification.subgraphs.route.graph import build_route_subgraph
from test_fact_workflow import assessment, inputs, make_plan, read
from test_route_workflow import FakeRouting, inputs as route_inputs, make_plan as route_plan, model_returning, resolve_ok


def test_standalone_xhs_workflow_admits_only_one_claim_at_a_time():
    async def exercise():
        active, peak = 0, 0

        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Fact Plan"):
                return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
            return json.dumps([{"claim_id": cid, "assessment": assessment(
                state["claim_states"][cid], sufficient=True)} for cid in state["active_claim_ids"]])

        async def search(session, prompt, task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.02)
                await read(session)
                return session.result().model_dump_json()
            finally:
                active -= 1

        result = (await build_fact_subgraph().ainvoke(inputs(2), context=VerificationCapabilities(
            llm=model, search=search, evidence_sources={"xiaohongshu": object()})))["result"]
        assert peak == 1 and active == 0
        assert result.status == "completed" and all(f.error is None for f in result.findings)

    asyncio.run(exercise())


@pytest.mark.parametrize("count", [1, 2])
def test_shared_budget_small_batch_reads_one_body_before_validation(count):
    async def exercise():
        quotas = []
        budget = RunBudget()
        original_deadline = budget.deadline_at

        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Fact Plan"):
                return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
            return json.dumps([{"claim_id": cid, "assessment": assessment(
                state["claim_states"][cid], sufficient=True)} for cid in state["active_claim_ids"]])

        async def search(session, prompt, task):
            quotas.append(session.source_result_limit)
            assert not session.first_pass_complete()
            await read(session)
            assert session.first_pass_complete()
            return session.result().model_dump_json()

        result = (await build_fact_subgraph().ainvoke(inputs(count), context=VerificationCapabilities(
            llm=model, search=search, run_budget=budget)))["result"]
        assert result.status == "completed" and quotas == [1] * count
        assert all(len(f.evidence) == 1 and f.error is None for f in result.findings)
        assert not budget.browser_contended and budget.deadline_at == original_deadline

    asyncio.run(exercise())


def test_slow_first_claims_cannot_leave_twenty_other_claims_unstarted(monkeypatch):
    monkeypatch.setattr("backend.verification.budget.MAX_CLAIM_SEARCH_SECONDS", 0.04)
    async def exercise():
        budget = RunBudget(timeout_seconds=1.5)
        original_deadline = budget.deadline_at
        started, source_limits = [], []

        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Fact Plan"):
                return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
            return json.dumps([{"claim_id": cid, "assessment": assessment(
                state["claim_states"][cid], sufficient=True)} for cid in state["active_claim_ids"]])

        async def search(session, prompt, task):
            started.append(session.claim_id)
            source_limits.append((session.source_result_limit, session.deadline_at, session.cleanup_deadline_at))
            state = json.loads(task)
            assert state["deadline_at"] == session.deadline_at.isoformat().replace("+00:00", "Z")
            if session.claim_id in {"c0", "c1"}:
                await asyncio.sleep(10)
            await read(session)
            return session.result().model_dump_json()

        result = (await build_fact_subgraph().ainvoke(inputs(22), context=VerificationCapabilities(
            llm=model, search=search, run_budget=budget)))["result"]
        assert len(started) == 22 and result.status == "partial"
        assert all("等待取证槽位" not in (f.error or "") for f in result.findings)
        assert all(f.assessment.verdict == "SUPPORTED" and f.error is None for f in result.findings[2:])
        assert all("Search: TimeoutError" in f.error for f in result.findings[:2])
        assert source_limits[0][0] == source_limits[1][0] == 1
        assert all(deadline <= cleanup <= original_deadline for _, deadline, cleanup in source_limits)
        assert budget.deadline_at == original_deadline and budget._browser_dispatcher is None
        assert budget._browser_inflight == budget._pending_browser_count() == 0

    asyncio.run(exercise())


def test_map_workflow_finishes_while_web_slots_are_occupied():
    async def exercise():
        budget = RunBudget(timeout_seconds=1)
        for _ in range(2):
            await budget.browser_slots.acquire()
        plan = route_plan("c0")
        try:
            result = (await build_route_subgraph().ainvoke(route_inputs(), context=VerificationCapabilities(
                llm=model_returning(plan, "MATCHED", 300), search=None,
                map_routing=FakeRouting(duration=300), place_resolver=resolve_ok(), run_budget=budget)))["result"]
        finally:
            for _ in range(2):
                budget.browser_slots.release()
        assert result.status == "completed" and result.findings[0].error is None
        assert result.findings[0].assessment.verdict == "MATCHED"
        assert not budget.map_slots.locked()

    asyncio.run(exercise())


def test_standalone_large_batch_limits_first_sessions_even_when_plan_selects_only_two():
    async def exercise():
        quotas = []

        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Fact Plan"):
                return json.dumps([make_plan(cid) for cid in state["active_claim_ids"][:2]])
            return json.dumps([{"claim_id": cid, "assessment": assessment(
                state["claim_states"][cid], sufficient=True)} for cid in state["active_claim_ids"]])

        async def search(session, prompt, task):
            quotas.append(session.source_result_limit)
            await read(session)
            return session.result().model_dump_json()

        result = (await build_fact_subgraph().ainvoke(inputs(22), context=VerificationCapabilities(
            llm=model, search=search)))["result"]
        assert result.status == "completed" and result.selected_claim_ids == ["c0", "c1"]
        assert quotas == [1, 1]

    asyncio.run(exercise())


def test_large_first_pass_keeps_claims_and_gaps_without_starting_an_unaffordable_retry():
    async def exercise():
        budget = RunBudget(timeout_seconds=90)
        plans, searches, quotas = [], [], []
        async def model(prompt, task):
            state = json.loads(task)
            if prompt.startswith("# Fact Plan"):
                plans.append(list(state["active_claim_ids"]))
                return json.dumps([make_plan(cid) for cid in state["active_claim_ids"]])
            return json.dumps([{"claim_id": cid, "assessment": assessment(state["claim_states"][cid])}
                               for cid in state["active_claim_ids"]])
        async def search(session, prompt, task):
            searches.append((session.claim_id, session.round.round_number))
            quotas.append(session.source_result_limit)
            await read(session)
            return session.result().model_dump_json()
        result = (await build_fact_subgraph().ainvoke(inputs(22), context=VerificationCapabilities(
            llm=model, search=search, run_budget=budget)))["result"]
        assert result.status == "completed" and len(result.findings) == 22
        assert len(plans) == 1 and len(searches) == 22 and set(quotas) == {1}
        assert all(len(f.evidence) == 1 and f.error is None and f.assessment.verdict == "UNVERIFIED"
                   and f.assessment.remaining_gaps for f in result.findings)
        assert any("不足以完成下一轮补搜" in note for note in result.notes)
        assert budget.browser_contended and budget._pending_browser_count() == budget._browser_inflight == 0
    asyncio.run(exercise())
