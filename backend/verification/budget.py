"""运行级取证预算：类别公平共享浏览器槽位，地图独立限流，使用一个总截止时间。

槽位必须跨子图共享——主图用 Send 并行广播子图，若每个子图各自持有信号量，
并发会随子图数量线性增长。类别轮换与服务切片避免一批慢主张耗尽其他类别的机会。
"""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

# 保守上限：一次运行内的浏览器取证并发，与子图数量无关。
BROWSER_CONCURRENCY = 2
# 运行级总超时，早于各子图自身的硬超时（subgraph_timeout_seconds 默认 300）兜住尾部。
RUN_TIMEOUT_SECONDS = 240.0
MAX_CLAIM_SEARCH_SECONDS = 30.0


def remaining(deadline: datetime) -> float:
    return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())


class RunBudget:
    """一次核验运行内的共享取证预算；每次运行新建，不能跨运行复用槽位。"""

    def __init__(self, timeout_seconds: float = RUN_TIMEOUT_SECONDS,
                 concurrency: int = BROWSER_CONCURRENCY, started_at: datetime | None = None):
        self.timeout_seconds = timeout_seconds
        self.browser_concurrency = concurrency
        self.browser_slots = asyncio.Semaphore(concurrency)
        self.map_slots = asyncio.Semaphore(BROWSER_CONCURRENCY)
        self.deadline_at = (started_at or datetime.now(timezone.utc)) + timedelta(seconds=timeout_seconds)
        self._browser_queues: dict[str, deque[asyncio.Future]] = {}
        self._browser_categories: deque[str] = deque()
        self._browser_dispatcher: asyncio.Task | None = None
        self._browser_inflight = 0
        self._last_browser_category: str | None = None
        self.browser_contended = False

    def prepare_browser_batch(self, claim_count: int):
        """模型选中前按完整批次预判积压，避免先完成 Plan 的类别独占浏览器。"""
        if claim_count > self.browser_concurrency:
            self.browser_contended = True

    def _pending_browser_count(self) -> int:
        return sum(not waiter.done() for queue in self._browser_queues.values() for waiter in queue)

    def _next_browser_waiter(self):
        # 新类别可能在上一轮只剩一个类别时加入；下一次先给它服务机会。
        if len(self._browser_categories) > 1 and self._browser_categories[0] == self._last_browser_category:
            self._browser_categories.rotate(-1)
        while self._browser_categories:
            category = self._browser_categories.popleft()
            queue = self._browser_queues[category]
            while queue and queue[0].done():
                queue.popleft()
            if not queue:
                del self._browser_queues[category]
                continue
            waiter = queue.popleft()
            if queue:
                self._browser_categories.append(category)
            else:
                del self._browser_queues[category]
            self._last_browser_category = category
            return waiter
        return None

    async def _dispatch_browser(self):
        acquired = False
        try:
            while self._pending_browser_count():
                # 取得 permit 后再选类别，使等待期间到达的新类别也参加轮换。
                await self.browser_slots.acquire()
                acquired = True
                waiter = self._next_browser_waiter()
                if waiter is None:
                    break
                self._browser_inflight += 1
                waiter.set_result(None)
                acquired = False  # permit 的所有权已经交给 browser_slot。
        finally:
            if acquired:
                self.browser_slots.release()
            if self._browser_dispatcher is asyncio.current_task():
                self._browser_dispatcher = None

    async def _stop_idle_dispatcher(self):
        if self._pending_browser_count() or self._browser_dispatcher is None:
            return
        dispatcher, self._browser_dispatcher = self._browser_dispatcher, None
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher

    @asynccontextmanager
    async def browser_slot(self, category: str, deadline_at: datetime):
        """类别轮换等待浏览器槽位；排队仍消耗原来的绝对截止时间。"""
        waiter = asyncio.get_running_loop().create_future()
        if category not in self._browser_queues:
            self._browser_queues[category] = deque()
            self._browser_categories.append(category)
        self._browser_queues[category].append(waiter)
        if self._pending_browser_count() + self._browser_inflight > self.browser_concurrency:
            self.browser_contended = True
        if self._browser_dispatcher is None:
            self._browser_dispatcher = asyncio.create_task(self._dispatch_browser())
        try:
            async with asyncio.timeout(remaining(min(deadline_at, self.deadline_at))):
                await waiter
            yield
        finally:
            if waiter.done() and not waiter.cancelled():
                self._browser_inflight -= 1
                self.browser_slots.release()
            else:
                waiter.cancel()
                queue = self._browser_queues.get(category)
                if queue is not None:
                    with suppress(ValueError):
                        queue.remove(waiter)
                    if not queue:
                        del self._browser_queues[category]
                        self._browser_categories.remove(category)
            await self._stop_idle_dispatcher()

    def claim_deadline(self, category: str, stage_deadline: datetime) -> datetime:
        """积压运行保留可完成一次读取的切片；绝对截止时间始终优先。"""
        deadline = min(stage_deadline, self.deadline_at)
        if not self.browser_contended:
            return deadline
        # 按排队数平分余量会把导航、读取和模型结束切碎成不足十秒的重复失败。
        return min(deadline, datetime.now(timezone.utc) + timedelta(seconds=MAX_CLAIM_SEARCH_SECONDS))

    def can_retry_browser(self, claim_count: int, stage_deadline: datetime) -> bool:
        """只有可容纳下一整轮服务及模型收尾时才开启可选补搜。"""
        if not self.browser_contended:
            return True
        demand = self._pending_browser_count() + self._browser_inflight + claim_count
        waves = (demand + self.browser_concurrency - 1) // self.browser_concurrency
        return remaining(min(stage_deadline, self.deadline_at)) >= waves * MAX_CLAIM_SEARCH_SECONDS + 30
