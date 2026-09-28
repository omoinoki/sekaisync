# 常驻行为规则（rules/）

[English](README.md) | 中文

本目录包含在每次会话中常驻生效的硬性行为规则，与按需调用的 `SKILL.md` 相互独立。规则语义不随接入协议变化；部署时按对应平台的文件名要求复制到项目中即可。

| 规则文件 | 目标平台 | 目标文件路径 |
| --- | --- | --- |
| `AGENTS.md` | Codex / Grok Build | 项目根目录（Codex 亦支持 `.agents/AGENTS.md`） |
| `CLAUDE.md` | Claude Code / Claude Code Desktop | 项目根目录 |
| `SekaiSync.mdc` | Cursor | `.cursor/rules/SekaiSync.mdc` |
| `SekaiSync.prompt.md` | OpenClaw | 作为 persona / prompt 模块引入 |
