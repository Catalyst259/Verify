"""一次请求内的小红书原始材料；图片不写入持久化上传目录或图状态。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LinkMaterial:
    original_url: str
    canonical_url: str
    title: str
    content: str
    images: tuple[bytes, ...] = ()  # 按笔记轮播顺序截取的 PNG 图片元素。
