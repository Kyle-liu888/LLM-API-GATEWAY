# -*- coding: utf-8 -*-
"""
统一入口模块。CLI 模式启动 FastAPI 服务 + 系统托盘。
"""

from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from tkinter import messagebox

from . import __version__
from .config import configure_logging, ensure_odbc_driver, get_local_port, load_env, prepare_runtime_environment
from .db import Database
from .server import ServerRunner
from .updater import check_and_prompt_startup, cleanup_old_version, register_single_instance_mutex, write_update_success_marker

logger = logging.getLogger("copilot-proxy.main")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="LLM API Gateway")
    parser.add_argument("--port", type=int, default=0, help="临时覆盖监听端口")
    parser.add_argument("--log-level", type=str, default="INFO", help="日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--no-tray", action="store_true", help="不显示系统托盘图标")
    parser.add_argument("--skip-update", action="store_true", help="跳过启动时的自动更新检查")
    return parser.parse_args()


def _ensure_single_instance() -> bool:
    """确保只有一个实例运行。返回 True 表示当前是首个实例。"""
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    # 创建命名互斥体，全局唯一
    mutex = kernel32.CreateMutexW(None, False, "Global\\LLM_API_Gateway_SingleInstance")
    last_error = kernel32.GetLastError()
    # ERROR_ALREADY_EXISTS = 183
    if last_error == 183:
        return False
    # 保存引用防止被 GC 回收，并注册到 updater 模块以便更新重启时释放
    _ensure_single_instance._mutex = mutex
    register_single_instance_mutex(mutex)
    return True


def main() -> int:
    """程序主入口。"""
    args = parse_args()
    configure_logging(args.log_level)
    prepare_runtime_environment()
    load_env()

    # 单实例检查
    if not _ensure_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("LLM API Gateway", "服务已在运行中。")
        root.destroy()
        return 0

    # 清理旧版 exe / 检测回滚
    if not args.skip_update:
        cleanup_old_version()

    # 初始化 tkinter 根窗口（隐藏，仅用于 messagebox）
    root = tk.Tk()
    root.withdraw()

    # 确保 ODBC 驱动已安装
    if not ensure_odbc_driver():
        messagebox.showerror(
            "LLM API Gateway",
            "缺少 SQL Server ODBC 驱动，且自动安装失败。\n"
            "请右键以管理员身份运行本程序。"
        )
        root.destroy()
        return 1

    # 初始化数据库
    db = Database()
    try:
        db.initialize()
    except Exception as exc:
        logger.error("Database initialization failed: %s", exc)
        logger.error("Please check .env DB_SERVER/DB_DATABASE/DB_UID/DB_PWD settings")
        messagebox.showerror("LLM API Gateway", f"启动失败:\n{exc}")
        root.destroy()
        return 1

    if args.port:
        import os
        os.environ["LOCAL_PORT"] = str(args.port)

    # 启动服务
    runner = ServerRunner(db)
    port = get_local_port()
    runner.start()

    # 等待服务就绪
    import time
    for _ in range(20):
        if runner.server is not None:
            break
        time.sleep(0.1)

    if runner.server is None:
        messagebox.showerror("LLM API Gateway", "启动失败:\n服务未能在预期时间内就绪")
        root.destroy()
        db.close()
        return 1

    # 启动成功
    write_update_success_marker()

    # 启动时立即检查更新（用户确认更新则自动替换重启，否则继续运行）
    is_latest = True
    if not args.skip_update:
        is_latest = check_and_prompt_startup(runner, db)

    if is_latest:
        messagebox.showinfo("LLM API Gateway", f"服务已启动 (v{__version__})\n管理界面: http://127.0.0.1:{port}/admin/")
    root.destroy()

    if args.no_tray:
        # 无托盘模式：阻塞运行
        try:
            runner.run_blocking()
        except KeyboardInterrupt:
            runner.stop()
    else:
        # 托盘模式：后台运行 + 系统托盘图标
        try:
            from .tray import run_tray
            run_tray(runner, db)
        except ImportError:
            logger.warning("pystray not available, running in no-tray mode")
            runner.run_blocking()

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
