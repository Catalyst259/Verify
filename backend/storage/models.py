from dataclasses import dataclass


@dataclass
class StoredImage:
    file_code: str
    mime_type: str
    data: bytes
