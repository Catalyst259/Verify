"""小红书笔记身份和输入链接校验；查询参数仅用于访问，不参与身份比较。"""

import re
from urllib.parse import urlsplit

NOTE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}
SHARE_HOSTS = {"xhslink.com", "www.xhslink.com"}
_NOTE_PATH = re.compile(r"^/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{24})/?$")


def _parts(url: str):
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or parts.username is not None
                or parts.password is not None
                or parts.port not in {None, {"http": 80, "https": 443}.get(parts.scheme)}):
            return None
        return parts
    except ValueError:
        return None


def canonical_note_id(url: str) -> str | None:
    parts = _parts(url)
    match = _NOTE_PATH.fullmatch(parts.path) if parts and parts.hostname in NOTE_HOSTS else None
    return match[1].lower() if match else None


def valid_note_url(url: str) -> bool:
    parts = _parts(url)
    return bool(canonical_note_id(url) or (
        parts and parts.hostname in SHARE_HOSTS
        and re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", parts.path)
    ))


def allowed_navigation(url: str) -> bool:
    """分享跳转只允许站内 HTTP(S) 页面；最终页面另行校验笔记身份。"""
    parts = _parts(url)
    return bool(parts and parts.hostname in NOTE_HOSTS | SHARE_HOSTS)
