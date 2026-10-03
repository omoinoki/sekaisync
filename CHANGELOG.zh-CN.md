# 更新日志

[English](CHANGELOG.md) | 中文

## 0.4.2-alpha - 未发布

### 修复

- 恢复角色档案正文，并支持按角色名查找档案。修复 [#1](https://github.com/omoinoki/sekaisync/issues/1)。
- 补齐卡牌标题与技能名、任务描述及道具风味文本。
- 支持按完整实体 ID、角色名和描述正文检索。
- 修复离线测试配置与 Python 3.10 测试兼容性。

### 新增

- CLI、MCP 和 HTTP 的 FactPack 支持区服筛选。
- FactPack 返回区服、正文语言、来源、版本及内容状态，并支持中文语言别名。
- 刮削器新增出现级词义、精确原文锚点及逐级语境复核。
- 内置 [SekaiSync Connect 插件](https://github.com/omoinoki/dsh-sekaisync-connect/releases/tag/v0.3.9-alpha.1) 更新至 0.3.9-alpha.1，支持区服结果、更清晰的工具错误提示和更安全的部署路径保存。

### 升级

使用新版后端重新同步，以恢复旧库中缺失的描述字段，然后重启 MCP 和 HTTP 服务。也可用本地 raw master 数据离线恢复，操作见[恢复指南](docs/ISSUE_1_PROFILE_RECOVERY_261003.md)。
