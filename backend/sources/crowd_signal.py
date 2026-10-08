"""Approximate crowd signal from recent public review density, not measured footfall.

The demo stage has no real crowd or transaction feed, so this source estimates crowding
from how densely recent public reviews talk about queues and crowds. It is a stand-in behind
the ``evidence_sources`` slot: a real footfall supplier implements the same
``search(query) -> list[Evidence]`` shape and replaces this class without touching the CROWD
subgraph. Every material it returns carries the approximation label in its content and source
name, so an approximation can never read as measured footfall.

The review records themselves come from an injected profiler; without one there is no public
review feed to aggregate and the source fails loudly rather than inventing a crowding level.
"""

import hashlib
import math
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone

from backend.verification.models import Evidence

APPROXIMATION_LABEL = "公开评价密度近似"
HONESTY_NOTE = ("crowd 子图的客流信号是「公开评价密度」近似：本次运行没有真实客流或交易信号，"
                "拥挤度依据近期公开评价的密度与拥挤度提及估计，不能当作实测客流或排队数据。")
# 拥挤度只识别明确描述人多或排队的说法，否定句（「人不多」）不算拥挤提及。
CROWD_MENTIONS = ("排队", "排长队", "人山人海", "人挤人", "拥挤", "爆满", "人很多", "人多", "挤满", "限流")
NEGATIONS = ("不", "没", "无", "少", "空")
FRESHNESS_DAYS = 180
# 等级只是「提及比例」的分档，不是客流绝对量；接入真实客流数据后应整体替换。
CROWDED_RATIO = 0.4
MILD_RATIO = 0.15

ReviewProfiler = Callable[[str], Awaitable[list[dict]]]


def _published(value: object) -> date | None:
    """评价的发布日；来源给的是日期字符串还是日期对象都按当地日历日解读。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _mentions(text: str) -> bool:
    """正文提到拥挤且附近没有否定词，才算一条拥挤提及。"""
    for word in CROWD_MENTIONS:
        start = 0
        while (index := text.find(word, start)) >= 0:
            start = index + len(word)
            if not any(negation in text[max(0, index - 4):index] for negation in NEGATIONS):
                return True
    return False


class ApproximateCrowdSignal:
    """One ``search`` call aggregates the profiler's recent review records for one query."""

    def __init__(self, profile: ReviewProfiler | None = None, *, freshness_days: float = FRESHNESS_DAYS):
        if isinstance(freshness_days, bool) or not math.isfinite(freshness_days) or freshness_days <= 0:
            raise ValueError("公开评价的时效窗口必须大于 0 天")
        self.profile = profile
        self.freshness_days = freshness_days

    async def search(self, query: str) -> list[Evidence]:
        """Return one aggregate material; an empty or stale feed yields no material at all."""
        query = query.strip()
        if not query:
            raise ValueError("客流近似查询不能为空")
        if self.profile is None:
            raise NotImplementedError("尚未接入公开评价数据供应商；客流近似信号缺少数据来源")
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.freshness_days)
        fresh = []
        for record in await self.profile(query):
            published = _published(record.get("published_at"))
            if published is None or datetime.combine(published, datetime.min.time(), timezone.utc) >= cutoff:
                fresh.append((published, record))
        if not fresh:
            return []
        mentions = [record for _, record in fresh
                    if _mentions(f"{record.get('title', '')} {record.get('content', '')}")]
        ratio = len(mentions) / len(fresh)
        level = ("拥挤度提及集中" if ratio >= CROWDED_RATIO else
                 "有一定拥挤度提及" if ratio >= MILD_RATIO else "拥挤度提及较少")
        dated = sorted(day for day, _ in fresh if day is not None)
        window = f"最近一条评价发布于 {dated[-1].isoformat()}" if dated else "评价发布时间未知"
        return [Evidence(
            source=f"{APPROXIMATION_LABEL}（非实测客流）",
            content=(f"近似客流信号：{level}。查询「{query}」，时效窗口 {self.freshness_days:.0f} 天内取得 "
                     f"{len(fresh)} 条公开评价，其中 {len(mentions)} 条提到拥挤或排队，"
                     f"{len(fresh) - len(dated)} 条发布时间未知；{window}。"
                     "这是公开评价密度的估算，不是客流或交易实测数据。"),
            url=None, evidence_id="crowd-signal-" + hashlib.sha1(query.encode()).hexdigest()[:12],
            source_type="WEB", published_at=dated[-1] if dated else None, retrieved_at=now,
        )]
