from typing import Annotated, NotRequired, TypedDict

from backend.extraction.models import Claim

from .models import ClaimFinding, SubgraphResult, VerificationContext, VerificationInput, VerificationRun


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


class SubgraphInput(TypedDict):
    """主图传入完整主张与共享上下文；子图自行选择需要核验的内容。"""

    claims: list[Claim]
    context: VerificationContext


class SubgraphOutput(TypedDict):
    """子图完成后必须返回结果，内部取证状态不作为主图输出。"""

    result: SubgraphResult


class SubgraphState(SubgraphInput, total=False):
    """各类别子图共用的状态；只有 claims/context 是调用时的必填字段。

    selected_claim_ids 在选择后写入，findings/notes/error 在执行中积累，
    result 由最终组装步骤写入。轮次、预算和中间材料由具体子图扩展，
    模型客户端和工具连接通过 runtime context 注入。
    """

    selected_claim_ids: list[str]
    findings: list[ClaimFinding]
    notes: list[str]
    error: str | None
    result: SubgraphResult


class BranchInput(TypedDict):
    graph_name: str
    claims: list[Claim]
    context: VerificationContext
