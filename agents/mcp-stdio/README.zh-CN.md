# MCP stdio（协议 1）

[English](README.md) | 中文

本地进程传输。目标 Agent 能直接拉起一个子进程时用它：零网络、零公网暴露，是最推荐的接入方式。

## 启动命令

```powershell
python -m sekaisync serve-mcp
```

工作目录是包含 `settings.json` 与 `store/` 的检出目录。

## 配置模板（按平台文件名取用）

| 文件 | 目标平台 | 落地位置 |
| --- | --- | --- |
| `codex.config.toml` | Codex CLI / ChatGPT 桌面 App / IDE | `~/.codex/config.toml`（全局）或 `.codex/config.toml`（项目） |
| `claude-code.mcp.json` | Claude Code | 项目 `.mcp.json`，或 `claude mcp add sekaisync ...` |
| `claude-desktop.json` | Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| `cursor.mcp.json` | Cursor | 项目 `.cursor/mcp.json`，或 `~/.cursor/mcp.json` |
| `grok.config.toml` | Grok Build | `~/.grok/config.toml` 或 `.grok/config.toml` |
| `grok.mcp.json` | Grok Build（兼容层） | 项目 `.mcp.json` |
| `hermes.yaml` | Hermes Agent | `~/.hermes/config.yaml` |
| `openclaw.json` | OpenClaw | `~/.openclaw/openclaw.json` 的 `mcp.servers` |
| `astrbot.json` | AstrBot | WebUI 的 MCP 服务器 command/args/env |
| `opencode.json` | OpenCode | `opencode.json` / `~/.config/opencode/opencode.json` 的 `mcp` 块 |
| `workbuddy-mcp.json` | WorkBuddy（腾讯云代码助手） | `~/.workbuddy/mcp.json`（用户级）或 `<项目>/.workbuddy/mcp.json`（项目级） |
| `trae.json` | TRAE（字节跳动） | AI 侧栏 → MCP → 手动配置，粘贴 JSON |
| `zcode-config.json` | ZCode | `~/.zcode/cli/config.json`（用户）或 `<repo>/.zcode/config.json`（工作区）的 `mcp.servers` |

所有路径占位符（`C:\path\to\python.exe`、`C:\path\to\sekaisync`）使用前替换为实际路径。
