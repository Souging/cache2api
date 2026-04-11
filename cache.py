#!/usr/bin/env python3
"""
Prompt Cache Gateway — 通用缓存代理网关

支持:
  POST /v1/chat/completions   (OpenAI 格式)
  POST /v1/messages           (Claude 格式)
  GET  /v1/models             (透传)
  GET  /health                (健康检查)
  GET  /cache/stats           (缓存统计)
  POST /cache/clear           (清空缓存)

核心逻辑:
  - 请求原封不动转发到上游 UPSTREAM_BASE_URL
  - 响应原封不动返回客户端
  - 仅修改 usage 字段，注入本地 prompt cache 统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import time
import math
import uuid
from typing import AsyncGenerator, List, Dict, Any

import httpx
import tiktoken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ============================================================
# 日志配置
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cache_gateway.log", mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("cache_gateway")

# ============================================================
# 配置项
# ============================================================

# 上游模型 API 地址（客户端请求原封不动转发到这里）
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "http://209.127.114.116:32001")

# 缓存相关
CACHE_DB_PATH = os.environ.get("CACHE_DB_PATH", "./prompt_cache.db")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))           # 默认 5 分钟
CACHE_MIN_TOKENS = int(os.environ.get("CACHE_MIN_TOKENS", "1024"))  # 最低 1024

# 上游请求超时
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "300"))

# tiktoken 编码
try:
    _ENCODING = tiktoken.get_encoding("o200k_base")
except Exception:
    _ENCODING = tiktoken.get_encoding("cl100k_base")


# ============================================================
# Token 计算工具
# ============================================================

def _count_text_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_ENCODING.encode(text, allowed_special="all"))


def _count_content_tokens(content) -> int:
    """
    计算消息 content 字段的 token 数。
    content 可能是 str、list[dict]、或 None。
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return _count_text_tokens(content)
    if isinstance(content, list):
        tokens = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                tokens += _count_text_tokens(part.get("text", ""))
            elif ptype == "image_url" or ptype == "image":
                detail = part.get("image_url", {}).get("detail", "auto")
                tokens += 85 if detail == "low" else 170
            elif ptype in ("tool_use", "tool_result"):
                tokens += _count_text_tokens(
                    json.dumps(part, ensure_ascii=False)
                )
        return tokens
    return 0


def _count_tool_calls_tokens(tool_calls: list) -> int:
    if not tool_calls:
        return 0
    tokens = 0
    for tc in tool_calls:
        tokens += 3
        tc_id = tc.get("id", "")
        if tc_id:
            tokens += _count_text_tokens(tc_id)
        fn = tc.get("function", {})
        name = fn.get("name", "")
        if name:
            tokens += _count_text_tokens(name)
        args = fn.get("arguments", "")
        if args:
            if isinstance(args, str):
                tokens += _count_text_tokens(args)
            else:
                tokens += _count_text_tokens(
                    json.dumps(args, ensure_ascii=False)
                )
    return tokens


def num_tokens_from_messages(
    messages: List[Dict[str, Any]],
) -> int:
    """计算 OpenAI 格式 messages 的 token 数"""
    num_tokens = 0
    for message in messages:
        num_tokens += 4  # 每条消息固定开销
        num_tokens += _count_content_tokens(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            num_tokens += _count_tool_calls_tokens(tool_calls)
        role = message.get("role", "user")
        if role == "tool":
            tool_call_id = message.get("tool_call_id", "")
            if tool_call_id:
                num_tokens += _count_text_tokens(tool_call_id)
            tool_name = message.get("name", "")
            if tool_name:
                num_tokens += _count_text_tokens(tool_name)
                num_tokens += 1
        elif "name" in message:
            num_tokens += _count_text_tokens(message["name"])
            num_tokens += 1
    num_tokens += 3  # assistant 前缀
    return num_tokens


def count_request_tokens(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] = None,
) -> int:
    """计算整个请求的输入 token 数（含 tools 定义）"""
    total = num_tokens_from_messages(messages)
    if tools:
        total += 12  # tools namespace 开销
        for tool in tools:
            total += 10
            fn = tool.get("function", tool)
            name = fn.get("name", "")
            if name:
                total += _count_text_tokens(name) * 2
            desc = fn.get("description", "")
            if desc:
                total += _count_text_tokens(desc)
            params = fn.get("parameters")
            if params:
                total += _count_text_tokens(
                    json.dumps(params, ensure_ascii=False)
                )
    return total


