from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class VerificationRequest(BaseModel):
    """VerificationRequestDTO，与前端交互时使用，前端传入四个字段"""
    target_place: str = Field(min_length=1, max_length=100)
    text: str = ""
    link: list[str] = Field(default_factory=list)
    image: list[str] = Field(default_factory=list)

    """长度校验"""
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
