"""类别子图注册入口，每个子图自行筛选主张和管理内部任务。"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..capabilities import VerificationCapabilities
from ..models import SubgraphResult
from ..state import SubgraphInput, SubgraphOutput, SubgraphState


VerificationSubgraph = CompiledStateGraph[SubgraphState, VerificationCapabilities, SubgraphInput, SubgraphOutput]


def build_placeholder_subgraph(name: str) -> VerificationSubgraph:
    """创建可执行占位子图，明确返回未实现状态。"""

    def pending(state: SubgraphInput) -> SubgraphOutput:
        return {"result": SubgraphResult(
            graph_name=name,
            status="not_implemented",
            notes=[f"{name} 子图尚未实现主张选择和核验流程"],
        )}

    builder = StateGraph(
        SubgraphState, input_schema=SubgraphInput, output_schema=SubgraphOutput,
        context_schema=VerificationCapabilities,
    )
    builder.add_node("pending", pending)
    builder.add_edge(START, "pending")
    builder.add_edge("pending", END)
    return builder.compile(name=name)


def default_subgraphs() -> dict[str, VerificationSubgraph]:
    """四类主张各一个可执行子图；新增类别在此注册。"""
    from .crowd.graph import build_crowd_subgraph
    from .experience.graph import build_experience_subgraph
    from .facts.graph import build_fact_subgraph
    from .route.graph import build_route_subgraph
    return {"fact": build_fact_subgraph(), "route": build_route_subgraph(),
            "crowd": build_crowd_subgraph(), "experience": build_experience_subgraph()}
