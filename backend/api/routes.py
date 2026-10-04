from io import BytesIO

from fastapi import APIRouter, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from .dto import VerificationRequest
from backend.storage.repository import StorageRepository
from backend.verification.models import VerificationInput, VerificationRun
from backend.verification.service import VerificationService

MAX_FILE_SIZE = 10 * 1024 * 1024
MIME_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


def create_router(storage: StorageRepository, verification: VerificationService) -> APIRouter:
    """创建 FastAPI 路由，包含文件上传和验证请求处理两个接口"""
    router = APIRouter(prefix="/api")

    @router.post("/files", status_code=201)
    def upload(file: UploadFile) -> dict:
        """上传文件接口，限制每张图片不超过 10 MB，并验证图片格式为 PNG、JPEG、WebP 或 GIF"""
        data = file.file.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(413, "每张图片不能超过 10 MB")
        try:
            with Image.open(BytesIO(data)) as image:
                # PIL 会根据文件内容自动识别格式，而不是仅依赖文件扩展名
                mime_type = MIME_TYPES[str(image.format)]
                image.verify()
        except (KeyError, UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(415, "请上传有效的 PNG、JPEG、WebP 或 GIF 图片") from None
        code = storage.save(file.filename or "image", mime_type, data)
        return {"file_code": code, "file_id": code, "mime_type": mime_type}

    @router.post("/verifications", response_model=VerificationRun)
    async def verify(request: VerificationRequest) -> VerificationRun:
        """运行核验流程，返回主张、核验上下文、子图结果和执行状态。"""
        return await verification.run(VerificationInput(**request.model_dump()))

    return router
