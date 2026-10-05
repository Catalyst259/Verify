"""类别子图注册入口，每个子图自行筛选主张和管理内部任务。"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..capabilities import VerificationCapabilities
from ..models import SubgraphResult
from ..state import SubgraphState


VerificationSubgraph = CompiledStateGraph[SubgraphState, VerificationCapabilities]


def build_placeholder_subgraph(name: str) -> VerificationSubgraph:
    """创建可执行占位子图，明确返回未实现状态。"""

    def pending(state: SubgraphState):
        return {"result": SubgraphResult(
            graph_name=name,
            status="not_implemented",
            notes=[f"{name} 子图尚未实现主张选择和核验流程"],
        )}

    builder = StateGraph(SubgraphState, context_schema=VerificationCapabilities)
    builder.add_node("pending", pending)
    builder.add_edge(START, "pending")
    builder.add_edge("pending", END)
    return builder.compile(name=name)


def default_subgraphs() -> dict[str, VerificationSubgraph]:
    return {name: build_placeholder_subgraph(name) for name in ("fact", "route", "crowd", "experience")}
