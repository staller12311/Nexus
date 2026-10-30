"""图片上传的落盘与读取（backend 自己的能力，agent/ 不感知）。

只存元数据、不把 base64 写库：图片放 ``data/uploads/<uuid>.<ext>``，
消息表只引用 ``id``。这样历史记录不会把 SQLite 撑爆，旧会话也不受影响。
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

#: 前端压缩后通常 200KB 左右；给多图/原图留余量，后端硬上限 5MB
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp)$")
_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class UploadError(Exception):
    """上传校验失败（映射成 400/413）。"""


def sniff_mime(data: bytes) -> str | None:
    """按文件头识别图片类型；不信任客户端声明的 Content-Type。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_name(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    return name[:120] or "image"


class UploadStore:
    """图片文件仓库；所有文件操作都限制在 ``directory`` 之下。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_id(upload_id: str) -> str:
        text = (upload_id or "").strip()
        if not _ID_PATTERN.match(text):
            raise UploadError("图片 id 非法")
        return text

    def save(self, data: bytes, filename: str | None, content_type: str | None) -> dict:
        if len(data) == 0:
            raise UploadError("图片内容为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise UploadError(f"单张图片不能超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB")
        mime = sniff_mime(data)
        if mime is None or mime not in ALLOWED_MIME:
            raise UploadError("只支持 JPG / PNG / WebP 图片")
        # 客户端声明的类型只做参考；文件头才是准绳
        _ = content_type
        upload_id = f"{uuid.uuid4().hex}{ALLOWED_MIME[mime]}"
        path = self.directory / upload_id
        path.write_bytes(data)
        return {
            "id": upload_id,
            "url": f"/api/uploads/{upload_id}",
            "mime": mime,
            "name": safe_name(filename),
            "size": len(data),
        }

    def resolve(self, upload_id: str) -> dict:
        """把消息里的附件引用解析成可下发的完整元数据。"""
        clean = self.validate_id(upload_id)
        path = self.directory / clean
        if not path.is_file():
            raise UploadError("图片不存在或已被清理")
        suffix = path.suffix.lower()
        mime = _MIME_BY_SUFFIX.get(suffix)
        if mime is None:
            raise UploadError("不支持的图片类型")
        return {
            "id": clean,
            "url": f"/api/uploads/{clean}",
            "mime": mime,
            "name": clean,
            "size": path.stat().st_size,
        }

    def path_for(self, upload_id: str) -> tuple[Path, str]:
        clean = self.validate_id(upload_id)
        path = self.directory / clean
        if not path.is_file():
            raise UploadError("图片不存在或已被清理")
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
        if mime is None:
            raise UploadError("不支持的图片类型")
        return path, mime

    def data_url(self, upload_id: str) -> str:
        path, mime = self.path_for(upload_id)
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{payload}"

    def resolve_many(self, refs: list[dict]) -> list[dict]:
        """校验一轮消息的附件数量并解析；重复 id 只保留一次。"""
        if len(refs) > MAX_IMAGES_PER_MESSAGE:
            raise UploadError(f"每条消息最多 {MAX_IMAGES_PER_MESSAGE} 张图片")
        resolved: list[dict] = []
        seen: set[str] = set()
        total = 0
        for ref in refs:
            upload_id = str(ref.get("id") or "").strip()
            if upload_id in seen:
                continue
            metadata = self.resolve(upload_id)
            if ref.get("name"):
                metadata["name"] = safe_name(str(ref["name"]))
            seen.add(upload_id)
            resolved.append(metadata)
            total += int(metadata["size"])
        if total > MAX_IMAGE_BYTES * MAX_IMAGES_PER_MESSAGE:
            raise UploadError("本轮图片总大小超出限制")
        return resolved