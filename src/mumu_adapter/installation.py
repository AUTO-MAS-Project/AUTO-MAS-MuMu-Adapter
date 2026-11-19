"""厂商驱动使用的安装参数；由已锁定的插件配置投影，不参与配置持久化。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallationInfo:
    Path: Path
    Type: str
    MaxWaitTime: int = 300
    ForceKillBeforeLaunch: bool = False
    ForceKillOnClose: bool = False
    Name: str = ""


@dataclass(frozen=True)
class InstallationConfig:
    Info: InstallationInfo
