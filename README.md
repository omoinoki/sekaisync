# SekaiSync 🎵

> A local knowledge base and AI Agent context (MCP) service for *Project SEKAI* fandom.

English | [中文](README.zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](pyproject.toml) [![Dependencies](https://img.shields.io/badge/Dependencies-Zero-brightgreen)](pyproject.toml) [![Protocol](https://img.shields.io/badge/Protocol-MCP-blue)](agents/README.md) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**SekaiSync** is built specifically for Large Language Models (LLMs) and coding Agents (such as Claude, Cursor, and ChatGPT). It syncs official Master Data, multi-region localized terminology, and community story texts for *Project SEKAI* (PJ:SEKAI) into a local `store/`. Exposing retrieval capabilities through **MCP (Model Context Protocol)** or CLI commands, it ensures AI Agents rely on **ground-truth local data** when answering game-related questions—eliminating AI hallucinations at the source.

---

## ✨ Core Features

- ⚡ **Zero External Dependencies**: Built entirely using Python 3.10+ standard library. Lightweight, pure, and ready out of the box.
- 🌐 **5-Region Data Alignment**: Full support for Master Data synchronization and cross-lingual translation mappings across JP, EN, CN, TC (Traditional Chinese), and KR servers.
- 🤖 **MCP-Native Agent Readiness**: Built-in MCP stdio and Streamable HTTP services for seamless integration with Claude Desktop, Cursor, and custom AI workflows.
- 🛡️ **Strict Data Boundaries**: Adheres to a "report unknown when uncovered" principle to prevent AI Agents from hallucinating nonexistent lore.
- 📰 **Flexible Data Extensions**: Supports official news announcements sync and TOS-compliant community story text crawling.

---

## 🚀 Quick Start

### 1. Installation

```bash
git clone <repo-url> sekaisync
cd sekaisync
pip install .
```
> 💡 *Since there are zero third-party PyPI dependencies, you can also run commands directly via `python -m sekaisync <command>` without installation.*

### 2. Initialization & Data Sync

```bash
# Initialize local store directory (v2)
python -m sekaisync init

# Sync Master Data across all 5 regions
python -m sekaisync sync --regions jp,en,cn,tc,kr

# (Optional) Sync official news & announcements
python -m sekaisync news sync
```

### 3. Local Queries & Inspection

```bash
# Cross-lingual name resolution (e.g., resolve "星乃一歌" to English)
python -m sekaisync resolve --query "星乃一歌" --target-language en

# Targeted lookup with specific language
python -m sekaisync lookup --query "Hoshino Ichika" --language zh_tw

# General knowledge base query
python -m sekaisync query --query "Hoshino Ichika"

# Check knowledge base and sync status
python -m sekaisync status
python -m sekaisync kb-status
```

---

## 🤖 Agent / AI Integration (MCP)

SekaiSync provides a standard **Model Context Protocol (MCP)** implementation to easily plug into LLM workflows:

#### Local Agents (Claude Desktop / Cursor)
Start the MCP service via standard input/output (stdio):
```bash
python -m sekaisync serve-mcp
```

#### Remote / Web Agents (ChatGPT Custom Actions / HTTP)
Start the HTTP + MCP Streamable service:
```bash
python -m sekaisync serve-http --host 127.0.0.1 --port 8787
```
> For detailed Agent configuration examples (e.g., `claude_desktop_config.json`), please refer to [`agents/README.md`](agents/README.md).

---

## 📊 Data Scope & Boundaries

To ensure accurate output from AI Agents, SekaiSync strictly defines its data storage boundaries:

| Category | Coverage | Description |
| :--- | :---: | :--- |
| **5-Region Master Data** | ✅ Included | Official metadata for cards, events, songs, characters, etc. |
| **Localized Terminology** | ✅ Included | Cross-lingual mappings for characters, songs, and terms across JP/EN/CN/TC/KR. |
| **Official Announcements** | ✅ Included | Synced via `news sync`. |
| **Community Story Text** | ⚠️ Optional | Text only. Requires custom endpoints and running `crawl`. |
| **Multimedia Assets / Runtime Data** | ❌ Excluded | Does NOT store images, audio, Live2D assets, chart files, or real-time player data. |

> 📌 **Anti-Hallucination Design**: When queried information is outside the current `store/` coverage, SekaiSync explicitly returns `not covered`, instructing the Agent to honestly respond that the information is unknown rather than inventing facts.

---

## ⚙️ Advanced Configuration (`settings.json`)

The `sync` command (Master Data) fetches from public community repositories by default and requires no extra setup.

To use `crawl` (community story text crawling) or custom news sources, configure your endpoint URLs in `settings.json`:

```json
{
  "version": 2,
  "sites": [
    {
      "id": "altsource_sv",
      "backend": "sekai_viewer",
      "enabled": true,
      "master_base": "<your_master_endpoint>",
      "asset_base": "<your_asset_endpoint>",
      "asset_buckets": { "jp": "<your_jp_asset_bucket>" },
      "i18n_base": "<your_i18n_endpoint>"
    },
    {
      "id": "altsource_ms",
      "backend": "moesekai",
      "enabled": true,
      "site_base": "<your_site_address>",
      "sitemap_url": "<your_sitemap_url>",
      "metadata_bases": ["<your_metadata_endpoint>"],
      "asset_bases": ["<your_asset_endpoint>"],
      "translation_base": "<your_translation_endpoint>",
      "news_base": "<your_news_endpoint>"
    }
  ]
}
```

> **Compliance & Terms Notice**:
> - Please ensure you read and agree to the game's Terms of Service, and respect target sites' `robots.txt` and content signals. SekaiSync crawls plain text only.
> - Executing the `crawl` command (via the `--accept-tos` flag or interactive prompt) constitutes your explicit agreement to the game's Terms of Service.
> - Compatibility with the two mainstream public WebDB systems for this game does not imply that SekaiSync endorses or suggests connecting to any particular instance.

---

## 🛠️ Developer Guide

Run unit tests:
```bash
python -m unittest discover -s tests -t .
```

Build Wheel package:
```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

---

## 📜 License & Disclaimer

- Distributed under the [MIT License](LICENSE).
- This repository **does NOT contain or bundle any original game binary data or multimedia assets**. The `sync` functionality relies on public Master Data repositories; users are responsible for confirming data source licenses and game Terms of Service before production use.