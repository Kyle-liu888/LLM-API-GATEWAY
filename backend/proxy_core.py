# -*- coding: utf-8 -*-
"""
核心代理层：
- 不依赖 FastAPI / PyQt5
- 负责协议转换、Header 组装、上游 httpx 调用、流式响应转换
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from .config import (
    AUTH_TYPE_DYNAMIC_TOKEN,
    AUTH_TYPE_NONE,
    SUPPORTED_MODELS,
    get_app_id,
    get_copilot_api_url,
    get_token_api_url,
    get_upstream_auth_type,
    get_upstream_timeout,
    get_token_cache_ttl,
    get_system_username,
    get_zj_api_url,
    get_zj_api_key,
    sanitize_header_value,
)

logger = logging.getLogger("copilot-proxy.core")


def json_dumps(obj: Any) -> str:
    """紧凑 JSON 序列化，中文不转义，用于 SSE payload 和工具调用参数。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def sse_event(event: Optional[str], data: Any) -> str:
    """构造一个 SSE 事件字符串。event 为 None 时只输出 data 行。"""
    payload = data if isinstance(data, str) else json_dumps(data)
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


def now_ts() -> int:
    """当前 Unix 时间戳（秒），用于 SSE chunk 的 created 字段。"""
    return int(time.time())


def mask_headers_for_log(headers: Dict[str, str]) -> Dict[str, str]:
    """对 Authorization 等敏感 Header 做脱敏处理，仅保留首尾字符。"""
    masked: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() == "authorization" and value and len(value) > 16:
            masked[key] = value[:10] + "..." + value[-4:]
        else:
            masked[key] = value
    return masked


