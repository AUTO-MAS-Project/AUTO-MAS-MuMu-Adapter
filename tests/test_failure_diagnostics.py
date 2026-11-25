"""验证会进任务异常的 MuMu 脱敏诊断。"""

import asyncio
from pathlib import Path

import pytest
from auto_mas_core.devices import DeviceStatus, EmulatorRuntimePolicy
from auto_mas_core.utils import ProcessResult, ProcessRunner

from mumu_adapter import applaunch
from mumu_adapter.backend import MuMu6Manager
from mumu_adapter.installation import InstallationConfig, InstallationInfo


def make_backend(tmp_path: Path) -> MuMu6Manager:
    manager = tmp_path / "MuMuManager.exe"
    manager.touch()
    config = InstallationConfig(InstallationInfo(Path=manager, Type="mumu"))
    policy = EmulatorRuntimePolicy(
        silence_enabled=lambda: False,
        block_ads_enabled=lambda: False,
        resource_path=tmp_path,
    )
    return MuMu6Manager(config, runtime_policy=policy)


def test_settings_failure_reports_codes_without_payload(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)
    private = "private-instance-name"
    commands = []

    async def run_process(program, *args, **kwargs):
        commands.append(args[0])
        if args[0] == "version":
            return ProcessResult('{"version":"6.8.8.0"}', "", 0)
        return ProcessResult(
            '{"errcode":-12,"name":"' + private + '"}', "D:/Private/mumu", 4294967284
        )

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    for _ in range(2):
        with pytest.raises(RuntimeError) as error:
            asyncio.run(backend._run("setting", "-v", "1", "-aw"))
        detail = str(error.value)
        assert "stage=read-settings" in detail
        assert "mumu=6.8.8.0" in detail
        assert "returncode=4294967284" in detail
        assert "errcode=-12" in detail
        assert private not in detail
        assert "D:/Private" not in detail
    assert commands == ["setting", "version", "setting"]


@pytest.mark.parametrize("operation", ("close", "show"))
def test_failed_status_probe_is_not_reported_as_success(
    tmp_path, monkeypatch, operation
):
    backend = make_backend(tmp_path)
    commands = []

    async def run_process(program, *args, **kwargs):
        commands.append(args[0])
        if args[0] == "version":
            return ProcessResult('{"version":"6.8.8.0"}', "", 0)
        return ProcessResult("invalid JSON", "", 0)

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    call = backend.close("1") if operation == "close" else backend.setVisible("1", True)
    with pytest.raises(RuntimeError, match=r"status=UNKNOWN, probe=invalid-json"):
        asyncio.run(call)
    assert commands == ["info", "version"]


def test_close_already_offline_stays_noop(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)

    async def run_process(program, *args, **kwargs):
        assert args == ("info", "-v", "1")
        return ProcessResult(
            '{"index":"1","name":"test","is_process_started":false}', "", 0
        )

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    assert asyncio.run(backend.close("1")) == DeviceStatus.OFFLINE


def test_app_failure_summary_keeps_adb_address_local():
    adb_address = "127.0.0.1:16416"

    async def offline(*args, **kwargs):
        return 1, f"adb.exe: device '{adb_address}' not found"

    result = asyncio.run(
        applaunch.ensure_app_running(
            offline, "com.example.game", boot_timeout=0, launch_timeout=0
        )
    )
    assert result.reason == "boot-timeout"
    assert adb_address in result.detail
    assert "adb=connection-lost" in result.diagnostics
    assert adb_address not in result.diagnostics
