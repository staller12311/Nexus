"""把数据库里的自定义模型变成可调用的 ``Provider``（不改 `agent/` 一个字符）。

做法：继承 ``agent.config.ModelConfig``，覆写 ``api_key()`` 让它从内存字段取值，
而不是读 ``os.environ``。``Provider`` 只依赖 ``ModelConfig`` 的这几个属性
（``base_url`` / ``model`` / ``api_key()`` / ``temperature`` / ``max_tokens``），
所以这样构造出来的实例和「从 YAML 读出来的」在行为上完全一致。
"""

from __future__ import annotations

from pydantic import Field, field_validator

from agent.config import AppConfig, ConfigError, ModelConfig
from agent.logging_setup import register_secret
from agent.provider import Provider

from .custom_models import CustomModel

#: 自定义模型不填这些参数时的默认值。
#:
#: ``max_tokens`` 从 2048 提到 8192：对推理模型来说「思考」和「正文」共用这一个预算，
#: 2048 会让长思考直接把配额烧光、正文为空（实测思考 7000+ 字后什么都没输出）。
#: ``context_window`` 从 32768 提到 128K：32768 会让摘要压缩过早触发，读长文档不够用；
#: 已知 1M 的模型另有按模型名推断的逻辑（``context_window_for``），这里只是兜底。
CUSTOM_DEFAULTS: dict[str, float | int] = {
    "temperature": 0.7,
    "max_tokens": 8192,
    "context_window": 131072,
}


class DbModelConfig(ModelConfig):
    """``ModelConfig`` 的「密钥在内存里」版本。"""

    #: 完整密钥，只存在内存中；repr=False + exclude=True 双保险，避免被打印/序列化
    stored_api_key: str = Field(default="", repr=False, exclude=True)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """用户常多敲一个 `/`（``https://api.x.cn/v1/``），SDK 拼路径时会出现双斜杠。

        父类的校验器虽然也 rstrip 了，这里显式再兜一次：这条规则属于本功能的契约，
        不应该依赖父类实现细节。
        """
        return (value or "").strip().rstrip("/")

    def api_key(self) -> str:
        """覆写：不再读环境变量，直接用内存里的密钥。"""
        key = (self.stored_api_key or "").strip()
        if not key:
            raise ConfigError("自定义模型的 API Key 为空，请在「模型管理」里重新填写")
        return key


def build_custom_provider(
    config: AppConfig,
    model: CustomModel,
    *,
    request_timeout: float | None = None,
    max_retries: int | None = None,
) -> Provider:
    """用数据库里的一行组装 Provider；同时把密钥登记给日志脱敏器。

    ``request_timeout`` / ``max_retries`` 可覆盖：起标题那种"失败就降级"的场景要用短超时 + 不重试。
    """
    runtime = dict(CUSTOM_DEFAULTS)
    # 用户显式填了就用用户的：不同厂商对输出上限的约束差别很大，只能由用户负责
    if model.max_tokens:
        runtime["max_tokens"] = int(model.max_tokens)
    if model.context_window:
        runtime["context_window"] = int(model.context_window)

    model_config = DbModelConfig(
        base_url=model.base_url,
        model=model.model,
        api_key_env="(自定义)",  # 仅用于 describe() 展示，不读这个变量
        stored_api_key=model.api_key,
        **runtime,
    )
    # 万一别处（异常栈、请求回显）把密钥打进日志，会被替换成 ***
    register_secret(model.api_key)
    return Provider(
        model_config,
        request_timeout=request_timeout or config.agent.request_timeout,
        max_retries=config.agent.max_retries if max_retries is None else max_retries,
    )