# ============================================================
# Claude Messages → 统一 token 计算
# ============================================================

def _claude_messages_to_token_list(
    system: str | list | None,
    messages: List[Dict[str, Any]],
    tools: list | None = None,
) -> int:
    """
    计算 Claude /v1/messages 格式的输入 token 数。
    将 system + messages + tools 统一估算。
    """
    total = 0
    # system
    if isinstance(system, str) and system:
        total += _count_text_tokens(system) + 4
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                total += _count_text_tokens(block.get("text", "")) + 4
    # messages
    for m in messages:
        total += 4
        content = m.get("content")
        if isinstance(content, str):
            total += _count_text_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    total += _count_text_tokens(block.get("text", ""))
                elif btype == "image":
                    total += 170
                elif btype in ("tool_use", "tool_result"):
                    total += _count_text_tokens(
                        json.dumps(block, ensure_ascii=False)
                    )
    total += 3  # assistant 前缀
    # tools
    if tools:
        total += 12
        for t in tools:
            total += 10
            name = t.get("name", "")
            if name:
                total += _count_text_tokens(name) * 2
            desc = t.get("description", "")
            if desc:
                total += _count_text_tokens(desc)
            schema = t.get("input_schema")
            if schema:
                total += _count_text_tokens(
                    json.dumps(schema, ensure_ascii=False)
                )
    return total


# ============================================================
# Prompt Cache（SQLite 持久化）
# ============================================================

