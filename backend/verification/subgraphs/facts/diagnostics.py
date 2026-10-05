"""Fact 执行诊断；日志与返回的 notes 共用记录，不作为核验证据。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import asyncio
import json
import logging
from time import perf_counter


logger = logging.getLogger(__name__)


def record(event: str, **fields) -> str:
    payload = json.dumps({"event": event, "recorded_at": datetime.now(timezone.utc).isoformat(), **fields},
                         ensure_ascii=False)
    level = logging.WARNING if event == "page_failure" or fields.get("outcome") in {"error", "timeout"} else logging.INFO
    logger.log(level, "fact_diagnostic %s", payload)
    return payload


@contextmanager
def timed(records: list[str], stage: str, **fields):
    """包含异常和取消路径的单调时钟计时；调用方可补充结果与预算字段。"""
    started = perf_counter()
    details = {"stage": stage, "outcome": "success", **fields}
    try:
        yield details
    except BaseException as error:
        details.update(outcome="timeout" if isinstance(error, TimeoutError) else
                       "cancelled" if isinstance(error, asyncio.CancelledError) else "error",
                       error_type=type(error).__name__, error=str(error) or type(error).__name__)
        raise
    finally:
        records.append(record("stage_timing", **details, elapsed_ms=round((perf_counter() - started) * 1000, 3)))


def page_category(diagnostic: dict, *, unrecognized: bool = False) -> str | None:
    """按可观察信号分类；403 只表示访问被拒绝，不能单独证明反爬。"""
    status = diagnostic.get("http_status")
    if status == 429:
        return "rate_limited"
    if diagnostic.get("challenge_detected"):
        return "challenge"
    if status == 403:
        return "access_denied"
    if status is not None and status >= 400:
        return "http_error"
    if diagnostic.get("category"):
        return diagnostic["category"]
    if unrecognized:
        if diagnostic.get("ready_state") in {"loading", "interactive"}:
            return "load_incomplete"
        return "parse_error" if diagnostic.get("ready_state") == "complete" else "unknown"
    return None
