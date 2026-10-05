"""Fact 的两轮 Plan → Search → Validate 编排和最终结果组装。"""

import asyncio
from datetime import datetime, timedelta, timezone
from time import perf_counter

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter
from pydantic_core import to_json

from ...capabilities import VerificationCapabilities
from ...models import ClaimFinding, SubgraphResult
from ...state import SubgraphInput, SubgraphOutput
from .diagnostics import timed
from .model import MAX_ROUNDS, FactPlan, ValidateResult
from .prompts import load_system_prompt
from .search import SearchSession
from .state import FactClaimState, FactState, PlanState, ValidateState


def remaining(deadline: datetime) -> float:
    return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())


def replace_claim(claim: FactClaimState, **changes) -> FactClaimState:
    """整条状态重新校验，避免 model_copy(update=...) 绕过跨字段约束。"""
    return FactClaimState.model_validate(claim.model_dump() | changes)


def check_ids(items, active_ids: list[str], *, complete: bool):
    ids = [item.claim_id for item in items]
    if len(ids) != len(set(ids)) or not set(ids) <= set(active_ids):
        raise ValueError("模型返回了重复或非活动的 claim_id")
    if complete and set(ids) != set(active_ids):
        raise ValueError("模型结果未覆盖全部活动主张")


def failed(state: FactState, stage: str, error: Exception) -> dict:
    detail = str(error) or ("阶段执行超时" if isinstance(error, TimeoutError) else type(error).__name__)
    message = f"{stage}: {type(error).__name__}: {detail}"
    claims = dict(state["claim_states"])
    for claim_id in state["active_claim_ids"]:
        if claim_id in claims:
            claims[claim_id] = replace_claim(claims[claim_id], error=message)
    return {"claim_states": claims, "active_claim_ids": [], "error": message}


async def call_model(state: FactState, runtime: Runtime[VerificationCapabilities], step: str) -> str:
    if remaining(state["deadline_at"]) <= 0:
        raise TimeoutError("Fact 已到内部截止时间")
    schema = PlanState if step == "plan" else ValidateState
    task = to_json({key: state[key] for key in schema.__annotations__}).decode()
    async with asyncio.timeout(remaining(state["deadline_at"])):
        return await runtime.context.fact_llm(load_system_prompt(step), task)


def initialize(state: SubgraphInput, runtime: Runtime[VerificationCapabilities]) -> dict:
    timeout = runtime.context.subgraph_timeout_seconds
    return {
        "selected_claim_ids": [], "active_claim_ids": [claim.claim_id for claim in state["claims"]],
        "claim_states": {}, "notes": [], "diagnostics": [], "error": None,
        # 在主图硬超时前留出结果组装和浏览器清理时间。
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=max(0, timeout - min(5, timeout * 0.1))),
    }


async def plan(state: FactState, runtime: Runtime[VerificationCapabilities]) -> dict:
    if not state["active_claim_ids"]:
        return {}
    try:
        plans = TypeAdapter(list[FactPlan]).validate_json(await call_model(state, runtime, "plan"))
        retry = bool(state["claim_states"])
        check_ids(plans, state["active_claim_ids"], complete=retry)
        by_id = {item.claim_id: item for item in plans}
        selected = [claim_id for claim_id in state["active_claim_ids"] if claim_id in by_id]
        claims = dict(state["claim_states"])
        for claim_id in selected:
            item = by_id[claim_id]
            if retry:
                old = claims[claim_id]
                if any(getattr(old.plan, key) != getattr(item, key) for key in ("fact_type", "target", "time_scope")):
                    raise ValueError("补搜计划改变了原主张类别或目标范围")
                claims[claim_id] = replace_claim(old, plan=item)
            else:
                claims[claim_id] = FactClaimState(plan=item)
        return {"claim_states": claims, "active_claim_ids": selected,
                "selected_claim_ids": state["selected_claim_ids"] if retry else selected}
    except Exception as error:
        return failed(state, "Plan", error)


async def search(state: FactState, runtime: Runtime[VerificationCapabilities]) -> dict:
    runner = runtime.context.fact_search
    claims = {claim.claim_id: claim for claim in state["claims"]}
    # 从剩余时间中预留三成给本轮 Validate，预留量最多为 30 秒。
    left = remaining(state["deadline_at"])
    deadline = state["deadline_at"] - timedelta(seconds=min(30, left * 0.3))
    semaphore = asyncio.Semaphore(3)
    system_prompt = load_system_prompt("search")

    async def search_claim(claim_id: str):
        old = state["claim_states"][claim_id]
        session = SearchSession(old, deadline, checked_at=state["context"].checked_at)
        error = None
        queued_at = perf_counter()
        async with semaphore:
            with timed(session.diagnostics, "search_claim", **session.diagnostic_context,
                       queue_ms=round((perf_counter() - queued_at) * 1000, 3),
                       remaining_ms=round(remaining(deadline) * 1000, 3)) as timing:
                try:
                    if runner is None:
                        session.update_round(search_error="本次运行未提供搜索能力")
                    else:
                        if remaining(deadline) <= 0:
                            raise TimeoutError("Search 已到内部截止时间")
                        task = to_json({
                            "claim": claims[claim_id], "context": state["context"], "claim_state": old,
                            "round_number": session.round.round_number,
                            "remaining_budget": session.remaining_budget(), "deadline_at": deadline,
                        }).decode()
                        async with asyncio.timeout(remaining(deadline)):
                            raw = await runner(session, system_prompt, task)
                        session.result(raw)
                except Exception as cause:
                    error = f"Search: {type(cause).__name__}: {str(cause) or '阶段执行未完成'}"
                    timing.update(outcome="timeout" if isinstance(cause, TimeoutError) else "error", error=error)
                    session.update_round(search_error="; ".join(filter(None, [session.round.search_error, error])))
        return claim_id, replace_claim(old, evidence=[*old.evidence, *session.evidence],
                                      rounds=[*old.rounds, session.round], error=error), session.diagnostics

    updates = await asyncio.gather(*(search_claim(claim_id) for claim_id in state["active_claim_ids"]))
    return {"claim_states": state["claim_states"] | {claim_id: claim for claim_id, claim, _ in updates},
            "diagnostics": [*state["diagnostics"], *(note for _, _, notes in updates for note in notes)]}


