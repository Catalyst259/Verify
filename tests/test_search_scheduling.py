"""Run-local admission must give later categories an opportunity without leaking slots."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.verification.budget import RunBudget, remaining


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def assert_idle(budget):
    assert budget._browser_dispatcher is None
    assert budget._pending_browser_count() == budget._browser_inflight == 0
    assert not budget._browser_queues and not budget._browser_categories
    acquired = 0
    try:
        async with asyncio.timeout(0.1):
            for _ in range(budget.browser_concurrency):
                await budget.browser_slots.acquire()
                acquired += 1
    finally:
        for _ in range(acquired):
            budget.browser_slots.release()


def test_late_category_runs_before_the_earlier_category_batch_finishes():
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        entered, active, maximum = [], 0, 0
        gates = {f"{category}{i}": asyncio.Event() for category, count in (("fact", 6), ("experience", 3))
                 for i in range(count)}

        async def claim(category, number):
            nonlocal active, maximum
            label = f"{category}{number}"
            async with budget.browser_slot(category, budget.deadline_at):
                active += 1
                maximum = max(maximum, active)
                entered.append(label)
                try:
                    await gates[label].wait()
                finally:
                    active -= 1

        first = [asyncio.create_task(claim("fact", i)) for i in range(6)]
        await wait_until(lambda: len(entered) == 2)
        later = [asyncio.create_task(claim("experience", i)) for i in range(3)]
        await wait_until(lambda: budget._pending_browser_count() == 7)
        gates["fact0"].set()
        await wait_until(lambda: len(entered) == 3)
        assert entered[:3] == ["fact0", "fact1", "experience0"]
        for gate in gates.values():
            gate.set()
        await asyncio.gather(*first, *later)
        assert len(entered) == 9 and maximum == 2 and active == 0
        await assert_idle(budget)

    asyncio.run(exercise())


def test_waiting_categories_alternate_when_one_browser_is_available():
    async def exercise():
        budget = RunBudget(timeout_seconds=2, concurrency=1)
        await budget.browser_slots.acquire()
        entered = []

        async def claim(category, number):
            async with budget.browser_slot(category, budget.deadline_at):
                entered.append((category, number))
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(claim(category, i)) for category in ("fact", "experience") for i in range(4)]
        await wait_until(lambda: budget._pending_browser_count() == 8)
        budget.browser_slots.release()
        await asyncio.gather(*tasks)
        assert entered == [(category, i) for i in range(4) for category in ("fact", "experience")]
        await assert_idle(budget)

    asyncio.run(exercise())


def test_external_slot_holder_preserves_queue_timeout_and_all_permits():
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        for _ in range(2):
            await budget.browser_slots.acquire()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=0.02)
        with pytest.raises(TimeoutError):
            async with budget.browser_slot("fact", deadline):
                pytest.fail("An expired queue must not begin taking evidence")
        assert budget._browser_dispatcher is None and not budget._browser_queues
        for _ in range(2):
            budget.browser_slots.release()
        await assert_idle(budget)

    asyncio.run(exercise())


def test_cancelled_waiter_stops_blocked_dispatcher_and_budget_can_be_reused():
    async def exercise():
        budget = RunBudget(timeout_seconds=2, concurrency=1)
        await budget.browser_slots.acquire()

        async def claim():
            async with budget.browser_slot("fact", budget.deadline_at):
                pytest.fail("A cancelled waiter must not enter")

        task = asyncio.create_task(claim())
        await wait_until(lambda: budget._pending_browser_count() == 1)
        dispatcher = budget._browser_dispatcher
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert dispatcher.done() and budget._browser_dispatcher is None
        budget.browser_slots.release()
        async with budget.browser_slot("experience", budget.deadline_at):
            assert budget._browser_inflight == 1
        await assert_idle(budget)

    asyncio.run(exercise())


def test_cancelling_one_category_does_not_remove_another_category():
    async def exercise():
        budget = RunBudget(timeout_seconds=2, concurrency=1)
        await budget.browser_slots.acquire()
        entered = []

        async def claim(category):
            async with budget.browser_slot(category, budget.deadline_at):
                entered.append(category)

        first = asyncio.create_task(claim("fact"))
        second = asyncio.create_task(claim("experience"))
        await wait_until(lambda: budget._pending_browser_count() == 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        budget.browser_slots.release()
        await second
        assert entered == ["experience"]
        await assert_idle(budget)

    asyncio.run(exercise())


def test_active_cancellation_releases_its_permit():
    async def exercise():
        budget = RunBudget(timeout_seconds=2, concurrency=1)
        entered = asyncio.Event()

        async def claim():
            async with budget.browser_slot("fact", budget.deadline_at):
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(claim())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_idle(budget)

    asyncio.run(exercise())


def test_cancellation_between_permit_grant_and_context_entry_releases_slot(monkeypatch):
    async def exercise():
        budget = RunBudget(timeout_seconds=2, concurrency=1)
        dispatch = budget._dispatch_browser
        entered = []

        async def cancel_after_grant():
            await dispatch()
            task.cancel()

        monkeypatch.setattr(budget, "_dispatch_browser", cancel_after_grant)

        async def claim():
            async with budget.browser_slot("fact", budget.deadline_at):
                entered.append(True)

        task = asyncio.create_task(claim())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert entered == []
        await assert_idle(budget)

    asyncio.run(exercise())


def test_exception_in_claim_releases_slot_and_dispatcher():
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        with pytest.raises(ValueError, match="search failed"):
            async with budget.browser_slot("fact", budget.deadline_at):
                raise ValueError("search failed")
        await assert_idle(budget)

    asyncio.run(exercise())


def test_map_slots_remain_available_while_all_browser_slots_are_held():
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        for _ in range(2):
            await budget.browser_slots.acquire()
        try:
            async with asyncio.timeout(0.1):
                async with budget.map_slots:
                    async with budget.map_slots:
                        assert budget.map_slots.locked()
        finally:
            for _ in range(2):
                budget.browser_slots.release()
        assert not budget.map_slots.locked()
        await assert_idle(budget)

    asyncio.run(exercise())


@pytest.mark.parametrize("count", [1, 2])
def test_one_or_two_claims_keep_the_original_stage_deadline(count):
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        stage = budget.deadline_at - timedelta(seconds=0.2)
        deadlines = []

        async def claim():
            async with budget.browser_slot("fact", stage):
                deadlines.append(budget.claim_deadline("fact", stage))
                await asyncio.sleep(0)

        await asyncio.gather(*(claim() for _ in range(count)))
        assert deadlines == [stage] * count
        assert budget.claim_deadline("fact", stage + timedelta(seconds=100)) == budget.deadline_at
        await assert_idle(budget)

    asyncio.run(exercise())


def test_backlog_gives_later_claims_time_when_first_two_searches_stall(monkeypatch):
    monkeypatch.setattr("backend.verification.budget.MAX_CLAIM_SEARCH_SECONDS", 0.04)
    async def exercise():
        budget = RunBudget(timeout_seconds=1.2)
        stage = datetime.now(timezone.utc) + timedelta(seconds=0.8)
        original_deadline = budget.deadline_at
        started, completed, timed_out = [], [], []

        async def claim(number):
            async with budget.browser_slot("fact", stage):
                deadline = budget.claim_deadline("fact", stage)
                assert deadline <= stage < original_deadline
                started.append(number)
                try:
                    async with asyncio.timeout(remaining(deadline)):
                        if number < 2:
                            await asyncio.sleep(30)
                        else:
                            await asyncio.sleep(0)
                            completed.append(number)
                except TimeoutError:
                    timed_out.append(number)

        await asyncio.gather(*(claim(i) for i in range(22)))
        assert started == list(range(22)) and completed == list(range(2, 22))
        assert set(timed_out) == {0, 1}
        assert budget.deadline_at == original_deadline and remaining(stage) > 0
        await assert_idle(budget)

    asyncio.run(exercise())


def test_large_backlog_keeps_a_useful_window_including_the_last_waiters():
    async def exercise():
        budget = RunBudget(timeout_seconds=240)
        windows = []
        async def claim():
            async with budget.browser_slot("fact", budget.deadline_at):
                windows.append(remaining(budget.claim_deadline("fact", budget.deadline_at)))
                await asyncio.sleep(0)
        await asyncio.gather(*(claim() for _ in range(22)))
        assert budget.browser_contended and len(windows) == 22
        assert all(29 < window <= 30 for window in windows)
        await assert_idle(budget)
    asyncio.run(exercise())


def test_optional_retry_requires_time_for_the_whole_wave_and_original_deadline():
    budget = RunBudget(timeout_seconds=240)
    budget.browser_contended = True
    assert not budget.can_retry_browser(22, budget.deadline_at)
    assert budget.can_retry_browser(2, budget.deadline_at)
    short = datetime.now(timezone.utc) + timedelta(seconds=50)
    assert not budget.can_retry_browser(2, short)
    assert RunBudget(timeout_seconds=50).can_retry_browser(22, short)


def test_claim_service_slice_is_capped_at_thirty_seconds_when_backlogged():
    async def exercise():
        budget = RunBudget(timeout_seconds=240)
        for _ in range(2):
            await budget.browser_slots.acquire()
        entered, slices = [], []
        release = asyncio.Event()

        async def claim(number):
            async with budget.browser_slot("fact", budget.deadline_at):
                slice_end = budget.claim_deadline("fact", budget.deadline_at)
                slices.append((number, remaining(slice_end)))
                entered.append(number)
                await release.wait()

        tasks = [asyncio.create_task(claim(i)) for i in range(5)]
        await wait_until(lambda: budget._pending_browser_count() == 5)
        for _ in range(2):
            budget.browser_slots.release()
        await wait_until(lambda: len(entered) == 2)
        assert all(29 < seconds <= 30 for _, seconds in slices)
        release.set()
        await asyncio.gather(*tasks)
        await assert_idle(budget)

    asyncio.run(exercise())


def test_all_tasks_finished_leaves_no_background_dispatcher():
    async def exercise():
        budget = RunBudget(timeout_seconds=2)
        baseline = asyncio.all_tasks()

        async def claim(category):
            async with budget.browser_slot(category, budget.deadline_at):
                await asyncio.sleep(0)

        await asyncio.gather(*(claim(f"category-{i % 3}") for i in range(30)))
        await assert_idle(budget)
        assert asyncio.all_tasks() <= baseline

    asyncio.run(exercise())
