# REST + OpenAPI（协议 3）

[English](README.md) | 中文

裸 HTTP JSON API，覆盖 `serve-http` 的 `/api/v1/*` 路由与 `/openapi.json` 文档。这是能力最弱的回退路径，只用于只支持 OpenAPI Actions 或裸 REST 的 Agent；优先用 `mcp-stdio` 或 `mcp-http`。

## 启动

```powershell
python -m sekaisync serve-http --host 127.0.0.1 --port 8787
```

- OpenAPI 文档：`http://127.0.0.1:8787/openapi.json`
- 查询端点：`/api/v1/lookup`、`/api/v1/resolve`、`/api/v1/fact_pack`、
  `/api/v1/freshness`、`/api/v1/verify_claims`、`/api/v1/web_lookup` 等。

## 文件

- `openapi.json`：ChatGPT Actions 用的 OpenAPI 3.1 子集（`lookup` / `fact_pack` /
  `freshness` / `verify_claims` / `web_lookup`）。
