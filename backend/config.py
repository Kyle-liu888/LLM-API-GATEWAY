# -*- coding: utf-8 -*-
"""
配置层：
- 从 .env 文件读取基础配置（无需管理员动态管理）
- 鉴权类型常量
- 日志初始化
- Header 安全化等通用工具
"""

from __future__ import annotations

import getpass
import json
import logging
import logging.handlers
import os
import sys
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

# ---- 鉴权类型常量 ----
AUTH_TYPE_DYNAMIC_TOKEN = "dynamic_token"  # 动态 IAM Token（默认模式，从固定 IAM 接口获取）
AUTH_TYPE_BASIC = "basic"                  # HTTP Basic 认证
AUTH_TYPE_BEARER = "bearer"                # Bearer Token 认证
AUTH_TYPE_RAW = "raw"                      # 原始 Authorization 头（直接写入）
AUTH_TYPE_NONE = "none"                    # 无鉴权

ALL_AUTH_TYPES = [
    AUTH_TYPE_DYNAMIC_TOKEN,
    AUTH_TYPE_BASIC,
    AUTH_TYPE_BEARER,
    AUTH_TYPE_RAW,
    AUTH_TYPE_NONE,
]

# ---- 提供商常量 ----
PROVIDER_COPILOT = "copilot"
PROVIDER_ZJ = "zj"

# 模型 → 提供商映射，同时派生 SUPPORTED_MODELS
MODEL_PROVIDER_MAP: Dict[str, str] = {
    "Qwen3.6-27B-VL": PROVIDER_COPILOT,
    "MiniMax-M2.7":    PROVIDER_COPILOT,
    "Glm-5.1":         PROVIDER_COPILOT,
    "GLM-V5":          PROVIDER_ZJ,
}

# 代理支持转发的上游模型列表，同时用于 /v1/models 接口返回
SUPPORTED_MODELS = list(MODEL_PROVIDER_MAP.keys())

APP_NAME = "CopilotGateway"  # AppData 子目录名，也是日志子目录名

def get_seed_accounts() -> list[tuple[str, str]]:
    """从本地环境读取账号种子；仓库不包含实际账号。"""
    accounts = json.loads(os.getenv("SEED_ACCOUNTS_JSON", "[]"))
    if not isinstance(accounts, list) or any(
        not isinstance(item, list) or len(item) != 2
        or not all(isinstance(value, str) and value.strip() for value in item)
        for item in accounts
    ):
        raise ValueError("SEED_ACCOUNTS_JSON must be an array of [account_id, display_name] pairs")
    return [(item[0], item[1]) for item in accounts]

logger = logging.getLogger("copilot-proxy")


def _find_env_file() -> Path:
    """查找 .env 文件：优先 exe 同级目录（用户可覆盖），其次内嵌默认值，再次包目录。"""
    if getattr(sys, "frozen", False):
        # 1) exe 同级目录（用户自定义覆盖）
        user_env = Path(sys.executable).resolve().parent / ".env"
        if user_env.exists():
            return user_env
        # 2) PyInstaller 内嵌的默认 .env
        return Path(sys._MEIPASS) / ".env"
    else:
        # 源码运行时，.env 在项目根目录
        base = Path(__file__).resolve().parents[1]
    return base / ".env"


def load_env() -> None:
    """加载 .env 文件到环境变量。exe同级目录优先，内嵌默认值兜底。"""
    from dotenv import load_dotenv
    env_path = _find_env_file()
    if env_path.exists():
        load_dotenv(env_path, override=True)
        logger.info("Loaded .env from %s", env_path)
    else:
        logger.warning(".env file not found at %s, using environment variables", env_path)


# ---- .env 配置读取 ----

def ensure_odbc_driver() -> bool:
    """确保 ODBC 驱动已安装，缺失则尝试静默安装。返回 True 表示可用。"""
    try:
        import pyodbc
        drivers = [d.lower() for d in pyodbc.drivers()]
        if any("sql server" in d and "odbc driver" in d for d in drivers):
            return True  # 已安装
    except Exception:
        pass

    # 查找内嵌的 MSI
    if getattr(sys, "frozen", False):
        msi_dir = Path(sys._MEIPASS) / "driver"
    else:
        msi_dir = Path(__file__).resolve().parents[1] / "driver"

    # 找到 MSI 文件（17 或 18 均可）
    msi_files = sorted(msi_dir.glob("msodbcsql*.msi"), reverse=True)
    if not msi_files:
        logger.warning("No ODBC driver MSI found in %s", msi_dir)
        return False

    msi_path = msi_files[0]
    logger.info("Installing ODBC driver from %s ...", msi_path.name)
    import subprocess
    try:
        result = subprocess.run(
            ["msiexec", "/i", str(msi_path), "/quiet", "/norestart",
             "IACCEPTMSODBCSQLLICENSETERMS=YES"],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("ODBC driver installed successfully")
            return True
        logger.error("ODBC driver install failed: exit code %d", result.returncode)
    except Exception as exc:
        logger.error("ODBC driver install failed: %s", exc)
    return False


def _detect_odbc_driver() -> str:
    """自动检测本机可用的 SQL Server ODBC 驱动名称。"""
    try:
        import pyodbc
        available = [d.lower() for d in pyodbc.drivers()]
        # 优先 18，其次 17，再次任意含 "sql server" 的
        for name, key in [("ODBC Driver 18 for SQL Server", "odbc driver 18 for sql server"),
                          ("ODBC Driver 17 for SQL Server", "odbc driver 17 for sql server")]:
            if key in available:
                return name
        for d in pyodbc.drivers():
            if "sql server" in d.lower() and "odbc driver" in d.lower():
                return d
    except Exception:
        pass
    return "ODBC Driver 17 for SQL Server"  # 默认回退


def get_db_connection_string() -> str:
    """组装 SQL Server 连接串，自动适配本机 ODBC 驱动版本。"""
    driver = os.getenv("DB_DRIVER") or _detect_odbc_driver()
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={os.getenv('DB_SERVER', 'localhost,1433')};"
        f"DATABASE={os.getenv('DB_DATABASE', 'gateway_dev')};"
        f"UID={os.getenv('DB_UID', 'gateway_user')};"
        f"PWD={os.getenv('DB_PWD', '')};"
        "TrustServerCertificate=yes;"
    )


