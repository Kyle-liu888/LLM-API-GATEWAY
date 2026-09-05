# -*- coding: utf-8 -*-
"""
管理后台 API 路由：
- 从 server.py 抽离的 /admin/* 端点
- Token 用量查询
"""

from __future__ import annotations

import logging

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .config import get_windows_domain_account
from .db import Database

logger = logging.getLogger("copilot-proxy.admin")

router = APIRouter(prefix="/admin")


def _get_db(request: Request) -> Database:
    return request.app.state.db


def require_admin(request: Request) -> str:
    """管理员鉴权：检查 Windows 域账号 + 限制本地访问。"""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Admin access only from localhost")

    domain_account = get_windows_domain_account()
    db = _get_db(request)
    if not db.is_admin(domain_account):
        raise HTTPException(status_code=403, detail=f"User '{domain_account}' is not an admin")
    return domain_account


# ---- Auth ----

@router.get("/auth/check")
async def admin_auth_check(request: Request):
    """检查当前用户是否为管理员。"""
    domain_account = get_windows_domain_account()
    db = _get_db(request)
    is_admin = db.is_admin(domain_account)
    return {"is_admin": is_admin, "domain_account": domain_account}


# ---- Status ----

@router.get("/status")
async def admin_status(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    return db.get_status_summary()


# ---- API Keys ----

@router.get("/keys")
async def admin_list_keys(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    return db.list_api_keys()


@router.post("/keys")
async def admin_create_key(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    data = await request.json()
    tenant_id = data.get("tenant_id", "default")
    name = data.get("name", "")
    key_value = db.create_api_key(tenant_id, name)
    return {"key_value": key_value, "tenant_id": tenant_id}


@router.delete("/keys/{key_value:path}")
async def admin_revoke_key(key_value: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    db.revoke_api_key(key_value)
    return {"status": "revoked"}


@router.post("/keys/{key_value:path}/rotate")
async def admin_rotate_key(key_value: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    new_key = db.rotate_api_key(key_value)
    return {"key_value": new_key}


# ---- Tenants ----

@router.get("/tenants")
async def admin_list_tenants(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    tenants = db.list_tenants()
    return [{"tenant_id": t.tenant_id, "name": t.name, "quota_limit": t.quota_limit,
             "allowed_models": t.allowed_models, "is_active": t.is_active,
             "daily_tokens": t.daily_tokens,
             "monthly_tokens": t.monthly_tokens, "yearly_tokens": t.yearly_tokens}
            for t in tenants]


@router.post("/tenants")
async def admin_create_tenant(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    data = await request.json()
    name = data.get("name", "")
    quota_limit = data.get("quota_limit", -1)
    allowed_models = data.get("allowed_models")
    tenant_id = db.create_tenant(name, quota_limit, allowed_models)
    return {"tenant_id": tenant_id}


@router.put("/tenants/{tenant_id}")
async def admin_update_tenant(tenant_id: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    data = await request.json()
    db.update_tenant(tenant_id, data.get("quota_limit"), data.get("allowed_models"), data.get("name"))
    return {"status": "updated"}


@router.delete("/tenants/{tenant_id}")
async def admin_delete_tenant(tenant_id: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    db.delete_tenant(tenant_id)
    return {"status": "deleted"}


# ---- Accounts ----

@router.get("/accounts")
async def admin_list_accounts(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    accounts = db.get_all_accounts()
    return [
        {
            "account_id": a.account_id,
            "display_name": a.display_name,
            "is_active": a.is_active,
            "active_count": a.active_count,
            "total_requests": a.total_requests,
            "daily_tokens": a.daily_tokens,
            "monthly_tokens": a.monthly_tokens,
            "yearly_tokens": a.yearly_tokens,
            "status": a.status,
            "consecutive_failures": a.consecutive_failures,
            "cooldown_until": a.cooldown_until,
        }
        for a in accounts
    ]


@router.post("/accounts/{account_id}/reset-health")
async def admin_reset_health(account_id: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    db.reset_account_health(account_id)
    return {"status": "reset"}


@router.post("/accounts/reset-counts")
async def admin_reset_counts(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    db.reset_all_active_counts()
    return {"status": "reset"}


# ---- Admins ----

@router.get("/admins")
async def admin_list_admins(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    return [{"domain_account": a.domain_account, "display_name": a.display_name}
            for a in db.list_admins()]


@router.post("/admins")
async def admin_add_admin(request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    data = await request.json()
    db.add_admin(data["domain_account"], data.get("display_name", data["domain_account"]))
    return {"status": "added"}


@router.delete("/admins/{domain_account}")
async def admin_remove_admin(domain_account: str, request: Request, _: str = Depends(require_admin)):
    db = _get_db(request)
    db.remove_admin(domain_account)
    return {"status": "removed"}


# ---- Token Usage ----

@router.get("/token-usage")
async def admin_token_usage(request: Request, _: str = Depends(require_admin), model: Optional[str] = None):
    """查询 token_usage 表，按 user_account + tenant_id 聚合日/月/年用量。可选按 model 过滤。"""
    db = _get_db(request)
    return db.get_token_usage_summary(model=model)
