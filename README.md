```markdown
# 🔄 Prompt Cache Gateway

[English](#english) | [中文](#中文)

---

## English

A lightweight reverse-proxy gateway that sits between your AI coding client and any upstream LLM API. It **transparently forwards** all requests and responses, only injecting **local prompt-cache statistics** into the `usage` field — so tools like Cline, Cursor, and aider display accurate cache hit/miss metrics.

### Features

- **Dual-protocol support** — OpenAI `/v1/chat/completions` + Anthropic Claude `/v1/messages`
- **Zero request mutation** — requests are forwarded to upstream as-is; responses are returned as-is
- **Only `usage` is rewritten** — local prompt-cache stats (cache_read / cache_creation) are injected
- **SQLite-backed prefix cache** — deterministic `sha256(messages[:n])` hashing, TTL auto-expiry, hit-count tracking
- **Streaming + non-streaming** — full SSE passthrough with surgical usage replacement
- **Pure-proxy mode** — pass `--no-cache` to disable caching entirely (transparent passthrough)
- **Management endpoints** — `/cache/stats`, `/cache/clear`, `/health`

### How It Works

```

Client (Cline / Cursor / aider / …)

│

▼

┌──────────────────────────────┐

│   Prompt Cache Gateway :8000 │

│                              │

│   POST /v1/chat/completions  │ ← OpenAI format

│   POST /v1/messages          │ ← Claude format

│   GET  /v1/models            │ ← passthrough

│   GET  /health               │

│   GET  /cache/stats          │

│   POST /cache/clear          │

│                              │

│   ┌────────────────────────┐ │

│   │ PromptCache (SQLite)   │ │

│   │ hash(msgs) → tokens    │ │

│   └────────────────────────┘ │

└─────────────┬────────────────┘

│ forward as-is

▼

Upstream LLM API

(UPSTREAM_BASE_URL)

```

1. Client sends a request → Gateway computes `hash(messages[:-1])` prefix lookup
2. Request is forwarded **unchanged** to the upstream API
3. Upstream response is returned **unchanged**, except the `usage` object is replaced with local cache stats
4. After a successful response, `hash(messages)` → `token_count` is stored for future prefix matching

### Cache Algorithm

| Step | Detail |
|------|--------|
| Hash | `sha256` of normalized `messages` (role + content + tool_calls, JSON-sorted) |
| Lookup | From `messages[:-1]` down to `messages[:max(1, len-10)]`, find longest cached prefix |
| Hit | `cache_read = cached_tokens`, `cache_creation = total - cached` |
| Miss | `cache_creation = total` (if ≥ min_tokens threshold) |
| Store | After each successful response, store `hash(messages) → token_count` |
| TTL | Default 300s (5 min), refreshed on hit (matches Claude ephemeral behavior) |
| Threshold | Default 1024 tokens minimum (matches OpenAI) |

### Quick Start

```

# Install dependencies

pip install fastapi uvicorn httpx tiktoken

# Start the gateway (point to your upstream API)

