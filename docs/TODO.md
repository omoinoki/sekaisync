# SekaiSync TODO 状态

## 当前状态与后续工作（2026-10-03）

- [x] [Issue #1：character_profile 丢失档案正文，且按角色名不可达](https://github.com/omoinoki/sekaisync/issues/1)：已修复档案正文、关联名称和区服证据，并扩展同类描述遗漏回归。见[恢复说明](ISSUE_1_PROFILE_RECOVERY_261003.md)。
- [x] 已发布 [SekaiSync Connect 0.3.9-alpha.1](https://github.com/omoinoki/dsh-sekaisync-connect/releases/tag/v0.3.9-alpha.1)。
- [ ] 发布 SekaiSync `0.4.2-alpha`。
- [ ] WinUI3 多区服实体详情与正文搜索适配。

升级已有知识库时，从 raw 重建或重新 sync，并重启 MCP/HTTP 进程。操作见[恢复说明](ISSUE_1_PROFILE_RECOVERY_261003.md)。

刮削工程检查点 `sekaisync-scraper-261002` 已合并，提交为 `b6c70aa`，历史 Git 标签为 `scraper-261002`。见[检查点说明](SCRAPER_CHECKPOINT_261002.md)。

## 已封存检查点目标（2026-10-02）

刮削系统本次任务已改为 [261002 限时质量跃迁](PROJECT_GOALS_261002.md)：
当日20:00（Asia/Singapore）结束持续改进、封存检查点并合并main；19:15冻结
功能改动。新样本源召回90%/每语85%，20方向200项端到端正确覆盖90%/最差
方向80%，相对发布版提升20个百分点；未达标如实记录，不以测试数代替效果。
下方2026-08-21状态是历史快照，其P0/P1“全部完成”不代表当前刮削目标已经完成。

261002本轮已封存并停止：工程PASS，首轮语义质量FAIL，修复后四语源发现
966/1004，新候选五语/二十方向完整质量INCOMPLETE。详见
[检查点](SCRAPER_CHECKPOINT_261002.md)；后续重启需重新明确任务，不能无限续跑。

核查日期：2026-08-21（第 3 次重构：sync 管道表扩展完成后重新评估）

## 历史状态快照（2026-08-21）

| 层 | 状态 |
| --- | --- |
| P0 可信度与诚实边界 | ✅ 全部完成 |
| P1 事实完整性与供给稳定 | ✅ 全部完成 |
| P2 接入体验与交付 | 剩 1 项：`crawler.py` 渐进拆分 |
| 事实层广度（sync 管道） | ✅ 已扩：registry 14.4k → 66.4k、glossary 14.5k → 59.1k |
| 管道 B 漫画正文 / 社区翻译 | 外部受限，P3 评估 |
| 调研候选方向 | 评估中（跨语言对齐 / 信任评分 / provenance 文献） |

## 历史基线（sync 表扩展后实测）

| 数据层 | 数量 | 说明 |
| --- | --- | --- |
| Master 实体（registry） | **66,435** | 原 14,463；新增 honor / bonds / mysekai / rank / 任务 / 物品 12 类 |
| Glossary | **59,129** | 原 14,465；+44.6k 条五服本地化官方词 |
| FactPacks | 每语言一份 | 新增类型已有分支（honor/flavor/rank/sentence） |
| 术语索引 | 9,324 | 2026-08-23 清洗污染数据（12,580 → 9,324）后的实测；新 kind 不进 NOUN_KINDS，头衔/任务名不是术语 |
| 术语五语覆盖 | 25 条五语齐全 | 单语言覆盖：ja 9,324、en 341、zh_hans 192、ko 146、zh_tw 63（2026-09-05 实测） |
| Web 正文页 | 638,335 | altsource + Sekai Viewer + 辅助页 |
| 辅助翻译参考 | 4,837 | altsource_ms_translation 4,730 + Sekai Viewer i18n 107 |
| 官方公告 | 293 | `zh_hans` 188、`ja` 105 |
| 测试 | **233** | 全部通过（2026-09-05 实测） |

`sekaisync integrity` 最新实测：镜像重复 67,707、正文冲突 0、哈希不一致 0、canonical_missing 0；
仅保留 8 条资产/语种错配和 422 条 scenarioId 命名错位（均为数据源异常，见 `docs/INTEGRITY.md`）。

## 两套同步管道（决策必须区分）

| | 管道 A：master 同步 | 管道 B：正文抓取 |
| --- | --- | --- |
| 命令 | `sekaisync sync` | `sekaisync crawl` |
| 数据源 | GitHub master 仓库 tarball | altsource_sv / altsource_ms |
| 落库 | registry / glossary / factpacks | web 索引 |
| 信任 | A（官方 master） | B（社区）/ C（辅助翻译） |
| 缺口性质 | 配置未覆盖（已基本补完） | 数据源外部限制 |

## 已完成（按主题归档）

### 数据同步与存储布局

- 五服 Master DB 同步与本地镜像导入；跨服 registry、glossary、factpacks、freshness 生成
- KB v2 存储布局：`kb/` 唯一数据、`raw/` 原始 JSON、`cache/` 可再生成；`kb-status` 可用
- 双站代号重命名 + 多站点配置（`altsource_sv` / `altsource_ms`，有序 `sites` 数组）；多实例后端（类型/实例选择器）
- altsource 页面 ID 语种化与旧数据迁移；`consent.json` 兼容迁移；`source_migrate.py` 就地迁移（dry-run / 原子写 / 失败隔离）
- fetcher 原子替换：staging + backup 回滚
- 术语库污染清洗（2026-08-23：12,580 → 9,324）；清洗残留备份已清理（2026-09-05，释放 161MB）。
  备份命名规范：`<name>.bak.<YYYYMMDD>-<purpose>`（全小写连字符，不含空格），完成使命后及时删除

### 正文抓取与供给稳定

- 双源正文全量抓取（仅文字，TOS 确认，增量 resume，depth 4）；九类正文落盘
- 缓存绕过与资产版本校验；AIGC 派生文本排除；网络异常兜底
- 429/Retry-After 退避（上限 60s）；实例健康探测与失败自动降级（`skipped_instances`）
- 跨实例完整性核对报告（`cross_instance_reconciliation`）
- 官方新闻/公告独立同步；Sekai Viewer 站内公告排除；游戏通知（`userInformations` 100 条 JP）
- altsource 句子级翻译覆盖层 + Sekai Viewer i18n 随 crawl 入库；独立 overlay 移除

### 可信度与诚实边界（P0）

- A/B/C/D 信任等级写入 registry / glossary / terms / web
- `query` 返回 `provenance_guide`（source_layers × trust_levels × 冲突优先级）
- `verify_claims` `verified`→`matched`，附 evidence + method + coverage_note
- `integrity` 不隐藏冲突（flagged 页加入 canonical 分组）；`data_gaps()` 一级可见
- 未翻译页原文可逆（`original_text`）；跨源 canonical_key + 镜像重复/冲突检测

### sync 管道事实层扩展（2026-08-21，本次新增）

- registry 12 个新 kind：`honor` / `honor_group` / `bonds_honor` / `mysekai_fixture` /
  `character_rank` / 6 个任务分表（event/live/normal/character/story/honor_mission）/ `event_item`
- 专用事实提取器：honor 稀有度+levels 条件、bonds_honor 双角色+条件、mysekai_fixture flavorText+分类、
  mission sentence+类型+requirement
- `character_rank` 经角色 join 生成「星乃一歌 Rank N」名称；live/story mission 生成 ID 派生标签
- **修复两处死表名**：`missions` / `items` 在 master 仓库不存在，改为真实分表
- factpacks 新增 7 类分支（Rarity/Condition/Flavor/Rank/Sentence）；crawler 表清单对齐
- 实测：registry 14,463 → 66,435；glossary 14,465 → 59,129；重建 ~9s 零网络；跨语言 lookup 命中

### 术语提取与跨语言对齐

- 本地确定性提取 + 翻译记忆（PMI 对齐 + 过滤）；繁体归一化、属格拒绝、韩文词干保留
- zh-first 简体中文优先管道（Bi-MM 分词 + language-block、26 主角 blocklist、官方名继承、LLM 语义过滤、
  黄金样本 recall 81%）；`terms zhfirst` / `merge-zhfirst` / `penetrate` / `tag-clouds`
- 六分类重叠标签（location/organization/person/event/product/other）；词频权重；同点位跨语言穿透
- 词云分区 released / unreleased；`terms export` 确定性导出 JSON/CSV

### 事件与活动

- 社区活动简称映射（`sekaisync alias`）；箱活映射 v2（banner 角色优先，13 个社区口径箱活）
- World Link 序列映射（`wl{轮}g{序号}`，17 个）；统一活动解析（wl → box → unresolved）
- 新活动自动检测与增量同步（东京日限频）；索引重建管道复用
- 实际活动期数 sequence_no（占位事件 E166/E186 排除，E213 → 211 与字幕组一致）

### 接入与交付

- CLI / HTTP（Streamable + REST OpenAPI）/ MCP（stdio）三端工具面；`web-search` kind 过滤 + token 预算
- Core 长生命周期刷新（`POST /api/v1/refresh` + `sekaisync_refresh`）
- 五服完整度统计（`sekaisync progress`）；事件/译名检索增强
- 文档：ARCHITECTURE / HANDOFF / EVENT_ALIAS / WORLD_LINK / SOURCES_CONFIG / SEKAI_VIEWER_GAP

## 剩余工作

### P2：接入体验与交付

| 任务 | 说明 |
| --- | --- |
| `crawler.py` 渐进拆分 | 维护性重构（3,100+ 行）：抽取 HTTP/重试、按后端类拆分、辅助页与落盘分离。收益低、风险中，可排在调研方向之后 |

### P2 候选（gap 分析新发现，低成本）

| 任务 | 说明 |
| --- | --- |
| `versions.json` 接入 freshness | 客户端/数据版本号（6.7.0 / 6.7.0.40）已采集未展示；Sekai Viewer 首页有，我们 freshness 缺 |
| `data_gaps()` 更新 | 任务/物品类缺口已修复，确认 gaps 列表是否需要补充「海外 characterMissions 缺失」（cn 服无此表） |

### P3：非核心 / 受外部限制

| 任务 | 说明 |
| --- | --- |
| WinUI 术语管理页 / WinUI 3 编译 | 用户明确暂搁；需 .NET SDK |
| 活动规划器 / 跟踪器 / 实时排行榜 | 超出本地知识库定位 |
| 漫画正文采集 | asset 图片 + OCR / 社区翻译 API，与「仅文字」TOS 冲突 |
| FR/RU/UA 社区翻译语言 | 我们只采 5 官方语言；漫画等支持 8 语言 |
| altsource_ms MySekai Lua / 故事级翻译中心 | 源站未镜像，数据源限制 |

### 调研候选方向（评估中，未立项）

> 基于 2024-2025 跨语言术语对齐 / 信任评分 / provenance 学术文献（arXiv）梳理。
> sync 表扩展后「事实广度」问题已缓解，剩余痛点集中在「事实质量」与「对齐鲁棒性」。

| 方向 | 可能落点 | 现状缺口 |
| --- | --- | --- |
| 跨语言术语对齐鲁棒性 | `termindex` 穿透 / 对齐门控 | 穿透错配靠规则补丁，无系统性评价集；可引入对齐置信度打分 |
| 信任评分形式化 | `trust.py` / `provenance_guide` | A/B/C/D 离散枚举，无连续打分、无时间衰减（新表全 A 级后更凸显） |
| provenance 标准接口 | `query` / `verify_claims` | citation 未标准化（无 W3C PROV 式模型），跨工具交换难 |

### 决策记录（避免重复评估）

- **不值得补的 master 表**：`topics` / `wordings`（纯 UI 配置键）、`bonds`（纯 ID 对无名称）、
  `tips` / `birthdayParties` / `virtualLivePamphlets` / `offlineEvents` / `specialSeasons`
  （数量少或价值低）——评估详见 `docs/SEKAI_VIEWER_GAP.md`「分表评估」。
- **不进术语索引**：honor / mysekai_fixture / mission 名称不是「术语」，`NOUN_KINDS` 保持排除。
- **Moesekai 采集面不扩展（2026-08-21）**：歌词/官方四格/漫画/服装等独占内容不补。
  `pjsk.moe/robots.txt` 实测 `Disallow: /zh-cn/api/`（全部语言路径），独占内容由前端
  JS 从 API 加载，采集即违反 robots；现有抓取（剧情页面 + 外部 metadata 域镜像）在
  允许范围内维持不变。详见 `docs/MOESEKAI_GAP.md`。
- **settings.json 本地保护（2026-08-21）**：远端仓库保留 `settings.json`（多实例
  占位符模板，含 example.com 示例实例，是正确的）；本地这台机器的 settings.json
  （临时借用的真实端点）改动不随本地更新推送。实现：`git update-index --skip-worktree
  settings.json`（本地索引标记，不随仓库分发；换机器需重新执行）。勿用 `git rm
  --cached` 或 `.gitignore` 处理——会破坏远端模板。

## 数据源已知缺口（不是爬虫 bug）

| 缺口 | 说明 |
| --- | --- |
| `home_line` 部分缺失 | `characterArchiveVoices` 约 52% `displayPhrase` 为空，源站无正文 |
| 海外 MySekai 对话 | altsource_sv 海外桶无 `mysekai/talk/` Lua，404；仅日服有正文 |
| 特殊剧情 | 第 5/6/7 类倒计时视频无文字，各服 90-96% |
| 海外虚拟 live | 部分资产日/韩文本错配，8 页标记语言错配 |
| scenarioId 错位 | altsource_ms 422 页 `ScenarioId` 用 event_id-1 命名，正文正确 |
| 海外 Master 卡池历史 | CN/TC/KR `gachas.json` 仅近期记录；fact% 100% = 源内完整度 ≠ 全历史 |
| altsource_ms 翻译 API 稳定性 | 事件 166 → 404，事件 183+ → 502 |
| 海外 `characterMissions` | cn 服无此表（en/tc/kr/jp 有）——新表扩展后暴露的边界 |

## 历史建议执行顺序（不用于261002验收）

0. 日常零维护：CLI 命令前自动事件检测，发现新活动按需补抓正文。
1. **保持测试全绿（233 项）**；后续改动回归 `python -m pytest tests/`。
2. **P2 低成本收尾**（~30 分钟）：`versions.json` 接入 freshness + `data_gaps()` 补海外
   characterMissions 边界说明。
3. **调研候选方向**：先出文献结论与可行性评估再立项——「信任评分形式化」最贴合当前
   痛点（新表全 A 级后离散枚举局限凸显）。
4. **P2 收尾**：`crawler.py` 渐进拆分（纯维护性，可与调研并行）。
5. P3：WinUI / 排行榜 / 漫画正文按外部条件评估。
