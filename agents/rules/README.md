# Persistent behaviour rules (rules/)

English | [中文](README.zh-CN.md)

These files are the hard rules loaded on every session — distinct from `SKILL.md`, which loads only when hit. The rule content does not change with the protocol; copy them into a project under the platform-required file names.

| File | Platform | Where it goes |
| --- | --- | --- |
| `AGENTS.md` | Codex / Grok Build | project root (Codex also reads `.agents/AGENTS.md`) |
| `CLAUDE.md` | Claude Code / Claude Code Desktop | project root |
| `SekaiSync.mdc` | Cursor | `.cursor/rules/SekaiSync.mdc` |
| `SekaiSync.prompt.md` | OpenClaw | imported as a persona / prompt module |
