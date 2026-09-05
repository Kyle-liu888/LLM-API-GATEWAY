# -*- coding: utf-8 -*-
"""
数据库层：
- SQL Server 连接管理
- Schema 建表（首次启动自动创建）
- 种子数据初始化
- 数据访问方法
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pyodbc

from .config import (
    get_seed_accounts,
    get_db_connection_string,
    get_default_api_key,
)

logger = logging.getLogger("copilot-proxy.db")


# ---- 数据行类型 ----

@dataclass
class AccountRow:
    """上游账号，合并了使用计数、健康状态和Token统计。"""
    account_id: str           # 账号标识，如 "demo_account"
    display_name: str         # 显示名，如 "演示账号 demo_account"
    is_active: bool           # 是否启用
    active_count: int         # 当前在飞请求数（负载均衡依据）
    total_requests: int       # 当月请求总数
    consecutive_failures: int # 连续失败次数（成功即归零）
    status: str               # healthy / degraded / down
    cooldown_until: Optional[str]  # 冷却截止时间，down 状态下 5 分钟
    daily_tokens: int         # 当日使用的 Token 总数
    monthly_tokens: int       # 当月使用的 Token 总数
    yearly_tokens: int        # 当年使用的 Token 总数


@dataclass
class TenantRow:
    """租户，不同团队/项目独立配额和模型权限。"""
    tenant_id: str            # 租户标识
    name: str                 # 租户名称
    quota_limit: int          # 每小时请求配额，-1=无限制
    allowed_models: List[str] # 允许的模型列表，空=全部
    is_active: bool           # 是否启用
    daily_tokens: int = 0     # 当日 Token 消耗
    monthly_tokens: int = 0   # 当月 Token 消耗
    yearly_tokens: int = 0    # 当年 Token 消耗


@dataclass
class ApiKeyRow:
    """API 密钥，每个 Key 归属一个租户。"""
    key_value: str            # 密钥值，如 "sk-gateway-xxxx"，同时也是主键
    tenant_id: str            # 所属租户
    name: str                 # 备注名，便于识别
    is_active: bool           # 是否有效


@dataclass
class AdminUserRow:
    """管理员，通过 Windows 域账号自动识别。"""
    domain_account: str       # Windows 域账号
    display_name: str         # 显示名


# ---- 数据库管理类 ----

class Database:
    """SQL Server 数据库管理器，线程安全。"""

    def __init__(self, connection_string: Optional[str] = None):
        self._connection_string = connection_string or get_db_connection_string()
        self._local = threading.local()
        self._initialized = False

    def _get_connection(self) -> pyodbc.Connection:
        """获取当前线程的数据库连接。"""
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            try:
                conn.cursor().execute("SELECT 1")
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
        conn = pyodbc.connect(self._connection_string, autocommit=False)
        self._local.connection = conn
        return conn

    @contextmanager
    def _cursor(self):
        """上下文管理器：获取 cursor，自动 commit/rollback。"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def initialize(self) -> None:
        """初始化数据库：建表 + 种子数据。幂等操作。"""
        if self._initialized:
            return
        logger.info("Initializing database...")
        self._create_tables()
        self._seed_data()
        self._initialized = True
        logger.info("Database initialized successfully.")

    def _create_tables(self) -> None:
        """创建所有表。"""
        with self._cursor() as cur:
            # accounts（合并了原 accounts + account_usage + account_health）
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'accounts')
                CREATE TABLE accounts (
                    account_id           NVARCHAR(64) PRIMARY KEY,
                    display_name         NVARCHAR(128) NOT NULL,
                    is_active            BIT NOT NULL DEFAULT 1,
                    active_count         INT NOT NULL DEFAULT 0,
                    total_requests       BIGINT NOT NULL DEFAULT 0,
                    requests_reset_date  DATETIME2 NULL,
                    consecutive_failures INT NOT NULL DEFAULT 0,
                    status               NVARCHAR(16) NOT NULL DEFAULT 'healthy',
                    cooldown_until       DATETIME2 NULL,
                    daily_tokens         BIGINT NOT NULL DEFAULT 0,
                    monthly_tokens       BIGINT NOT NULL DEFAULT 0,
                    yearly_tokens        BIGINT NOT NULL DEFAULT 0,
                    tokens_reset_date    DATETIME2 NULL
                )
            """)

            # tenants
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'tenants')
                CREATE TABLE tenants (
                    tenant_id        NVARCHAR(64) PRIMARY KEY,
                    name             NVARCHAR(128) NOT NULL,
                    quota_limit      INT NOT NULL DEFAULT -1,
                    allowed_models   NVARCHAR(MAX) NULL,
                    is_active        BIT NOT NULL DEFAULT 1,
                    daily_tokens     BIGINT NOT NULL DEFAULT 0,
                    monthly_tokens   BIGINT NOT NULL DEFAULT 0,
                    yearly_tokens    BIGINT NOT NULL DEFAULT 0,
                    tokens_reset_date DATETIME2 NULL
                )
            """)

            # 幂等 ALTER：为已有 tenants 表补充新字段
            for col, typ, default in [
                ("daily_tokens",      "BIGINT NOT NULL DEFAULT 0", None),
                ("monthly_tokens",    "BIGINT NOT NULL DEFAULT 0", None),
                ("yearly_tokens",     "BIGINT NOT NULL DEFAULT 0", None),
                ("tokens_reset_date", "DATETIME2 NULL", None),
            ]:
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                    f"WHERE TABLE_NAME='tenants' AND COLUMN_NAME='{col}') "
                    f"ALTER TABLE tenants ADD {col} {typ}"
                )

            # 幂等 ALTER：删除废弃的 provider 列（先删默认约束再删列）
            rows = cur.execute(
                "SELECT dc.name FROM sys.default_constraints dc "
                "JOIN sys.columns c ON dc.parent_column_id = c.column_id AND dc.parent_object_id = c.object_id "
                "WHERE OBJECT_NAME(dc.parent_object_id)='tenants' AND c.name='provider'"
            ).fetchall()
            for row in rows:
                cur.execute(f"ALTER TABLE tenants DROP CONSTRAINT [{row[0]}]")
            cur.execute(
                "IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='tenants' AND COLUMN_NAME='provider') "
                "ALTER TABLE tenants DROP COLUMN provider"
            )

            # api_keys（key_value 为主键，去掉 key_id/revoked_at/last_used_at）
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'api_keys')
                CREATE TABLE api_keys (
                    key_value    NVARCHAR(128) PRIMARY KEY,
                    tenant_id    NVARCHAR(64) NOT NULL,
                    name         NVARCHAR(128) NOT NULL DEFAULT '',
                    is_active    BIT NOT NULL DEFAULT 1
                )
            """)

            # request_log
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'request_log')
                CREATE TABLE request_log (
                    id         BIGINT IDENTITY(1,1) PRIMARY KEY,
                    tenant_id  NVARCHAR(64) NOT NULL,
                    model      NVARCHAR(128) NULL,
                    created_at DATETIME2 NOT NULL DEFAULT GETUTCDATE()
                )
            """)

            # 幂等 ALTER：为已有 request_log 表补充 model 列
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='request_log' AND COLUMN_NAME='model') "
                "ALTER TABLE request_log ADD model NVARCHAR(128) NULL"
            )

            # token_usage（按用户+租户维度记录每次请求的 token 消耗）
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'token_usage')
                CREATE TABLE token_usage (
                    id           BIGINT IDENTITY(1,1) PRIMARY KEY,
                    user_account NVARCHAR(128) NOT NULL,
                    tenant_id    NVARCHAR(64) NOT NULL,
                    model        NVARCHAR(128) NULL,
                    token_count  BIGINT NOT NULL DEFAULT 0,
                    created_at   DATETIME2 NOT NULL DEFAULT GETUTCDATE()
                )
            """)

            # 幂等 ALTER：已有 token_usage 表将 provider 列替换为 tenant_id
            # 先删除依赖 provider 的旧索引
            cur.execute(
                "IF EXISTS (SELECT * FROM sys.indexes WHERE name = 'ix_token_usage_account_provider') "
                "DROP INDEX ix_token_usage_account_provider ON token_usage"
            )
            # 删除 provider 列上的默认约束（如有）
            rows = cur.execute(
                "SELECT dc.name FROM sys.default_constraints dc "
                "JOIN sys.columns c ON dc.parent_column_id = c.column_id AND dc.parent_object_id = c.object_id "
                "WHERE OBJECT_NAME(dc.parent_object_id)='token_usage' AND c.name='provider'"
            ).fetchall()
            for row in rows:
                cur.execute(f"ALTER TABLE token_usage DROP CONSTRAINT [{row[0]}]")
            cur.execute(
                "IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='token_usage' AND COLUMN_NAME='provider') "
                "ALTER TABLE token_usage DROP COLUMN provider"
            )
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='token_usage' AND COLUMN_NAME='tenant_id') "
                "ALTER TABLE token_usage ADD tenant_id NVARCHAR(64) NOT NULL DEFAULT 'default'"
            )

            # token_usage 索引：按 user_account + tenant_id + model 查询日/月/年用量
            cur.execute(
                "IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'ix_token_usage_account_tenant_model') "
                "CREATE NONCLUSTERED INDEX ix_token_usage_account_tenant_model "
                "ON token_usage (user_account, tenant_id, model, created_at)"
            )

            # request_log 索引：配额查询按 tenant_id + created_at 过滤
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'ix_request_log_tenant_created')
                CREATE NONCLUSTERED INDEX ix_request_log_tenant_created
                ON request_log (tenant_id, created_at)
            """)

            # admin_users
            cur.execute("""
                IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'admin_users')
                CREATE TABLE admin_users (
                    domain_account NVARCHAR(128) PRIMARY KEY,
                    display_name   NVARCHAR(128) NOT NULL
                )
            """)

    def _seed_data(self) -> None:
        """插入种子数据（仅首次，幂等）。"""
        with self._cursor() as cur:
            for account_id, display_name in get_seed_accounts():
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM accounts WHERE account_id = ?) "
                    "INSERT INTO accounts (account_id, display_name) VALUES (?, ?)",
                    (account_id, account_id, display_name)
                )

            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM tenants WHERE tenant_id = 'default') "
                "INSERT INTO tenants (tenant_id, name, quota_limit) VALUES ('default', 'Default', -1)"
            )

            default_key = get_default_api_key()
            if default_key:
                cur.execute(
                    "IF NOT EXISTS (SELECT 1 FROM api_keys WHERE key_value = ?) "
                    "INSERT INTO api_keys (key_value, tenant_id, name) VALUES (?, 'default', 'default-key')",
                    (default_key, default_key)
                )

    def close(self) -> None:
        """关闭当前线程的连接。"""
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.connection = None

    # ---- Account 查询 ----

    def get_active_accounts(self) -> List[AccountRow]:
        """获取所有活跃账号。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT account_id, display_name, is_active, active_count, "
                "  total_requests, consecutive_failures, status, cooldown_until, "
                "  daily_tokens, monthly_tokens, yearly_tokens "
                "FROM accounts WHERE is_active = 1"
            )
            return [self._row_to_account(r) for r in cur.fetchall()]

    def get_all_accounts(self) -> List[AccountRow]:
        """获取所有账号（含禁用的）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT account_id, display_name, is_active, active_count, "
                "  total_requests, consecutive_failures, status, cooldown_until, "
                "  daily_tokens, monthly_tokens, yearly_tokens "
                "FROM accounts"
            )
            return [self._row_to_account(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_account(r) -> AccountRow:
        return AccountRow(
            account_id=r[0], display_name=r[1], is_active=bool(r[2]),
            active_count=r[3], total_requests=r[4],
            consecutive_failures=r[5], status=r[6],
            cooldown_until=str(r[7]) if r[7] else None,
            daily_tokens=r[8] if len(r) > 8 else 0,
            monthly_tokens=r[9] if len(r) > 9 else 0,
            yearly_tokens=r[10] if len(r) > 10 else 0,
        )

    def add_account(self, account_id: str, display_name: str) -> None:
        """添加账号。"""
        with self._cursor() as cur:
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM accounts WHERE account_id = ?) "
                "INSERT INTO accounts (account_id, display_name) VALUES (?, ?)",
                (account_id, account_id, display_name)
            )

    def set_account_active(self, account_id: str, active: bool) -> None:
        """启用/禁用账号。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET is_active = ? WHERE account_id = ?",
                (1 if active else 0, account_id)
            )

    # ---- 负载均衡 ----

    def increment_active_count(self, account_id: str) -> None:
        """分配账号后递增 active_count 和 total_requests（跨月自动归零）。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET "
                "  active_count = active_count + 1, "
                "  total_requests = CASE "
                "    WHEN DATEDIFF(MONTH, COALESCE(requests_reset_date, '2000-01-01'), GETUTCDATE()) > 0 "
                "    THEN 1 ELSE total_requests + 1 END, "
                "  requests_reset_date = GETUTCDATE() "
                "WHERE account_id = ?",
                (account_id,)
            )

    def decrement_active_count(self, account_id: str) -> None:
        """请求完成后递减 active_count（不低于 0）。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET active_count = CASE "
                "  WHEN active_count > 0 THEN active_count - 1 ELSE 0 END "
                "WHERE account_id = ?",
                (account_id,)
            )

    def get_least_used_account(self, exclude_account_ids: Optional[List[str]] = None) -> Optional[str]:
        """获取当前最空闲（active_count 最小）的活跃（is_active==1）且健康(status == healthy)的账号。"""
        exclude = exclude_account_ids or []

        with self._cursor() as cur:
            # 泄漏检测：active_count > 0 但无近期请求 → 归零
            cur.execute(
                "UPDATE accounts SET active_count = 0 "
                "WHERE active_count > 0 AND total_requests > 0 "
                "AND DATEDIFF(MINUTE, COALESCE("
                "  (SELECT MAX(created_at) FROM request_log), "
                "  DATEADD(MINUTE, -20, GETUTCDATE())), GETUTCDATE()) > 10"
            )

            if exclude:
                placeholders = ",".join(["?"] * len(exclude))
                cur.execute(
                    f"SELECT TOP 1 account_id FROM accounts "
                    f"WHERE is_active = 1 "
                    f"  AND (status != 'down' OR cooldown_until IS NULL OR cooldown_until < GETUTCDATE()) "
                    f"  AND account_id NOT IN ({placeholders}) "
                    f"ORDER BY active_count ASC, daily_tokens ASC",
                    exclude
                )
            else:
                cur.execute(
                    "SELECT TOP 1 account_id FROM accounts "
                    "WHERE is_active = 1 "
                    "  AND (status != 'down' OR cooldown_until IS NULL OR cooldown_until < GETUTCDATE()) "
                    "ORDER BY active_count ASC, daily_tokens ASC"
                )

            row = cur.fetchone()
            return row[0] if row else None

    def reset_all_active_counts(self) -> None:
        """管理员手动重置所有 active_count。"""
        with self._cursor() as cur:
            cur.execute("UPDATE accounts SET active_count = 0")

    # ---- 健康追踪 ----

    def record_success(self, account_id: str) -> None:
        """记录请求成功，重置健康状态。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET status = 'healthy', consecutive_failures = 0, "
                "  cooldown_until = NULL WHERE account_id = ?",
                (account_id,)
            )

    def record_failure(self, account_id: str) -> None:
        """记录请求失败，自动退化健康状态。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET "
                "  consecutive_failures = consecutive_failures + 1, "
                "  status = CASE "
                "    WHEN consecutive_failures + 1 >= 3 THEN 'down' "
                "    ELSE 'degraded' END, "
                "  cooldown_until = CASE "
                "    WHEN consecutive_failures + 1 >= 3 THEN DATEADD(MINUTE, 5, GETUTCDATE()) "
                "    ELSE cooldown_until END "
                "WHERE account_id = ?",
                (account_id,)
            )

    def reset_account_health(self, account_id: str) -> None:
        """管理员手动重置账号健康状态。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET status = 'healthy', consecutive_failures = 0, "
                "  cooldown_until = NULL WHERE account_id = ?",
                (account_id,)
            )

    def add_token_usage(self, account_id: str, token_count: int) -> None:
        """记录账号 token 使用量，自动按日/月/年重置计数器。"""
        if token_count <= 0:
            return
        with self._cursor() as cur:
            cur.execute(
                "UPDATE accounts SET "
                "  daily_tokens = CASE "
                "    WHEN CONVERT(DATE, GETUTCDATE()) != COALESCE(CONVERT(DATE, tokens_reset_date), '2000-01-01') "
                "    THEN ? ELSE daily_tokens + ? END, "
                "  monthly_tokens = CASE "
                "    WHEN DATEDIFF(MONTH, COALESCE(tokens_reset_date, '2000-01-01'), GETUTCDATE()) > 0 "
                "    THEN ? ELSE monthly_tokens + ? END, "
                "  yearly_tokens = CASE "
                "    WHEN YEAR(GETUTCDATE()) != YEAR(COALESCE(tokens_reset_date, '2000-01-01')) "
                "    THEN ? ELSE yearly_tokens + ? END, "
                "  tokens_reset_date = GETUTCDATE() "
                "WHERE account_id = ?",
                (token_count, token_count, token_count, token_count,
                 token_count, token_count, account_id)
            )

    def add_tenant_token_usage(self, tenant_id: str, token_count: int) -> None:
        """记录租户 token 使用量，自动按日/月/年重置计数器。"""
        if token_count <= 0:
            return
        with self._cursor() as cur:
            cur.execute(
                "UPDATE tenants SET "
                "  daily_tokens = CASE "
                "    WHEN CONVERT(DATE, GETUTCDATE()) != COALESCE(CONVERT(DATE, tokens_reset_date), '2000-01-01') "
                "    THEN ? ELSE daily_tokens + ? END, "
                "  monthly_tokens = CASE "
                "    WHEN DATEDIFF(MONTH, COALESCE(tokens_reset_date, '2000-01-01'), GETUTCDATE()) > 0 "
                "    THEN ? ELSE monthly_tokens + ? END, "
                "  yearly_tokens = CASE "
                "    WHEN YEAR(GETUTCDATE()) != YEAR(COALESCE(tokens_reset_date, '2000-01-01')) "
                "    THEN ? ELSE yearly_tokens + ? END, "
                "  tokens_reset_date = GETUTCDATE() "
                "WHERE tenant_id = ?",
                (token_count, token_count, token_count, token_count,
                 token_count, token_count, tenant_id)
            )

    def log_token_usage(self, user_account: str, tenant_id: str,
                        token_count: int, model: Optional[str] = None) -> None:
        """记录 token 使用明细（写入 token_usage 表）。"""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO token_usage (user_account, tenant_id, model, token_count) "
                "VALUES (?, ?, ?, ?)",
                (user_account, tenant_id, model, token_count)
            )

    def get_token_usage_summary(self, model: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询 token 使用汇总，按 user_account + tenant_id 聚合日/月/年用量。可选按 model 过滤。"""
        with self._cursor() as cur:
            where_clause = ""
            params: list = []
            if model:
                where_clause = "WHERE u.model = ?"
                params.append(model)

            cur.execute(
                "SELECT u.user_account, u.tenant_id, ISNULL(t.name, u.tenant_id) AS tenant_name, "
                "  SUM(CASE WHEN CONVERT(DATE, u.created_at) = CONVERT(DATE, GETUTCDATE()) "
                "    THEN u.token_count ELSE 0 END) AS daily_tokens, "
                "  SUM(CASE WHEN DATEDIFF(MONTH, u.created_at, GETUTCDATE()) = 0 "
                "    THEN u.token_count ELSE 0 END) AS monthly_tokens, "
                "  SUM(CASE WHEN YEAR(u.created_at) = YEAR(GETUTCDATE()) "
                "    THEN u.token_count ELSE 0 END) AS yearly_tokens "
                "FROM token_usage u "
                "LEFT JOIN tenants t ON u.tenant_id = t.tenant_id "
                + where_clause + " "
                "GROUP BY u.user_account, u.tenant_id, ISNULL(t.name, u.tenant_id) "
                "ORDER BY u.user_account, u.tenant_id",
                params
            )
            return [
                {
                    "user_account": r[0],
                    "tenant_id": r[1],
                    "tenant_name": r[2],
                    "daily_tokens": r[3],
                    "monthly_tokens": r[4],
                    "yearly_tokens": r[5],
                }
                for r in cur.fetchall()
            ]

    # ---- API Key 查询 ----

    def validate_api_key(self, key_value: str) -> Optional[ApiKeyRow]:
        """验证 API Key，返回 Key 信息或 None。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT key_value, tenant_id, name, is_active "
                "FROM api_keys WHERE key_value = ? AND is_active = 1",
                (key_value,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return ApiKeyRow(
                key_value=row[0], tenant_id=row[1],
                name=row[2], is_active=bool(row[3])
            )

    def create_api_key(self, tenant_id: str, name: str = "") -> str:
        """创建新 API Key，返回 key_value。"""
        key_value = f"sk-gateway-{uuid.uuid4().hex[:24]}"
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (key_value, tenant_id, name) VALUES (?, ?, ?)",
                (key_value, tenant_id, name)
            )
        return key_value

    def revoke_api_key(self, key_value: str) -> None:
        """吊销 API Key。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET is_active = 0 WHERE key_value = ?",
                (key_value,)
            )

    def rotate_api_key(self, key_value: str) -> str:
        """轮换 API Key：吊销旧 Key，创建同租户新 Key。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT tenant_id, name FROM api_keys WHERE key_value = ?",
                (key_value,)
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"API Key {key_value} not found")
            tenant_id, name = row[0], row[1]
            cur.execute(
                "UPDATE api_keys SET is_active = 0 WHERE key_value = ?",
                (key_value,)
            )
        return self.create_api_key(tenant_id, name)

    def list_api_keys(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出 API Key。返回完整 key_value（用于操作）和截断的 key_display（用于展示）。"""
        with self._cursor() as cur:
            if tenant_id:
                cur.execute(
                    "SELECT key_value, tenant_id, name, is_active "
                    "FROM api_keys WHERE tenant_id = ?",
                    (tenant_id,)
                )
            else:
                cur.execute(
                    "SELECT key_value, tenant_id, name, is_active FROM api_keys"
                )
            return [
                {
                    "key_value": r[0],
                    "key_display": r[0][:12] + "...",
                    "tenant_id": r[1],
                    "name": r[2],
                    "is_active": bool(r[3]),
                }
                for r in cur.fetchall()
            ]

    # ---- Tenant 查询 ----

    def get_tenant(self, tenant_id: str) -> Optional[TenantRow]:
        """获取租户信息。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT tenant_id, name, quota_limit, allowed_models, is_active, "
                "  COALESCE(daily_tokens, 0), COALESCE(monthly_tokens, 0), COALESCE(yearly_tokens, 0) "
                "FROM tenants WHERE tenant_id = ?",
                (tenant_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            return TenantRow(
                tenant_id=row[0], name=row[1],
                quota_limit=row[2],
                allowed_models=json.loads(row[3]) if row[3] else [],
                is_active=bool(row[4]),
                daily_tokens=row[5] if len(row) > 5 else 0,
                monthly_tokens=row[6] if len(row) > 6 else 0,
                yearly_tokens=row[7] if len(row) > 7 else 0,
            )

    def create_tenant(self, name: str, quota_limit: int = -1,
                      allowed_models: Optional[List[str]] = None) -> str:
        """创建租户。"""
        tenant_id = name.lower().replace(" ", "-")
        models_json = json.dumps(allowed_models) if allowed_models else None
        with self._cursor() as cur:
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM tenants WHERE tenant_id = ?) "
                "INSERT INTO tenants (tenant_id, name, quota_limit, allowed_models) "
                "VALUES (?, ?, ?, ?)",
                (tenant_id, tenant_id, name, quota_limit, models_json)
            )
        return tenant_id

    def update_tenant(self, tenant_id: str, quota_limit: Optional[int] = None,
                      allowed_models: Optional[List[str]] = None,
                      name: Optional[str] = None) -> None:
        """更新租户名称、配额、模型权限。"""
        with self._cursor() as cur:
            if name is not None:
                cur.execute(
                    "UPDATE tenants SET name = ? WHERE tenant_id = ?",
                    (name, tenant_id)
                )
            if quota_limit is not None:
                cur.execute(
                    "UPDATE tenants SET quota_limit = ? WHERE tenant_id = ?",
                    (quota_limit, tenant_id)
                )
            if allowed_models is not None:
                models_json = json.dumps(allowed_models) if allowed_models else None
                cur.execute(
                    "UPDATE tenants SET allowed_models = ? WHERE tenant_id = ?",
                    (models_json, tenant_id)
                )

    def list_tenants(self) -> List[TenantRow]:
        """列出所有租户。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT tenant_id, name, quota_limit, allowed_models, is_active, "
                "  COALESCE(daily_tokens, 0), COALESCE(monthly_tokens, 0), COALESCE(yearly_tokens, 0) "
                "FROM tenants"
            )
            return [
                TenantRow(
                    tenant_id=r[0], name=r[1],
                    quota_limit=r[2],
                    allowed_models=json.loads(r[3]) if r[3] else [],
                    is_active=bool(r[4]),
                    daily_tokens=r[5] if len(r) > 5 else 0,
                    monthly_tokens=r[6] if len(r) > 6 else 0,
                    yearly_tokens=r[8] if len(r) > 8 else 0,
                )
                for r in cur.fetchall()
            ]

    def delete_tenant(self, tenant_id: str) -> None:
        """删除租户（同时删除该租户下的 API Key）。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM api_keys WHERE tenant_id = ?", (tenant_id,))
            cur.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))

    def get_tenant_hourly_usage(self, tenant_id: str) -> int:
        """获取租户当前小时的请求总数（配额检查用）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE tenant_id = ? "
                "  AND YEAR(created_at) = YEAR(GETUTCDATE()) "
                "  AND MONTH(created_at) = MONTH(GETUTCDATE()) "
                "  AND DAY(created_at) = DAY(GETUTCDATE()) "
                "  AND DATEPART(HOUR, created_at) = DATEPART(HOUR, GETUTCDATE())",
                (tenant_id,)
            )
            row = cur.fetchone()
            return row[0] if row else 0

    # ---- Admin User 查询 ----

    def is_admin(self, domain_account: str) -> bool:
        """检查是否为管理员。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM admin_users WHERE domain_account = ?",
                (domain_account,)
            )
            row = cur.fetchone()
            return row[0] > 0 if row else False

    def add_admin(self, domain_account: str, display_name: str) -> None:
        """添加管理员。"""
        with self._cursor() as cur:
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM admin_users WHERE domain_account = ?) "
                "INSERT INTO admin_users (domain_account, display_name) VALUES (?, ?)",
                (domain_account, domain_account, display_name)
            )

    def remove_admin(self, domain_account: str) -> None:
        """移除管理员。"""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM admin_users WHERE domain_account = ?",
                (domain_account,)
            )

    def list_admins(self) -> List[AdminUserRow]:
        """列出所有管理员。"""
        with self._cursor() as cur:
            cur.execute("SELECT domain_account, display_name FROM admin_users")
            return [AdminUserRow(domain_account=r[0], display_name=r[1]) for r in cur.fetchall()]

    # ---- Request Log ----

    def log_request(self, tenant_id: str, model: Optional[str] = None) -> None:
        """记录请求（用于配额统计）。"""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO request_log (tenant_id, model) VALUES (?, ?)",
                (tenant_id, model)
            )

    def cleanup_old_request_logs(self, months: int = 3) -> int:
        """清理超过指定月数的请求日志，返回删除行数。"""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM request_log "
                "WHERE created_at < DATEADD(MONTH, -?, GETUTCDATE())",
                (months,)
            )
            return cur.rowcount

    # ---- Status 概览 ----

    def get_status_summary(self) -> Dict[str, Any]:
        """获取网关状态概览。"""
        from .config import MODEL_PROVIDER_MAP, PROVIDER_ZJ
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 1")
            active_accounts = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1")
            active_keys = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM tenants WHERE is_active = 1")
            active_tenants = cur.fetchone()[0]

            zj_model_count = sum(1 for p in MODEL_PROVIDER_MAP.values() if p == PROVIDER_ZJ)

            cur.execute(
                "SELECT account_id, display_name, active_count, total_requests, "
                "  status, consecutive_failures, cooldown_until, "
                "  daily_tokens, monthly_tokens, yearly_tokens "
                "FROM accounts ORDER BY active_count DESC"
            )
            accounts = [
                {
                    "account_id": r[0], "display_name": r[1],
                    "active_count": r[2], "total_requests": r[3],
                    "status": r[4], "consecutive_failures": r[5],
                    "cooldown_until": str(r[6]) if r[6] else None,
                    "daily_tokens": r[7], "monthly_tokens": r[8],
                    "yearly_tokens": r[9],
                }
                for r in cur.fetchall()
            ]

            cur.execute("SELECT COUNT(*) FROM accounts WHERE status != 'healthy'")
            unhealthy_accounts = cur.fetchone()[0]

            return {
                "active_accounts": active_accounts,
                "active_keys": active_keys,
                "active_tenants": active_tenants,
                "unhealthy_accounts": unhealthy_accounts,
                "zj_model_count": zj_model_count,
                "accounts": accounts,
            }
