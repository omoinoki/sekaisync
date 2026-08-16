# MCP Streamable HTTP (Protocol 2)

English | [中文](README.zh-CN.md)

Streamable HTTP transport via the `/mcp` endpoint of `serve-http`. Use it when the target Agent can only fill in a public HTTPS URL (web / cloud Agents). Local desktop / CLI Agents should prefer `mcp-stdio` — do not trade away the zero-exposure advantage of a local process for uniformity.

## Start

```powershell
python -m sekaisync serve-http --host 0.0.0.0 --port 8787
```

Then expose `http://127.0.0.1:8787/mcp` as `https://your-host/mcp` through an HTTPS tunnel / reverse proxy.

## Files

- `chatgpt-work.md`: behaviour instructions for ChatGPT Work / ChatGPT Web.
- `developer-mode.md`: steps to register a custom MCP app via Developer Mode.
