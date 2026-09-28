# SekaiSync Agent 接入选择（协议选型）

[English](README.md) | 中文

SekaiSync 面向智能体（Agent）仅提供三种标准化协议。接入前，请根据宿主环境支持的连接方式选择对应协议，并前往相应目录获取配置模板。接入文档按协议组织，同一种协议适用于支持该协议的各类智能体。

## 三种协议

| 协议标识 | 传输通道 | 入口命令 / 路径 | 适用场景 |
| --- | --- | --- | --- |
| `mcp-stdio` | stdio（本地进程） | `python -m sekaisync serve-mcp` | 支持拉起本地子进程的桌面应用、CLI 或 IDE |
| `mcp-http` | Streamable HTTP | `serve-http` 服务的 `/mcp` 端点 | 仅支持通过公网 HTTPS URL 接入的 Web 或云端智能体 |
| `rest-openapi` | HTTP JSON | `serve-http` 的 `/api/v1/*` 与 `/openapi.json` | 仅支持 OpenAPI Actions 或基础 REST API 的智能体（回退方案） |

## 选择规则（机读）

1. 支持拉起本地子进程 → 采用 `mcp-stdio`，获取 `mcp-stdio/` 目录下对应平台的配置。
2. 仅支持配置 HTTPS URL → 采用 `mcp-http`，参考 `mcp-http/`。
3. 仅支持 OpenAPI/REST 接口 → 采用 `rest-openapi/`（功能受限，不推荐作为首选路径）。

## Agent → 协议映射（机读）

| 智能体 / 宿主 | 协议标识 | 配置参考与说明 |
| --- | --- | --- |
| Codex CLI / ChatGPT 桌面应用 / IDE 插件 | `mcp-stdio` | `mcp-stdio/codex.config.toml` |
| Claude Code / Claude Desktop | `mcp-stdio` | `mcp-stdio/claude-code.mcp.json` + `claude-desktop.json` |
| Cursor | `mcp-stdio` | `mcp-stdio/cursor.mcp.json` |
| Grok Build | `mcp-stdio` | `mcp-stdio/grok.config.toml` + `grok.mcp.json` |
| Hermes Agent | `mcp-stdio` | `mcp-stdio/hermes.yaml` |
| OpenClaw | `mcp-stdio` | `mcp-stdio/openclaw.json` |
| AstrBot | `mcp-stdio` | `mcp-stdio/astrbot.json` |
| OpenCode | `mcp-stdio` / `mcp-http` | 本地优先 `mcp-stdio/opencode.json`；远程参考 `mcp-http/README.zh-CN.md` |
| WorkBuddy（腾讯云代码助手） | `mcp-stdio` | `mcp-stdio/workbuddy-mcp.json` |
| TRAE（字节跳动） | `mcp-stdio` / `mcp-http` | 本地采用 `mcp-stdio/trae.json`；远程采用 Streamable HTTP |
| ZCode | `mcp-stdio` | `mcp-stdio/zcode-config.json`（`.zcode/config.json`） |
| DeepSeek Harness | 插件 | [SekaiSync Connect for DeepSeek Harness](https://github.com/omoinoki/dsh-sekaisync-connect) |
| ChatGPT Work / ChatGPT Web | `mcp-http` | `mcp-http/chatgpt-work.md` + `developer-mode.md` |
| ChatGPT Actions（历史回退路径） | `rest-openapi` | `rest-openapi/openapi.json` |

## 行为层（与协议无关）

完成连接后，智能体的业务规范与调用流程与底层协议无关，统一定义于以下两处：

- `SKILL.md`：仅在对话涉及 Project SEKAI 领域问题时加载的专用技能定义（包含工作流与输出契约）。
- `rules/`：常驻硬性规则，按各平台规范命名并放置在项目中：
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

示例配置中的路径占位符（如 `C:\path\to\python.exe`、`C:\path\to\sekaisync`）在实际接入时均须替换为本机实际路径。
