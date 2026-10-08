"""提取、类别子图分发和结果汇总的主图。"""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import logging
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from backend.extraction.service import ClaimExtractionService

from .budget import remaining
from .capabilities import VerificationCapabilities
from .models import ClaimConflict, SubgraphResult, VerificationContext, VerificationRun, verdict_polarity
from .state import BranchInput, GraphInput, GraphOutput, SubgraphInput, VerificationState
from .subgraphs import VerificationSubgraph, default_subgraphs

logger = logging.getLogger(__name__)


def build_verification_graph(
    extraction: ClaimExtractionService,
    subgraphs: Mapping[str, VerificationSubgraph] | None = None,
) -> CompiledStateGraph[VerificationState, VerificationCapabilities, GraphInput, GraphOutput]:
    """编译主图；注册表决定子图范围，主图不按 Claim 类型分派。"""
    registered = dict(default_subgraphs() if subgraphs is None else subgraphs)

    def initialize(state: GraphInput):
        """初始化，生成全局 run_id 和 context"""
        return {
            "run_id": uuid4().hex, "stage": "initialized", "subgraph_results": {},
            "context": VerificationContext(
                target_place=state["request"].target_place,
                checked_at=datetime.now(timezone.utc),
            ),
        }

    async def extract(state: GraphInput):
        """调用提取器，生成主张列表并更新 stage"""
        result = await extraction.extract(**state["request"].model_dump())
        return {"claims": result.claims, "stage": "extracted"}

    def after_extraction(state: VerificationState):
        """提取后分支，若无主张则直接汇总结果"""
        return "prepare_context" if state["claims"] else "assemble_result"

    async def prepare_context(state: VerificationState, runtime: Runtime[VerificationCapabilities]):
        """调用地点解析器，更新 context 中的 resolved_place"""
        context = state["context"]
        resolver = runtime.context.place_resolver
        if resolver is not None:
            place = await resolver(context.target_place)
            context = context.model_copy(update={"resolved_place": place})
        return {"context": context, "stage": "context_prepared"}

    def dispatch(state: VerificationState):
        """分发子图，若无注册子图则直接汇总结果"""
        if not registered:
            return "assemble_result"
        return [Send("run_subgraph", {"graph_name": name, "claims": state["claims"], "context": state["context"],}) for name in registered]

    async def run_subgraph(state: BranchInput, runtime: Runtime[VerificationCapabilities]):
        name = state["graph_name"]
        budget = runtime.context.run_budget
        # 图片提取和地点解析已消耗运行预算，分支只能使用绝对截止时间前的剩余量。
        timeout = runtime.context.subgraph_timeout_seconds
        if budget is not None:
            timeout = min(timeout, remaining(budget.deadline_at))
        try:
            if timeout <= 0:
                raise TimeoutError("运行预算已耗尽")
            # 子图获得独立副本，内部选择和修改不会改变其他分支的材料。
            payload: SubgraphInput = {
                "claims": [claim.model_copy(deep=True) for claim in state["claims"]],
                "context": state["context"].model_copy(deep=True),
            }
            async with asyncio.timeout(timeout):
                output = await registered[name].ainvoke(payload, context=runtime.context)
            if "result" not in output:
                raise KeyError("result")
            result = SubgraphResult.model_validate(output["result"])
            if result.graph_name != name:
                raise ValueError("子图结果名称与注册名称不一致")
            claim_ids = {claim.claim_id for claim in state["claims"]}
            selected = set(result.selected_claim_ids)
            if not selected <= claim_ids:
                raise ValueError("子图引用了未提交的主张")
            if any(finding.claim_id not in selected for finding in result.findings):
                raise ValueError("子图发现未关联其选择的主张")
        except TimeoutError:
            # 超时必须记为执行失败，不能伪装成证据不足：两者对用户含义不同。
            logger.warning("Verification subgraph %s timed out after %.1fs", name, timeout)
            result = SubgraphResult(graph_name=name, status="failed", error=f"TimeoutError: 子图超过运行预算 {timeout:g} 秒")
        except Exception as error:
            # 子图失败单独记录，其他分支仍能完成并进入汇总。
            logger.exception("Verification subgraph %s failed", name)
            result = SubgraphResult(graph_name=name, status="failed", error=f"{type(error).__name__}: {error}")
        return {"subgraph_results": {name: result}}

    def collect(state: VerificationState):
        if set(state["subgraph_results"]) != set(registered):
            raise ValueError("子图结果未完整汇合")
        return {"stage": "collected"}

    def detect_conflicts(results: dict[str, SubgraphResult]) -> list[ClaimConflict]:
        """同一主张被多个子图选中时，找出结论互斥的发现对。

        冲突只陈述事实——哪两条结论打架、各自依据什么——不替用户裁决。一致的多项发现是
        正常情况，不产生矛盾；缺证据或有条件成立都不与任何结论互斥。
        """
        by_claim: dict[str, dict[str, ClaimFinding]] = {}
        for name, result in results.items():
            for finding in result.findings:
                if finding.assessment is not None:
                    by_claim.setdefault(finding.claim_id, {})[name] = finding
        conflicts = []
        for claim_id, picked in by_claim.items():
            if len(picked) < 2:
                continue
            supporting = {name: item for name, item in picked.items()
                          if verdict_polarity(item.assessment.verdict) == "supports"}
            refuting = {name: item for name, item in picked.items()
                        if verdict_polarity(item.assessment.verdict) == "refutes"}
            if not (supporting and refuting):
                continue
            detail = "；".join(f"{name} 判「{item.assessment.verdict}」：{item.summary}"
                              for name, item in sorted(picked.items()))
            conflicts.append(ClaimConflict(claim_id=claim_id, by_graph=picked, detail=detail))
        return conflicts

    def assemble_result(state: VerificationState):
        results = state["subgraph_results"]
        statuses = {result.status for result in results.values()}
        if not state["claims"]:
            status = "no_claims"
        elif not results or statuses == {"not_implemented"}:
            status = "not_implemented"
        elif statuses <= {"completed", "skipped"}:
            status = "completed"
        elif statuses == {"failed"}:
            status = "failed"
        else:
            status = "partial"
        # 汇总执行数据；面向用户的报告由前端按结论、理由、证据三级展示。
        return {"stage": "finished", "result": VerificationRun(
            run_id=state["run_id"], context=state["context"], claims=state["claims"],
            subgraph_results={name: results[name] for name in registered if name in results},
            conflicts=detect_conflicts(results), status=status,
        )}

    builder = StateGraph(
        VerificationState, input_schema=GraphInput, output_schema=GraphOutput,
        context_schema=VerificationCapabilities,
    )
    builder.add_node("initialize", initialize)
    builder.add_node("extract_claims", extract)
    builder.add_node("prepare_context", prepare_context)
    builder.add_node("run_subgraph", run_subgraph)
    builder.add_node("collect_results", collect)
    builder.add_node("assemble_result", assemble_result)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "extract_claims")
    builder.add_conditional_edges("extract_claims", after_extraction, ["prepare_context", "assemble_result"])
    builder.add_conditional_edges("prepare_context", dispatch, ["run_subgraph", "assemble_result"])
    builder.add_edge("run_subgraph", "collect_results")
    builder.add_edge("collect_results", "assemble_result")
    builder.add_edge("assemble_result", END)
    return builder.compile(name="verification")
