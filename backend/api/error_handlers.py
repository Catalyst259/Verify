"""将业务异常转换为 HTTP 响应，并记录提取失败和未预期错误。"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.common.errors import ExtractionFailed, ExtractionTimeout, ImageNotFound, LinkReadError, ModelNotConfigured

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(LinkReadError)
    async def link_read_failed(request: Request, error: LinkReadError):
        logger.warning("Link material unavailable: %s", error)
        return JSONResponse(status_code=error.status_code, content={"detail": str(error)})

    @app.exception_handler(ImageNotFound)
    async def image_not_found(request: Request, error: ImageNotFound):
        return JSONResponse(status_code=404, content={"detail": f"图片不存在：{error}"})

    @app.exception_handler(ModelNotConfigured)
    async def model_not_configured(request: Request, error: ModelNotConfigured):
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.exception_handler(ExtractionTimeout)
    async def extraction_timeout(request: Request, error: ExtractionTimeout):
        return JSONResponse(status_code=504, content={"detail": "提取超时，请减少材料后重试"})

    @app.exception_handler(ExtractionFailed)
    async def extraction_failed(request: Request, error: ExtractionFailed):
        logger.error("Claim extraction failed", exc_info=error)
        return JSONResponse(status_code=502, content={
            "detail": "Agent 提取失败，请检查模型配置、浏览器及链接；详情见后端日志",
        })

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.error("Unhandled request error", exc_info=error)
        return JSONResponse(status_code=500, content={"detail": "服务内部错误，请稍后重试"})
