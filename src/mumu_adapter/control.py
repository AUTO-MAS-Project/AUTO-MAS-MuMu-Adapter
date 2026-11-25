"""MuMu 实例控制、资源占用与配置恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from auto_mas_core import LockTicket, config_manager
from auto_mas_core.adapters.game import TRIGGER_TICKET, DeviceSubscribeError
from auto_mas_core.devices import (
    DeviceInfo,
    DeviceRef,
    DeviceStatus,
    EmulatorControl,
    EmulatorExpect,
    EmulatorRuntimePolicy,
    InstanceSettings,
)
from auto_mas_core.utils import file_lock, get_logger

from .applaunch import AdbReconnectReport, adb_output_kind, build_adb_runner
from .backend import MuMu6Manager
from .config import MuMuDevice, MuMuGame
from .discovery import search_installations
from .installation import InstallationConfig, InstallationInfo

KEYS = {
    "width": "resolution_width.custom",
    "height": "resolution_height.custom",
    "dpi": "resolution_dpi.custom",
    "keep_alive": "app_keptlive",
    "high_frame_rate": "dynamic_adjust_frame_rate",
    "vertical_sync": "vertical_sync",
    "display_fps": "show_frame_rate",
    "auto_rotate": "window_auto_rotate",
    "vram_strategy": "renderer_strategy",
}

logger = get_logger("MuMu 模拟器适配")
RestorePhase = Literal["startup", "before-task", "after-task"]


class Control(EmulatorControl[MuMuGame, MuMuDevice]):
    kind = "mumu"

    device_type = MuMuDevice

    capabilities = (
        "open",
        "close",
        "show",
        "hide",
        "minimize",
        "maximize",
        "create_instance",
        "delete_instance",
        "install_app",
        "open_store",
        "settings",
        "stable_mode",
    )

    def recovery_pending(self, uid: UUID) -> bool:
        return bool(self.config.devices[uid].session.backup)

    def __init__(self, config: MuMuGame | None = None) -> None:
        super().__init__(config=config)
        self._backend: MuMu6Manager | None = None
        self._operation = asyncio.Lock()
        self._resources: dict[UUID, BinaryIO] = {}

    @property
    def busy(self) -> bool:
        return self._operation.locked()

    async def build_backend(self, policy: EmulatorRuntimePolicy) -> MuMu6Manager:
        info = self.config.info
        if info.path is None:
            raise ValueError("尚未配置 MuMu 管理程序")
        return MuMu6Manager(
            InstallationConfig(
                InstallationInfo(
                    Path=info.path,
                    Type="mumu",
                    MaxWaitTime=info.max_wait_time,
                    ForceKillBeforeLaunch=info.force_kill_before_launch,
                    ForceKillOnClose=info.force_kill_on_close,
                )
            ),
            runtime_policy=policy,
        )

    async def backend(self) -> MuMu6Manager:
        if self._backend is None:
            self._backend = await self.build_backend(
                EmulatorRuntimePolicy(
                    silence_enabled=lambda: self.runtime.silent,
                    block_ads_enabled=lambda: self.runtime.block_ad,
                    resource_path=self.runtime.resource_path,
                )
            )
        return self._backend

    def device(self, uid: UUID) -> MuMuDevice:
        device = self.config.devices[uid]
        if device.info.tombstone:
            raise KeyError("所选模拟器实例已删除")
        return device

    async def _refresh(self) -> None:
        backend = await self.backend()
        devices = await backend.getInfo(None)
        entry = self.config
        existing = {
            d.info.index: d for d in entry.devices.values() if not d.info.tombstone
        }
        next_slot = max((d.info.slot for d in entry.devices.values()), default=-1) + 1
        for index, info in devices.items():
            device = existing.get(str(index))
            if device is None:
                uid = uuid5(entry.uid, f"{index}:{next_slot}")
                entry.devices.add(
                    self.device_type,
                    uid=uid,
                    payload={"info": {"index": str(index), "slot": next_slot}},
                )
                await entry.devices.commit()
                device = entry.devices[uid]
                next_slot += 1
            device.info.name = info.title
            device.info.status = DeviceStatus(int(info.status))
            device.info.adb_address = info.adb_address
            await device.commit()
        # 发现暂缺不删除条目，尤其不能丢掉残留备份与有效绑定。
        for index, device in existing.items():
            if index not in devices:
                device.info.status = DeviceStatus.NOT_FOUND
                await device.commit()

    async def refresh(self) -> None:
        async with self.operation_scope(self._operation):
            await self._refresh()

    async def _acquire_resource(self, uid: UUID) -> None:
        device = self.device(uid)
        path = self.config.info.path
        if path is None:
            raise ValueError("尚未配置模拟器管理程序")
        identity = (
            f"{self.kind}:{os.path.normcase(str(path.resolve()))}:{device.info.index}"
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        directory = Path(tempfile.gettempdir()) / "AUTO-MAS" / "device-locks"
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / key).open("a+b")
        try:
            file_lock.acquire(handle)
        except OSError as error:
            handle.close()
            raise DeviceSubscribeError(
                "设备已被其他托管或 MAS 进程占用", usage="其他设备任务"
            ) from error
        self._resources[uid] = handle

    async def _backup(self, uid):
        backend = await self.backend()
        output = await backend._run("setting", "-v", self.device(uid).info.index, "-aw")
        data = json.loads(output[output.index("{") : output.rindex("}") + 1])
        if not isinstance(data, dict) or not data:
            raise RuntimeError("无法读取 MuMu 原始设置")
        return data

    async def _retry_restore(self, uid: UUID, *, phase: RestorePhase) -> None:
        device = self.config.devices[uid]
        backup = device.session.backup
        if not backup:
            return

        backend = await self.backend()
        index = device.info.index
        shutdown = backend.shutdown_reports.pop(index, None)
        context = (
            f", phase={phase}, fields={len(backup)}, "
            f"empty={sum(value == '' or value is None for value in backup.values())}, "
            f"non_string={sum(not isinstance(value, str) for value in backup.values())}"
        )
        if phase == "after-task" and shutdown is not None:
            context += (
                f", shutdown={shutdown.outcome}, "
                f"since_offline={time.monotonic() - shutdown.observed_at:.2f}s"
            )
        with tempfile.TemporaryDirectory(prefix="auto-mas-mumu-") as directory:
            path = Path(directory) / "restore.json"
            path.write_text(json.dumps(backup, ensure_ascii=False), encoding="utf-8")
            await backend._run_cli(
                "restore-settings",
                "setting",
                "-v",
                index,
                "-p",
                str(path),
                context=context,
            )

        device.session.backup = {}
        await device.commit()
        await config_manager.flush()
        logger.info(f"MuMu 配置已恢复（{phase}）")

    async def _on_device_subscribed(
        self, device_uid: UUID, expect: object | None
    ) -> None:
        if expect is not None and not isinstance(expect, EmulatorExpect):
            raise TypeError("模拟器期望须为 EmulatorExpect")
        await self._acquire_resource(device_uid)
        async with self.operation_scope(self._operation):
            device = self.device(device_uid)
            backend = await self.backend()
            backend.shutdown_reports.pop(device.info.index, None)
            try:
                await self._retry_restore(device_uid, phase="before-task")
            except Exception as error:
                logger.warning(f"上次配置仍未恢复，保留备份并继续本轮任务：{error}")
            await self._ensure(device_uid)
            if expect is not None:
                if not device.session.backup:
                    device.session.backup = await self._backup(device_uid)
                    await device.commit()
                    await config_manager.flush()
                await self.apply_expect(device_uid, expect)

    async def _on_device_unsubscribed(
        self, device_uid: UUID, *, phase: RestorePhase = "after-task"
    ) -> None:
        if device_uid not in self._resources:
            return
        try:
            async with self.operation_scope(self._operation):
                await self._retry_restore(device_uid, phase=phase)
        finally:
            handle = self._resources.pop(device_uid, None)
            if handle is not None:
                try:
                    file_lock.release(handle)
                finally:
                    handle.close()

    async def apply_expect(self, uid, expect: EmulatorExpect):
        backend = await self.backend()
        current = await backend._setting_get(
            self.device(uid).info.index, list(KEYS.values())
        )
        values = {}
        for field, value in asdict(expect).items():
            if value is None:
                continue
            key = KEYS[field]
            if isinstance(value, tuple):
                if current.get(key) in value:
                    continue
                value = value[0]
            values[key] = str(value).lower() if isinstance(value, bool) else str(value)
        if any(key in values for key in (KEYS["width"], KEYS["height"], KEYS["dpi"])):
            values["resolution_mode"] = "custom"
        if values:
            await backend._setting_set(self.device(uid).info.index, values)

    async def _ensure(self, uid: UUID) -> None:
        backend = await self.backend()
        device = self.device(uid)
        if self.config.info.stable_mode:
            await backend.apply_stable_mode(device.info.index)
        if self.config.info.config_guard:
            changes = self.config.info.baselines.get(str(uid), {})
            if changes:
                await self._write_settings(uid, changes)

    async def apply_stable_mode(self, uid: UUID, *, ticket: LockTicket) -> list[str]:
        self.check_subscription(uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            return await backend.apply_stable_mode(self.device(uid).info.index)

    async def open(
        self, device_uid: UUID, package: str | None = None, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            # 任务订阅已按守卫基准备份，再写入本轮期望。开机不能覆盖该期望。
            if ticket is TRIGGER_TICKET and self.device_usage(device_uid) is None:
                await self._ensure(device_uid)
            backend = await self.backend()
            await self.open_backend(backend, device_uid, package, ticket=ticket)
            await self._refresh()

    async def open_backend(
        self,
        backend: MuMu6Manager,
        uid: UUID,
        package: str | None,
        *,
        ticket: LockTicket,
    ) -> None:
        await self.set_block_ad(self.runtime.block_ad)
        await backend.open(self.device(uid).info.index, package or "")

    async def close(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await backend.close(self.device(device_uid).info.index)
            if ticket is TRIGGER_TICKET and self.device_usage(device_uid) is None:
                await self._ensure(device_uid)
            await self._refresh()

    async def _visible(self, uid: UUID, ticket: LockTicket, visible: bool) -> None:
        self.check_subscription(uid, ticket)
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            await backend.setVisible(self.device(uid).info.index, visible)
            await self._refresh()

    async def show(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        await self._visible(device_uid, ticket, True)

    async def hide(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        await self._visible(device_uid, ticket, False)

    async def minimize(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            await self.window_state(device_uid, minimized=True)

    async def maximize(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            await self.window_state(device_uid, minimized=False)

    async def window_state(self, uid, *, minimized):
        import win32con
        import win32gui

        backend = await self.backend()
        raw = await backend.get_device_info(self.device(uid).info.index)
        data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
        rows = backend._extract_device_entries(data)
        row = next(
            (
                row
                for row in rows
                if str(row.get("index", row.get("vm_index", "")))
                == self.device(uid).info.index
            ),
            data,
        )
        value = row.get("main_wnd")
        if isinstance(value, str):
            hwnd = int(value, 0)
        else:
            hwnd = int(value) if isinstance(value, int) else 0
        if not hwnd or not win32gui.IsWindow(hwnd):
            raise RuntimeError("MuMu 未提供有效主窗口")
        win32gui.ShowWindow(
            hwnd, win32con.SW_MINIMIZE if minimized else win32con.SW_MAXIMIZE
        )
        expected = win32con.SW_SHOWMINIMIZED if minimized else win32con.SW_SHOWMAXIMIZED
        if win32gui.GetWindowPlacement(hwnd)[1] != expected:
            raise RuntimeError("模拟器窗口操作失败")

    async def launch_app(
        self, device_uid: UUID, package: str, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        backend = await self.backend()
        result = await backend.launch_app(self.device(device_uid).info.index, package)
        if not result.ok:
            raise RuntimeError(f"应用启动失败：{result.reason} {result.diagnostics}")

    async def open_store(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        backend = await self.backend()
        result = await backend.open_store(self.device(device_uid).info.index)
        if not result.ok:
            raise RuntimeError(
                f"应用商店启动失败：{result.reason} {result.diagnostics}"
            )

    async def create_instance(self, name: str | None = None) -> UUID:
        async with self.operation_scope(self._operation):
            backend = await self.backend()
            index = str(await backend.create_instance(name))
            await self._refresh()
            return next(
                d.uid
                for d in self.config.devices.values()
                if d.info.index == index and not d.info.tombstone
            )

    async def delete_instance(self, device_uid: UUID, *, ticket: LockTicket) -> None:
        self.check_subscription(device_uid, ticket)
        async with self.operation_scope(self._operation):
            device = self.device(device_uid)
            backend = await self.backend()
            await backend.delete_instance(device.info.index)
            device.info.tombstone = True
            device.info.status = DeviceStatus.NOT_FOUND
            await device.commit()

    async def install_app(
        self, device_uid: UUID, path: Path, *, ticket: LockTicket
    ) -> None:
        self.check_subscription(device_uid, ticket)
        backend = await self.backend()
        info = await self.device_info(device_uid)
        if not info.adb_address:
            raise RuntimeError("模拟器尚未提供 ADB 地址")
        reconnect = AdbReconnectReport()
        run = build_adb_runner(
            backend.get_adb_path(), info.adb_address, report=reconnect
        )
        code, output = await run(
            "install", "-r", str(path), timeout=self.config.info.max_wait_time
        )
        if code != 0 or "Success" not in output:
            logger.debug(f"MuMu 安装 APK 失败: {output}")
            raise RuntimeError(
                f"MuMu 安装 APK 失败（returncode={code}, reason={adb_output_kind(code, output)}, "
                f"{reconnect.detail()}）"
            )

    async def device_info(self, uid: UUID) -> DeviceInfo:
        backend = await self.backend()
        return (await backend.getInfo(self.device(uid).info.index))[
            self.device(uid).info.index
        ]

    def resolve_device(self, uid: UUID) -> DeviceRef:
        return DeviceRef(
            self.kind, str(self.config.info.path), self.device(uid).info.index
        )

    def get_adb_path(self) -> Path | None:
        return self._backend.get_adb_path() if self._backend is not None else None

    async def read_settings(self, uid: UUID) -> InstanceSettings:
        backend = await self.backend()
        return await backend.read_instance_settings(self.device(uid).info.index)

    async def _write_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None = None,
    ) -> dict[str, int]:
        backend = await self.backend()
        return await backend.write_instance_settings(
            self.device(uid).info.index, changes, expected
        )

    async def write_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None,
        *,
        ticket: LockTicket,
    ) -> dict[str, int]:
        self.check_subscription(uid, ticket)
        async with self.operation_scope(self._operation):
            return await self._write_settings(uid, changes, expected)

    async def save_settings(
        self,
        uid: UUID,
        changes: dict[str, int],
        expected: dict[str, int | None] | None,
        *,
        ticket: LockTicket,
    ) -> InstanceSettings:
        await self.write_settings(uid, changes, expected, ticket=ticket)
        baselines = dict(self.config.info.baselines)
        baselines[str(uid)] = {**baselines.get(str(uid), {}), **changes}
        self.config.info.baselines = baselines
        await self.config.commit()
        return await self.read_settings(uid)

    async def capture_baseline(
        self, uid: UUID, *, ticket: LockTicket
    ) -> dict[str, int]:
        self.check_subscription(uid, ticket)
        settings = await self.read_settings(uid)
        baseline = {
            key: field.value
            for key, field in settings.fields.items()
            if field.state == "saved" and field.value is not None
        }
        self.config.info.baselines = {**self.config.info.baselines, str(uid): baseline}
        await self.config.commit()
        return baseline

    async def sweep_backups(self) -> None:
        for device in list(self.config.devices.values()):
            if device.session.backup and not device.info.tombstone:
                try:
                    await self._acquire_resource(device.uid)
                except DeviceSubscribeError:
                    continue
                try:
                    await self._on_device_unsubscribed(device.uid, phase="startup")
                except Exception as error:
                    logger.warning(f"启动恢复失败，留待下次尝试：{error}")

    async def startup(self) -> None:
        await self.sweep_backups()

    async def teardown(self):
        if self._backend is not None:
            tasks = [
                r.task
                for r in self._backend._audio_mute_states.values()
                if r.task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def set_block_ad(self, enabled):
        from .master_mode import apply_host_mode

        await asyncio.to_thread(apply_host_mode, enabled)


async def search() -> list[MuMuGame]:
    return [
        MuMuGame.build(
            uid=uuid5(NAMESPACE_URL, str(Path(path).resolve()).casefold()),
            info={"name": "MuMu", "path": path},
        )
        for path in await asyncio.to_thread(search_installations)
    ]