class PromptCache:
    """
    模拟 Claude / OpenAI 标准 prompt caching 报数行为。

    核心原理:
      1. 每次请求完成后，存储 hash(messages) → token_count
      2. 下一次请求时，从 messages[:-1] 逐步缩短，找最长已缓存前缀
      3. 命中: cache_read = 缓存 token 数, cache_creation = 新增部分
      4. 未命中: cache_creation = 全部 token 数（达到门槛时）
      5. 命中自动刷新 TTL
    """

    def __init__(
        self,
        db_path: str = CACHE_DB_PATH,
        ttl: int = CACHE_TTL,
        min_tokens: int = CACHE_MIN_TOKENS,
    ):
        self.db_path = db_path
        self.ttl = ttl
        self.min_tokens = min_tokens
        self._stats = {"hits": 0, "misses": 0, "writes": 0}
        self._last_cleanup_at: float = 0.0
        self._cleanup_interval: float = 60.0
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_prefix_cache (
                prefix_hash   TEXT PRIMARY KEY,
                token_count   INTEGER NOT NULL,
                created_at    REAL NOT NULL,
                last_hit_at   REAL NOT NULL,
                hit_count     INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ppc_last_hit
            ON prompt_prefix_cache(last_hit_at)
        """)
        conn.commit()
        conn.close()
        count = self._count()
        log.info(
            f"PromptCache initialized: {count} prefixes, "
            f"TTL={self.ttl}s, min_tokens={self.min_tokens}"
        )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _count(self) -> int:
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM prompt_prefix_cache"
            ).fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _hash_messages(messages: list) -> str:
        """
        对 messages 做确定性哈希。
        提取语义内容: role + content + tool_calls + tool_call_id + name
        """
        normalized = []
        for m in messages:
            entry = {"role": m.get("role", "")}
            content = m.get("content")
            if isinstance(content, str):
                entry["content"] = content if content else None
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            parts.append({
                                "type": "text",
                                "text": p.get("text", ""),
                            })
                        elif p.get("type") == "image_url":
                            url = p.get("image_url", {}).get("url", "")
                            parts.append({
                                "type": "image",
                                "url_hash": hashlib.md5(
                                    url.encode()
                                ).hexdigest()[:16],
                            })
                        elif p.get("type") == "tool_use":
                            parts.append({
                                "type": "tool_use",
                                "id": p.get("id", ""),
                                "name": p.get("name", ""),
                                "input": p.get("input", {}),
                            })
                        elif p.get("type") == "tool_result":
                            parts.append({
                                "type": "tool_result",
                                "tool_use_id": p.get("tool_use_id", ""),
                                "content": p.get("content", ""),
                            })
                        else:
                            parts.append(p)
                entry["content"] = parts
            elif content is None:
                entry["content"] = None
            # tool_calls (OpenAI assistant)
            tc = m.get("tool_calls")
            if tc:
                normalized_tcs = []
                for t in tc:
                    fn = t.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    if isinstance(args_raw, str):
                        try:
                            args_parsed = json.loads(args_raw)
                        except (json.JSONDecodeError, TypeError):
                            args_parsed = args_raw
                    else:
                        args_parsed = args_raw
                    normalized_tcs.append({
                        "id": t.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": args_parsed,
                    })
                entry["tool_calls"] = normalized_tcs
            # tool_call_id (OpenAI tool)
            tcid = m.get("tool_call_id")
            if tcid:
                entry["tool_call_id"] = tcid
                entry["name"] = m.get("name", "")
            normalized.append(entry)

        raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _maybe_cleanup(self, conn):
        now = time.time()
        if now - self._last_cleanup_at < self._cleanup_interval:
            return
        self._last_cleanup_at = now
        cutoff = now - self.ttl
        cur = conn.execute(
            "DELETE FROM prompt_prefix_cache WHERE last_hit_at < ?",
            (cutoff,),
        )
        if cur.rowcount:
            conn.commit()
            log.info(f"[CACHE] cleaned {cur.rowcount} expired prefixes")

    def lookup_best_prefix(self, messages: list) -> dict | None:
        if len(messages) <= 1:
            return None
        now = time.time()
        cutoff = now - self.ttl
        max_lookback = min(len(messages) - 1, 10)
        conn = self._conn()
        try:
            self._maybe_cleanup(conn)
            for i in range(max_lookback):
                prefix_len = len(messages) - 1 - i
                if prefix_len <= 0:
                    break
                prefix = messages[:prefix_len]
                prefix_hash = self._hash_messages(prefix)
                row = conn.execute(
                    "SELECT token_count FROM prompt_prefix_cache "
                    "WHERE prefix_hash = ? AND last_hit_at >= ?",
                    (prefix_hash, cutoff),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE prompt_prefix_cache "
                        "SET last_hit_at = ?, hit_count = hit_count + 1 "
                        "WHERE prefix_hash = ?",
                        (now, prefix_hash),
                    )
                    conn.commit()
                    self._stats["hits"] += 1
                    log.info(
                        f"[CACHE] HIT hash={prefix_hash[:16]}... "
                        f"cached={row[0]} prefix={prefix_len}/{len(messages)}"
                    )
                    return {
                        "token_count": row[0],
                        "prefix_len": prefix_len,
                    }
            self._stats["misses"] += 1
            return None
        finally:
            conn.close()

    def store_prefix(
        self, messages: list, total_tokens: int,
    ):
        if total_tokens < self.min_tokens:
            return
        prefix_hash = self._hash_messages(messages)
        now = time.time()
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO prompt_prefix_cache
                    (prefix_hash, token_count, created_at, last_hit_at, hit_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(prefix_hash) DO UPDATE SET
                    token_count = excluded.token_count,
                    last_hit_at = excluded.last_hit_at
                """,
                (prefix_hash, total_tokens, now, now),
            )
            conn.commit()
            self._stats["writes"] += 1
            log.info(
                f"[CACHE] STORED hash={prefix_hash[:16]}... "
                f"tokens={total_tokens}"
            )
        finally:
            conn.close()

    def calc_usage(
        self, messages: list, total_tokens: int,
    ) -> dict:
        cached = self.lookup_best_prefix(messages)
        if cached:
            cached_tokens = min(cached["token_count"], total_tokens)
            new_tokens = total_tokens - cached_tokens
            return {
                "total_input_tokens": total_tokens,
                "cache_read_input_tokens": cached_tokens,
                "cache_creation_input_tokens": new_tokens,
                "uncached_input_tokens": 0,
            }
        else:
            if total_tokens >= self.min_tokens:
                return {
                    "total_input_tokens": total_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": total_tokens,
                    "uncached_input_tokens": 0,
                }
            else:
                return {
                    "total_input_tokens": total_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "uncached_input_tokens": total_tokens,
                }

    def stats(self) -> dict:
        conn = self._conn()
        try:
            self._maybe_cleanup(conn)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(token_count), 0), "
                "COALESCE(SUM(hit_count), 0) FROM prompt_prefix_cache"
            ).fetchone()
        finally:
            conn.close()
        total_lookups = self._stats["hits"] + self._stats["misses"]
        return {
            "active_prefixes": row[0],
            "total_cached_tokens": row[1],
            "cumulative_hits": row[2],
            "session_hits": self._stats["hits"],
            "session_misses": self._stats["misses"],
            "session_writes": self._stats["writes"],
            "session_hit_ratio": round(
                self._stats["hits"] / total_lookups, 4
            ) if total_lookups > 0 else 0.0,
            "ttl_seconds": self.ttl,
            "min_tokens": self.min_tokens,
        }

    def clear(self):
        conn = self._conn()
        try:
            conn.execute("DELETE FROM prompt_prefix_cache")
            conn.commit()
        finally:
            conn.close()
        self._stats = {"hits": 0, "misses": 0, "writes": 0}
        log.info("[CACHE] cleared all prefixes")


