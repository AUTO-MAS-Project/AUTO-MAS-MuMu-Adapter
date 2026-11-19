"""MuMu 模拟器适配配置。"""

from auto_mas_core import ConfigCollection, collection
from auto_mas_core.devices import ManagedEmulatorDevice, ManagedEmulatorEntry


class MuMuDevice(ManagedEmulatorDevice):
    pass


class MuMuGame(ManagedEmulatorEntry):
    devices: ConfigCollection[MuMuDevice] = collection(MuMuDevice)  # type: ignore[assignment]  # pyright: ignore[reportIncompatibleVariableOverride]
