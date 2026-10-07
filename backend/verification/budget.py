"""运行级取证预算：所有类别子图共享一个浏览器并发槽位池和一个总截止时间。

槽位必须跨子图共享——主图用 Send 并行广播子图，若每个子图各自持有信号量，
并发会随子图数量线性增长，一条子图占满槽位还会把其他子图饿死到各自超时。
"""

import asyncio
from datetime import datetime, timedelta, timezone

# 保守上限：一次运行内的浏览器取证并发，与子图数量无关。
BROWSER_CONCURRENCY = 2
# 运行级总超时，早于各子图自身的硬超时（subgraph_timeout_seconds 默认 300）兜住尾部。
RUN_TIMEOUT_SECONDS = 240.0


def remaining(deadline: datetime) -> float:
    return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())


class RunBudget:
    """一次核验运行内的共享取证预算；每次运行新建，不能跨运行复用槽位。"""

    def __init__(self, timeout_seconds: float = RUN_TIMEOUT_SECONDS,
                 concurrency: int = BROWSER_CONCURRENCY, started_at: datetime | None = None):
        self.timeout_seconds = timeout_seconds
        self.browser_slots = asyncio.Semaphore(concurrency)
        self.deadline_at = (started_at or datetime.now(timezone.utc)) + timedelta(seconds=timeout_seconds)
