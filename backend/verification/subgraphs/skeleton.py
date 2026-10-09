"""四类类别子图共用的 Plan → Search → Validate 编排骨架。

骨架持有节点编排、两轮回边判定、工具预算、失败收束、计时诊断和 SubgraphResult 组装。
类别之间真正不同的只有三处——提示词、取证方式、判定校验——全部经由 CategorySpec 注入，
因此新增类别不必复制编排代码。取证会话只要满足骨架读取的属性即可，见 make_session 的说明。
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from time import perf_counter
from typing import Any, Callable, get_args

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_json

from backend.common.errors import ModelOutputError

from ..budget import RunBudget, remaining
from ..capabilities import VerificationCapabilities
from ..models import ClaimFinding, FactScore, NonEmptyText, SubgraphResult, unverified_without_evidence
from ..state import SubgraphInput, SubgraphOutput
from .diagnostics import timed


def shared_search(runtime: Runtime):
    """默认取证器：复用运行级的网页取证能力；不取证的类别自行覆盖。"""
    return runtime.context.search


@dataclass(frozen=True)
class CategorySpec:
    """一个类别区别于其他类别的全部内容；编排骨架对四类一视同仁。

    check_plan 在补搜时校验计划没有改变原主张的类别或目标范围，违规应抛异常。
    check_assessment 校验判定回显的范围一致且证据充分性自洽，返回可写回的判定，违规应抛异常。
    needs_more 判定是否还要补搜一轮；各子图的缺口表达不同，因此由类别自己决定。
    search_runner 决定本轮取证由谁执行——网页取证、地图调用或别的来源；返回 None 表示本次运行
    不具备该类别的取证能力，骨架会记录为缺证据而不是失败。
    accepts_claim 排除类别契约无法表达的主张，其余主张仍由 Plan 按内容选择。
    uses_browser 控制取证资源池；地图 HTTP 调用使用独立槽位，不占网页浏览器。
    """

    name: str
    state_type: type
    claim_state_type: type
    plan_type: type
    validate_type: type
    plan_task: type
    validate_task: type
    load_prompt: Callable[[str], str]
    make_session: Callable[..., Any]
    check_plan: Callable[[Any, Any], None]
    check_assessment: Callable[[Any, Any], Any]
    needs_more: Callable[[Any, Runtime, datetime], bool]
    search_runner: Callable[[Runtime], Any] = shared_search
    accepts_claim: Callable[[Any], bool] = lambda claim: True
    uses_browser: bool = True


def build_category_subgraph(spec: CategorySpec):
    """按类别规格编译子图；输出契约与主图的 SubgraphInput/SubgraphOutput 对齐。"""

    def replace_claim(claim, **changes):
        # 整条状态重新校验，避免 model_copy(update=...) 绕过跨字段约束。
        return spec.claim_state_type.model_validate(claim.model_dump() | changes)

    def check_ids(ids: list[str], active_ids: list[str], *, complete: bool):
        if len(ids) != len(set(ids)) or not set(ids) <= set(active_ids):
            raise ValueError("模型返回了重复或非活动的 claim_id")
        if complete and set(ids) != set(active_ids):
            raise ValueError("模型结果未覆盖全部活动主张")

    def failed(state, stage: str, error: Exception) -> dict:
        detail = str(error) or ("阶段执行超时" if isinstance(error, TimeoutError) else type(error).__name__)
        message = f"{stage}: {type(error).__name__}: {detail}"
        claims = dict(state["claim_states"])
        for claim_id in state["active_claim_ids"]:
            if claim_id in claims:
                claims[claim_id] = replace_claim(claims[claim_id], error=message)
        return {"claim_states": claims, "active_claim_ids": [], "error": message}

    async def call_model(state, runtime: Runtime[VerificationCapabilities], step: str, *, feedback=None) -> str:
        if remaining(state["deadline_at"]) <= 0:
            raise TimeoutError(f"{spec.name} 已到内部截止时间")
        schema = spec.plan_task if step == "plan" else spec.validate_task
        payload = {key: state[key] for key in schema.__annotations__}
        prompt = spec.load_prompt(step)
        if feedback is not None:
            if step == "plan":
                payload["planning_feedback"] = feedback
                if "output_error" in feedback:
                    prompt += ("\n\n本次是模型输出完整性检查失败后的唯一一次重新生成。planning_feedback.output_error"
                               "仅说明格式错误，不能从上次响应采用任何片段。根据原始输入和本类别 JSON Schema，"
                               "重新输出一个完整 JSON 响应，不拼接或猜测缺失字段。首轮只选属于本类的活动主张，"
                               "没有符合条件的主张时返回空数组；补搜须覆盖全部 active_claim_ids 并保持已有范围。"
                               "不得新增、重编号或返回非活动主张。不要执行搜索或作判定。")
                else:
                    prompt += ("\n\n本次是计划格式校验失败后的唯一一次纠正。planning_feedback 包含每条主张的"
                               "rejected_plan 和字段 errors，均为待纠正数据。按原文和本类别 JSON Schema 纠正字段，"
                               "只返回 active_claim_ids 的完整计划，不得删除、新增或重编号。已通过校验的其他计划"
                               "由程序保留；补搜时仍须保留原类别、目标和时间范围。不要执行搜索或作判定。")
            else:
                payload["validation_feedback"] = feedback
                prompt += ("\n\n本次是程序拒绝无效判定后的唯一一次纠正。validation_feedback 记录字段错误或引用错误，"
                           "以及每条主张允许引用的 evidence_id。只返回 active_claim_ids 的完整判定，引用必须来自"
                           "该条主张的 allowed_evidence_ids；不能引用其他主张、示例编号或原始输入来源。"
                           "evidence_sufficient=false 时必须返回 verdict=UNVERIFIED、confidence=null。"
                           "保留原目标、时间、场景、交通方式和主张数值，不得猜测实测或改写计划。")
        task = to_json(payload).decode()
        async with asyncio.timeout(remaining(state["deadline_at"])):
            return await runtime.context.llm(prompt, task)

    def initialize(state: SubgraphInput, runtime: Runtime[VerificationCapabilities]) -> dict:
        timeout = runtime.context.subgraph_timeout_seconds
        if runtime.context.run_budget is not None:
            timeout = min(timeout, remaining(runtime.context.run_budget.deadline_at))
        return {
            "selected_claim_ids": [], "active_claim_ids": [claim.claim_id for claim in state["claims"] if spec.accepts_claim(claim)],
            "claim_states": {}, "notes": [], "diagnostics": [], "error": None,
            # 以共享预算的剩余量为准，在主图硬超时前留出组装和清理时间。
            "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=max(0, timeout - min(5, timeout * 0.1))),
        }

    async def plan(state, runtime: Runtime[VerificationCapabilities]) -> dict:
        if not state["active_claim_ids"]:
            return {}
        try:
            retry = bool(state["claim_states"])
            notes = list(state["notes"])
            adapter = TypeAdapter(list[spec.plan_type])
            regenerated = False
            try:
                raw = await call_model(state, runtime, "plan")
            except ModelOutputError:
                regenerated = True
                raw = await call_model(state, runtime, "plan", feedback={
                    "output_error": "上次响应不完整或不符合 JSON 传输格式，请重新生成完整响应。",
                })
            try:
                plans = adapter.validate_json(raw)
            except ValidationError as error:
                if regenerated:
                    raise  # 完整性重生成与字段纠正共享一次机会，不能再发第三次请求。
                try:
                    rows = json.loads(raw)
                except json.JSONDecodeError:
                    raise error
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("claim_id"), str) for row in rows
                ):
                    raise error
                ids = [row["claim_id"] for row in rows]
                check_ids(ids, state["active_claim_ids"], complete=retry)
                valid, feedback = {}, {}
                for row in rows:
                    try:
                        valid[row["claim_id"]] = spec.plan_type.model_validate(row)
                    except ValidationError as cause:
                        feedback[row["claim_id"]] = {
                            "rejected_plan": row,
                            "errors": cause.errors(include_input=False, include_url=False, include_context=False),
                        }
                repair_state = state | {"active_claim_ids": list(feedback)}
                corrected = adapter.validate_json(await call_model(repair_state, runtime, "plan", feedback=feedback))
                check_ids([item.claim_id for item in corrected], list(feedback), complete=True)
                valid.update({item.claim_id: item for item in corrected})
                plans = [valid[claim_id] for claim_id in ids]
                notes.extend(f"{claim_id}：计划格式纠正成功。" for claim_id in feedback)
            check_ids([item.claim_id for item in plans], state["active_claim_ids"], complete=retry)
            if regenerated:
                notes.append("计划输出格式重新生成成功。")
            by_id = {item.claim_id: item for item in plans}
            selected = [claim_id for claim_id in state["active_claim_ids"] if claim_id in by_id]
            claims = dict(state["claim_states"])
            for claim_id in selected:
                item = by_id[claim_id]
                if retry:
                    spec.check_plan(claims[claim_id], item)
                    claims[claim_id] = replace_claim(claims[claim_id], plan=item)
                else:
                    claims[claim_id] = spec.claim_state_type(plan=item)
            return {"claim_states": claims, "active_claim_ids": selected,
                    "selected_claim_ids": state["selected_claim_ids"] if retry else selected, "notes": notes}
        except Exception as error:
            return failed(state, "Plan", error)

    async def search(state, runtime: Runtime[VerificationCapabilities]) -> dict:
        runner = spec.search_runner(runtime)
        claims = {claim.claim_id: claim for claim in state["claims"]}
        # 从剩余时间中预留三成给本轮 Validate，预留量最多为 30 秒。
        left = remaining(state["deadline_at"])
        deadline = state["deadline_at"] - timedelta(seconds=min(30, left * 0.3))
        # 运行级预算由主图提供并跨子图共享；直接调用子图时退回同上限的局部槽位。
        budget = runtime.context.run_budget or RunBudget(timeout_seconds=remaining(state["deadline_at"]))
        system_prompt = spec.load_prompt("search")

        async def gather(claim_id: str):
            old = state["claim_states"][claim_id]
            session = spec.make_session(old, deadline, checked_at=state["context"].checked_at,
                                        input_urls=runtime.context.input_urls,
                                        evidence_sources=runtime.context.evidence_sources)
            # 浏览器关闭可使用 Validate 余量，但必须留下主图组装结果的时间。
            session.cleanup_deadline_at = state["deadline_at"]
            diagnostic_context = session.diagnostic_context
            diagnostic_context["graph_name"] = spec.name
            error = None
            queued_at = perf_counter()
            started = False
            with timed(session.diagnostics, "search_claim", **diagnostic_context,
                       remaining_ms=round(remaining(deadline) * 1000, 3)) as timing:
                try:
                    if runner is None:
                        session.update_round(search_error="本次运行未提供搜索能力")
                    else:
                        # 排队也占用 Search 预算，必须及时让出 Validate 和结果汇总的时间。
                        async with asyncio.timeout(remaining(deadline)):
                            slot = budget.browser_slot(spec.name, deadline) if spec.uses_browser else budget.map_slots
                            async with slot:
                                started = True
                                timing.update(queue_ms=round((perf_counter() - queued_at) * 1000, 3),
                                              remaining_ms=round(remaining(deadline) * 1000, 3))
                                if remaining(deadline) <= 0:
                                    raise TimeoutError("Search 已到内部截止时间")
                                if spec.uses_browser:
                                    claim_deadline = budget.claim_deadline(spec.name, deadline)
                                    if claim_deadline < deadline:
                                        cleanup_seconds = min(3, remaining(claim_deadline) * 0.1)
                                        session.deadline_at = claim_deadline - timedelta(seconds=cleanup_seconds)
                                        session.cleanup_deadline_at = claim_deadline
                                        session.source_result_limit = 2 if remaining(session.deadline_at) >= 20 else 1
                                        timing.update(service_deadline_at=session.deadline_at.isoformat(),
                                                      source_result_limit=session.source_result_limit)
                                task = to_json({
                                    "claim": claims[claim_id], "context": state["context"], "claim_state": old,
                                    "round_number": session.round.round_number,
                                    "remaining_budget": session.remaining_budget(), "deadline_at": session.deadline_at,
                                }).decode()
                                async with asyncio.timeout(remaining(session.deadline_at)):
                                    raw = await runner(session, system_prompt, task)
                                session.result(raw)
                except Exception as cause:
                    completed = getattr(session, "completed_result", None)
                    if completed is not None:
                        session.result(completed)
                        # 有效 done 已在截止前完成；这里的异常来自后续浏览器关闭。
                        timing.update(outcome="cleanup_error", error=f"Browser cleanup: {type(cause).__name__}: {str(cause) or '清理使用了 Search 后预留的时间'}")
                    else:
                        detail = str(cause) or "阶段执行未完成"
                        if isinstance(cause, TimeoutError) and not str(cause):
                            detail = "取证超时" if started else "等待取证槽位超时"
                        error = f"Search: {type(cause).__name__}: {detail}"
                        timing.update(outcome="timeout" if isinstance(cause, TimeoutError) else "error", error=error)
                        session.update_round(search_error="; ".join(filter(None, [session.round.search_error, error])))
                finally:
                    timing.setdefault("queue_ms", round((perf_counter() - queued_at) * 1000, 3))
            return claim_id, replace_claim(old, evidence=[*old.evidence, *session.evidence],
                                          rounds=[*old.rounds, session.round], error=error), session.diagnostics

        updates = await asyncio.gather(*(gather(claim_id) for claim_id in state["active_claim_ids"]))
        return {"claim_states": state["claim_states"] | {claim_id: claim for claim_id, claim, _ in updates},
                "diagnostics": [*state["diagnostics"], *(note for _, _, notes in updates for note in notes)]}

    async def validate(state, runtime: Runtime[VerificationCapabilities]) -> dict:
        claims = dict(state["claim_states"])
        notes = list(state["notes"])
        model_ids = []
        for claim_id in state["active_claim_ids"]:
            old = claims[claim_id]
            if old.error and old.assessment is not None:
                # 补搜失败时保留上轮有效判定，不能由新输出覆盖。
                continue
            if not old.evidence:
                claims[claim_id] = replace_claim(old, assessment=unverified_without_evidence(spec.name, old.plan))
            else:
                model_ids.append(claim_id)

        def reject(claim_id, message):
            old = claims[claim_id]
            claims[claim_id] = replace_claim(old, error="; ".join(filter(None, [old.error, message])))

        def parse_results(raw, expected_ids):
            try:
                rows = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("模型判定不是有效 JSON") from None
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("claim_id"), str) for row in rows
            ):
                raise ValueError("模型判定必须是包含 claim_id 的 JSON 数组")
            ids = [row["claim_id"] for row in rows]
            check_ids(ids, expected_ids, complete=False)
            valid, feedback, normalized = [], {}, set()
            assessment_type = spec.validate_type.model_fields["assessment"].annotation
            allowed_verdicts = get_args(assessment_type.model_fields["verdict"].annotation)
            for row in rows:
                claim_id = row["claim_id"]
                assessment = row.get("assessment")
                if isinstance(assessment, dict) and all(key in assessment for key in ("verdict", "confidence", "reason")) and (
                    assessment.get("verdict") in allowed_verdicts and assessment.get("evidence_sufficient") is False
                ) and (
                    assessment.get("verdict") != "UNVERIFIED" or assessment.get("confidence") is not None
                ):
                    try:
                        TypeAdapter(NonEmptyText).validate_python(assessment["reason"])
                        TypeAdapter(FactScore | None).validate_python(assessment["confidence"])
                    except ValidationError:
                        pass  # 非法字段仍交给完整 Schema 拒绝，不能借规范化补齐。
                    else:
                        # 只向证据不足收束；范围、引用和实测字段仍须通过全部校验。
                        row = row | {"assessment": assessment | {"verdict": "UNVERIFIED", "confidence": None,
                                                                "reason": "模型报告证据不足，暂无法形成结论。"}}
                        normalized.add(claim_id)
                try:
                    valid.append(spec.validate_type.model_validate(row))
                except ValidationError as cause:
                    errors = cause.errors(include_input=False, include_url=False, include_context=False)
                    detail = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors)
                    feedback[claim_id] = {"error": detail, "errors": errors, "rejected_result": row}
            for claim_id in set(expected_ids) - set(ids):
                feedback[claim_id] = {"error": "模型未返回该主张的判定"}
            for claim_id, item in feedback.items():
                item["allowed_evidence_ids"] = sorted(e.evidence_id for e in state["claim_states"][claim_id].evidence)
            return valid, feedback, normalized

        def apply_results(results, normalized):
            feedback = {}
            for item in results:
                old = state["claim_states"][item.claim_id]
                assessment = item.assessment
                allowed = {e.evidence_id for e in old.evidence}
                references = set(assessment.supporting_evidence + assessment.counter_evidence + assessment.context_evidence)
                try:
                    if not references <= allowed:
                        raise ValueError("判定引用了该 Claim 未取得的证据")
                    claims[item.claim_id] = replace_claim(old, assessment=spec.check_assessment(old, item))
                except ValueError as error:
                    reject(item.claim_id, f"Validate: {error}")
                    if not references <= allowed:
                        feedback[item.claim_id] = {"error": "判定引用了该 Claim 未取得的证据",
                                                   "allowed_evidence_ids": sorted(allowed)}
                else:
                    if item.claim_id in normalized:
                        notes.append(f"{item.claim_id}：证据不足的判定已规范为 UNVERIFIED，confidence=null。")
            return feedback

        model_state = state | {"claim_states": claims, "active_claim_ids": model_ids}
        try:
            results, feedback, normalized = [], {}, set()
            if model_ids:
                results, feedback, normalized = parse_results(await call_model(model_state, runtime, "validate"), model_ids)
        except Exception as error:
            return failed(model_state, "Validate", error)

        for claim_id, item in feedback.items():
            reject(claim_id, f"Validate: {item['error']}")
        feedback.update(apply_results(results, normalized))
        if feedback and remaining(state["deadline_at"]) > 0:
            repair_state = state | {"claim_states": claims, "active_claim_ids": list(feedback)}
            try:
                corrected, invalid, normalized = parse_results(
                    await call_model(repair_state, runtime, "validate", feedback=feedback), list(feedback))
                for claim_id, item in invalid.items():
                    reject(claim_id, f"Validate correction: {item['error']}")
                apply_results(corrected, normalized)
                notes.extend(f"{claim_id}：判定{'引用' if '引用' in feedback[claim_id]['error'] else '格式'}纠正成功。" for claim_id in feedback
                             if claims[claim_id].assessment is not None and claims[claim_id].error == state["claim_states"][claim_id].error)
            except Exception as error:
                for claim_id in feedback:
                    reject(claim_id, f"Validate correction: {type(error).__name__}: {str(error) or '纠正已到截止时间'}")

        active = [claim_id for claim_id in state["active_claim_ids"] if (
            claims[claim_id].error is None and claims[claim_id].assessment is not None
            and spec.needs_more(claims[claim_id], runtime, state["deadline_at"])
        )]
        return {"claim_states": claims, "active_claim_ids": active, "notes": notes}

    def finalize(state) -> SubgraphOutput:
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
            graph_name=spec.name, status=status, selected_claim_ids=state["selected_claim_ids"],
            findings=findings, notes=[*notes, *state["diagnostics"]], error=state["error"],
        )}

    def measured_node(node):
        """记录节点完整耗时，包括模型请求、解析和校验；诊断不进入模型输入。"""
        async def measured(state, runtime: Runtime[VerificationCapabilities]):
            records = []
            completed_rounds = max((len(state["claim_states"][claim_id].rounds)
                                    for claim_id in state["active_claim_ids"] if claim_id in state["claim_states"]), default=0)
            with timed(records, node.__name__, claim_ids=state["active_claim_ids"],
                       graph_name=spec.name,
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

    builder = StateGraph(spec.state_type, input_schema=SubgraphInput, output_schema=SubgraphOutput,
                         context_schema=VerificationCapabilities)
    for node in (initialize, plan, search, validate, finalize):
        builder.add_node(node.__name__, measured_node(node) if node in (plan, search, validate) else node)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "plan")
    builder.add_conditional_edges("plan", lambda state: "search" if state["active_claim_ids"] else "finalize")
    builder.add_edge("search", "validate")
    builder.add_conditional_edges("validate", lambda state: "plan" if state["active_claim_ids"] else "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(name=spec.name)
