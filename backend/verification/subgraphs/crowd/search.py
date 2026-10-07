"""CROWD 的取证：网页会话负责常规材料，客流近似来源作为独立来源并入同一轮。

近似来源不是网页，不能通过浏览器的 read_page 登记，所以这里在网页取证之后额外向
它查询一次，把材料去重后并入本轮。近似材料不是网页，因此本轮结果用 CROWD 自己的
SearchResult 承载；网页材料仍按网页规则登记。

只要接入了客流近似来源，本条的诚实标注就写进诊断；骨架把诊断并入
SubgraphResult.notes，用户看到的是「近似而非实测」，而不是一个看起来像实测客流数字的结论。
来源失败只记入本轮错误并保留已有材料，不使整轮取证失败。
"""

from collections.abc import Mapping
from datetime import datetime

from backend.sources.crowd_signal import HONESTY_NOTE

from ...capabilities import EvidenceSource
from ...models import Evidence
from ..facts.model import MAX_EVIDENCE_PER_ROUND
from ..facts.search import SearchSession
from .model import CrowdSearchResult

# 客流近似信号的来源键；接真实客流数据时替换同名来源即可，子图不改。
CROWD_SIGNAL_SOURCE = "crowd_signal"


class CrowdSearchSession(SearchSession):
    """在 Fact 的网页会话之上追加客流近似来源的材料。"""

    def __init__(self, claim, deadline_at: datetime, *, checked_at: datetime | None = None,
                 input_urls: tuple[str, ...] = (), evidence_sources: Mapping[str, EvidenceSource] | None = None):
        super().__init__(claim, deadline_at, checked_at=checked_at, input_urls=input_urls,
                         evidence_sources=evidence_sources)
        self.signal_query = f"{claim.plan.target} {claim.plan.scenario}".strip()
        # 近似材料按来源的稳定 ID 去重，补搜重复查询不会把同一份材料再登记一次。
        self.known_ids = {item.evidence_id for item in claim.evidence}
        if CROWD_SIGNAL_SOURCE in self.evidence_sources:
            self.diagnostics.append(HONESTY_NOTE)

    def add_signal(self, items: list[Evidence]) -> None:
        """登记近似材料；不占用网页工具预算，但受本轮证据上限约束。"""
        added = 0
        for item in items:
            if len(self.evidence) >= MAX_EVIDENCE_PER_ROUND:
                break
            if item.evidence_id in self.known_ids:
                continue
            self.known_ids.add(item.evidence_id)
            self.evidence.append(item)
            added += 1
        if added:
            self.update_round(new_evidence_count=len(self.evidence))

    def result(self, raw: str | None = None) -> CrowdSearchResult:
        """以实际登记的材料为准；模型输出不能改写或伪造已取材料。

        网页材料是 FactEvidence 子类，与按基类还原的实例类型不同即不相等，
        因此这里按字段值比对。
        """
        if raw is not None:
            output = CrowdSearchResult.model_validate_json(raw)
            records = {item.evidence_id: item.model_dump() for item in self.evidence}
            if output.claim_id != self.claim_id:
                raise ValueError("Search 返回了其他 Claim 的结果")
            if any(records.get(item.evidence_id) != item.model_dump() for item in output.evidence):
                raise ValueError("Search 返回了未登记或被改写的证据")
        return CrowdSearchResult(claim_id=self.claim_id, evidence=self.evidence, error=self.round.search_error)


def crowd_signal_runner(runtime):
    """网页取证照旧，并额外查询一次客流近似来源；来源失败只记入本轮错误。"""
    source = runtime.context.evidence_sources.get(CROWD_SIGNAL_SOURCE)
    if source is None:
        return runtime.context.search

    async def run(session: CrowdSearchSession, system_prompt: str, task: str) -> str | None:
        raw = None
        if runtime.context.search is not None:
            raw = await runtime.context.search(session, system_prompt, task)
        try:
            session.add_signal(await source.search(session.signal_query))
        except Exception as error:
            message = f"{CROWD_SIGNAL_SOURCE}: {type(error).__name__}: {str(error) or '来源未完成'}"
            previous = session.round.search_error
            session.update_round(search_error="; ".join(filter(None, [previous, message])))
        return raw

    return run