def extract_text_from_content_blocks(content: Any) -> str:
    """从 Anthropic 内容块列表中提取纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    text_parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue

        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_result":
            tool_content = block.get("content", "")
            if isinstance(tool_content, str):
                text_parts.append(tool_content)
            elif isinstance(tool_content, list):
                for item in tool_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        text_parts.append(item)
                    else:
                        text_parts.append(json_dumps(item))
            else:
                text_parts.append(str(tool_content))
        elif block_type == "tool_use":
            text_parts.append(json_dumps(block))
        elif block_type == "image":
            text_parts.append("[Image content omitted]")
        else:
            text_parts.append(json_dumps(block))

    return "\n".join(part for part in text_parts if part is not None)


def convert_anthropic_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    """将 Anthropic 格式的 tools 列表转为 OpenAI function calling 格式。"""
    if not isinstance(tools, list):
        return []

    result: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return result


def convert_anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    """将 Anthropic tool_choice 映射为 OpenAI tool_choice。"""
    if not isinstance(tool_choice, dict):
        return None

    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    if choice_type == "none":
        return "none"
    return None


def convert_anthropic_messages_to_openai_full(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """将 Anthropic Messages API 的请求体完整转换为 OpenAI Messages 格式。"""
    converted: List[Dict[str, Any]] = []

    system = body.get("system")
    if system:
        system_text = extract_text_from_content_blocks(system)
        if system_text:
            converted.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"
            converted.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            converted.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            for block in content:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json_dumps(block.get("input") or {}),
                            },
                        }
                    )

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(part for part in text_parts if part) or None,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            converted.append(assistant_msg)
            continue

        if role == "user":
            user_text_parts: List[str] = []
            tool_msgs: List[Dict[str, Any]] = []

            for block in content:
                if not isinstance(block, dict):
                    user_text_parts.append(str(block))
                    continue

                block_type = block.get("type")
                if block_type == "text":
                    user_text_parts.append(block.get("text", ""))
                elif block_type == "tool_result":
                    tool_use_id = block.get("tool_use_id") or ""
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        text_acc: List[str] = []
                        for item in tool_content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_acc.append(item.get("text", ""))
                            elif isinstance(item, str):
                                text_acc.append(item)
                            else:
                                text_acc.append(json_dumps(item))
                        tool_text = "\n".join(text_acc)
                    elif isinstance(tool_content, str):
                        tool_text = tool_content
                    else:
                        tool_text = json_dumps(tool_content)

                    tool_msgs.append({"role": "tool", "tool_call_id": tool_use_id, "content": tool_text})
                elif block_type == "image":
                    user_text_parts.append("[Image omitted]")
                else:
                    user_text_parts.append(json_dumps(block))

            user_text = "\n".join(part for part in user_text_parts if part)
            if user_text:
                converted.append({"role": "user", "content": user_text})
            converted.extend(tool_msgs)
            continue

        converted.append(
            {
                "role": role if role in ("system", "tool") else "user",
                "content": extract_text_from_content_blocks(content),
            }
        )

    return converted


def map_openai_finish_to_anthropic_stop(finish_reason: Optional[str]) -> Optional[str]:
    """将 OpenAI finish_reason 映射为 Anthropic stop_reason。"""
    if not finish_reason:
        return None
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "max_tokens": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "stop_sequence",
    }
    return mapping.get(finish_reason, "end_turn")


def openai_usage_to_anthropic_usage(usage: Dict[str, int]) -> Dict[str, int]:
    """把 OpenAI usage 转为 Anthropic messages usage。"""
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }


_SSE_DONE = "[DONE]"


async def iter_upstream_sse(response: httpx.Response) -> AsyncGenerator[Any, None]:
    """解析上游 SSE 流，yield 解析后的 JSON 对象、原始字符串"""
    event_data_lines: List[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.strip("\r")

        if line == "":
            if event_data_lines:
                data_str = "\n".join(event_data_lines).strip()
                event_data_lines = []
                if not data_str:
                    continue
                if data_str == _SSE_DONE:
                                       return
                try:
                    yield json.loads(data_str)
                except json.JSONDecodeError:
                    yield data_str
            continue

        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            event_data_lines.append(line[5:].lstrip())
            continue
        if line.startswith(("event:", "id:", "retry:")):
            continue
        # 不严格遵循 SSE 标准的上游接口
        if line.startswith("{") and line.endswith("}"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield line
            continue
        if line:
            yield line

    # 处理最后一批未通过空行触发的数据
    if event_data_lines:
        data_str = "\n".join(event_data_lines).strip()
        if data_str == _SSE_DONE:
                       return
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            yield data_str


class TokenManager:
    """动态 Token 管理器：负责从 IAM 接口获取 Token 并缓存，避免频繁请求。"""

    def __init__(self, verify_ssl: bool = False):
        self._verify_ssl = verify_ssl
        self._token: Optional[str] = None
        self._expire_time: float = 0.0

    async def _fetch_dynamic_token(self) -> str:
        """从固定 IAM 接口获取动态 Token。"""
        token_api_url = get_token_api_url()
        async with httpx.AsyncClient(verify=self._verify_ssl) as client:
            response = await client.get(token_api_url, timeout=10.0)
            response.raise_for_status()
            token_text = response.text.strip()
            if not token_text:
                raise RuntimeError("IAM token API returned empty response")
            return token_text

    async def get_token(self) -> str:
        """获取有效 Token。若缓存未过期则直接返回，否则重新获取。"""
        current = time.time()
        if self._token and current < self._expire_time:
            return self._token
        self._token = await self._fetch_dynamic_token()
        self._expire_time = current + get_token_cache_ttl()
        return self._token


class ProxyCore:
    """可独立单元测试的代理核心，不引用 FastAPI。"""

    def __init__(self, token_manager: Optional[TokenManager] = None, verify_ssl: bool = False):
        self.token_manager = token_manager or TokenManager(verify_ssl=verify_ssl)
        self._verify_ssl = verify_ssl

    @property
    def copilot_api_url(self) -> str:
        return get_copilot_api_url()

    @property
    def upstream_timeout(self) -> float:
        return get_upstream_timeout()

    async def build_upstream_headers(self, account_id: Optional[str] = None) -> Dict[str, str]:
        """根据配置构建上游请求头（Copilot 路径专用）。"""
        auth_type = get_upstream_auth_type().lower()

        raw_user_account = account_id or get_system_username()
        user_account = sanitize_header_value(raw_user_account, "unknown")
        if user_account != raw_user_account:
            logger.warning("User-Account contains non-ASCII chars, sanitized: %r -> %r", raw_user_account, user_account)

        call_source = sanitize_header_value(get_app_id(), "unknown")

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "User-Agent": "ClaudeProxy/1.0",
            "User-Account": user_account,
            "X-HDP-Call-Source": call_source,
        }

        if auth_type == AUTH_TYPE_DYNAMIC_TOKEN:
            token = await self.token_manager.get_token()
            safe_token = sanitize_header_value(token, "")
            if safe_token:
                headers["Authorization"] = safe_token
        elif auth_type == AUTH_TYPE_NONE:
            pass
        else:
            logger.warning("Unknown auth type: %s, fallback to dynamic_token", auth_type)
            token = await self.token_manager.get_token()
            safe_token = sanitize_header_value(token, "")
            if safe_token:
                headers["Authorization"] = safe_token

        logger.debug("Upstream headers: %s", mask_headers_for_log(headers))
        return headers

    async def build_zj_headers(self) -> Dict[str, str]:
        """构建 ZJ 上游请求头：Bearer API Key 鉴权。"""
        api_key = get_zj_api_key()
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {api_key}",
        }

    @property
    def zj_api_url(self) -> str:
        return get_zj_api_url()

    def build_upstream_body_from_openai(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """将 OpenAI 格式请求体转为上游请求体。原样透传，仅补充 stream_options。"""
        upstream = dict(body)

        # 默认 model 回填
        if not upstream.get("model"):
            upstream["model"] = SUPPORTED_MODELS[0]

        # 流式时注入 stream_options.include_usage，确保上游返回 usage 统计
        if upstream.get("stream"):
            stream_options = upstream.get("stream_options")
            if isinstance(stream_options, dict):
                upstream["stream_options"] = dict(stream_options)
                upstream["stream_options"]["include_usage"] = True
            else:
                upstream["stream_options"] = {"include_usage": True}

        return upstream

    def build_upstream_body_from_anthropic(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """将 Anthropic Messages 格式请求体转为 OpenAI 格式上游请求体。"""
        model = body.get("model") or SUPPORTED_MODELS[0]
        anth_tools = body.get("tools") if isinstance(body.get("tools"), list) else []
        anth_tool_choice = body.get("tool_choice")

        upstream: Dict[str, Any] = {
            "model": model,
            "messages": convert_anthropic_messages_to_openai_full(body),
            "stream": bool(body.get("stream", False)),
            "max_tokens": body.get("max_tokens", 4096),
        }

        for key in ("temperature", "top_p", "stop_sequences"):
            if key in body and body[key] is not None:
                upstream["stop" if key == "stop_sequences" else key] = body[key]

        openai_tools = convert_anthropic_tools_to_openai(anth_tools)
        if openai_tools:
            upstream["tools"] = openai_tools
            openai_tool_choice = convert_anthropic_tool_choice_to_openai(anth_tool_choice)
            upstream["tool_choice"] = openai_tool_choice if openai_tool_choice is not None else "auto"

        logger.info(
            "Upstream body: model=%s msgs=%s tools=%s tool_choice=%s",
            upstream["model"],
            len(upstream["messages"]),
            len(upstream.get("tools") or []),
            upstream.get("tool_choice"),
        )
        return upstream

    async def stream_openai_response(
        self,
        upstream_body: Dict[str, Any],
        headers: Dict[str, str],
        model: str,
        url: Optional[str] = None,
        stats: Optional[Dict[str, int]] = None,
    ) -> AsyncGenerator[str, None]:
        """流式透传上游 OpenAI 响应，原样转发 chunk，仅提取 total_tokens 用于统计。"""
        target_url = url or self.copilot_api_url

        try:
            async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self.upstream_timeout) as client:
                async with client.stream("POST", target_url, json=upstream_body, headers=headers) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        msg = error_body.decode(errors="ignore")
                        logger.error("Upstream error %s: %s", response.status_code, msg)
                        yield sse_event(
                            None,
                            {"error": {"message": f"Upstream error ({response.status_code}): {msg}", "type": "upstream_error", "code": response.status_code}},
                        )
                        yield "data: " + _SSE_DONE + "\n\n"
                        return

                    async for data in iter_upstream_sse(response):
                        if data == _SSE_DONE:
                            yield "data: " + _SSE_DONE + "\n\n"
                            return

                        if isinstance(data, dict):
                            usage = data.get("usage")
                            if isinstance(usage, dict) and usage.get("total_tokens", 0) > 0:
                                if stats is not None:
                                    stats["total_tokens"] = usage["total_tokens"]
                            yield sse_event(None, data)
                        else:
                            yield sse_event(None, {
                                "choices": [{"index": 0, "delta": {"content": str(data)}, "finish_reason": None}],
                            })

        except Exception as exc:
            logger.exception("Upstream request failed")
            yield sse_event(None, {"error": {"message": f"Upstream request failed: {type(exc).__name__}: {exc}", "type": "connection_error"}})
            yield "data: " + _SSE_DONE + "\n\n"

    async def non_stream_openai_response(
        self,
        upstream_body: Dict[str, Any],
        headers: Dict[str, str],
        model: str,
        url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """非流式透传上游 OpenAI 响应，原样返回上游 JSON。"""
        target_url = url or self.copilot_api_url
        upstream_body = dict(upstream_body)
        upstream_body["stream"] = False

        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self.upstream_timeout) as client:
            response = await client.post(target_url, json=upstream_body, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(f"Upstream error ({response.status_code}): {response.text}")

            return response.json()

    async def stream_anthropic_response(
        self,
        upstream_body: Dict[str, Any],
        headers: Dict[str, str],
        model: str,
        url: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """流式转发上游响应，转换为 Anthropic Messages SSE 格式。"""
        target_url = url or self.copilot_api_url
        request_id = f"msg_{uuid.uuid4().hex[:24]}"
        output_chars = 0
        stop_reason = "end_turn"
        thinking_block_started = False
        text_block_started = False
        block_counter = 0
        thinking_index: Optional[int] = None
        text_index: Optional[int] = None
        tool_state: Dict[int, Dict[str, Any]] = {}

        yield sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": request_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield sse_event("ping", {"type": "ping"})

        async def start_thinking_block():
            nonlocal thinking_block_started, thinking_index, block_counter
            if not thinking_block_started:
                thinking_block_started = True
                thinking_index = block_counter
                block_counter += 1
                return sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": thinking_index, "content_block": {"type": "thinking", "thinking": ""}},
                )
            return None

        async def start_text_block():
            nonlocal text_block_started, text_index, block_counter
            if not text_block_started:
                text_block_started = True
                text_index = block_counter
                block_counter += 1
                return sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}},
                )
            return None

        try:
            async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self.upstream_timeout) as client:
                async with client.stream("POST", target_url, json=upstream_body, headers=headers) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        msg = error_body.decode(errors="ignore")
                        logger.error("Upstream error %s: %s", response.status_code, msg)
                        start_event = await start_text_block()
                        if start_event:
                            yield start_event
                        yield sse_event(
                            "content_block_delta",
                            {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": f"[Upstream error {response.status_code}] {msg}"}},
                        )
                        yield sse_event("error", {"type": "error", "error": {"type": "api_error", "message": f"Upstream error ({response.status_code}): {msg}"}})
                    else:
                        async for data in iter_upstream_sse(response):
                            if data == _SSE_DONE:
                                break

                            if not isinstance(data, dict):
                                start_event = await start_text_block()
                                if start_event:
                                    yield start_event
                                output_chars += len(str(data))
                                yield sse_event(
                                    "content_block_delta",
                                    {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": str(data)}},
                                )
                                continue

                            choices = data.get("choices") or []
                            delta = choices[0].get("delta", {}) if choices else {}
                            finish_reason = choices[0].get("finish_reason") if choices else None
                            tool_call_deltas = delta.get("tool_calls") or []

                            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                            text = delta.get("content")

                            if reasoning:
                                start_event = await start_thinking_block()
                                if start_event:
                                    yield start_event
                                yield sse_event(
                                    "content_block_delta",
                                    {"type": "content_block_delta", "index": thinking_index, "delta": {"type": "thinking_delta", "thinking": reasoning}},
                                )

                            if text:
                                start_event = await start_text_block()
                                if start_event:
                                    yield start_event
                                output_chars += len(text)
                                yield sse_event(
                                    "content_block_delta",
                                    {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": text}},
                                )

                            for tool_call in tool_call_deltas:
                                if not isinstance(tool_call, dict):
                                    continue
                                tc_idx = int(tool_call.get("index", 0) or 0)
                                function_part = tool_call.get("function") or {}
                                tc_id = tool_call.get("id")
                                tc_name = function_part.get("name")
                                tc_args = function_part.get("arguments")

                                state = tool_state.get(tc_idx)
                                if state is None:
                                    state = {
                                        "block_index": block_counter,
                                        "id": tc_id or f"toolu_{uuid.uuid4().hex[:16]}",
                                        "name": tc_name or "",
                                        "started": False,
                                        "args_buf": "",
                                        "id_locked": False,
                                    }
                                    tool_state[tc_idx] = state
                                    block_counter += 1
                                else:
                                    if tc_id and not state.get("id_locked"):
                                        state["id"] = tc_id
                                    if tc_name:
                                        state["name"] = (state["name"] or "") + tc_name

                                if not state["started"] and state["name"]:
                                    state["started"] = True
                                    state["id_locked"] = True
                                    yield sse_event(
                                        "content_block_start",
                                        {
                                            "type": "content_block_start",
                                            "index": state["block_index"],
                                            "content_block": {"type": "tool_use", "id": state["id"], "name": state["name"], "input": {}},
                                        },
                                    )

                                if tc_args and state["started"]:
                                    state["args_buf"] += tc_args
                                    yield sse_event(
                                        "content_block_delta",
                                        {"type": "content_block_delta", "index": state["block_index"], "delta": {"type": "input_json_delta", "partial_json": tc_args}},
                                    )

                            if finish_reason:
                                mapped = map_openai_finish_to_anthropic_stop(finish_reason)
                                if mapped:
                                    stop_reason = mapped
                                if tool_state:
                                    stop_reason = "tool_use"
                                break

        except Exception as exc:
            logger.exception("Upstream request failed")
            start_event = await start_text_block()
            if start_event:
                yield start_event
            yield sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": f"[Connection error] {type(exc).__name__}: {exc}"}},
            )
            yield sse_event("error", {"type": "error", "error": {"type": "api_error", "message": f"Upstream connection failed: {type(exc).__name__}: {exc}"}})

        if thinking_block_started:
            yield sse_event("content_block_stop", {"type": "content_block_stop", "index": thinking_index})

        if text_block_started:
            yield sse_event("content_block_stop", {"type": "content_block_stop", "index": text_index})
        elif not tool_state and not thinking_block_started:
            yield sse_event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            yield sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})

        for state in tool_state.values():
            if state.get("started"):
                yield sse_event("content_block_stop", {"type": "content_block_stop", "index": state["block_index"]})

        yield sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}})
        yield sse_event("message_stop", {"type": "message_stop"})
        logger.info("Anthropic stream finished: chars=%s tool_calls=%s stop_reason=%s", output_chars, len(tool_state), stop_reason)

    async def non_stream_anthropic_response(
        self,
        upstream_body: Dict[str, Any],
        headers: Dict[str, str],
        model: str,
        url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """非流式转发上游响应，组装为 Anthropic Messages 格式。"""
        target_url = url or self.copilot_api_url
        request_id = f"msg_{uuid.uuid4().hex[:24]}"
        upstream_body = dict(upstream_body)
        upstream_body["stream"] = False

        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self.upstream_timeout) as client:
            response = await client.post(target_url, json=upstream_body, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(f"Upstream error ({response.status_code}): {response.text}")

            try:
                data = response.json()
            except Exception:
                data = None

            content_blocks: List[Dict[str, Any]] = []
            stop_reason = "end_turn"

            if isinstance(data, dict):
                choices = data.get("choices") or []
                message = choices[0].get("message", {}) if choices else {}
                finish_reason = choices[0].get("finish_reason") if choices else None

                reasoning = message.get("reasoning_content") or message.get("reasoning")
                text = message.get("content")

                if reasoning:
                    content_blocks.append({"type": "thinking", "thinking": reasoning})
                if text:
                    content_blocks.append({"type": "text", "text": text})

                for tool_call in message.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function_part = tool_call.get("function") or {}
                    name = function_part.get("name") or ""
                    args_raw = function_part.get("arguments") or "{}"
                    try:
                        args_obj = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except Exception:
                        args_obj = {"_raw": args_raw}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                            "name": name,
                            "input": args_obj if isinstance(args_obj, dict) else {"value": args_obj},
                        }
                    )

                mapped = map_openai_finish_to_anthropic_stop(finish_reason)
                if mapped:
                    stop_reason = mapped
                if any(block.get("type") == "tool_use" for block in content_blocks):
                    stop_reason = "tool_use"
            else:
                content_blocks.append({"type": "text", "text": response.text})

            if not content_blocks:
                content_blocks.append({"type": "text", "text": ""})

            openai_usage = data.get("usage", {}) if isinstance(data, dict) else {}
            anthropic_usage = openai_usage_to_anthropic_usage(openai_usage)

            return {
                "id": request_id,
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "model": model,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": anthropic_usage,
            }
