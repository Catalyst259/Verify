from typing import Annotated, TypedDict

from backend.extraction.models import Claim

from .models import SubgraphResult, VerificationContext, VerificationInput, VerificationRun


def merge_subgraph_results(
    current: dict[str, SubgraphResult], updates: dict[str, SubgraphResult]
) -> dict[str, SubgraphResult]:
    """合并并行子图结果，防止其他分支的结果被覆盖。"""
    merged = dict(current)
    for name, result in updates.items():
        if name in merged and merged[name] != result:
            raise ValueError(f"子图 {name} 返回了冲突结果")
        merged[name] = result
    return merged


class GraphInput(TypedDict):
    request: VerificationInput


class GraphOutput(TypedDict):
    result: VerificationRun


class VerificationState(TypedDict, total=False):
    request: VerificationInput
    run_id: str
    stage: str
    claims: list[Claim]
    context: VerificationContext
    subgraph_results: Annotated[dict[str, SubgraphResult], merge_subgraph_results]
    result: VerificationRun


class SubgraphState(TypedDict, total=False):
    """子图只接收完整主张列表和共享上下文，内部字段可自行扩展。"""

    claims: list[Claim]
    context: VerificationContext
    result: SubgraphResult


class BranchInput(TypedDict):
    graph_name: str
    claims: list[Claim]
    context: VerificationContext
