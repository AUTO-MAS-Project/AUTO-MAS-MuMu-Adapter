"""MuMu 主管理器路径解析和卸载表发现。"""

import os
import re
from pathlib import Path

from auto_mas_core.utils import IS_WINDOWS, get_logger, registry

logger = get_logger("MuMu 安装发现")

_KEYWORDS = (
    "MuMu Player",
    "MuMuPlayer",
    "Netease MuMu",
    "MuMu模拟器",
    "YXArkNights",
    "YXReverse1999",
)
_UNINSTALL_ROOTS = ("SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",) + (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def _normalize_fs_path_candidate(raw: str) -> str:
    """规整注册表/服务 ImagePath（如 \\??\\、\\?\\、\\SystemRoot）为本地路径。"""
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return ""
    # \??\C:\... ；\??\UNC\server\share\... 去掉前缀后 UNC 仍缺前导 \\，需补全
    if len(s) >= 4 and s.startswith("\\??\\"):
        rest = s[4:]
        ul = rest.upper()
        if ul.startswith("UNC\\"):
            rest = "\\\\" + rest[4:].lstrip("\\")
        s = rest
    # \\?\C:\... 扩展路径前缀（去掉 \\?\ 保留盘符路径）
    if s.startswith("\\\\?\\") and len(s) >= 7 and s[5] == ":":
        s = s[4:]
    # \SystemRoot\...（服务镜像偶见）
    sr = "\\SystemRoot"
    if s[: len(sr)].lower() == sr.lower() and (
        len(s) == len(sr) or s[len(sr) : len(sr) + 1] in ("\\", "/")
    ):
        root = os.environ.get("SystemRoot", r"C:\Windows").rstrip("\\/")
        s = root + s[len(sr) :]
    return s.strip()


# 未加引号时，从命令行中提取「盘符:\...\文件名.扩展名」（贪婪，支持空格与 / 或 \）
_CMDLINE_PATH_WITH_EXT = re.compile(
    r"([A-Za-z]:[/\\](?:[^/\\:*?\"<>|\r\n]+[/\\])*[^/\\:*?\"<>|\r\n]+"
    r"\.(?:exe|cmd|bat|lnk|msi))(?:,\d+)?",
    re.IGNORECASE,
)


def _strip_icon_index_suffix(path: str) -> str:
    return re.sub(r",\d+\s*$", "", path).strip()


def _merge_unquoted_path_tokens(s: str) -> str:
    """无引号且路径含空格时，合并 token 直至拼成存在的文件路径。"""
    parts = s.split()
    if not parts:
        return ""
    if not re.match(r"^[A-Za-z]:[/\\]", parts[0]):
        return ""
    candidate = _strip_icon_index_suffix(parts[0])
    if Path(candidate).is_file():
        return candidate
    for extra in parts[1:]:
        if extra.startswith("/") or extra.startswith("-"):
            break
        trial = _strip_icon_index_suffix(f"{candidate} {extra}")
        if Path(trial).is_file():
            return trial
        candidate = f"{candidate} {extra}"
    final = _strip_icon_index_suffix(candidate)
    return final if Path(final).is_file() else ""


def _extract_path_from_command(value: str) -> str:
    """从注册表命令行字段抽取 exe/目录路径，并去掉图标索引尾缀（如 ,0）。"""

    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # 优先取第一个引号内片段
    m = re.match(r'^\s*"([^"]+)"', s)
    if m:
        extracted = _strip_icon_index_suffix(m.group(1).strip())
        return extracted

    # 未加引号：贪婪匹配带扩展名的 Windows 路径（含空格、正斜杠/反斜杠、.msi）
    m = _CMDLINE_PATH_WITH_EXT.search(s)
    if m:
        return _strip_icon_index_suffix(m.group(1).strip())

    merged = _merge_unquoted_path_tokens(s)
    if merged:
        return merged

    # 兜底：取第一个 token（到空格为止）
    token = _strip_icon_index_suffix(s.split(" ", 1)[0].strip())
    return token


_MANAGER_PATHS = ("MuMuManager.exe", "nx_main/MuMuManager.exe", "shell/MuMuManager.exe")


def resolve_installation(raw: str) -> str | None:
    """从旧目录、管理器或卸载程序位置寻找 MuMu 管理器。"""
    candidate = Path(_normalize_fs_path_candidate(raw))
    if not candidate.exists():
        return None
    if candidate.is_file() and candidate.name.casefold() == "mumumanager.exe":
        return str(candidate)
    directory = candidate if candidate.is_dir() else candidate.parent
    for base in (directory, *directory.parents[:2]):
        for relative_path in _MANAGER_PATHS:
            manager = base / relative_path
            if manager.is_file():
                return str(manager)
    return None


def search_installations() -> list[str]:
    """扫描本品牌登记的卸载项，一条安装路径只返回一次。"""
    if not IS_WINDOWS:
        return []
    found: dict[str, str] = {}
    unresolved = 0
    for name, command in registry.uninstall_entries(_UNINSTALL_ROOTS):
        if not any(keyword.casefold() in name.casefold() for keyword in _KEYWORDS):
            continue
        candidate = _extract_path_from_command(command)
        if candidate and (path := resolve_installation(candidate)):
            found.setdefault(os.path.normcase(str(Path(path).resolve())), path)
        else:
            unresolved += 1
    if unresolved:
        logger.info(
            f"MuMu 安装发现结果: 已定位 {len(found)} 个，未定位管理程序 {unresolved} 个"
        )
    return list(found.values())
