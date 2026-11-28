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
    asyncio.run(backend.probe_cli_version())
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
    assert commands == ["version", "setting", "setting"]


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
    assert commands == ["info"]


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


def test_optional_window_hide_during_boot_preserves_probe_fallback(
    tmp_path, monkeypatch
):
    backend = make_backend(tmp_path)

    async def run_process(program, *args, **kwargs):
        assert args == ("info", "-v", "1")
        return ProcessResult("invalid JSON", "", 0)

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    assert (
        asyncio.run(backend.setVisible("1", False, require_known_status=False))
        == DeviceStatus.UNKNOWN
    )


def test_outer_timeout_preserves_interrupt_stage(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)

    async def run_process(program, *args, **kwargs):
        assert args == ("info", "-v", "1")
        await asyncio.Event().wait()

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)

    async def close():
        await asyncio.wait_for(backend.close("1"), timeout=0.03)

    with pytest.raises(TimeoutError) as error:
        asyncio.run(close())
    assert isinstance(error.value.__cause__, asyncio.CancelledError)
    assert "stage=read-info" in str(error.value.__cause__)


def test_app_install_error_summary_uses_known_codes():
    assert (
        applaunch.adb_output_kind(
            1,
            "adb: failed to install C:/private.apk: Failure [INSTALL_FAILED_VERSION_DOWNGRADE]",
        )
        == "INSTALL_FAILED_VERSION_DOWNGRADE"
    )
    assert (
        applaunch.adb_output_kind(
            1, "adb: failed to install C:/INSTALL_FAILED_PRIVATE.apk"
        )
        == "adb-error"
    )


def test_android_not_ready_is_distinct_from_adb_failure():
    async def booting(*args, **kwargs):
        return 0, "0"

    result = asyncio.run(
        applaunch.ensure_app_running(
            booting, "com.example.game", boot_timeout=0, launch_timeout=0
        )
    )
    assert result.reason == "boot-timeout"
    assert "adb=android-not-ready, returncode=0" in result.diagnostics


def test_diagnostics_probe_failure_preserves_command_error(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)

    async def run_process(program, *args, **kwargs):
        if args[0] == "version":
            raise RuntimeError("desktop process unavailable")
        return ProcessResult('{"errcode":-12}', "", 4294967284)

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    asyncio.run(backend.probe_cli_version())
    with pytest.raises(RuntimeError, match="errcode=-12"):
        asyncio.run(backend._run("setting", "-v", "1", "-aw"))


def test_known_cli_error_does_not_wait_for_diagnostics(tmp_path, monkeypatch):
    backend = make_backend(tmp_path)

    async def check():
        pending_probe = asyncio.Event()

        async def run_process(program, *args, **kwargs):
            if args[0] == "version":
                pending_probe.set()
                await asyncio.Event().wait()
            return ProcessResult('{"errcode":-12}', "", 4294967284)

        monkeypatch.setattr(ProcessRunner, "run_process", run_process)
        probe = asyncio.create_task(backend.probe_cli_version())
        try:
            await asyncio.wait_for(pending_probe.wait(), timeout=2)
            with pytest.raises(RuntimeError, match=r"mumu=unknown.*errcode=-12"):
                await asyncio.wait_for(
                    backend._run("setting", "-v", "1", "-aw"), timeout=1
                )
        finally:
            probe.cancel()
            with pytest.raises(asyncio.CancelledError):
                await probe

    asyncio.run(check())


def test_transient_desktop_transport_failure_allows_next_status_probe(
    tmp_path, monkeypatch
):
    backend = make_backend(tmp_path)
    attempts = 0

    async def run_process(program, *args, **kwargs):
        nonlocal attempts
        assert args == ("info", "-v", "1")
        attempts += 1
        if attempts == 1:
            raise RuntimeError("desktop worker did not start process")
        return ProcessResult(
            '{"index":"1","name":"test","is_process_started":false}', "", 0
        )

    monkeypatch.setattr(ProcessRunner, "run_process", run_process)
    assert asyncio.run(backend.getStatus("1")) == DeviceStatus.ERROR
    assert asyncio.run(backend.getStatus("1")) == DeviceStatus.OFFLINE


#: adb server 自己起不来时的真实输出（1999 那套 MuMu 的 adb.exe 被打上低完整性标签后
#: 写不了临时目录里的 adb.log, server 就一直起不来）。这不是「连接过期」, 重连救不回来。
_DAEMON_FAILED_OUTPUT = (
    "adb F 11-28 13:47:43 31824 5020 main.cpp:52] "
    "cannot open D:\\aWa\\AUTO-MAS\\temp\\adb.log: Permission denied\n"
    "could not read ok from ADB Server\n"
    "* failed to start daemon\n"
    "error: cannot connect to daemon"
)


@pytest.mark.parametrize(
    "output",
    (
        _DAEMON_FAILED_OUTPUT,
        "could not read ok from ADB Server",
        "* failed to start daemon",
        "error: cannot connect to daemon",
    ),
)
def test_daemon_start_failure_is_not_a_stale_connection(output):
    assert applaunch.adb_server_failed(output)
    assert not applaunch.is_stale_connection(output)
    assert applaunch.adb_output_kind(1, output) == "adb-error"


def test_daemon_start_failure_hint_points_at_adb_itself():
    hint = applaunch.describe_adb_failure(_DAEMON_FAILED_OUTPUT)
    assert "adb 自己没能启动" in hint
    assert "顶掉了" not in hint


def test_stale_connection_keeps_the_other_emulator_hint():
    hint = applaunch.describe_adb_failure("error: device offline")
    assert "5037 的 server" in hint
    assert "adb 自己没能启动" not in hint
