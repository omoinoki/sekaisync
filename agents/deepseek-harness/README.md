# DeepSeek Harness integration

English | [中文](README.zh-CN.md)

> **Status: `dsh-sekaisync-connect` is in development and coming soon — you can start integrating by hand today.**

DeepSeek Harness (`deepseek-ai/deepseek-harness`) is DeepSeek's official Agent harness, **not an ordinary MCP client**: it is built on the Cordis plugin system, where models, tools, skills, sessions and other capabilities are all provided by plugins — there is no built-in `mcp.json` / `config.toml` style MCP block.

We are building **`dsh-sekaisync-connect`** — the official SekaiSync connection plugin for Harness — and it is coming soon. Until it ships, you are not blocked: SekaiSync already exposes both MCP transports, so you can integrate by hand right now.

| SekaiSync side | Transport | Endpoint |
| --- | --- | --- |
| `python -m sekaisync serve-mcp` | stdio | direct subprocess |
| `python -m sekaisync serve-http` | Streamable HTTP | `http://127.0.0.1:8787/mcp` |

## Path A: install `dsh-sekaisync-connect` when it ships (recommended)

Once `dsh-sekaisync-connect` is released, install it through the dsh plugin manager and it wires SekaiSync's `sekaisync_*` tools into Harness for you. Keep an eye on this directory and the [dsh-plugin topic](https://github.com/topics/dsh-plugin) for the release. Until then, use Path B below.

## Path B: build a small bridge plugin yourself (works today)

Write a bridge plugin in the official "develop a Tool" shape: inside `apply(ctx)` spawn `python -m sekaisync serve-mcp`, discover tools via `tools/list` over MCP stdio, and register them with `ctx.tools.register(...)`. Skeleton (following the `develop/basic/tool` DSL; you need to add an MCP stdio client dependency):

```ts
import type { Context } from '@deepseek-ai/cordis'

export const name = 'sekaisync-mcp'
export const inject = ['tools']

export function apply(ctx: Context) {
  // 1. spawn: python -m sekaisync serve-mcp  (cwd = the checkout with settings.json + store/)
  // 2. send tools/list over MCP stdio, map each sekaisync_* tool to ctx.tools.register(...)
  // 3. forward arguments to the subprocess on tools/call and return the result
}
```

Then register it in the `cordis.yml` patch overlay, or publish it as a [dsh-plugin](https://github.com/topics/dsh-plugin) for reuse.

## Reusing the behaviour layer

After connecting, "how to use" is protocol-independent: reuse the workflow from `agents/SKILL.md` and the hard rules from `agents/rules/`, injected through Harness skills / instructions.
