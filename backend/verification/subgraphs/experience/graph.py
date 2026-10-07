"""Experience 的类别规格：两轮 Plan → Search → Validate 的编排由共享骨架提供。

这里只保留体验核验真正区别于其他类别的内容——计划与判定的校验规则、补搜
条件、体验提示词；取证会话沿用 Fact 的网页会话（同预算、同工具协议），节点
编排、预算、计时与结果组装见 sibling 模块 skeleton.py。
"""

from datetime import datetime

from langgraph.runtime import Runtime

from ...budget import remaining
from ...capabilities import VerificationCapabilities
from ..facts.model import MAX_ROUNDS
from ..facts.search import SearchSession
from ..skeleton import CategorySpec, build_category_subgraph
from .model import ExperiencePlan, ValidateResult
from .prompts import load_system_prompt
from .state import ExperienceClaimState, ExperienceState, PlanState, ValidateState


def check_plan(old: ExperienceClaimState, plan: ExperiencePlan) -> None:
    """补搜不得改写原主张的体验类别或目标范围，否则结论会答非所问。"""
    if any(getattr(old.plan, key) != getattr(plan, key) for key in ("experience_type", "target", "time_scope")):
        raise ValueError("补搜计划改变了原主张类别或目标范围")


def check_assessment(old: ExperienceClaimState, item: ValidateResult):
    """先检查回显范围，再检查判定与所依赖的依据自洽。

    一致度是来源之间的关系，因此确定结论必须由至少两份内容不同的材料
    支撑：只有一份来源、或其转载/同文重复，都只能给证据不足。正文完全
    相同的重复可在此识别，改写后的转载仍由模型按提示词判断独立性。
    「高度依赖场景」不指明场景等于把任何分歧都塞进同一档，同样拒绝。
    """
    assessment = item.assessment
    if (assessment.target, assessment.time_scope) != (old.plan.target, old.plan.time_scope):
        raise ValueError("Validate 改变了目标范围")
    if assessment.verdict == "UNVERIFIED":
        if assessment.evidence_sufficient or assessment.confidence is not None or assessment.source_agreement is not None:
            raise ValueError("UNVERIFIED 必须保持证据不足，且不给出一致度或置信度")
        return assessment.model_copy(update={"target": old.plan.target, "time_scope": old.plan.time_scope})
    if assessment.source_agreement is None:
        raise ValueError("确定结论必须给出来源一致度")
    if assessment.verdict == "SCENARIO_DEPENDENT" and not assessment.conditions:
        raise ValueError("SCENARIO_DEPENDENT 必须说明场景条件")
    # 内容完全相同的材料（同文转载、同一页重复读取）不构成第二个独立来源。
    if len({item.content for item in old.evidence}) < 2:
        raise ValueError("来源一致度需要至少两份内容不同的材料")
    return assessment.model_copy(update={"target": old.plan.target, "time_scope": old.plan.time_scope})


def needs_more(claim: ExperienceClaimState, runtime: Runtime[VerificationCapabilities], deadline: datetime) -> bool:
    """证据不足、还有轮次预算、且本次运行具备取证能力时才补搜。"""
    return (not claim.assessment.evidence_sufficient and len(claim.rounds) < MAX_ROUNDS
            and runtime.context.search is not None and remaining(deadline) > 0)


EXPERIENCE_SPEC = CategorySpec(
    name="experience",
    state_type=ExperienceState,
    claim_state_type=ExperienceClaimState,
    plan_type=ExperiencePlan,
    validate_type=ValidateResult,
    plan_task=PlanState,
    validate_task=ValidateState,
    load_prompt=load_system_prompt,
    make_session=SearchSession,
    check_plan=check_plan,
    check_assessment=check_assessment,
    needs_more=needs_more,
)


def build_experience_subgraph():
    return build_category_subgraph(EXPERIENCE_SPEC)
