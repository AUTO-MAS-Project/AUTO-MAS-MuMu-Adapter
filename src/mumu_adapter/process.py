"""实例音频生命周期与宿主窗口操作。"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from auto_mas_core.utils import IS_WINDOWS, get_logger
from auto_mas_core.utils import window as platform_window

logger = get_logger("模拟器运行")


@dataclass
class AudioMuteRecord:
    """一次启动的模拟器音频静音记录。

    ``states`` 只含已找到音频会话的 pid（记录到它们静音前的状态）；音频会话是
    进程首次发声时才建立的，尚未出现的 pid 由 ``task`` 指向的后台任务继续等待。
    """

    pids: Set[int]
    states: Dict[int, bool] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    timeout: float = 600.0
    poll_interval: float = 5.0

    def pending(self) -> Set[int]:
        return self.pids - self.states.keys()


async def _watch_and_mute(record: AudioMuteRecord) -> None:
    """轮询等待目标进程的音频会话出现，出现即静音并记录先前状态。

    游戏引擎往往在启动完成后数分钟才首次发声，音频会话届时才建立；按
    ``poll_interval`` 重试到 ``timeout`` 为止，超时放弃（说明那条进程没在出声）。
    被取消时先让进行中的一轮把结果落进记录再退出，保证这一轮刚静音上的会话不漏还原。
    """
    try:
        from app.utils import audio
    except ImportError:
        logger.warning("音频静音组件不可用（缺少 pycaw），跳过静音")
        return

    deadline = time.monotonic() + record.timeout
    while time.monotonic() < deadline:
        mute_task = asyncio.create_task(audio.mute_once(record.pending()))
        try:
            states = await asyncio.shield(mute_task)
        except asyncio.CancelledError:
            # 取消落在枚举进行中：线程无法中断，等这一轮跑完并把结果落进记录再传播，
            # 否则它刚静音上的会话会因结果被丢弃而漏还原
            with suppress(Exception):
                record.states.update(await mute_task)
            raise
        if states:
            record.states.update(states)
            logger.info(f"已静音音频会话: {sorted(states)}")
            if not record.pending():
                return
        await asyncio.sleep(record.poll_interval)
    logger.warning(f"等待音频会话超时，未能静音: {sorted(record.pending())}")


async def apply_launch_audio_mute(
    states_store: Dict[str, AudioMuteRecord],
    idx: str,
    resolve_pids: Callable[[], Awaitable[List[int]]],
    *,
    silence_enabled: Callable[[], bool],
) -> None:
    """静默模式下静音全新启动实例的声音，把记录存入 ``states_store``。

    只对实例由本次 ``open()`` 全新拉起的情况调用——已在线早退的实例可能是用户手动
    开着的，不能动。PID 查询完成后，音频会话由后台任务跟随（见
    :class:`AudioMuteRecord`），启动流程不等待会话建立。PID 查询只在开启静默时
    执行，查询与静音准备的任何失败只记日志，不影响启动。
    """
    if not IS_WINDOWS:
        return
    try:
        if not silence_enabled():
            return
        pids = await resolve_pids()
        if not pids:
            return
        old = states_store.get(idx)
        if old is not None and old.task is not None and not old.task.done():
            old.task.cancel()
        record = AudioMuteRecord(pids=set(pids))
        record.task = asyncio.create_task(_watch_and_mute(record))
        states_store[idx] = record
        logger.debug(f"开始跟随静音模拟器 {idx} 的音频进程: {sorted(record.pids)}")
    except Exception:  # noqa: BLE001 - 音频辅助步骤失败不应阻断模拟器启动
        logger.opt(exception=True).warning(f"模拟器 {idx} 音频静音失败，将继续运行")


async def restore_audio_before_close(
    states_store: Dict[str, AudioMuteRecord], idx: str
) -> None:
    """关闭实例前还原 ``apply_launch_audio_mute`` 的静音状态并停掉跟随任务。

    实例已被外部关掉时音频会话随之销毁，等价跳过；极端时序下飞行中的一轮静音可能
    落在还原之后，此时实例已在关闭流程里，会话随进程销毁。失败只记日志，不阻断
    关闭流程。
    """
    if not IS_WINDOWS:
        return
    record = states_store.pop(idx, None)
    if record is None:
        return
    if record.task is not None and not record.task.done():
        record.task.cancel()
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 跟随任务的意外异常不应阻断关闭流程
            logger.opt(exception=True).warning("音频静音跟随任务异常退出")
    if not record.states:
        return
    try:
        from app.utils import audio
    except ImportError:
        logger.warning("音频静音组件不可用（缺少 pycaw），跳过还原")
        return
    restored = await audio.restore_processes(record.states)
    if restored:
        logger.info(f"已还原模拟器 {idx} 的音频静音状态: {sorted(restored)}")
    else:
        logger.debug(f"模拟器 {idx} 的音频进程已退出，无需还原静音状态")


_WINDOW_RESOLVE_TIMEOUT = 1.0
"""解析实例主窗口句柄的上限（秒）：实例刚启动时窗口可能还没建出来"""

_WINDOW_POLL_INTERVAL = 0.5
"""窗口句柄重读与可见性轮询的间隔（秒）"""


async def resolve_main_window(
    idx: str, resolve: Callable[[], Awaitable[int | None]]
) -> int:
    """在 :data:`_WINDOW_RESOLVE_TIMEOUT` 内反复取该实例的主窗口句柄，取不到报错。

    ``resolve`` 每次给出一个候选句柄（拿不到返回 None），窗口可能等实例起来才
    建出来，所以按 :data:`_WINDOW_POLL_INTERVAL` 重试到超时为止；解析不到就明确
    报错，不空转到 ``MaxWaitTime``（#948：把 PID 当句柄传的判据恒为假，只会白等到超时）。
    """

    deadline = time.monotonic() + _WINDOW_RESOLVE_TIMEOUT
    while True:
        hwnd = await resolve()
        if hwnd:
            return hwnd
        if time.monotonic() >= deadline:
            raise RuntimeError(f"未找到设备{idx}的主窗口，无法切换窗口可见性")
        await asyncio.sleep(_WINDOW_POLL_INTERVAL)


async def apply_window_visibility(
    hwnd: int, idx: str, is_visible: bool, max_wait: float
) -> None:
    """按 ``max_wait`` 轮询切换 ``hwnd`` 的可见性，超时抛 ``RuntimeError``。

    老板键不带实例信息，多开时会把别的实例一起翻过去，所以直接对该实例自己的窗口
    句柄调 ``ShowWindow``；单次切换失败只记日志，接着重试。
    """

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if platform_window.is_visible(hwnd) == is_visible:
            return
        try:
            if is_visible:
                platform_window.show_window(hwnd)
            else:
                platform_window.hide_window(hwnd)
        except Exception as e:
            logger.error(f"切换设备{idx}窗口可见性失败: {e}")
        await asyncio.sleep(_WINDOW_POLL_INTERVAL)

    raise RuntimeError(f"{'显示' if is_visible else '隐藏'}设备{idx}窗口超时")
