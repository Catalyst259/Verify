import logging
from io import BytesIO
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator

from .storage.repository import StorageRepository
from .verification.models import ClaimExtractionResult
from .verification.service import VerificationService

MAX_FILE_SIZE = 10 * 1024 * 1024
MIME_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class VerificationRequest(BaseModel):
    target_place: str = Field(min_length=1, max_length=100)
    text: str = ""
    link: list[str] = Field(default_factory=list)
    image: list[str] = Field(default_factory=list)

    @field_validator("target_place")
    @classmethod
    def valid_place(cls, value):
        if not value.strip():
            raise ValueError("请填写目标地点")
        return value

    @field_validator("link")
    @classmethod
    def valid_links(cls, values):
        for value in values:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username:
                raise ValueError("链接必须是完整的 HTTP(S) URL")
        return values


def create_router(storage: StorageRepository, verification: VerificationService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/files", status_code=201)
    def upload(file: UploadFile):
        data = file.file.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(413, "每张图片不能超过 10 MB")
        try:
            with Image.open(BytesIO(data)) as image:
                mime_type = MIME_TYPES[image.format]
                image.verify()
        except (KeyError, UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(415, "请上传有效的 PNG、JPEG、WebP 或 GIF 图片") from None
        code = storage.save(file.filename or "image", mime_type, data)
        return {"file_code": code, "file_id": code, "mime_type": mime_type}

    @router.post("/verifications", response_model=ClaimExtractionResult)
    async def verify(request: VerificationRequest):
        try:
            return await verification.verify(**request.model_dump())
        except FileNotFoundError as error:
            raise HTTPException(404, f"图片不存在：{error}") from None
        except PermissionError as error:
            raise HTTPException(503, str(error)) from None
        except TimeoutError:
            raise HTTPException(504, "提取超时，请减少材料后重试") from None
        except Exception:
            logging.getLogger(__name__).exception("Claim extraction failed")
            raise HTTPException(502, "Agent 提取失败，请检查模型配置、浏览器及链接；详情见后端日志") from None

    return router