async def validate(state: FactState, runtime: Runtime[VerificationCapabilities]) -> dict:
    try:
        results = TypeAdapter(list[ValidateResult]).validate_json(await call_model(state, runtime, "validate"))
        check_ids(results, state["active_claim_ids"], complete=True)
    except Exception as error:
        return failed(state, "Validate", error)

    claims = dict(state["claim_states"])
    for item in results:
        old = claims[item.claim_id]
        if old.error and old.assessment is not None:
            # 补搜未完成时保留上轮有效判定及本轮已取材料，并明确标记 partial。
            continue
        try:
            # 先检查回显范围，不能覆盖字段后接受针对错误范围作出的结论。
            assessment = item.assessment
            if (assessment.target, assessment.time_scope) != (old.plan.target, old.plan.time_scope):
                raise ValueError("Validate 改变了目标范围")
            if assessment.verdict == "UNVERIFIED" and (assessment.evidence_sufficient or assessment.confidence is not None):
                raise ValueError("UNVERIFIED 必须保持证据不足且置信度为空")
            assessment = assessment.model_copy(update={"target": old.plan.target, "time_scope": old.plan.time_scope})
            claims[item.claim_id] = replace_claim(old, assessment=assessment)
        except ValueError as error:
            claims[item.claim_id] = replace_claim(old, error=f"Validate: {error}")

    active = [claim_id for claim_id in state["active_claim_ids"] if (
        claims[claim_id].error is None and claims[claim_id].assessment is not None
        and not claims[claim_id].assessment.evidence_sufficient and claims[claim_id].assessment.remaining_gaps
        and len(claims[claim_id].rounds) < MAX_ROUNDS
        and runtime.context.fact_search is not None and remaining(state["deadline_at"]) > 0
    )]
    return {"claim_states": claims, "active_claim_ids": active}


def finalize(state: FactState) -> SubgraphOutput:
    findings = []
    notes = list(state["notes"])
    for claim_id in state["selected_claim_ids"]:
        claim = state["claim_states"][claim_id]
        error = claim.error
        if claim.assessment is None:
            error = error or "未形成有效判定"
        findings.append(ClaimFinding(
            claim_id=claim_id, assessment=claim.assessment, evidence=claim.evidence, error=error,
            summary=claim.assessment.reason if claim.assessment else error,
        ))
        notes.extend(f"{claim_id} 第 {item.round_number} 轮：{item.search_error}"
                     for item in claim.rounds if item.search_error)
    if not findings:
        status = "failed" if state["error"] else "skipped"
    elif all(item.assessment is None for item in findings):
        status = "failed"
    elif any(item.error for item in findings):
        status = "partial"
    else:
        status = "completed"
    return {"result": SubgraphResult(
        graph_name="fact", status=status, selected_claim_ids=state["selected_claim_ids"],
        findings=findings, notes=[*notes, *state["diagnostics"]], error=state["error"],
    )}


def measured_node(node):
    """记录节点完整耗时，包括模型请求、解析和校验；诊断不进入模型输入。"""
    async def measured(state, runtime: Runtime[VerificationCapabilities]):
        records = []
        completed_rounds = max((len(state["claim_states"][claim_id].rounds)
                                for claim_id in state["active_claim_ids"] if claim_id in state["claim_states"]), default=0)
        with timed(records, node.__name__, claim_ids=state["active_claim_ids"],
                   round_number=completed_rounds + (node.__name__ != "validate"),
                   checked_at=state["context"].checked_at.isoformat(),
                   remaining_ms=round(remaining(state["deadline_at"]) * 1000, 3)) as timing:
            update = await node(state, runtime)
            errors = [claim.error for claim_id, claim in update.get("claim_states", {}).items()
                      if claim_id in state["active_claim_ids"] and claim.error and
                      (claim_id not in state["claim_states"] or claim.error != state["claim_states"][claim_id].error)]
            if update.get("error") or errors:
                error = update.get("error") or "; ".join(errors)
                timing.update(outcome="timeout" if "TimeoutError:" in error else "error", error=error)
        return update | {"diagnostics": [*update.get("diagnostics", state["diagnostics"]), *records]}
    return measured


def build_fact_subgraph():
    builder = StateGraph(FactState, input_schema=SubgraphInput, output_schema=SubgraphOutput,
                         context_schema=VerificationCapabilities)
    for node in (initialize, plan, search, validate, finalize):
        builder.add_node(node.__name__, measured_node(node) if node in (plan, search, validate) else node)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "plan")
    builder.add_conditional_edges("plan", lambda state: "search" if state["active_claim_ids"] else "finalize")
    builder.add_edge("search", "validate")
    builder.add_conditional_edges("validate", lambda state: "plan" if state["active_claim_ids"] else "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(name="fact")
