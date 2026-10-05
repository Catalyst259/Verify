from typing import Annotated, NotRequired, TypedDict

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


class VerificationState(TypedDict):
    """LangGraph 主图状态，包含业务输入、运行 ID、执行阶段、主张列表、共享上下文、子图结果和最终输出。

    提取完成后的节点使用此类型；初始化和提取节点仅依赖 GraphInput。
    result 在最终汇总后才存在，其余字段由输入、初始化和提取步骤准备。
    """
    request: VerificationInput
    run_id: str
    stage: str
    claims: list[Claim]
    context: VerificationContext
    subgraph_results: Annotated[dict[str, SubgraphResult], merge_subgraph_results]
    result: NotRequired[VerificationRun]


class SubgraphState(TypedDict):
    """子图输入必须包含主张列表和共享上下文，result 在子图完成后写入。"""

    claims: list[Claim]
    context: VerificationContext
    result: NotRequired[SubgraphResult]


class BranchInput(TypedDict):
    graph_name: str
    claims: list[Claim]
    context: VerificationContext