python cache_[gateway.py](http://gateway.py) --upstream http://your-model-api:8080

# Custom port + cache config

python cache_[gateway.py](http://gateway.py) \

--port 9000 \

--upstream https://api.openai.com \

--cache-ttl 600 \

--cache-min-tokens 2048

# Pure proxy mode (no caching)

python cache_[gateway.py](http://gateway.py) --upstream http://localhost:11434 --no-cache

```

### Configuration

#### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `0.0.0.0` | Listen host |
| `--port` | `8000` | Listen port |
| `--upstream` | `http://localhost:11434` | Upstream LLM API base URL |
| `--timeout` | `300` | Upstream request timeout (seconds) |
| `--cache-db` | `./prompt_cache.db` | SQLite cache database path |
| `--cache-ttl` | `300` | Cache TTL in seconds |
| `--cache-min-tokens` | `1024` | Minimum token threshold for caching |
| `--no-cache` | `false` | Disable caching (pure proxy) |

#### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UPSTREAM_BASE_URL` | `http://localhost:11434` | Upstream API address |
| `CACHE_DB_PATH` | `./prompt_cache.db` | Cache database path |
| `CACHE_TTL` | `300` | Cache expiry (seconds) |
| `CACHE_MIN_TOKENS` | `1024` | Minimum cache threshold |
| `UPSTREAM_TIMEOUT` | `300` | Upstream timeout (seconds) |

### Client Setup

Point your client's API Base URL to the gateway:

```

# Before

https://api.openai.com

# After

http://localhost:8000

```

Works with: **Cline**, **Cursor**, **aider**, **Open Interpreter**, **ChatGPT-Next-Web**, and any OpenAI/Claude-compatible client.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI proxy (stream + batch) |
| `POST` | `/v1/messages` | Claude proxy (stream + batch) |
| `GET` | `/v1/models` | Passthrough to upstream |
| `GET` | `/health` | Health check |
| `GET` | `/cache/stats` | Cache statistics |
| `POST` | `/cache/clear` | Clear all cached prefixes |

### License

MIT

---

## 中文

一个轻量级反向代理网关，部署在 AI 编程客户端和上游 LLM API 之间。它**透明转发**所有请求和响应，仅在 `usage` 字段中注入**本地 prompt cache 统计**——让 Cline、Cursor、aider 等工具正确显示缓存命中/未命中指标。

### 特性

- **双协议支持** — OpenAI `/v1/chat/completions` + Anthropic Claude `/v1/messages`
- **零请求修改** — 请求原封不动转发到上游；响应原封不动返回客户端
- **仅重写 `usage`** — 注入本地 prompt cache 统计（cache_read / cache_creation）
- **SQLite 前缀缓存** — 确定性 `sha256(messages[:n])` 哈希，TTL 自动过期，命中计数
- **流式 + 非流式** — 完整 SSE 透传，精准替换 usage
- **纯透传模式** — 传入 `--no-cache` 完全禁用缓存（纯透明代理）
- **管理接口** — `/cache/stats`、`/cache/clear`、`/health`

### 工作原理

```

客户端 (Cline / Cursor / aider / …)

│

▼

┌──────────────────────────────┐

│   Prompt Cache Gateway :8000 │

│                              │

│   POST /v1/chat/completions  │ ← OpenAI 格式

│   POST /v1/messages          │ ← Claude 格式

│   GET  /v1/models            │ ← 透传

│   GET  /health               │

│   GET  /cache/stats          │

│   POST /cache/clear          │

│                              │

│   ┌────────────────────────┐ │

│   │ PromptCache (SQLite)   │ │

│   │ hash(msgs) → tokens    │ │

│   └────────────────────────┘ │

└─────────────┬────────────────┘

│ 原封不动转发

▼

上游模型 API

(UPSTREAM_BASE_URL)

```

1. 客户端发送请求 → 网关计算 `hash(messages[:-1])` 前缀查找
2. 请求**不做任何修改**转发到上游 API
3. 上游响应**不做任何修改**返回客户端，仅替换 `usage` 对象为本地缓存统计
4. 成功响应后，存储 `hash(messages)` → `token_count` 供后续前缀匹配

### 缓存算法

| 步骤 | 详情 |
|------|------|
| 哈希 | 对规范化的 `messages`（role + content + tool_calls，JSON 排序）取 `sha256` |
| 查找 | 从 `messages[:-1]` 向下到 `messages[:max(1, len-10)]`，找最长已缓存前缀 |
| 命中 | `cache_read = 缓存 token 数`，`cache_creation = 总数 - 缓存数` |
| 未命中 | `cache_creation = 总 token 数`（需达到最小 token 门槛）|
| 存储 | 每次成功响应后，存储 `hash(messages)` → `token_count` |
| TTL | 默认 300 秒（5 分钟），命中时自动刷新（与 Claude ephemeral 行为一致）|
| 门槛 | 默认最少 1024 token（与 OpenAI 一致）|

### 快速开始

```

# 安装依赖

pip install fastapi uvicorn httpx tiktoken

# 启动网关（指向你的上游 API）

python cache_[gateway.py](http://gateway.py) --upstream http://your-model-api:8080

# 自定义端口 + 缓存配置

python cache_[gateway.py](http://gateway.py) \

--port 9000 \

--upstream https://api.openai.com \

--cache-ttl 600 \

--cache-min-tokens 2048

# 纯透传模式（不做缓存）

python cache_[gateway.py](http://gateway.py) --upstream http://localhost:11434 --no-cache

```

### 配置

#### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8000` | 监听端口 |
| `--upstream` | `http://localhost:11434` | 上游 LLM API 地址 |
| `--timeout` | `300` | 上游请求超时（秒）|
| `--cache-db` | `./prompt_cache.db` | SQLite 缓存数据库路径 |
| `--cache-ttl` | `300` | 缓存 TTL（秒）|
| `--cache-min-tokens` | `1024` | 最小缓存 token 门槛 |
| `--no-cache` | `false` | 禁用缓存（纯代理模式）|

#### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UPSTREAM_BASE_URL` | `http://localhost:11434` | 上游 API 地址 |
| `CACHE_DB_PATH` | `./prompt_cache.db` | 缓存数据库路径 |
| `CACHE_TTL` | `300` | 缓存过期秒数 |
| `CACHE_MIN_TOKENS` | `1024` | 最小缓存门槛 |
| `UPSTREAM_TIMEOUT` | `300` | 上游超时秒数 |

### 客户端配置

将客户端的 API Base URL 指向本网关即可：

```

# 原来

https://api.openai.com

# 改为

http://localhost:8000

```

兼容：**Cline**、**Cursor**、**aider**、**Open Interpreter**、**ChatGPT-Next-Web** 及所有 OpenAI/Claude 兼容客户端。

### API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | OpenAI 代理（流式 + 非流式）|
| `POST` | `/v1/messages` | Claude 代理（流式 + 非流式）|
| `GET` | `/v1/models` | 透传到上游 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/cache/stats` | 缓存统计 |
| `POST` | `/cache/clear` | 清空缓存 |

### 开源协议

MIT
```
