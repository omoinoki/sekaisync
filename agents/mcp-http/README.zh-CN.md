# MCP Streamable HTTP（协议 2）

[English](README.md) | 中文

Streamable HTTP 传输，走 `serve-http` 的 `/mcp` 端点。目标 Agent 只能填一个公网 HTTPS URL 时用它（Web / 云端 Agent）。本地桌面/CLI Agent 应优先走 `mcp-stdio`，不要为了统一而牺牲本地进程的零暴露优势。

## 启动

```powershell
python -m sekaisync serve-http --host 0.0.0.0 --port 8787
```

再把 `http://127.0.0.1:8787/mcp` 通过 HTTPS 隧道/反代暴露为 `https://your-host/mcp`。

## 文件

- `chatgpt-work.md`：ChatGPT Work / ChatGPT Web 的行为指令。
- `developer-mode.md`：通过 Developer Mode 注册自定义 MCP App 的步骤。