def get_copilot_api_url() -> str:
    return os.getenv("COPILOT_API_URL", "")


def get_token_api_url() -> str:
    return os.getenv("TOKEN_API_URL", "")


def get_app_id() -> str:
    return os.getenv("APP_ID", "com.example.gateway")


def get_local_port() -> int:
    return int(os.getenv("LOCAL_PORT", "9899"))


def get_upstream_timeout() -> float:
    return float(os.getenv("UPSTREAM_TIMEOUT", "300"))


def get_token_cache_ttl() -> int:
    return int(os.getenv("TOKEN_CACHE_TTL", "240"))


def get_upstream_auth_type() -> str:
    return os.getenv("UPSTREAM_AUTH_TYPE", AUTH_TYPE_DYNAMIC_TOKEN)


def get_zj_api_url() -> str:
    return os.getenv("ZJ_API_URL", "")


def get_zj_api_key() -> str:
    return os.getenv("ZJ_API_KEY", "")


def get_provider_for_model(model: str) -> str:
    """根据模型名返回对应的上游提供商。"""
    return MODEL_PROVIDER_MAP.get(model, PROVIDER_COPILOT)


def get_default_api_key() -> str:
    """默认 API Key（首次启动时写入数据库的种子数据）。"""
    return os.getenv("DEFAULT_API_KEY", "")


def get_update_share_path() -> str:
    """获取更新共享目录路径，为空则禁用自动更新。"""
    return os.getenv("UPDATE_SHARE_PATH", "")


def get_update_check_interval_hours() -> int:
    return int(os.getenv("UPDATE_CHECK_INTERVAL_HOURS", "24"))


# ---- 通用工具函数 ----

def get_app_data_dir() -> Path:
    """获取可写的应用数据目录。"""
    if sys.platform == "win32":
        root = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / APP_NAME


def get_log_dir() -> Path:
    return get_app_data_dir() / "logs"


def get_log_file_path() -> Path:
    return get_log_dir() / "proxy.log"


def get_runtime_base_dir() -> Path:
    """获取程序运行目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def configure_logging(level: Optional[str] = None) -> Path:
    """初始化全局日志。"""
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    log_file = get_log_file_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(numeric_level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    logging.raiseExceptions = False

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("copilot-proxy.crash").critical(
            "Uncaught exception:\n%s", "".join(traceback.format_exception(exc_type, exc, tb))
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    logging.getLogger("copilot-proxy").info("Log file: %s", log_file)
    return log_file


def prepare_runtime_environment() -> None:
    """准备运行环境：Windows event loop 策略 + 清空代理环境变量。"""
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    os.environ["HTTP_PROXY"] = ""
    os.environ["HTTPS_PROXY"] = ""
    os.environ["ALL_PROXY"] = ""
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


def get_system_username() -> str:
    """获取当前系统用户名，用于管理者鉴权。"""
    for getter in (getpass.getuser, os.getlogin):
        try:
            value = getter()
            if value:
                return value
        except Exception:
            pass
    return os.getenv("USERNAME") or os.getenv("USER") or os.getenv("LOGNAME") or "unknown"


def get_windows_domain_account() -> str:
    """获取 Windows 域账号（用于管理员识别）。"""
    return os.getenv("USERNAME") or getpass.getuser() or "unknown"


def sanitize_header_value(value: Any, fallback: str = "unknown") -> str:
    """把任意值转换成 HTTP Header 可安全承载的字符串。"""
    if value is None:
        return fallback

    text = str(value).strip()
    if not text:
        return fallback

    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        pass

    try:
        normalized = (
            unicodedata.normalize("NFKD", text)
            .encode("ascii", errors="ignore")
            .decode("ascii")
            .strip()
        )
        if normalized:
            return normalized
    except Exception:
        pass

    replaced = "".join(ch if ord(ch) < 128 else "_" for ch in text).strip()
    return replaced or fallback
