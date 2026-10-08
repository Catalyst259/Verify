"""类别子图共用的执行诊断；日志与返回的 notes 共用记录，不作为核验证据。

四类子图的编排一致，诊断格式也必须一致，否则汇总和前端无法统一读取。
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
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
