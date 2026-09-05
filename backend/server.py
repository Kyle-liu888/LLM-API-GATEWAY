# -*- coding: utf-8 -*-
"""
服务层：
- FastAPI App 初始化
- 路由注册
- API Key 鉴权（数据库驱动）
- Uvicorn 启动/停止管理
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    PROVIDER_COPILOT,
    PROVIDER_ZJ,
    SUPPORTED_MODELS,
    get_copilot_api_url,
    get_local_port,
    get_provider_for_model,
    get_windows_domain_account,
)
from .db import Database
from .proxy_core import ProxyCore, TokenManager, now_ts
from .admin_api import router as admin_router

logger = logging.getLogger("copilot-proxy.server")


# ---- Tenant Context ----

@dataclass
class TenantContext:
    """请求鉴权后返回的租户上下文，在路由处理器中传递。"""
    tenant_id: str
    tenant_name: str
    allowed_models: List[str]  # 空 = 全部允许
    quota_limit: int           # -1 = 无限
    current_usage: int


def create_app(db: Database) -> FastAPI:
    """创建 FastAPI 应用。路由层负责 HTTP 协议适配，业务逻辑交给 ProxyCore + AuthManager。"""
    token_manager = TokenManager(verify_ssl=False)
    core = ProxyCore(token_manager=token_manager, verify_ssl=False)

    app = FastAPI(title="LLM API Gateway")

    # 全局 CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 将共享对象挂到 app.state
    app.state.db = db
    app.state.core = core

    # ---- 鉴权依赖 ----

    async def verify_api_key(request: Request) -> TenantContext:
        """API Key 鉴权：从数据库验证，返回租户上下文。"""
        x_api_key = request.headers.get("x-api-key")
        auth_header = request.headers.get("authorization", "")

        if auth_header.lower().startswith("bearer "):
            token_value = auth_header[7:].strip()
        else:
            token_value = x_api_key or auth_header

        if not token_value:
            raise HTTPException(status_code=401, detail="Missing API key")

        key_row = db.validate_api_key(token_value)
        if key_row is None:
            logger.warning("API key mismatch: %s...", token_value[:8] if len(token_value) >= 8 else token_value)
            raise HTTPException(status_code=403, detail="Invalid API key")

        # 加载租户信息
        tenant = db.get_tenant(key_row.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=403, detail="Tenant inactive or not found")

        # 配额检查（每小时）
        current_usage = db.get_tenant_hourly_usage(key_row.tenant_id)
        if tenant.quota_limit > 0 and current_usage >= tenant.quota_limit:
            raise HTTPException(status_code=429, detail="Quota exceeded")

        return TenantContext(
            tenant_id=tenant.tenant_id,
            tenant_name=tenant.name,
            allowed_models=tenant.allowed_models,
            quota_limit=tenant.quota_limit,
            current_usage=current_usage,
        )

    # ---- 请求日志中间件 ----

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        logger.info("Request: %s %s", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception("Unhandled error in request")
            return JSONResponse(
                status_code=500,
                content={"error": {"message": f"Internal error: {exc}", "type": "internal_error"}},
            )
        logger.info("Response: %s %s", request.url.path, response.status_code)
        return response

    # ---- 健康检查 ----

    @app.get("/health")
    async def health():
        """健康检查端点，无需鉴权。"""
        return {
            "status": "ok",
            "listen": f"http://127.0.0.1:{get_local_port()}",
            "upstream": get_copilot_api_url(),
            "supported_models": SUPPORTED_MODELS,
            "timestamp": now_ts(),
        }

    @app.get("/")
    async def root():
        """根路径：返回服务基本信息。"""
        return {
            "name": "LLM API Gateway",
            "status": "ok",
            "endpoints": ["/v1/messages", "/messages", "/v1/chat/completions", "/v1/models", "/health"],
        }

    # ---- 模型列表 ----

    @app.get("/v1/models")
    async def list_models(ctx: TenantContext = Depends(verify_api_key)):
        """列出可用模型，根据租户权限过滤。"""
        created = now_ts()
        # 如果租户有 allowed_models 限制，只返回允许的模型
        if ctx.allowed_models:
            models = [m for m in SUPPORTED_MODELS if m in ctx.allowed_models]
        else:
            models = list(SUPPORTED_MODELS)
        return {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "owned_by": get_provider_for_model(model_id), "created": created}
                for model_id in models
            ],
        }

    @app.get("/v1/models/{model_id}")
    async def get_model(model_id: str, ctx: TenantContext = Depends(verify_api_key)):
        """查询单个模型元数据。"""
        if ctx.allowed_models and model_id not in ctx.allowed_models:
            raise HTTPException(status_code=403, detail=f"Model {model_id} not allowed for this tenant")
        return {"id": model_id, "object": "model", "owned_by": get_provider_for_model(model_id), "created": now_ts()}

    # ---- OpenAI Chat Completions ----

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, ctx: TenantContext = Depends(verify_api_key)):
        """OpenAI Chat Completions 兼容端口，根据 model 路由到不同上游。"""
        body = await request.json()
        model = body.get("model") or SUPPORTED_MODELS[0]

        # 租户模型权限检查
        if ctx.allowed_models and model not in ctx.allowed_models:
            raise HTTPException(status_code=403, detail=f"Model {model} not allowed for tenant {ctx.tenant_id}")

        provider = get_provider_for_model(model)
        logger.info(
            "OpenAI request: model=%s stream=%s provider=%s tenant=%s",
            model, body.get("stream"), provider, ctx.tenant_id,
        )

        upstream_body = core.build_upstream_body_from_openai(body)
        if provider == PROVIDER_ZJ:
            return await _handle_zj_openai(upstream_body, ctx, model, db, core)
        else:
            return await _handle_copilot_openai(upstream_body, ctx, model, db, core)

    # ---- Anthropic Messages ----

    @app.post("/v1/messages")
    async def anthropic_messages_v1(request: Request, ctx: TenantContext = Depends(verify_api_key)):
        return await _handle_anthropic_messages(request, ctx)

    @app.post("/messages")
    async def anthropic_messages_root(request: Request, ctx: TenantContext = Depends(verify_api_key)):
        return await _handle_anthropic_messages(request, ctx)

    async def _handle_anthropic_messages(request: Request, ctx: TenantContext):
        """Anthropic Messages 统一处理逻辑，根据 model 路由到不同上游。"""
        body = await request.json()
        upstream_body = core.build_upstream_body_from_anthropic(body)
        model = upstream_body["model"]
        stream = bool(body.get("stream", False))

        if ctx.allowed_models and model not in ctx.allowed_models:
            raise HTTPException(status_code=403, detail=f"Model {model} not allowed for tenant {ctx.tenant_id}")

        provider = get_provider_for_model(model)
        logger.info(
            "Anthropic request: model=%s stream=%s provider=%s tenant=%s",
            model, stream, provider, ctx.tenant_id,
        )

        if provider == PROVIDER_ZJ:
            return await _handle_zj_anthropic(upstream_body, ctx, model, db, core)
        else:
            return await _handle_copilot_anthropic(upstream_body, ctx, model, db, core)

    # ---- Admin API Router ----
    app.include_router(admin_router)

    # ---- Admin UI 静态文件 ----
    if getattr(sys, "frozen", False):
        admin_ui_dir = Path(sys._MEIPASS) / "frontend"
    else:
        base = Path(__file__).resolve().parents[1]
        admin_ui_dir = base / "frontend" / "dist" if (base / "frontend" / "dist").exists() else base / "frontend"
    if admin_ui_dir.is_dir():
        app.mount("/admin", StaticFiles(directory=str(admin_ui_dir), html=True), name="admin-ui")

    return app


# ---- Copilot 路径处理器 ----

async def _allocate_with_retry(db: Database) -> str:
    """分配最空闲的 Copilot 账号，最多重试3次排除不可用的。"""
    exclude: List[str] = []

    for attempt in range(3):
        account_id = db.get_least_used_account(exclude)
        if account_id is None:
            break

        db.increment_active_count(account_id)
        return account_id

    # 所有账号尝试完毕，强制选择一个（即使 degraded）
    accounts = db.get_active_accounts()
    if accounts:
        fallback = accounts[0].account_id
        db.increment_active_count(fallback)
        return fallback

    raise HTTPException(status_code=503, detail="No available accounts")


def _release_account(db: Database, account_id: str, success: bool, total_tokens: int = 0):
    """请求完成后释放账号并记录健康状态和 token 用量。"""
    if success:
        db.record_success(account_id)
        db.add_token_usage(account_id, total_tokens)
    else:
        db.record_failure(account_id)
    db.decrement_active_count(account_id)


async def _handle_copilot_openai(upstream_body: Dict[str, Any], ctx: TenantContext, model: str,
                                  db: Database, core: ProxyCore):
    """Copilot 上游 OpenAI 路径：需要账号分配、健康追踪。"""
    account_id = await _allocate_with_retry(db)

    try:
        headers = await core.build_upstream_headers(account_id=account_id)
    except Exception as exc:
        db.decrement_active_count(account_id)
        logger.exception("Failed to build upstream headers")
        raise HTTPException(status_code=500, detail=f"Build upstream headers failed: {exc}")

    start_time = time.time()
    try:
        if upstream_body.get("stream"):
            stats: Dict[str, int] = {"total_tokens": 0}
            return StreamingResponse(
                _stream_copilot_cleanup(
                    core.stream_openai_response(upstream_body, headers, model, url=core.copilot_api_url, stats=stats),
                    db, account_id, ctx, start_time, stats, model,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            result = await core.non_stream_openai_response(upstream_body, headers, model, url=core.copilot_api_url)
            total_tokens = result.get("usage", {}).get("total_tokens", 0)
            db.record_success(account_id)
            db.add_token_usage(account_id, total_tokens)
            db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
            db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
            db.decrement_active_count(account_id)
            db.log_request(ctx.tenant_id, model)
            return JSONResponse(content=result)
    except RuntimeError as exc:
        db.record_failure(account_id)
        db.decrement_active_count(account_id)
        db.log_request(ctx.tenant_id, model)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        db.record_failure(account_id)
        db.decrement_active_count(account_id)
        db.log_request(ctx.tenant_id, model)
        logger.exception("Upstream connection failed")
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {type(exc).__name__}: {exc}")


async def _handle_copilot_anthropic(upstream_body: Dict[str, Any], ctx: TenantContext, model: str,
                                     db: Database, core: ProxyCore):
    """Copilot 上游 Anthropic 路径：需要账号分配、健康追踪。"""
    account_id = await _allocate_with_retry(db)

    try:
        headers = await core.build_upstream_headers(account_id=account_id)
    except Exception as exc:
        db.decrement_active_count(account_id)
        logger.exception("Failed to build upstream headers")
        raise HTTPException(status_code=500, detail=f"Build upstream headers failed: {exc}")

    start_time = time.time()
    try:
        if upstream_body.get("stream"):
            stats: Dict[str, int] = {"total_tokens": 0}
            return StreamingResponse(
                _stream_copilot_cleanup(
                    core.stream_anthropic_response(upstream_body, headers, model, url=core.copilot_api_url),
                    db, account_id, ctx, start_time, stats, model,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            result = await core.non_stream_anthropic_response(upstream_body, headers, model, url=core.copilot_api_url)
            # Anthropic 响应的 usage 是转换后的格式，需要从原始上游获取 total_tokens
            openai_usage = result.get("usage", {})
            total_tokens = openai_usage.get("input_tokens", 0) + openai_usage.get("output_tokens", 0)
            db.record_success(account_id)
            db.add_token_usage(account_id, total_tokens)
            db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
            db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
            db.decrement_active_count(account_id)
            db.log_request(ctx.tenant_id, model)
            return JSONResponse(content=result)
    except RuntimeError as exc:
        db.record_failure(account_id)
        db.decrement_active_count(account_id)
        db.log_request(ctx.tenant_id, model)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        db.record_failure(account_id)
        db.decrement_active_count(account_id)
        db.log_request(ctx.tenant_id, model)
        logger.exception("Upstream connection failed")
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {type(exc).__name__}: {exc}")


# ---- ZJ 路径处理器 ----

async def _handle_zj_openai(upstream_body: Dict[str, Any], ctx: TenantContext, model: str,
                            db: Database, core: ProxyCore):
    """ZJ 上游 OpenAI 路径：无需账号分配，Bearer API Key 鉴权。"""
    headers = await core.build_zj_headers()

    start_time = time.time()
    try:
        if upstream_body.get("stream"):
            stats: Dict[str, int] = {"total_tokens": 0}
            return StreamingResponse(
                _stream_zj_cleanup(
                    core.stream_openai_response(upstream_body, headers, model, url=core.zj_api_url, stats=stats),
                    db, ctx, start_time, stats, model,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            result = await core.non_stream_openai_response(upstream_body, headers, model, url=core.zj_api_url)
            total_tokens = result.get("usage", {}).get("total_tokens", 0)
            db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
            db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
            db.log_request(ctx.tenant_id, model)
            return JSONResponse(content=result)
    except RuntimeError as exc:
        db.log_request(ctx.tenant_id, model)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        db.log_request(ctx.tenant_id, model)
        logger.exception("Upstream connection failed")
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {type(exc).__name__}: {exc}")


async def _handle_zj_anthropic(upstream_body: Dict[str, Any], ctx: TenantContext, model: str,
                               db: Database, core: ProxyCore):
    """ZJ 上游 Anthropic 路径：无需账号分配，Bearer API Key 鉴权。"""
    headers = await core.build_zj_headers()

    start_time = time.time()
    try:
        if upstream_body.get("stream"):
            return StreamingResponse(
                _stream_zj_cleanup(
                    core.stream_anthropic_response(upstream_body, headers, model, url=core.zj_api_url),
                    db, ctx, start_time, model=model,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            result = await core.non_stream_anthropic_response(upstream_body, headers, model, url=core.zj_api_url)
            openai_usage = result.get("usage", {})
            total_tokens = openai_usage.get("input_tokens", 0) + openai_usage.get("output_tokens", 0)
            db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
            db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
            db.log_request(ctx.tenant_id, model)
            return JSONResponse(content=result)
    except RuntimeError as exc:
        db.log_request(ctx.tenant_id, model)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        db.log_request(ctx.tenant_id, model)
        logger.exception("Upstream connection failed")
        raise HTTPException(status_code=502, detail=f"Upstream connection failed: {type(exc).__name__}: {exc}")


# ---- 流式清理生成器 ----

async def _stream_copilot_cleanup(stream_gen, db: Database, account_id: str,
                                   ctx: TenantContext, start_time: float,
                                   stats: Optional[Dict[str, int]] = None, model: str = ""):
    """Copilot 流式响应生成器：在流结束后释放账号、记录健康状态和 token 用量。"""
    success = True
    total_tokens = (stats or {}).get("total_tokens", 0)
    try:
        async for chunk in stream_gen:
            # stats 字典在迭代过程中被 stream_openai_response 更新
            total_tokens = (stats or {}).get("total_tokens", 0)
            yield chunk
    except Exception:
        success = False
        raise
    finally:
        _release_account(db, account_id, success, total_tokens)
        db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
        db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
        db.log_request(ctx.tenant_id, model)


async def _stream_zj_cleanup(stream_gen, db: Database, ctx: TenantContext,
                              start_time: float, stats: Optional[Dict[str, int]] = None, model: str = ""):
    """ZJ 流式响应生成器：在流结束后记录 token 用量（无账号分配）。"""
    total_tokens = (stats or {}).get("total_tokens", 0)
    try:
        async for chunk in stream_gen:
            total_tokens = (stats or {}).get("total_tokens", 0)
            yield chunk
    except Exception:
        raise
    finally:
        db.add_tenant_token_usage(ctx.tenant_id, total_tokens)
        db.log_token_usage(get_windows_domain_account(), ctx.tenant_id, total_tokens, model)
        db.log_request(ctx.tenant_id, model)


# ---- Server Runner ----

class ServerRunner:
    """Uvicorn 服务运行器。"""

    def __init__(self, db: Database):
        self.db = db
        self.server: Optional[uvicorn.Server] = None
        self.thread: Optional[threading.Thread] = None
        self._cleanup_timer: Optional[threading.Timer] = None

    def _build_server(self) -> uvicorn.Server:
        app = create_app(self.db)
        port = get_local_port()
        uvicorn_config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            access_log=False,
            log_config=None,
        )
        return uvicorn.Server(uvicorn_config)

    def run_blocking(self) -> None:
        """在当前线程阻塞运行。"""
        port = get_local_port()
        logger.info("Starting gateway at http://127.0.0.1:%s", port)
        self._start_cleanup_timer()
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.server = self._build_server()
        self.server.run()
        logger.info("Gateway stopped.")

    def _start_cleanup_timer(self) -> None:
        """启动 request_log 定期清理定时器（每24小时检查一次，每90天执行清理）。"""
        self._cleanup_counter = 0  # 每24小时+1，到90执行清理

        def _check_cleanup():
            try:
                self._cleanup_counter += 1
                if self._cleanup_counter >= 90:
                    self._cleanup_counter = 0
                    deleted = self.db.cleanup_old_request_logs(months=3)
                    logger.info("Request log cleanup: deleted %d rows older than 3 months", deleted)
            except Exception as exc:
                logger.error("Request log cleanup failed: %s", exc)
            # 每24小时检查一次
            self._cleanup_timer = threading.Timer(86400, _check_cleanup)
            self._cleanup_timer.daemon = True
            self._cleanup_timer.start()

        # 首次启动时立即执行一次清理
        try:
            deleted = self.db.cleanup_old_request_logs(months=3)
            logger.info("Request log startup cleanup: deleted %d rows older than 3 months", deleted)
        except Exception as exc:
            logger.error("Request log startup cleanup failed: %s", exc)

        self._cleanup_timer = threading.Timer(86400, _check_cleanup)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()
        logger.info("Request log cleanup scheduled: check every 24h, cleanup every 90 days")

    def start(self) -> None:
        """在 daemon thread 中启动。"""
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run_blocking, daemon=True)
        self.thread.start()
        time.sleep(0.2)

    def stop(self) -> None:
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
        if self.server:
            logger.info("Stopping gateway...")
            self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
