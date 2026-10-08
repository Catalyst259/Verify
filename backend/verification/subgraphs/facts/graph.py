"""Fact 的类别规格：两轮 Plan → Search → Validate 的编排由共享骨架提供。

这里只保留 Fact 真正区别于其他类别的内容——计划与判定的校验规则、补搜条件、
网页取证会话和提示词；节点编排、预算、计时与结果组装见 sibling 模块 skeleton.py。
"""

from datetime import datetime

from langgraph.runtime import Runtime

from ...capabilities import VerificationCapabilities
from ..skeleton import CategorySpec, build_category_subgraph
from .model import MAX_ROUNDS, FactPlan, ValidateResult
from .prompts import load_system_prompt
from .search import SearchSession
from .state import FactClaimState, FactState, PlanState, ValidateState

from ...budget import remaining


def check_plan(old: FactClaimState, plan: FactPlan) -> None:
    """补搜不得改写原主张的类别或目标范围，否则结论会答非所问。"""
    if any(getattr(old.plan, key) != getattr(plan, key) for key in ("fact_type", "target", "time_scope")):
        raise ValueError("补搜计划改变了原主张类别或目标范围")


def check_assessment(old: FactClaimState, item: ValidateResult):
    """先检查回显范围，不能覆盖字段后接受针对错误范围作出的结论。"""
    assessment = item.assessment
    if (assessment.target, assessment.time_scope) != (old.plan.target, old.plan.time_scope):
        raise ValueError("Validate 改变了目标范围")
    if assessment.verdict == "UNVERIFIED" and (assessment.evidence_sufficient or assessment.confidence is not None):
        raise ValueError("UNVERIFIED 必须保持证据不足且置信度为空")
    return assessment.model_copy(update={"target": old.plan.target, "time_scope": old.plan.time_scope})


def needs_more(claim: FactClaimState, runtime: Runtime[VerificationCapabilities], deadline: datetime) -> bool:
    """仍有可查缺口、还有轮次预算、且本次运行具备取证能力时才补搜。"""
    return (not claim.assessment.evidence_sufficient and bool(claim.assessment.remaining_gaps)
            and len(claim.rounds) < MAX_ROUNDS
            and runtime.context.search is not None and remaining(deadline) > 0)


FACT_SPEC = CategorySpec(
    name="fact",
    state_type=FactState,
    claim_state_type=FactClaimState,
    plan_type=FactPlan,
    validate_type=ValidateResult,
    plan_task=PlanState,
    validate_task=ValidateState,
    load_prompt=load_system_prompt,
    make_session=SearchSession,
    check_plan=check_plan,
    check_assessment=check_assessment,
    needs_more=needs_more,
)


def build_fact_subgraph():
    return build_category_subgraph(FACT_SPEC)
