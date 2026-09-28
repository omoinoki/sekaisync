# MCP Streamable HTTP（协议 2）

[English](README.md) | 中文

基于 Streamable HTTP 协议传输，使用 `serve-http` 服务的 `/mcp` 端点。适用于仅支持通过公网 HTTPS URL 接入的环境（如 Web 或云端智能体）。对于本地桌面与 CLI 智能体，建议优先采用 `mcp-stdio`，以保持本地进程无需暴露网络端口的安全性。

## 启动

```powershell
python -m sekaisync serve-http --host 0.0.0.0 --port 8787
```

随后通过 HTTPS 隧道或反向代理，将 `http://127.0.0.1:8787/mcp` 映射为公网可访问的 `https://your-host/mcp`。

## 文件

- `chatgpt-work.md`：ChatGPT Work / ChatGPT Web 的行为指导提示词。
- `developer-mode.md`：通过 Developer Mode 注册自定义 MCP App 的操作步骤。
