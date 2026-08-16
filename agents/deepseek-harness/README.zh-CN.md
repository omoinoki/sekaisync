# DeepSeek Harness 接入

[English](README.md) | 中文

> **状态：`dsh-sekaisync-connect` 正在开发中、即将推出——但你今天就可以自己动手接入。**

DeepSeek Harness（`deepseek-ai/deepseek-harness`）是 DeepSeek 官方的 Agent harness，**不是普通 MCP 客户端**：它基于 Cordis 插件系统，模型、工具、技能、会话等能力全部由插件提供，没有内置的 `mcp.json` / `config.toml` 式 MCP 配置块。

我们正在开发 **`dsh-sekaisync-connect`** —— SekaiSync 官方的 Harness 连接插件——即将推出。在它发布之前，你不会被阻塞：SekaiSync 已同时暴露两种 MCP 传输，你可以立刻自己动手接入。

| SekaiSync 侧 | 传输 | 端点 |
| --- | --- | --- |
| `python -m sekaisync serve-mcp` | stdio | 子进程直连 |
| `python -m sekaisync serve-http` | Streamable HTTP | `http://127.0.0.1:8787/mcp` |

## 路径 A：等 `dsh-sekaisync-connect` 发布后安装（推荐）

`dsh-sekaisync-connect` 发布后，通过 dsh 插件管理器安装，即可自动把 SekaiSync 的 `sekaisync_*` 工具接入 Harness。关注本目录与 [dsh-plugin 主题](https://github.com/topics/dsh-plugin) 留意发布。在此之前，用下面的路径 B。

## 路径 B：自己写一个轻量桥接插件（今天就能跑通）

按官方「开发一个 Tool」的插件形态写一个桥接插件：在 `apply(ctx)` 里拉起 `python -m sekaisync serve-mcp` 子进程，通过 MCP stdio 协议 `tools/list` 发现工具，再用 `ctx.tools.register(...)` 注册到 Harness。骨架如下（遵循 `develop/basic/tool` 的 DSL，需要补一个 MCP stdio client 依赖）：

```ts
import type { Context } from '@deepseek-ai/cordis'

export const name = 'sekaisync-mcp'
export const inject = ['tools']

export function apply(ctx: Context) {
  // 1. spawn: python -m sekaisync serve-mcp  (cwd = 含 settings.json + store/ 的目录)
  // 2. 通过 MCP stdio 发 tools/list，映射每个 sekaisync_* 工具到 ctx.tools.register(...)
  // 3. tools/call 时转发参数给子进程，回传结果
}
```

再把它注册进 `cordis.yml` 的 patch 覆盖层，或按「打包与安装插件」发布成 [dsh-plugin](https://github.com/topics/dsh-plugin) 供复用。

## 行为层复用

接入后 Agent 的「怎么用」与协议无关，直接复用 `agents/SKILL.md` 的工作流与 `agents/rules/` 的硬规则，通过 Harness 的 skills / 指令注入即可。
