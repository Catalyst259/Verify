"""跨模块共享的业务异常；HTTP 状态码和响应格式由接口层决定。"""


class ImageNotFound(Exception):
    """提交的图片标识没有对应记录或文件，异常消息为图片标识。"""


class ModelNotConfigured(Exception):
    """模型缺少必要配置，异常消息为可向用户展示的配置提示。"""


class ExtractionTimeout(Exception):
    """主张提取未在限定时间内完成。"""


class ExtractionFailed(Exception):
    """Agent 未完成提取，或返回结果不符合结构、来源约束。"""


class LinkReadError(Exception):
    """可向用户显示的链接读取错误；detail 不含原始签名链接或浏览器异常。"""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.status_code = status_code
