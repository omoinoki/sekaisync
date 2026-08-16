# SekaiSync Agent 接入选择（协议选型）

[English](README.md) | 中文

SekaiSync 对 Agent 只暴露三种协议。接入前，Agent 先读本文件，按「能怎么连」选择其中一种，再到对应目录取配置模板。不要按具体 Agent 品牌建立接口：同一种协议对所有支持该协议的 Agent 都成立。

## 三种协议

| 协议 | 传输 | 入口 | 适用场景 |
| --- | --- | --- | --- |
| `mcp-stdio` | stdio（本地进程） | `python -m sekaisync serve-mcp` | 能拉起本地子进程的桌面 App / CLI / IDE |
| `mcp-http` | Streamable HTTP | `serve-http` 的 `/mcp` | 只能通过公网 HTTPS URL 接入的 Web / 云端 Agent |
| `rest-openapi` | HTTP JSON | `serve-http` 的 `/api/v1/*` 与 `/openapi.json` | 只支持 OpenAPI Actions 或裸 REST 的 Agent（旧回退） |

## 选择规则（机读）

1. 能 spawn 本地进程 → 用 `mcp-stdio`，取 `mcp-stdio/` 下与平台对应的配置。
2. 只能填一个 HTTPS URL → 用 `mcp-http`，见 `mcp-http/`。
3. 只有 OpenAPI/REST 可用 → 用 `rest-openapi/`（能力最少，不推荐作为主路径）。

## Agent → 协议映射（机读）

| agent | protocol | 配置/说明 |
| --- | --- | --- |
| Codex CLI / ChatGPT 桌面 App / IDE 扩展 | `mcp-stdio` | `mcp-stdio/codex.config.toml` |
| Claude Code / Claude Desktop | `mcp-stdio` | `mcp-stdio/claude-code.mcp.json` + `claude-desktop.json` |
| Cursor | `mcp-stdio` | `mcp-stdio/cursor.mcp.json` |
| Grok Build | `mcp-stdio` | `mcp-stdio/grok.config.toml` + `grok.mcp.json` |
| Hermes Agent | `mcp-stdio` | `mcp-stdio/hermes.yaml` |
| OpenClaw | `mcp-stdio` | `mcp-stdio/openclaw.json` |
| AstrBot | `mcp-stdio` | `mcp-stdio/astrbot.json` |
| OpenCode | `mcp-stdio` / `mcp-http` | `mcp-stdio/opencode.json`（local）；远程见 `mcp-http/README.zh-CN.md` |
| WorkBuddy（腾讯云代码助手） | `mcp-stdio` | `mcp-stdio/workbuddy-mcp.json` |
| TRAE（字节跳动） | `mcp-stdio` / `mcp-http` | `mcp-stdio/trae.json`（stdio）；远程走 Streamable HTTP |
| ZCode | `mcp-stdio` | `mcp-stdio/zcode-config.json`（`.zcode/config.json`） |
| DeepSeek Harness | 插件 | `deepseek-harness/`（Cordis 插件桥接 MCP） |
| ChatGPT Work / ChatGPT Web | `mcp-http` | `mcp-http/chatgpt-work.md` + `developer-mode.md` |
| ChatGPT Actions（旧回退） | `rest-openapi` | `rest-openapi/openapi.json` |

## 行为层（与协议无关）

接入成功后的「怎么用」不随协议变化，集中在两处：

- `SKILL.md`：命中 Project Sekai 问题才加载的技能（工作流 + 输出契约）。
- `rules/`：常驻硬规则，按平台要求的文件名复制到项目里：
  `AGENTS.md`（Codex / Grok）、`CLAUDE.md`（Claude Code）、`SekaiSync.mdc`（Cursor）、
  `SekaiSync.prompt.md`（OpenClaw）。

## 目录结构

```text
agents/
  README.md            本文件：机读接入选择
  SKILL.md             共享技能（单一来源）
  mcp-stdio/           协议 1：本地进程 MCP 配置模板
  mcp-http/            协议 2：Streamable HTTP（HTTPS /mcp）
  rest-openapi/        协议 3：REST + OpenAPI
  rules/               常驻行为规则（按平台文件名）
```

所有示例路径（`C:\path\to\python.exe`、`C:\path\to\sekaisync`）都是占位符，接入时替换成实际路径。
