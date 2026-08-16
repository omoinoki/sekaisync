# SekaiSync 🎵

> 面向《世界计划》(Project SEKAI) 粉丝的本地知识库与 AI Agent 上下文（MCP）服务。

[English](README.md) | 中文

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](pyproject.toml) [![Dependencies](https://img.shields.io/badge/Dependencies-Zero-brightgreen)](pyproject.toml) [![Protocol](https://img.shields.io/badge/Protocol-MCP-blue)](agents/README.zh-CN.md) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**SekaiSync** 专为 LLM 与编码 Agent（如 Claude、Cursor、ChatGPT）构建。它把《世界计划》的官方 Master Data、多区服本地化术语与社区剧情正文同步到本地 `store/`，通过 **MCP（模型上下文协议）** 或 CLI 提供检索能力，让 Agent 回答游戏问题时依赖**本地真实数据**，从源头消除幻觉。

---

## ✨ 核心特性

- ⚡ **零外部依赖**：仅用 Python 3.10+ 标准库实现，轻量、纯粹、开箱即用。
- 🌐 **五区服数据对齐**：支持 JP / EN / CN / TC（繁中）/ KR 的 Master Data 同步与跨语言译名映射。
- 🤖 **MCP 原生 Agent 就绪**：内置 MCP stdio 与 Streamable HTTP 服务，可无缝接入 Claude Desktop、Cursor 及自定义 AI 工作流。
- 🛡️ **严格数据边界**：坚持「未覆盖即如实报告」原则，防止 Agent 编造不存在的设定。
- 📰 **灵活数据扩展**：支持官方公告同步与符合 TOS 的社区剧情正文抓取。

---

## 🚀 快速开始

### 1. 安装

```bash
git clone <repo-url> sekaisync
cd sekaisync
pip install .
```
> 💡 *由于无任何第三方 PyPI 依赖，你也可以不安装、直接用 `python -m sekaisync <command>` 运行。*

### 2. 初始化与数据同步

```bash
# 初始化本地 store（v2 布局）
python -m sekaisync init

# 同步五区服 Master Data
python -m sekaisync sync --regions jp,en,cn,tc,kr

# （可选）同步官方公告
python -m sekaisync news sync
```

### 3. 本地查询与检查

```bash
# 跨语言译名解析（如把「星乃一歌」解析为英文）
python -m sekaisync resolve --query "星乃一歌" --target-language en

# 按语言定向查询
python -m sekaisync lookup --query "Hoshino Ichika" --language zh_tw

# 综合知识库查询
python -m sekaisync query --query "Hoshino Ichika"

# 查看知识库与同步状态
python -m sekaisync status
python -m sekaisync kb-status
```

---

## 🤖 Agent 接入（MCP）

SekaiSync 提供标准 **MCP（模型上下文协议）** 实现，可轻松接入 LLM 工作流：

#### 本地 Agent（Claude Desktop / Cursor）
通过标准输入输出（stdio）启动 MCP 服务：
```bash
python -m sekaisync serve-mcp
```

#### 远程 / Web Agent（ChatGPT Custom Actions / HTTP）
启动 HTTP + MCP Streamable 服务：
```bash
python -m sekaisync serve-http --host 127.0.0.1 --port 8787
```
> 详细的 Agent 配置示例（如 `claude_desktop_config.json`）见 [`agents/README.zh-CN.md`](agents/README.zh-CN.md)。

---

## 📊 数据范围与边界

为确保 Agent 输出准确，SekaiSync 严格定义数据存储边界：

| 类别 | 覆盖 | 说明 |
| :--- | :---: | :--- |
| **五区服 Master Data** | ✅ 包含 | 卡牌、活动、歌曲、角色等官方元数据 |
| **本地化术语** | ✅ 包含 | 角色、歌曲、术语的跨语言映射（JP/EN/CN/TC/KR） |
| **官方公告** | ✅ 包含 | 通过 `news sync` 同步 |
| **社区剧情正文** | ⚠️ 可选 | 仅文字；需配置自定义端点并运行 `crawl` |
| **多媒体资产 / 运行时数据** | ❌ 不包含 | 不存储图片、音频、Live2D、谱面文件或实时玩家数据 |

> 📌 **抗幻觉设计**：当查询的信息超出当前 `store/` 覆盖范围时，SekaiSync 会明确返回 `not covered`，指示 Agent 如实回答「未知」而非编造事实。

---

## ⚙️ 高级配置（`settings.json`）

`sync` 命令（Master Data）默认从公开社区仓库获取，无需额外配置。

要使用 `crawl`（社区剧情正文抓取）或自定义公告源，请在 `settings.json` 中配置你的端点地址：

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

> **合规与服务条款提示**：
> - 请确保你已阅读并同意游戏服务条款，并尊重目标站点的 `robots.txt` 与内容信号。SekaiSync 只抓取纯文本。
> - 执行 `crawl` 命令（通过 `--accept-tos` 或交互式确认）即构成你明确同意游戏服务条款。
> - 对当前主流的两类游戏公开 WebDB 的兼容，不代表 SekaiSync 赞成或暗示接入任何特定实例。

---

## 🛠️ 开发者指南

运行单元测试：
```bash
python -m unittest discover -s tests -t .
```

构建 Wheel 包：
```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

---

## 📜 许可证与免责声明

- 以 [MIT 许可证](LICENSE) 分发。
- 本仓库**不包含、不捆绑任何原始游戏二进制数据或多媒体资产**。`sync` 功能依赖公开的 Master Data 仓库；生产使用前，用户应自行确认数据源许可与游戏服务条款。