# ============================================================
# Usage 构建（兼容 OpenAI + Claude）
# ============================================================

def build_openai_usage(
    cache_info: dict | None,
    upstream_usage: dict | None,
) -> dict:
    """
    合并上游 usage + 本地缓存计算，输出 OpenAI 格式。
    上游 usage 中的 completion_tokens 保留，prompt_tokens 被缓存覆盖。
    """
    output_tokens = 0
    if upstream_usage:
        output_tokens = upstream_usage.get("completion_tokens", 0)

    if not cache_info:
        return upstream_usage or {}

    total_input = cache_info["total_input_tokens"]
    cache_read = cache_info["cache_read_input_tokens"]
    cache_creation = cache_info["cache_creation_input_tokens"]

    return {
        "prompt_tokens": total_input,
        "completion_tokens": output_tokens,
        "total_tokens": total_input + output_tokens,
        "prompt_tokens_details": {
            "cached_tokens": cache_read,
        },
        "completion_tokens_details": {
            "reasoning_tokens": upstream_usage.get(
                "completion_tokens_details", {}
            ).get("reasoning_tokens", 0) if upstream_usage else 0,
            "accepted_prediction_tokens": 0,
            "rejected_prediction_tokens": 0,
        },
        # Claude 兼容字段
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "prompt_cache_hit_tokens": cache_read,
    }


def build_claude_usage(
    cache_info: dict | None,
    upstream_usage: dict | None,
) -> dict:
    """
    合并上游 usage + 本地缓存计算，输出 Claude 格式。
    """
    output_tokens = 0
    log.info(f"[CACHE] upstream_usage:{upstream_usage}")
    if upstream_usage:
        output_tokens = upstream_usage.get("output_tokens", 0)

    if not cache_info:
        return upstream_usage or {}

    total_input = cache_info["total_input_tokens"]
    cache_read = cache_info["cache_read_input_tokens"]
    cache_creation = cache_info["cache_creation_input_tokens"]
    if cache_read  == 0 and cache_creation == 0:
        total_input = total_input 
    else:
        total_input = total_input - cache_creation - cache_read 
        total_input = max(total_input, 1)
    return {
        "input_tokens": total_input,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": math.floor(cache_creation),
        "cache_read_input_tokens": math.floor(cache_read)
    }


# ============================================================
# 全局实例
# ============================================================

prompt_cache: PromptCache | None = None
DEBUG_REQ = 0

# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(title="Prompt Cache Gateway")


# -------------------- 健康检查 --------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "upstream": UPSTREAM_BASE_URL,
        "cache_enabled": prompt_cache is not None,
    }


