from io import BytesIO
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import api, main
from backend.storage.repository import StorageRepository
from backend.verification.models import ClaimExtractionResult


def png(color="blue"):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_persists_and_submission_recovers_in_request_order(tmp_path):
    captured = {}

    async def extract(target_place, description_text, links, images):
        captured.update(target_place=target_place, text=description_text, links=links, images=images)
        return ClaimExtractionResult(target_place="Agent 改写的地点", claims=[{
            "claim_id": "wrong-id", "type": "CROWD", "content": "周末人少。",
            "sources": [{"source_type": "TEXT", "source_ref": "description", "source_text": description_text}],
        }])

    with TestClient(main.create_app(tmp_path, extract)) as client:
        codes = []
        for color in ("red", "blue"):
            response = client.post("/api/files", files={"file": ("../test.png", png(color), "image/jpeg")})
            assert response.status_code == 201
            data = response.json()
            assert data["file_code"] == data["file_id"]
            assert data["mime_type"] == "image/png"
            codes.append(data["file_code"])
        assert codes[0] != codes[1]
    # 重启应用，确认数据不只是保存在内存。
    with TestClient(main.create_app(tmp_path, extract)) as client:
        payload = {"target_place": " 上海迪士尼乐园 ", "text": "周末人少", "image": codes[::-1],
                   "link": ["https://example.com/a", "https://example.com/b"]}
        response = client.post("/api/verifications", json=payload)
        assert response.status_code == 200
        assert response.json() == {"target_place": payload["target_place"], "claims": [{
            "claim_id": "claim_001", "type": "CROWD", "content": "周末人少。",
            "sources": [{"source_type": "TEXT", "source_ref": None, "source_text": payload["text"]}],
        }]}
        assert captured["target_place"] == payload["target_place"]
        assert captured["text"] == payload["text"]
        assert captured["links"] == payload["link"]
        assert [image.file_code for image in captured["images"]] == codes[::-1]
        assert [image.data for image in captured["images"]] == [png("blue"), png("red")]
        assert client.get("/").status_code == 200
    with sqlite3.connect(tmp_path / "files.sqlite3") as db:
        rows = db.execute('SELECT file_code, size, "order" FROM files ORDER BY "order"').fetchall()
    assert rows == [(codes[0], len(png("red")), 1), (codes[1], len(png("blue")), 2)]


def test_invalid_material_is_rejected_before_calling_agent(tmp_path, monkeypatch):
    async def unexpected_call(*args):
        pytest.fail("无效材料不应进入 Agent")

    with TestClient(main.create_app(tmp_path, unexpected_call)) as client:
        assert client.post("/api/verifications", json={"target_place": " "}).status_code == 422
        assert client.post("/api/verifications", json={"target_place": "公园", "link": ["file:///etc/passwd"]}).status_code == 422
        assert client.post("/api/files", files={"file": ("x.png", b"not an image", "image/png")}).status_code == 415
        monkeypatch.setattr(api, "MAX_FILE_SIZE", 10)
        assert client.post("/api/files", files={"file": ("x.png", png(), "image/png")}).status_code == 413
        assert client.post("/api/verifications", json={"target_place": "公园", "image": ["unknown"]}).status_code == 404

        storage = StorageRepository(tmp_path)
        code = storage.save("image.png", "image/png", png())
        (storage.uploads / code).unlink()
        assert client.post("/api/verifications", json={"target_place": "公园", "image": [code]}).status_code == 404
