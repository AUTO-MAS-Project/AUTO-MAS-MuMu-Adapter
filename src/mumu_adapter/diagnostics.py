"""MuMu CLI 错误的脱敏摘要。"""

import re
from importlib import metadata

try:
    ADAPTER_VERSION = metadata.version("auto-mas-mumu-adapter")
except metadata.PackageNotFoundError:
    ADAPTER_VERSION = "unknown"


def mumu_error_code(output: str) -> int | None:
    """提取混有 CLI 日志的 JSON 输出中的厂商错误码。"""
    match = re.search(r'"errcode"\s*:\s*(-?\d{1,10})(?!\d)', output)
    return int(match[1]) if match else None


def operation_detail(stage: str, version: str, elapsed: float, timeout: float) -> str:
    return (
        f"stage={stage}, mumu={version}, adapter={ADAPTER_VERSION}, "
        f"elapsed={elapsed:.2f}s, timeout={timeout:g}s"
    )


class MuMuCommandError(RuntimeError):
    """CLI 执行失败；异常正文仅使用固定阶段和解析后的值。"""