# -------------------- 缓存管理 --------------------

@app.get("/cache/stats")
async def api_cache_stats():
    if not prompt_cache:
        return JSONResponse({"error": "cache not initialized"}, 500)
    return prompt_cache.stats()


@app.post("/cache/clear")
async def api_cache_clear():
    if not prompt_cache:
        return JSONResponse({"error": "cache not initialized"}, 500)
    prompt_cache.clear()
    return {"ok": True}


# -------------------- 透传 /v1/models --------------------

@app.get("/v1/models")
async def proxy_models():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{UPSTREAM_BASE_URL}/v1/models",
            timeout=30,
        )
        return JSONResponse(r.json(), status_code=r.status_code)


# ============================================================
# OpenAI /v1/chat/completions
# ============================================================

@app.post("/v1/chat/completions")
async def proxy_openai_chat(req: Request):
    global DEBUG_REQ
    DEBUG_REQ += 1
    req_id = f"OAI-{DEBUG_REQ}"

    body = await req.json()
    messages = body.get("messages", [])
    tools = body.get("tools") or None
    stream = body.get("stream", False)
    model = body.get("model", "unknown")

    log.info(
        f"[{req_id}] /v1/chat/completions "
        f"model={model} stream={stream} msgs={len(messages)}"
    )

    # ===== 缓存计算 =====
    total_tokens = count_request_tokens(messages, tools=tools)
    cache_info = (
        prompt_cache.calc_usage(messages, total_tokens)
        if prompt_cache else None
    )
    if cache_info:
        log.info(
            f"[{req_id}] cache: total={cache_info['total_input_tokens']} "
            f"read={cache_info['cache_read_input_tokens']} "
            f"creation={cache_info['cache_creation_input_tokens']}"
        )

    # ===== 获取客户端原始 headers 中的 Authorization =====
    auth_header = req.headers.get("authorization", "")
    headers = {
        "Content-Type": "application/json",
        "Accept": req.headers.get("accept", "application/json"),
    }
    if auth_header:
        headers["Authorization"] = auth_header

    upstream_url = f"{UPSTREAM_BASE_URL}/v1/chat/completions"

    if stream:
        return StreamingResponse(
            _stream_openai(req_id, upstream_url, headers, body,
                           messages, tools, cache_info),
            media_type="text/event-stream",
        )
    else:
        return await _batch_openai(
            req_id, upstream_url, headers, body,
            messages, tools, cache_info,
        )


async def _stream_openai(
    req_id: str,
    upstream_url: str,
    headers: dict,
    body: dict,
    messages: list,
    tools: list | None,
    cache_info: dict | None,
) -> AsyncGenerator[str, None]:
    """
    流式转发: 逐行 SSE 透传，仅在最后一个 chunk 替换 usage。
    """
    client = httpx.AsyncClient()
    try:
        async with client.stream(
            "POST", upstream_url,
            json=body,
            headers=headers,
            timeout=UPSTREAM_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                yield f"data: {json.dumps({'error': {'message': text.decode()[:500], 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
                return

            async for line in resp.aiter_lines():
                if not line:
                    continue

                # 透传非 data 行
                if not line.startswith("data:"):
                    yield line + "\n\n"
                    continue

                raw = line[5:].strip()
                if raw == "[DONE]":
                    # 存缓存
                    if prompt_cache and cache_info:
                        prompt_cache.store_prefix(
                            messages,
                            cache_info["total_input_tokens"],
                        )
                    yield "data: [DONE]\n\n"
                    break

                # 尝试解析并替换 usage
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    yield line + "\n\n"
                    continue

                if "usage" in chunk and cache_info:
                    log.info(f"778: {chunk['usage']}")
                    chunk["usage"] = build_openai_usage(
                        cache_info, chunk.get("usage")
                    )
                    log.info(f"782: {chunk['usage']}")
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                else:
                    yield line + "\n\n"

    except Exception as e:
        log.error(f"[{req_id}] stream error: {e}")
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'proxy_error'}}, ensure_ascii=False)}\n\n"
    finally:
        await client.aclose()


