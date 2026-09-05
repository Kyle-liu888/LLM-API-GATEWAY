# -*- coding: utf-8 -*-
"""
系统托盘模块：
- 系统托盘图标 + 右键菜单
- 后台运行 FastAPI 服务
"""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from typing import Optional

from . import __version__
from .config import get_local_port, get_log_file_path
from .db import Database
from .server import ServerRunner
from .updater import check_and_prompt_manual, schedule_periodic_check

logger = logging.getLogger("copilot-proxy.tray")


def _create_icon_image():
    """创建托盘图标（简单的圆形图标）。"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), color=(30, 120, 200))
        draw = ImageDraw.Draw(img)
        draw.ellipse([8, 8, 56, 56], fill=(255, 255, 255))
        draw.ellipse([18, 18, 46, 46], fill=(30, 120, 200))
        return img
    except ImportError:
        logger.warning("Pillow not available, using default icon")
        return None


def run_tray(runner: ServerRunner, db: Database) -> None:
    """启动系统托盘应用。"""
    import pystray

    port = get_local_port()
    base_url = f"http://127.0.0.1:{port}"
    admin_url = f"{base_url}/admin/"

    def on_open_admin(icon, item):
        """打开管理者界面。"""
        webbrowser.open(admin_url)

    def on_open_log(icon, item):
        """打开日志文件。"""
        log_path = get_log_file_path()
        if log_path.exists():
            os.startfile(str(log_path))
        else:
            logger.warning("Log file not found: %s", log_path)

    def on_quit(icon, item):
        """关闭服务并退出。"""
        logger.info("Quitting from tray...")
        runner.stop()
        icon.stop()

    def on_status(icon, item):
        """显示当前状态。"""
        icon.notify(f"Gateway running at {base_url}", "LLM API Gateway")

    def on_check_update(icon, item):
        """检查更新。"""
        threading.Thread(
            target=check_and_prompt_manual,
            args=(icon, runner, db),
            daemon=True,
        ).start()

    # 先启动服务
    runner.start()

    # 创建托盘图标
    image = _create_icon_image()

    menu = pystray.Menu(
        pystray.MenuItem("管理者界面", on_open_admin, default=True),
        pystray.MenuItem("打开日志", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"运行中: {base_url}", on_status, enabled=False),
        pystray.MenuItem(f"版本: v{__version__}", lambda icon, item: None, enabled=False),
        pystray.MenuItem("检查更新...", on_check_update),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("关闭服务", on_quit),
    )

    icon = pystray.Icon(
        name="LLM API Gateway",
        icon=image,
        title=f"LLM API Gateway v{__version__} - {base_url}",
        menu=menu,
    )

    logger.info("System tray icon started. Admin UI: %s", admin_url)

    # 启动周期性更新检查
    schedule_periodic_check(icon)

    # pystray.icon.run() 会阻塞当前线程
    icon.run()

    # 退出后确保服务停止
    runner.stop()
    db.close()
