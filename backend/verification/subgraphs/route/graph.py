"""ROUTE 的类别规格：测量计划、地图取证与数值比对判定。

编排由共享骨架提供，这里只保留 ROUTE 真正与其他类别不同的部分。
"""

from datetime import datetime

from langgraph.runtime import Runtime

from ...budget import remaining
from ...capabilities import VerificationCapabilities
from ..skeleton import CategorySpec, build_category_subgraph
from .model import RoutePlan, RouteValidateResult
from .prompts import load_system_prompt
from .search import RouteSearchSession, run_route_search
from .state import RouteClaimState, RouteState, PlanState, ValidateState


def check_plan(old: RouteClaimState, plan: RoutePlan) -> None:
    """补搜不得改写主张的目标范围或交通方式，否则测的是另一件事。"""
    if (plan.target, plan.time_scope, plan.transport_mode) != (
            old.plan.target, old.plan.time_scope, old.plan.transport_mode):
        raise ValueError("补搜计划改变了原主张目标范围或交通方式")


def check_assessment(old: RouteClaimState, item: RouteValidateResult):
    """判定必须回显计划的范围和主张数值，与实测数字一致，并只引用已取得的证据。

    数值比对类主张的全部价值在于「结论和两个数字对得上」。只检查实测字段存在是不够的：
    模型可以给出与实测相反的判定，或填一个证据里根本不存在的数字，或引用从未取得的证据。
    这三种都会让用户读到一个比模型更可信的假结论，所以由代码强制。
    """
    assessment, plan = item.assessment, old.plan
    if (assessment.target, assessment.time_scope) != (plan.target, plan.time_scope):
        raise ValueError("Validate 改变了目标范围")
    if (assessment.claimed_seconds, assessment.transport_mode, assessment.tolerance_seconds) != (
            plan.claimed_seconds, plan.transport_mode, plan.tolerance_seconds):
        raise ValueError("判定改写了主张的数值、交通方式或容差")

    evidence_ids = {e.evidence_id for e in old.evidence}
    references = set(assessment.supporting_evidence + assessment.counter_evidence + assessment.context_evidence)
    if not references <= evidence_ids:
        raise ValueError("判定引用了该 Claim 未取得的证据")

    measured = assessment.measured_seconds
    recorded = {e.measured_seconds for e in old.evidence if e.measured_seconds is not None}
    if measured is None:
        if recorded:
            raise ValueError("已有实测值，判定不得记为无实测")
    elif measured not in recorded:
        raise ValueError("判定给出的实测值不在已取得的测量中")

    if assessment.verdict in {"MATCHED", "MISMATCHED"}:
        if measured is None:
            raise ValueError("数值比对必须给出实测值")
        within = abs(measured - plan.claimed_seconds) <= plan.tolerance_seconds
        if within and assessment.verdict != "MATCHED":
            raise ValueError("实测落在主张容差内，判定必须为 MATCHED")
        if not within and assessment.verdict != "MISMATCHED":
            raise ValueError("实测超出主张容差，判定必须为 MISMATCHED")
    if assessment.verdict == "MISMATCHED" and not assessment.counter_evidence:
        raise ValueError("MISMATCHED 必须引用反证")
    if assessment.verdict == "MATCHED" and not assessment.supporting_evidence:
        raise ValueError("MATCHED 必须引用支持证据")
    return assessment.model_copy(update={"target": plan.target, "time_scope": plan.time_scope})


def needs_more(claim: RouteClaimState, runtime: Runtime[VerificationCapabilities], deadline: datetime) -> bool:
    """测量是确定性的，一次实测即可判定；不为对称而强行补搜。"""
    return False


def route_search_runner(runtime: Runtime):
    """地图能力缺失时本次运行没有 ROUTE 取证能力，骨架会记为缺证据而非失败。"""
    routing = runtime.context.map_routing
    if routing is None:
        return None

    async def run(session, system_prompt, task):
        return await run_route_search(session, system_prompt, task, routing=routing,
                                      resolver=runtime.context.place_resolver)
    return run


ROUTE_SPEC = CategorySpec(
    name="route",
    state_type=RouteState,
    claim_state_type=RouteClaimState,
    plan_type=RoutePlan,
    validate_type=RouteValidateResult,
    plan_task=PlanState,
    validate_task=ValidateState,
    load_prompt=load_system_prompt,
    make_session=RouteSearchSession,
    check_plan=check_plan,
    check_assessment=check_assessment,
    needs_more=needs_more,
    search_runner=route_search_runner,
)


def build_route_subgraph():
    return build_category_subgraph(ROUTE_SPEC)
