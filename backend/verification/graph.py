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

from .capabilities import VerificationCapabilities
from .models import SubgraphResult, VerificationContext, VerificationRun
from .state import BranchInput, GraphInput, GraphOutput, VerificationState
from .subgraphs import default_subgraphs

logger = logging.getLogger(__name__)


def build_verification_graph(
    extraction: ClaimExtractionService,
    subgraphs: Mapping[str, CompiledStateGraph] | None = None,
) -> CompiledStateGraph:
    """编译主图；注册表决定子图范围，主图不按 Claim 类型分派。"""
    registered = dict(default_subgraphs() if subgraphs is None else subgraphs)

    def initialize(state: VerificationState):
        return {
            "run_id": uuid4().hex, "stage": "initialized", "subgraph_results": {},
            "context": VerificationContext(
                target_place=state["request"].target_place,
                checked_at=datetime.now(timezone.utc),
            ),
        }

    async def extract(state: VerificationState):
        result = await extraction.extract(**state["request"].model_dump())
        return {"claims": result.claims, "stage": "extracted"}

    def after_extraction(state: VerificationState):
        return "prepare_context" if state["claims"] else "assemble_result"

    async def prepare_context(state: VerificationState, runtime: Runtime[VerificationCapabilities]):
        context = state["context"]
        resolver = runtime.context.place_resolver
        if resolver is not None:
            place = await resolver(context.target_place)
            context = context.model_copy(update={"resolved_place": place})
        return {"context": context, "stage": "context_prepared"}

    def dispatch(state: VerificationState):
        if not registered:
            return "assemble_result"
        return [Send("run_subgraph", {
            "graph_name": name, "claims": state["claims"], "context": state["context"],
        }) for name in registered]

    async def run_subgraph(state: BranchInput, runtime: Runtime[VerificationCapabilities]):
        name = state["graph_name"]
        try:
            # 子图获得独立副本，内部选择和修改不会改变其他分支的材料。
            payload = {
                "claims": [claim.model_copy(deep=True) for claim in state["claims"]],
                "context": state["context"].model_copy(deep=True),
            }
            async with asyncio.timeout(runtime.context.subgraph_timeout_seconds):
                output = await registered[name].ainvoke(payload, context=runtime.context)
            result = SubgraphResult.model_validate(output["result"])
            if result.graph_name != name:
                raise ValueError("子图结果名称与注册名称不一致")
            claim_ids = {claim.claim_id for claim in state["claims"]}
            selected = set(result.selected_claim_ids)
            if not selected <= claim_ids:
                raise ValueError("子图引用了未提交的主张")
            if any(finding.claim_id not in selected for finding in result.findings):
                raise ValueError("子图发现未关联其选择的主张")
        except Exception as error:
            # 子图失败单独记录，其他分支仍能完成并进入汇总。
            logger.exception("Verification subgraph %s failed", name)
            result = SubgraphResult(graph_name=name, status="failed", error=f"{type(error).__name__}: {error}")
        return {"subgraph_results": {name: result}}

    def collect(state: VerificationState):
        if set(state["subgraph_results"]) != set(registered):
            raise ValueError("子图结果未完整汇合")
        return {"stage": "collected"}

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
        # 汇总执行数据；逐主张评估与面向用户的报告尚未实现。
        return {"stage": "finished", "result": VerificationRun(
            run_id=state["run_id"], context=state["context"], claims=state["claims"],
            subgraph_results={name: results[name] for name in registered if name in results},
            status=status,
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
