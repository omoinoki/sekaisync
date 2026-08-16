---
name: SekaiSync
purpose: Select the correct access protocol for a SekaiSync agent
protocols:
  - id: mcp-stdio
    label: MCP stdio (local process)
    command: python -m sekaisync serve-mcp
    transport: stdio
    endpoint: null
    when: "agent can spawn a local subprocess (desktop app, CLI, IDE)"
    directory: mcp-stdio/
  - id: mcp-http
    label: MCP Streamable HTTP
    command: python -m sekaisync serve-http
    transport: streamable-http
    endpoint: /mcp
    when: "agent can only reach a public HTTPS URL (web / cloud agents)"
    directory: mcp-http/
  - id: rest-openapi
    label: REST + OpenAPI
    command: python -m sekaisync serve-http
    transport: http
    endpoint: "/api/v1/* and /openapi.json"
    when: "agent only supports OpenAPI Actions or a plain JSON HTTP API"
    directory: rest-openapi/
---

# SekaiSync Agent Access (Protocol Selection)

English | [中文](README.zh-CN.md)

SekaiSync exposes exactly three protocols to Agents. Before integrating, an Agent
reads this file, picks the protocol it can connect to, then takes the matching
config template from the corresponding directory. Do not build interfaces per
Agent brand: the same protocol works for every Agent that supports it.

## The three protocols

| Protocol | Transport | Entry | When to use |
| --- | --- | --- | --- |
| `mcp-stdio` | stdio (local process) | `python -m sekaisync serve-mcp` | Agents that can spawn a local subprocess (desktop app / CLI / IDE) |
| `mcp-http` | Streamable HTTP | `/mcp` of `serve-http` | Agents that can only reach a public HTTPS URL (web / cloud Agents) |
| `rest-openapi` | HTTP JSON | `/api/v1/*` and `/openapi.json` of `serve-http` | Agents that only support OpenAPI Actions or plain REST (legacy fallback) |

## Selection rules (machine-readable)

1. Can spawn a local process → use `mcp-stdio`, pick the platform config under `mcp-stdio/`.
2. Can only fill in an HTTPS URL → use `mcp-http`, see `mcp-http/`.
3. Only OpenAPI/REST available → use `rest-openapi/` (fewest capabilities, not the primary path).

## Agent → protocol mapping (machine-readable)

| agent | protocol | config / notes |
| --- | --- | --- |
| Codex CLI / ChatGPT desktop / IDE extension | `mcp-stdio` | `mcp-stdio/codex.config.toml` |
| Claude Code / Claude Desktop | `mcp-stdio` | `mcp-stdio/claude-code.mcp.json` + `claude-desktop.json` |
| Cursor | `mcp-stdio` | `mcp-stdio/cursor.mcp.json` |
| Grok Build | `mcp-stdio` | `mcp-stdio/grok.config.toml` + `grok.mcp.json` |
| Hermes Agent | `mcp-stdio` | `mcp-stdio/hermes.yaml` |
| OpenClaw | `mcp-stdio` | `mcp-stdio/openclaw.json` |
| AstrBot | `mcp-stdio` | `mcp-stdio/astrbot.json` |
| OpenCode | `mcp-stdio` / `mcp-http` | `mcp-stdio/opencode.json` (local); remote via `mcp-http/README.md` |
| WorkBuddy (Tencent Cloud Code Assistant) | `mcp-stdio` | `mcp-stdio/workbuddy-mcp.json` |
| TRAE (ByteDance) | `mcp-stdio` / `mcp-http` | `mcp-stdio/trae.json` (stdio); remote via Streamable HTTP |
| ZCode | `mcp-stdio` | `mcp-stdio/zcode-config.json` (`.zcode/config.json`) |
| DeepSeek Harness | plugin | `deepseek-harness/` (Cordis plugin bridging MCP) |
| ChatGPT Work / ChatGPT Web | `mcp-http` | `mcp-http/chatgpt-work.md` + `developer-mode.md` |
| ChatGPT Actions (legacy fallback) | `rest-openapi` | `rest-openapi/openapi.json` |

## Behaviour layer (protocol-independent)

How to use SekaiSync after connecting does not change with the protocol and lives in two places:

- `SKILL.md`: the skill loaded only when a Project Sekai question is hit (workflow + output contract).
- `rules/`: persistent hard rules copied into a project under the platform-required file names:
  `AGENTS.md` (Codex / Grok), `CLAUDE.md` (Claude Code), `SekaiSync.mdc` (Cursor),
  `SekaiSync.prompt.md` (OpenClaw).

## Directory layout

```text
agents/
  README.md            this file: machine-readable access selection
  SKILL.md             shared skill (single source)
  mcp-stdio/           protocol 1: local-process MCP config templates
  mcp-http/            protocol 2: Streamable HTTP (HTTPS /mcp)
  rest-openapi/        protocol 3: REST + OpenAPI
  rules/               persistent behaviour rules (per platform file name)
```

All example paths (`C:\path\to\python.exe`, `C:\path\to\sekaisync`) are placeholders —
replace them with your real paths when integrating.
