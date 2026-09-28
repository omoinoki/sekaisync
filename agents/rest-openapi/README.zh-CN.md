# REST + OpenAPI（协议 3）

[English](README.md) | 中文

基础 HTTP JSON API，提供 `serve-http` 服务的 `/api/v1/*` 路由与 `/openapi.json` 规范文档。该路径为功能受限的回退方案，仅用于仅支持 OpenAPI Actions 或基础 REST API 的智能体；常规环境建议优先使用 `mcp-stdio` 或 `mcp-http`。

## 启动

```powershell
python -m sekaisync serve-http --host 127.0.0.1 --port 8787
```

- OpenAPI 描述文档：`http://127.0.0.1:8787/openapi.json`
- 核心查询端点：`/api/v1/lookup`、`/api/v1/resolve`、`/api/v1/fact_pack`、
  `/api/v1/freshness`、`/api/v1/verify_claims`、`/api/v1/web_lookup` 等。

## 文件

- `openapi.json`：面向 ChatGPT Actions 的 OpenAPI 3.1 规范子集（涵盖 `lookup`、`fact_pack`、`freshness`、`verify_claims` 与 `web_lookup`）。
