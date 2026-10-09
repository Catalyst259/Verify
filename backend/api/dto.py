from pydantic import BaseModel, Field, field_validator

from backend.common.xiaohongshu_links import valid_note_url


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
        links = [value.strip() for value in values]
        for index, value in enumerate(links, 1):
            if not valid_note_url(value):
                raise ValueError(f"第 {index} 条链接必须是小红书笔记链接或 xhslink.com 分享短链")
        return links
