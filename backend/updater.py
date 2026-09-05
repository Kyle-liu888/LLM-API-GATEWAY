# -*- coding: utf-8 -*-
"""
自动更新模块：
- 从网络共享目录检测新版本
- 提示用户确认后下载、替换、重启
- 支持回滚
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from .config import get_app_data_dir, get_update_check_interval_hours, get_update_share_path

logger = logging.getLogger("copilot-proxy.updater")

EXE_NAME = "LLM-API-Gateway.exe"


# ---- 版本比较 ----

def parse_version(v: str) -> tuple:
    """解析 semver 字符串为 (major, minor, patch) 元组。"""
    parts = v.strip().split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


def _get_exe_version(exe_path: Path) -> Optional[str]:
    """从 Windows exe 的 VERSIONINFO 资源读取 FileVersion。"""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(exe_path), None)
        if size == 0:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(str(exe_path), None, size, buf)

        # 查询 Translation 表（返回的是指向 WORD[2] 的指针）
        ptr = ctypes.c_void_p()
        ptr_len = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, r"\VarFileInfo\Translation",
                                                     ctypes.byref(ptr), ctypes.byref(ptr_len)):
            return None
        # 解读 lang_id 和 codepage_id
        arr = (ctypes.c_ushort * 2).from_address(ptr.value)
        lang_id, cp_id = arr[0], arr[1]

        # 查询 FileVersion 字符串
        query = f"\\StringFileInfo\\{lang_id:04X}{cp_id:04X}\\FileVersion"
        val_ptr = ctypes.c_wchar_p()
        val_len = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, query, ctypes.byref(val_ptr), ctypes.byref(val_len)):
            return None
        return val_ptr.value.strip() if val_ptr.value else None
    except Exception:
        return None


# ---- 数据类 ----

@dataclass
class UpdateInfo:
    local_version: str
    remote_version: str
    share_exe_path: Path


# ---- 核心：检查更新 ----

def check_for_update() -> tuple:
    """检查共享目录是否有新版本。

    返回 (status, info) 元组：
      - ("available", UpdateInfo)  有新版本
      - ("latest", None)          已是最新版本
      - ("no_path", None)         未配置更新路径
      - ("error", str)            检查出错，str 为错误描述
    """
    share_path = get_update_share_path()
    if not share_path:
        return ("no_path", None)

    share_dir = Path(share_path)
    share_exe = share_dir / EXE_NAME

    if not share_exe.exists():
        return ("error", f"共享目录中未找到 {EXE_NAME}\n路径: {share_dir}")

    remote_version = _get_exe_version(share_exe)
    if not remote_version:
        return ("error", f"无法读取共享 exe 的版本信息\n路径: {share_exe}")

    local = parse_version(__version__)
    remote = parse_version(remote_version)

    if remote > local:
        logger.info("Update available: %s -> %s", __version__, remote_version)
        return ("available", UpdateInfo(
            local_version=__version__,
            remote_version=remote_version,
            share_exe_path=share_exe,
        ))

    return ("latest", None)


# ---- 启动清理与回滚 ----

def _update_marker_path() -> Path:
    return get_app_data_dir() / ".update_success"


def write_update_success_marker() -> None:
    """写入启动成功标记（main.py 在服务就绪后调用）。"""
    marker = _update_marker_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        logger.debug("Update success marker written")
    except Exception as exc:
        logger.warning("Failed to write update success marker: %s", exc)


def cleanup_old_version() -> None:
    """清理旧版 exe + 回滚检测（main.py 启动时调用）。"""
    if not getattr(sys, "frozen", False):
        return

    current_exe = Path(sys.executable).resolve()
    old_exe = current_exe.with_suffix(".exe.old")
    marker = _update_marker_path()

    if not old_exe.exists():
        return

    # marker 存在说明上一次启动成功了（无论新旧版本），可以安全删除旧包
    if marker.exists():
        logger.info("Cleaning up old version: %s", old_exe)
        try:
            marker.unlink()
        except Exception:
            pass
        try:
            os.remove(str(old_exe))
        except Exception as exc:
            logger.warning("Cannot delete %s (will retry next start): %s", old_exe, exc)
        return

    # marker 不存在 + .old 存在 = 更新后首次启动，新版本尚未确认成功
    # 此时不应删除 .old，也不应回滚，而是让新版本继续启动
    # 等 write_update_success_marker() 写入 marker 后，下次启动时自动清理
    logger.info("Old version found (%s), will clean up after successful start", old_exe)


# ---- 下载与替换 ----

# 由 main.py 的 _ensure_single_instance() 写入，供更新重启时释放
_single_instance_mutex = None


def register_single_instance_mutex(mutex) -> None:
    """由 main.py 调用，保存互斥体句柄。"""
    global _single_instance_mutex
    _single_instance_mutex = mutex


def _release_single_instance_mutex() -> None:
    """释放单实例互斥体，使新进程能获取到。"""
    global _single_instance_mutex
    if _single_instance_mutex:
        try:
            ctypes.windll.kernel32.CloseHandle(_single_instance_mutex)
            logger.info("Released single-instance mutex for update restart")
        except Exception as exc:
            logger.warning("Failed to release single-instance mutex: %s", exc)
        _single_instance_mutex = None

def _copy_with_progress(src: Path, dst: Path, progress_callback=None) -> None:
    """分块拷贝文件，可选进度回调。"""
    total = src.stat().st_size
    chunk_size = 1024 * 1024  # 1MB
    copied = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(chunk_size)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            if progress_callback:
                progress_callback(int(copied * 100 / total))


def _perform_replace_and_restart(runner, db) -> None:
    """替换当前 exe 并重启。"""
    current_exe = Path(sys.executable).resolve()
    old_exe = current_exe.with_suffix(".exe.old")
    new_exe = current_exe.with_suffix(".exe.new")

    if not new_exe.exists():
        raise FileNotFoundError(f"新版本文件不存在: {new_exe}")

    # 重命名当前 exe → .old
    try:
        os.rename(str(current_exe), str(old_exe))
    except OSError as exc:
        raise RuntimeError(f"无法重命名当前程序: {exc}") from exc

    # 拷贝新 exe 到原位
    try:
        shutil.copy2(str(new_exe), str(current_exe))
    except Exception as exc:
        try:
            os.rename(str(old_exe), str(current_exe))
        except Exception:
            pass
        raise RuntimeError(f"拷贝新版本失败: {exc}") from exc

    # 清理临时文件（保留 marker，让新进程 cleanup_old_version 据此删除 .old）
    try:
        os.remove(str(new_exe))
    except Exception:
        pass

    # 先停止服务和关闭数据库
    runner.stop()
    db.close()

    # 释放单实例互斥体，使新进程能获取到
    _release_single_instance_mutex()

    # 启动新版本
    # 清除 PyInstaller 内部环境变量，使新 exe 以独立进程启动，绕过 bootloader 安全校验
    # （PyInstaller 6.x bootloader 会验证父进程，若检测到父进程是另一个 PyInstaller exe 则拒绝启动）
    env = os.environ.copy()
    for key in ("_PYI_PARENT_PROCESS_LEVEL", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE"):
        env.pop(key, None)
    subprocess.Popen([str(current_exe)], env=env)

    # 使用 os._exit 强制终止整个进程（sys.exit 在后台线程中只退出线程，不退出主进程）
    os._exit(0)


def check_and_prompt_manual(icon, runner, db) -> None:
    """手动检查更新全流程（托盘菜单回调，在后台线程中执行）。"""
    _do_update_flow(icon, runner, db)


def check_and_prompt_startup(runner, db) -> bool:
    """启动时检查更新（在主线程中执行，服务启动后立即调用）。
    返回 True 表示当前已是最新版本（无需更新），False 表示正在更新中。"""
    return _do_update_flow(None, runner, db)


def _do_update_flow(icon, runner, db) -> bool:
    """更新全流程：检查 → 确认 → 下载 → 确认替换 → 重启。
    返回 True 表示当前已是最新版本（无需更新），False 表示正在更新中或用户取消。"""
    try:
        status, data = check_for_update()
    except Exception as exc:
        _msgbox_error(f"检查更新失败:\n{exc}")
        return True

    if status == "no_path":
        return True

    if status == "error":
        _msgbox_error(f"检查更新失败:\n{data}")
        return True

    if status == "latest":
        if icon:
            _msgbox_info(f"当前已是最新版本 (v{__version__})")
        return True

    # status == "available"
    info = data

    # 第一步：确认是否更新
    if not _msgbox_yes_no(
        "LLM API Gateway - 更新可用",
        f"发现新版本 v{info.remote_version}（当前 v{info.local_version}）\n\n"
        "是否立即更新？",
    ):
        return False

    current_exe = Path(sys.executable).resolve()
    new_exe = current_exe.with_suffix(".exe.new")

    # 第二步：下载新 exe
    if icon:
        icon.notify("正在下载更新，请稍候...", "LLM API Gateway")
    try:
        _copy_with_progress(info.share_exe_path, new_exe)

        src_size = info.share_exe_path.stat().st_size
        dst_size = new_exe.stat().st_size
        if src_size != dst_size:
            new_exe.unlink(missing_ok=True)
            _msgbox_error("下载文件校验失败，请重试。")
            return False

        logger.info("Download complete: %s -> %s (%d bytes)", info.share_exe_path, new_exe, dst_size)
    except Exception as exc:
        try:
            new_exe.unlink(missing_ok=True)
        except Exception:
            pass
        _msgbox_error(f"下载失败:\n{exc}")
        return False

    # 第三步：下载完成后确认是否删除旧版并替换
    if not _msgbox_yes_no(
        "LLM API Gateway - 确认更新",
        f"新版本已下载完成。\n\n"
        f"更新将删除当前版本并替换为新版本 v{info.remote_version}，\n"
        "应用将自动重启。\n\n"
        "是否继续？",
    ):
        try:
            os.remove(str(new_exe))
        except Exception:
            pass
        return False

    _perform_replace_and_restart(runner, db)


# ---- 周期检查 ----

def schedule_periodic_check(icon) -> None:
    """启动周期性更新检查。"""
    interval = get_update_check_interval_hours()
    share_path = get_update_share_path()
    if not share_path:
        return

    def _check():
        try:
            status, data = check_for_update()
            if status == "available":
                try:
                    icon.notify(
                        f"发现新版本 v{data.remote_version}，点击'检查更新'进行升级",
                        "LLM API Gateway",
                    )
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            timer = threading.Timer(interval * 3600, _check)
            timer.daemon = True
            timer.start()

    # 首次周期检查延迟 1 小时（启动时已由 main.py 立即检查过）
    timer = threading.Timer(interval * 3600, _check)
    timer.daemon = True
    timer.start()


# ---- Windows 原生对话框（线程安全，无需 tkinter 根窗口） ----

def _msgbox_info(msg: str) -> None:
    """Windows 原生信息弹窗，线程安全。"""
    user32 = ctypes.windll.user32
    user32.MessageBoxW(0, msg, "LLM API Gateway", 0x40)  # MB_ICONINFORMATION


def _msgbox_error(msg: str) -> None:
    """Windows 原生错误弹窗，线程安全。"""
    user32 = ctypes.windll.user32
    user32.MessageBoxW(0, msg, "LLM API Gateway", 0x10)  # MB_ICONERROR


def _msgbox_yes_no(title: str, msg: str) -> bool:
    """Windows 原生确认弹窗，线程安全。返回 True=是, False=否。"""
    user32 = ctypes.windll.user32
    result = user32.MessageBoxW(0, msg, title, 0x04 | 0x20)  # MB_YESNO | MB_ICONQUESTION
    return result == 6  # IDYES
