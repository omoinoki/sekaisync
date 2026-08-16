# 常驻行为规则（rules/）

[English](README.md) | 中文

这些文件是「每次会话常驻」的硬规则，与「命中才加载」的 `SKILL.md` 区分开。规则内容不随协议变化；按平台要求的文件名复制到项目里即可。

| 文件 | 平台 | 落地位置 |
| --- | --- | --- |
| `AGENTS.md` | Codex / Grok Build | 项目根目录（Codex 也读 `.agents/AGENTS.md`） |
| `CLAUDE.md` | Claude Code / Claude Code Desktop | 项目根目录 |
| `SekaiSync.mdc` | Cursor | `.cursor/rules/SekaiSync.mdc` |
| `SekaiSync.prompt.md` | OpenClaw | 作为 persona / prompt 模块引入 |
