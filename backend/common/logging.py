"""只在日志记录中移除小红书访问参数，原始输入和返回的来源保持不变。"""

import logging
import re

_SIGNED_URL = re.compile(r'''(https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)/[^\s"'<>?\\]*)\?[^\s"'<>\\]*''')


def install_link_redaction():
    previous = logging.getLogRecordFactory()
    if getattr(previous, '_verify_link_redaction', False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        message = record.getMessage()
        if _SIGNED_URL.search(message):
            record.msg = _SIGNED_URL.sub(r'\1?[redacted]', message)
            record.args = ()
        return record

    factory._verify_link_redaction = True
    logging.setLogRecordFactory(factory)
