"""MuMu 模拟器适配声明与生命周期。"""

import sys
from typing import cast

from auto_mas_core import GameAdapterPlugin, GameTypeDecl
from auto_mas_core.utils import get_logger

from .config import MuMuDevice, MuMuGame
from .control import Control, search
from .discovery import resolve_installation


class Plugin(GameAdapterPlugin):
    game_type_key = "mumu"
    decl = GameTypeDecl(
        game_class=MuMuGame,
        device_class=MuMuDevice,
        control_class=Control,
        search=search,
        resolve_installation=resolve_installation,
    )

    async def pre_enable(self):
        if sys.platform != "win32":
            raise RuntimeError("MuMu 模拟器适配仅支持 Windows")

    async def on_enable(self):
        for entry in self.instances():
            try:
                control = cast(Control, entry._control)
                await control.refresh()
                await control.sweep_backups()
            except Exception as error:
                get_logger("MuMu 模拟器适配").warning(
                    f"模拟器暂不可用，保留配置：{error}"
                )

    async def on_disable(self):
        await self.on_teardown()

    async def on_teardown(self):
        for entry in self.instances():
            await entry._control.teardown()
