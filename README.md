## 概述

从原始 Accio ADK Proxy 中提取 **Prompt Cache** 逻辑，制作一个**纯缓存代理网关**。

> 本网关**不做任何请求转换**，只负责：
1. 接收客户端请求（OpenAI / Claude 格式）
2. 计算 prompt cache 命中情况
3. 将请求**原封不动**转发到上游模型 API
4. 将上游响应**原封不动**返回客户端，仅修改 `usage` 字段注入缓存统计
> 

---

## 架构图

```
客户端 (Cline/Cursor/etc.)
    │
    ▼
┌─────────────────────────────┐
│  Prompt Cache Gateway       │
│  :8000                      │
│                             │
│  POST /v1/chat/completions  │  ← OpenAI 格式
│  POST /v1/messages          │  ← Claude 格式
│  GET  /v1/models            │  ← 透传
│  GET  /health               │
│  GET  /cache/stats          │
│  POST /cache/clear          │
│                             │
│  ┌───────────────────────┐  │
│  │ PromptCache (SQLite)  │  │
│  │ hash(messages)→tokens │  │
│  └───────────────────────┘  │
└────────────┬────────────────┘
             │  原封不动转发
             ▼
     上游模型 API
     (UPSTREAM_BASE_URL)
```

---




## 使用方法

### 安装依赖

```bash
pip install fastapi uvicorn httpx tiktoken
```

### 启动网关

```bash
# 基本用法：指向你的上游 API
python cache_gateway.py --upstream http://your-model-api:8080

# 自定义端口 + 缓存配置
python cache_gateway.py \
    --port 9000 \
    --upstream https://api.openai.com \
    --cache-ttl 600 \
    --cache-min-tokens 2048

# 纯透传模式（不做缓存）
python cache_gateway.py --upstream http://localhost:11434 --no-cache
```

### 环境变量（可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | `http://localhost:11434` | 上游 API 地址 |
| `CACHE_DB_PATH` | `./prompt_cache.db` | 缓存数据库路径 |
| `CACHE_TTL` | `300` | 缓存过期秒数 |
| `CACHE_MIN_TOKENS` | `1024` | 最小缓存门槛 |
| `UPSTREAM_TIMEOUT` | `300` | 上游超时秒数 |

### 客户端配置

将客户端（Cline / Cursor / aider 等）的 API Base URL 指向本网关即可：

```
# 原来指向
https://api.openai.com

# 改为指向网关
http://localhost:8000
```

---

## 与原始代码的区别

| 对比项 | 原始 Accio Proxy | 本网关 |
| --- | --- | --- |
| 请求转换 | OpenAI → ADK 格式 | ❌ 不做转换，原封不动 |
| Cookie 池 | ✅ 内置 | ❌ 已移除 |
| 上游协议 | ADK 私有协议 | 标准 OpenAI / Claude API |
| 修改范围 | 整个请求/响应 | **仅 usage 字段** |
| 支持格式 | OpenAI 格式 | OpenAI + Claude 格式 |
| 缓存逻辑 | ✅ 相同 | ✅ 相同（提取并独立） |
