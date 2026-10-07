"""Fact 网页取证的页面失败分类；计时与记录由共享诊断模块提供。"""


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
