# REST + OpenAPI (Protocol 3)

English | [中文](README.zh-CN.md)

Plain HTTP JSON API covering the `/api/v1/*` routes and the `/openapi.json` document of `serve-http`. This is the lowest-capability fallback, only for Agents that support OpenAPI Actions or plain REST; prefer `mcp-stdio` or `mcp-http`.

## Start

```powershell
python -m sekaisync serve-http --host 127.0.0.1 --port 8787
```

- OpenAPI document: `http://127.0.0.1:8787/openapi.json`
- Query endpoints: `/api/v1/lookup`, `/api/v1/resolve`, `/api/v1/fact_pack`,
  `/api/v1/freshness`, `/api/v1/verify_claims`, `/api/v1/web_lookup`, etc.

## Files

- `openapi.json`: an OpenAPI 3.1 subset for ChatGPT Actions (`lookup` / `fact_pack` /
  `freshness` / `verify_claims` / `web_lookup`).
