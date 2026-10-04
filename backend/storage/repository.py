import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from backend.common.errors import ImageNotFound

from .models import StoredImage


class StorageRepository:
    def __init__(self, directory: Path):
        # 模拟云对象上传，暂存本地文件
        self.uploads = directory / "uploads"
        # 数据库，直连本地 sqlite3
        self.database = directory / "files.sqlite3"


    def initialize(self):
        """启动服务时执行一次，云存储开文件夹，数据库执行建表语句"""
        self.uploads.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(Path(__file__).with_name("schema.sql").read_text())

    def save(self, name: str, mime_type: str, data: bytes) -> str:
        """生成文件唯一码，保存文件至对象存储，并执行SQL语句插入文件信息"""
        code = "file_" + uuid4().hex
        path = self.uploads / code
        try:
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                order = db.execute('SELECT COALESCE(MAX("order"), 0) + 1 FROM files').fetchone()[0]
                # 这里模拟云对象存储
                path.write_bytes(data)
                db.execute(
                    'INSERT INTO files (file_code, file_name, mime_type, storage_key, size, "order") '
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (code, name, mime_type, code, len(data), order),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return code

    def get_images(self, codes: list[str]) -> list[StoredImage]:
        images = []
        with closing(sqlite3.connect(self.database)) as db:
            for code in codes:  # 以本次提交顺序为准，不依赖上传完成顺序。
                row = db.execute(
                    "SELECT mime_type, storage_key FROM files WHERE file_code = ?", (code,)
                ).fetchone()
                if row is None:
                    raise ImageNotFound(code)
                try:
                    data = (self.uploads / row[1]).read_bytes()
                except FileNotFoundError:
                    raise ImageNotFound(code) from None
                images.append(StoredImage(code, row[0], data))
        return images
