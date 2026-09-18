# SekaiSync 0.4.0-alpha 发布说明（2026-09-18）

> 版本号统一为 `0.4.0-alpha`（`__init__.py` 与 `pyproject.toml`；此前分别为
> 0.3.0 / 0.3.2-alpha，互相不一致）。
> 本版相对 0.3.x 是一次**数据格式升级**：请完整阅读"升级"一节再动库。

---

## 概要

本版完成了 GPT-6 Astra 审计（`work/ASTRA_RESPONSE_2026-09-16.md`）的主体修复：
schema 升到 **v3**（逐区服事实）、术语译名改由**逐语言槽**权威裁决、
raw/news 以**不可变代际**发布、每个请求固定**读快照**、运行时端点**不再全局串台**、
MCP 支持协议协商。查询性能在真实库（75 万页 / 6.2 亿字符）上有数量级改善。

测试基线 361 → **967**（全部本地运行，0 skip、0 expectedFailure、零第三方运行依赖不变）。

## 升级（必读）

**不兼容声明**：schema v3 的库**不能被 0.3.x 旧程序打开**——旧构建会拒绝并提示
（这是 P07 的设计行为：宁拒不读，不静默重写）。请先备份再升级。

```bash
# 0. 备份（强烈建议：任何迁移前先做一致性副本）
python -c "import sqlite3; s=sqlite3.connect('file:store/kb/sekaisync.db?mode=ro',uri=True); d=sqlite3.connect('backup.db'); s.backup(d)"

# 1. 迁移 schema（一次只升一级；每级先 dry-run，再带着 digest 与新备份路径 apply）
python -m sekaisync --store store migrate --to 2 --dry-run
python -m sekaisync --store store migrate --to 2 --plan-digest <上一步输出的 plan_digest> --backup backup-v1.db
python -m sekaisync --store store migrate --to 3 --dry-run
python -m sekaisync --store store migrate --to 3 --plan-digest <上一步输出的 plan_digest> --backup backup-v2.db

# 2. 补建 browse 索引（老库不会自动获得；3 秒左右，收益见下）
python -m sekaisync --store store web-rebuild
```

迁移语义（重要）：
- 旧 `terms` 的每个译名会变成一个 **pending 槽**（`legacy_unverified`），旧 trust 只保留在
  `legacy_payload` 里作审计——**不会**把旧 A 级借给任何译名。用
  `sekaisync terms review` 流程或带 verifier 的提交路径逐个认证为 accepted。
- 迁移后 `terms.names_json` 由 accepted 槽投影：初始为空属预期，认证一个亮一个。
- digest 用于防"授权时与执行时库已变化"；store 变过会被拒绝，重跑 dry-run 即可。

## 新能力

- **逐区服事实（P03）**：`entity_region_facts` 按区服保存事实与来源；多区服实体不再
  "后写覆盖先写"。查询带 `region` 才给出该区服的答案，否则显式 `unknown`/`needs_region_data`。
- **逐语言槽权威（P08/P09/P11）**：译名按 (term, language) 逐槽裁决；accepted 槽是
  `names_json` 的唯一投影来源；`--layered` 现在真正把裁决写入槽库。
- **不可变代际发布（P13/P17）**：raw 主数据与 news 按 SQL 指针指向的完整代际读取，
  部分区域同步不再影响其他区域的读取一致性。
- **MCP 协议协商（P15）**：`initialize` 支持并回显 `2025-11-25` / `2025-06-18`；
  不可识别的合法版本以**反报价**（而非报错）应答——经生态调研，拒绝式会中断全部主流客户端。
  畸形版本仍拒绝。资源模板独立列出，读取字面模板 URI 被拒。
- **查询性能**：`web_search` 163.6s → **0.18s**（选择性查询，召回经差分测试逐行等价）；
  `web_browse` 8.0s → **245ms**（老库需执行上面的 `web-rebuild` 补索引）。

## 修复

- web 页写入现在递增 `data_revision`（P02：派生缓存此前无法感知页面变化）。
- i18n 增量爬取按**内容**判重（此前只按页 id 跳过，远端内容变化永远爬不到）。
- `terms extract` 的槽提交路径此前在写入前即抛错（TermRecord 传给按 Mapping 读取的函数），
  从未成功提交过；已修复并由驱动真实命令的测试覆盖。
- `apply_methodology` 一次性裁决落库、冲突结构不再把候选名当语言键（P10/P11）。
- factpack 语言按请求选取并如实报告 `effective_language`；未公开/无日期实体扣下全部内容（P05）。

## 已知边界（不在本版宣称）

- **消费方同步（P21）未做**：WinUI / DSH 对 v3 新表（逐区事实、逐槽状态）的展示需各自适配。
  DSH 走 REST，实测不受 MCP 变更影响。
- **性能**：browse 带 `source=`/`kind=` 过滤仍慢（过滤值枚举 ~8s）；broad 文本查询回退全扫描；
  `status()` 冷启动 ~60s。
- **MCP**：实现了 initialize 协商与版本头校验；2025-06-18 其余一致性项（通知 202、GET 行为）
  与真实客户端互操作矩阵未验收——不宣称"符合 MCP 2025-06-18"。
- **平台矩阵**：实测仅 Windows / Python 3.13.14 / SQLite 3.50.4；FTS5 trigram 索引要求
  SQLite ≥ 3.34（缺失时自动回退慢路径，功能不受影响）。
- 运行依赖仍为**零第三方**（`dependencies = []`）。
