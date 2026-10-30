"""把「文本 + 附件元数据」转换成 OpenAI Vision content 数组（只存在于请求期）。"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .uploads import UploadError, UploadStore

#: 历史里最多重放几轮带图消息（当前轮 + 最近 2 轮），避免 base64 把请求体撑爆
MAX_IMAGE_TURNS = 3


def build_vision_transform(
    uploads: UploadStore, *, max_image_turns: int = MAX_IMAGE_TURNS
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """返回 AgentLoop 的 request_transform 纯函数。"""

    def transform(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_user_indexes: list[int] = []
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "user" and message.get("attachments"):
                latest_user_indexes.append(index)
        keep_images = set(latest_user_indexes[:max_image_turns])
        data_url_cache: dict[str, str] = {}

        transformed: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            clean: dict[str, Any] = {
                key: value for key, value in message.items() if key != "attachments"
            }
            attachments = message.get("attachments") or []
            if message.get("role") != "user" or not attachments:
                transformed.append(clean)
                continue

            text = str(message.get("content") or "").strip()
            if index not in keep_images:
                clean["content"] = f"{text} [图片]".strip()
                transformed.append(clean)
                continue

            parts: list[dict[str, Any]] = []
            if text and text != "[图片]":
                parts.append({"type": "text", "text": text})
            for attachment in _safe_list(attachments):
                upload_id = str((attachment or {}).get("id") or "")
                try:
                    data_url = data_url_cache.get(upload_id)
                    if data_url is None:
                        data_url = uploads.data_url(upload_id)
                        data_url_cache[upload_id] = data_url
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        }
                    )
                except UploadError:
                    # 文件被删/损坏：保留文本占位，不让整轮对话失败
                    parts.append({"type": "text", "text": "[图片不可用]"})
            if not parts:
                parts.append({"type": "text", "text": text or "[图片]"})
            clean["content"] = parts
            transformed.append(clean)
        return transformed

    return transform


def _safe_list(value: Any) -> list[Mapping]:
    return value if isinstance(value, list) else []