async def _batch_openai(
    req_id: str,
    upstream_url: str,
    headers: dict,
    body: dict,
    messages: list,
    tools: list | None,
    cache_info: dict | None,
) -> JSONResponse:
    """非流式转发: 等待完整响应，替换 usage 后返回。"""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                upstream_url,
                json=body,
                headers=headers,
                timeout=UPSTREAM_TIMEOUT,
            )
        except Exception as e:
            log.error(f"[{req_id}] upstream error: {e}")
            return JSONResponse(
                {"error": {"message": str(e), "type": "proxy_error"}},
                502,
            )

    if r.status_code != 200:
        return JSONResponse(r.json(), status_code=r.status_code)

    data = r.json()

    # 替换 usage
    if cache_info:
        
        log.info(f"827: {data['usage']}")
        data["usage"] = build_openai_usage(
            cache_info, data.get("usage")
        )
        log.info(f"831: {data['usage']}")

    # 存缓存
    if prompt_cache and cache_info:
        prompt_cache.store_prefix(
            messages, cache_info["total_input_tokens"]
        )

    log.info(f"[{req_id}] done (non-stream)")
    return JSONResponse(data)


# ============================================================
# Claude /v1/messages
# ============================================================

@app.post("/v1/messages")
async def proxy_claude_messages(req: Request):
    global DEBUG_REQ
    DEBUG_REQ += 1
    req_id = f"CLD-{DEBUG_REQ}"

    body = await req.json()
    messages = body.get("messages", [])
    system = body.get("system")
    tools = body.get("tools") or None
    stream = body.get("stream", False)
    model = body.get("model", "unknown")

    log.info(
        f"[{req_id}] /v1/messages "
        f"model={model} stream={stream} msgs={len(messages)}"
    )

    # ===== 缓存计算 =====
    total_tokens = _claude_messages_to_token_list(
        system, messages, tools
    )
    cache_info = (
        prompt_cache.calc_usage(messages, total_tokens)
        if prompt_cache else None
    )
    if cache_info:
        log.info(
            f"[{req_id}] cache: total={cache_info['total_input_tokens']} "
            f"read={cache_info['cache_read_input_tokens']} "
            f"creation={cache_info['cache_creation_input_tokens']}"
        )

    # ===== Headers =====
    headers = {"Content-Type": "application/json"}
    for key in (
        "authorization", "anthropic-version",
        "anthropic-beta", "x-api-key",
    ):
        val = req.headers.get(key)
        if val:
            headers[key] = val

    upstream_url = f"{UPSTREAM_BASE_URL}/v1/messages"

    if stream:
        return StreamingResponse(
            _stream_claude(req_id, upstream_url, headers, body,
                           messages, tools, cache_info),
            media_type="text/event-stream",
        )
    else:
        return await _batch_claude(
            req_id, upstream_url, headers, body,
            messages, tools, cache_info,
        )


