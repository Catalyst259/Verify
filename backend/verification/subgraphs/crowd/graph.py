"""CROWD 的类别规格：场景条件贯穿计划、证据筛选与判定。

编排由共享骨架提供，这里只保留 CROWD 真正区别于其他类别的部分——按场景条件核对
证据时间的校验规则、补搜条件、客流提示词与取证会话。拥挤度判定是「条件成立性」，
所以补搜保留原场景，判定只在对齐材料上成立。
"""

from datetime import datetime

from langgraph.runtime import Runtime

from ...budget import remaining
from ...capabilities import VerificationCapabilities
from ..facts.model import MAX_ROUNDS
from ..skeleton import CategorySpec, build_category_subgraph
from .model import CrowdPlan, ValidateResult
from .prompts import load_system_prompt
from .scenario import alignment
from .search import CROWD_SIGNAL_SOURCE, CrowdSearchSession, crowd_signal_runner
from .state import CrowdClaimState, CrowdState, PlanState, ValidateState


def check_plan(old: CrowdClaimState, plan: CrowdPlan) -> None:
    """补搜不得改写原主张的目标范围或场景条件，否则核验的是另一件事。"""
    if (plan.target, plan.time_scope, plan.scenario) != (old.plan.target, old.plan.time_scope, old.plan.scenario):
        raise ValueError("补搜计划改变了原主张目标范围或场景条件")


def check_assessment(old: CrowdClaimState, item: ValidateResult):
    """判定必须回显计划的场景条件，且只引用时间对得上的材料支持结论。

    证据的发布时间由代码核对：周中发布的评价支持不了「周末人少」，发布时间未知或
    远早于目标时段的材料也只能作背景。判定自报的覆盖范围一律用代码复算的结果覆盖，
    避免写出无法核对的覆盖说明。
    """
    assessment, plan = item.assessment, old.plan
    if (assessment.target, assessment.time_scope) != (plan.target, plan.time_scope):
        raise ValueError("Validate 改变了目标范围")
    if assessment.scenario != plan.scenario:
        raise ValueError("Validate 改变了主张的场景条件")
    if assessment.verdict == "UNVERIFIED":
        if assessment.evidence_sufficient or assessment.confidence is not None:
            raise ValueError("UNVERIFIED 必须保持证据不足且置信度为空")
    # 场景条件只取计划的场景字段。questions 必须按提示词写入反证场景（例如「周末前往的
    # 游客是否反映排队很久？」），把它们当条件源会把提示词要求写的话当成假约束，误伤真匹配的证据。
    aligned = alignment(plan.scenario, old.evidence)
    cited = set(assessment.supporting_evidence + assessment.counter_evidence)
    stray = sorted(cited - aligned.aligned)
    if stray:
        raise ValueError("判定引用了不支持结论的材料："
                         f"场景不符 {sorted(cited & aligned.mismatched)}；"
                         f"超出时效 {sorted(cited & aligned.stale)}；"
                         f"发布时间未知 {sorted(cited & aligned.undated)}")
    if assessment.verdict == "SUPPORTED" and not assessment.supporting_evidence:
        raise ValueError("SUPPORTED 必须引用场景对齐的支持材料")
    if assessment.verdict == "NOT_SUPPORTED" and not assessment.counter_evidence:
        raise ValueError("NOT_SUPPORTED 必须引用场景对齐的反证")
    if assessment.verdict == "SCENARIO_ONLY":
        if not assessment.conditions:
            raise ValueError("SCENARIO_ONLY 必须说明具体成立条件")
        if not (assessment.supporting_evidence or assessment.counter_evidence):
            raise ValueError("SCENARIO_ONLY 必须引用支持材料或反证")
    return assessment.model_copy(update={"target": plan.target, "time_scope": plan.time_scope,
                                         "evidence_time_coverage": aligned.coverage()})


def needs_more(claim: CrowdClaimState, runtime: Runtime[VerificationCapabilities], deadline: datetime) -> bool:
    """证据不足、还有轮次预算、且本次运行具备任一取证能力时才补搜。"""
    has_source = runtime.context.search is not None or CROWD_SIGNAL_SOURCE in runtime.context.evidence_sources
    return (not claim.assessment.evidence_sufficient and len(claim.rounds) < MAX_ROUNDS
            and has_source and remaining(deadline) > 0)


CROWD_SPEC = CategorySpec(
    name="crowd",
    state_type=CrowdState,
    claim_state_type=CrowdClaimState,
    plan_type=CrowdPlan,
    validate_type=ValidateResult,
    plan_task=PlanState,
    validate_task=ValidateState,
    load_prompt=load_system_prompt,
    make_session=CrowdSearchSession,
    check_plan=check_plan,
    check_assessment=check_assessment,
    needs_more=needs_more,
    search_runner=crowd_signal_runner,
)


def build_crowd_subgraph():
    return build_category_subgraph(CROWD_SPEC)
