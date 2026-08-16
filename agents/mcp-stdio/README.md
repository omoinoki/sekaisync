# MCP stdio (Protocol 1)

English | [中文](README.zh-CN.md)

Local-process transport. Use it when the target Agent can spawn a subprocess directly: zero network, zero public exposure — the most recommended integration path.

## Start command

```powershell
python -m sekaisync serve-mcp
```

The working directory is the checkout that contains `settings.json` and `store/`.

## Config templates (pick by platform file name)

| File | Target platform | Where it goes |
| --- | --- | --- |
| `codex.config.toml` | Codex CLI / ChatGPT desktop / IDE | `~/.codex/config.toml` (global) or `.codex/config.toml` (project) |
| `claude-code.mcp.json` | Claude Code | project `.mcp.json`, or `claude mcp add sekaisync ...` |
| `claude-desktop.json` | Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| `cursor.mcp.json` | Cursor | project `.cursor/mcp.json`, or `~/.cursor/mcp.json` |
| `grok.config.toml` | Grok Build | `~/.grok/config.toml` or `.grok/config.toml` |
| `grok.mcp.json` | Grok Build (compat layer) | project `.mcp.json` |
| `hermes.yaml` | Hermes Agent | `~/.hermes/config.yaml` |
| `openclaw.json` | OpenClaw | `mcp.servers` of `~/.openclaw/openclaw.json` |
| `astrbot.json` | AstrBot | WebUI MCP server command/args/env |
| `opencode.json` | OpenCode | `mcp` block of `opencode.json` / `~/.config/opencode/opencode.json` |
| `workbuddy-mcp.json` | WorkBuddy (Tencent Cloud Code Assistant) | `~/.workbuddy/mcp.json` (user) or `<project>/.workbuddy/mcp.json` (project) |
| `trae.json` | TRAE (ByteDance) | AI sidebar → MCP → manual config, paste the JSON |
| `zcode-config.json` | ZCode | `mcp.servers` of `~/.zcode/cli/config.json` (user) or `<repo>/.zcode/config.json` (workspace) |

All path placeholders (`C:\path\to\python.exe`, `C:\path\to\sekaisync`) must be replaced with your real paths before use.