async def _stream_claude(
    req_id: str,
    upstream_url: str,
    headers: dict,
    body: dict,
    messages: list,
    tools: list | None,
    cache_info: dict | None,
) -> AsyncGenerator[str, None]:
    """
    流式转发 Claude SSE，在 message_start 和 message_delta
    事件中替换 usage 字段。
    """
    client = httpx.AsyncClient()
    try:
        async with client.stream(
            "POST", upstream_url,
            json=body,
            headers=headers,
            timeout=UPSTREAM_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                yield f"data: {json.dumps({'type': 'error', 'error': {'message': text.decode()[:500]}}, ensure_ascii=False)}\n\n"
                return

            event_type = ""
            async for line in resp.aiter_lines():
                if not line:
                    yield "\n"
                    continue

                # event: xxx
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    yield line + "\n"
                    continue

                if not line.startswith("data:"):
                    yield line + "\n"
                    continue

                raw = line[5:].strip()

                # 解析 JSON 并按需修改 usage
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    yield line + "\n"
                    continue

                if cache_info:
                    # message_start → data.message.usage
                    if (
                        event_type == "message_start"
                        and "message" in data
                        and "usage" in data["message"]
                    ):
                        log.info(f"960: {data['message']['usage']}")
                        data["message"]["usage"] = build_claude_usage(
                            cache_info,
                            data["message"].get("usage"),
                        )
                        log.info(f"967: {data['message']['usage']}")
                    # message_delta → data.usage
                    if (
                        event_type == "message_delta"
                        and "usage" in data
                    ):
                        
                        log.info(f"973: {data['usage']}")
                        output_tokens = data["usage"].get("output_tokens", 0)
                        data["usage"] = {"output_tokens": output_tokens,
                            "cache_creation_input_tokens":
                                cache_info["cache_creation_input_tokens"],
                            "cache_read_input_tokens":
                                cache_info["cache_read_input_tokens"],
                        }
                        log.info(f"981: {data['usage']}")

                yield f"data: {json.dumps(data, ensure_ascii=False)}\n"
                event_type = ""  # reset

        # 存缓存
        if prompt_cache and cache_info:
            prompt_cache.store_prefix(
                messages, cache_info["total_input_tokens"]
            )

    except Exception as e:
        log.error(f"[{req_id}] stream error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(e)}}, ensure_ascii=False)}\n\n"
    finally:
        await client.aclose()


async def _batch_claude(
    req_id: str,
    upstream_url: str,
    headers: dict,
    body: dict,
    messages: list,
    tools: list | None,
    cache_info: dict | None,
) -> JSONResponse:
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                upstream_url,
                json=body,
                headers=headers,
                timeout=UPSTREAM_TIMEOUT,
            )
        except Exception as e:
            log.error(f"[{req_id}] upstream error: {e}")
            return JSONResponse(
                {"type": "error", "error": {"message": str(e)}},
                502,
            )

    if r.status_code != 200:
        return JSONResponse(r.json(), status_code=r.status_code)

    data = r.json()

    if cache_info and "usage" in data:
        log.info(f"1029: {data['usage']}")
        data["usage"] = build_claude_usage(
            cache_info, data.get("usage")
        )
        log.info(f"1033: {data['usage']}")

    if prompt_cache and cache_info:
        prompt_cache.store_prefix(
            messages, cache_info["total_input_tokens"]
        )

    log.info(f"[{req_id}] done (non-stream)")
    return JSONResponse(data)


# ============================================================
# Main
# ============================================================

def main():
    global prompt_cache, UPSTREAM_BASE_URL, UPSTREAM_TIMEOUT

    parser = argparse.ArgumentParser(
        description="Prompt Cache Gateway"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--upstream",
        default=UPSTREAM_BASE_URL,
        help="上游模型 API base URL",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="上游超时(秒)",
    )
    parser.add_argument(
        "--cache-db",
        default=CACHE_DB_PATH,
        help="缓存 SQLite DB 路径",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=CACHE_TTL,
        help="缓存 TTL(秒)",
    )
    parser.add_argument(
        "--cache-min-tokens",
        type=int,
        default=CACHE_MIN_TOKENS,
        help="最小缓存 token 门槛",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用缓存（纯透传模式）",
    )
    args = parser.parse_args()

    UPSTREAM_BASE_URL = args.upstream
    UPSTREAM_TIMEOUT = args.timeout

    if not args.no_cache:
        prompt_cache = PromptCache(
            db_path=args.cache_db,
            ttl=args.cache_ttl,
            min_tokens=args.cache_min_tokens,
        )

    import uvicorn
    log.info(f"Prompt Cache Gateway 启动")
    log.info(f"  Listen:    http://{args.host}:{args.port}")
    log.info(f"  Upstream:  {UPSTREAM_BASE_URL}")
    log.info(f"  Timeout:   {UPSTREAM_TIMEOUT}s")
    log.info(f"  OpenAI:    POST /v1/chat/completions")
    log.info(f"  Claude:    POST /v1/messages")
    log.info(f"  Models:    GET  /v1/models")
    if prompt_cache:
        log.info(
            f"  Cache:     TTL={prompt_cache.ttl}s "
            f"min={prompt_cache.min_tokens}tok"
        )
    else:
        log.info(f"  Cache:     DISABLED (pure proxy)")
    uvicorn.run(
        app, host=args.host, port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
