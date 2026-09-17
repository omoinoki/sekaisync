# 未完成 / 未完全完成项复盘（2026-09-18）

> 目的：在"已确认完成且可验证"的修复之外，逐项判断剩余项**是否还有必要修**。
> 核心判据不是"当初说没做完"，而是：**问题现在是否依然存在？还是已被后来的重构超越？**
> 所有结论均对**当前代码**（分支 `longrun/2026-09-16-b0-b6-interim` @ `e260602`）
> 核实，不依据 CHECKPOINT 等文档——文档已被证明系统性落后于代码。
> 本文只做判断与建议，**未改动任何源码或测试**。

---

## 结论摘要

| 项 | 当初判定 | 当前核实 | 是否仍需修 | 优先级 |
| :--- | :--- | :--- | :--- | :--- |
| **P11** layered 落库 | 未做 | 仍缺失（`apply_scrub_result` 等 0 处存在） | **要修**，且与 P08 证书门是同一件事 | 高 |
| **P08/P09** 槽证书门 | 未做 | 通道证据形态不满足证书门；外部提交路径 `verifier=None` | **要修**（是 P11 的前置） | 高 |
| **P06** `web_browse` | 未做 | **已被超越**（过滤/排序/LIMIT 已下推 SQL），但**仍 9.5s** | **要修，但性质变了** | 中 |
| **P14** worker ContextVar | 未做完 | **核心缺陷已修**（`CrawlTaskContext` + 每任务 `copy_context`） | **不必再修**（仅剩代码整洁度） | 低 |
| **P15** MCP 协议 | 未做完 | `templates/list` 已做；**版本协商仍无** | **要修（仅剩一半）** | 中 |
| `status()` 性能 | 未测 | 已缓存，cold 57s / warm 2.2s，366 KB < 512 KB 上限 | **不必修**（有更值得做的项） | 低 |
| **B8** 发布门禁 | 未达标 | 性能/互操作/平台矩阵/故障点矩阵均有缺口 | **作为门禁保留，不单独"修"** | — |

**一句话**：6 项里 **3 项仍要修（P11+P08 实为一件、P06-browse、P15 一半）**，
**2 项已被后来的重构超越，不必再投入**，1 项维持门禁性质。

---

## 一、P11 + P08/P09：仍要修，而且是**同一件事**

### 现状（硬证据）

- `apply_scrub_result` / `validate_slot_for_commit` / `review_item_from_decision`
  在整个仓库（含文档）出现 **0 次**。是"不存在"，不是"存在但没人调"。
- `scrub_trinity` **确实返回** `slot_decisions`（`trinity.py:2371`，由 `24a26fb` 补上），
  但 `--layered` 分支（`cli.py:826-905`）只读 `accepted/pending/conflicts/rejected/stats`，
  **`cli.py:843` 把 `slot_decisions` 丢掉了**。
- 该分支最后的写操作只有两个：`apply_methodology_batch`（写 `methodology.json`）与
  `enqueue`（写 `review_queue.json`）——**都是 JSON 文件**，
  **全程没有触碰 SQLite `term_slots`**。
- 易混淆点：`cli.py:944` 的 `commit_slot_decisions` 属于**非 layered** 的
  `terms extract` 路径，不是 layered 落库。此前我自己的表述也差点把两者混为一谈。

### 但真正的阻塞不在"有没有函数"，而在证书门

即便现在写出 `apply_scrub_result`，也不会真正采纳——三个独立原因：

1. **形态不匹配。** 证书门要求**每条证据**带 `story_key`(单数)/`language`/`source`，
   且 `term`/`value`/`sentence` 三者之一匹配（`term_slots.py:139-142`）。
   而六个通道产出的是**聚合计数**：`stories=<count>`、`votes`、`sim`，
   外加一个**复数** `story_keys=[...]`。全仓 `trinity.py` 里
   单数 `story_key` 只作为 `_Corpus` 的方法参数名出现，**从未作为证据字段**。
2. **唯一的 verifier 只认官方索引。** `index_verifier`（`term_slots.py:156-210`）
   只查 `glossary_terms WHERE official=1`，与语料证据无关——
   它能认证 L0，**永远无法认证 trunk/hub/translit**，而后者才是真正产出发现的通道。
3. **外部提交路径一律 `verifier=None`。** `cli.py:944` 与 `termindex.py:873`
   调用 `commit_slot_decisions` 时**都没有传 verifier**；
   `_certificate` 在 `term_slots.py:104-105` 遇 `None` 直接返回 `None`，
   于是任何非 L0 决策**静默落入 `pending`/`insufficient_evidence`**，且不报错。

### 判断

**仍要修，且必须按序**：(a) 让通道产出**可入库的证据行**（单数 `story_key` +
`language` + `source` + 原句/值）→ (b) 增加**语料感知的 verifier**并从
`cli.py:944` 传入 → (c) 才写 `apply_scrub_result` 把 `slot_decisions` 翻译成
decisions + `evidence_by_id`。**顺序颠倒会得到"成功但全是 pending"的假绿。**

**警告**：不要为了省事放宽 `_certificate`。它正在做真实的"反借权威"工作，
且语义被 `tests/test_term_slot_migration.py::
test_explicit_matching_verifier_evidence_accepts_only_one_slot` 钉住。

---

## 二、P06 `web_browse`：**性质已变**——原缺陷已被修复，但仍有真实成本

### 我此前的判断**不准确**，在此更正

我在前几轮多次把 browse 描述为"全表有序扫描"。核实后：**它早已被重构超越**。

- `web_browse`（`webindex.py:961`）委托 `dbstore.browse_web_rows`
  （`dbstore.py:924-1023`），过滤 + `ORDER BY` + `LIMIT` **已下推 SQL**
  （`dbstore.py:990-995`）。
- 它是**两阶段**：阶段一**只扫元数据**（显式排除 `text`，`dbstore.py:989`）；
  阶段二仅对 K 个 winner 按主键取 `length(text)` 与 `substr(text,1,300)`
  （`dbstore.py:1010-1017`）。
- 换句话说，Astra 当初反对的"全库对象化"**已经解决**。

### 但仍有真实成本，不应就此关闭

- **实测 `web_browse(limit=20)` 中位数 9.5s**（真实库）。
- 原因不是物化，而是**排序**：排序键是
  `(CASE source 优先级, crawled_at DESC, source, seq)`。
  `web_pages` 的索引为 `idx_pages_kind/lang/aux/canonical/seq`（`dbstore.py:138-142`），
  **没有 `crawled_at` 索引**，而优先级是**计算出来的 CASE，本质上不可索引**。
  所以它在过滤后的集合上排序，LIMIT 只能限制返回、不能限制排序工作量。
- **且这与候选索引无关**：`candidates`（`searchindex.py:374`）需要 `query`
  参数，browse 没有查询串，无从做 trigram 查找。把索引套到 browse 上**不解决问题**。

### 判断

**要修，但方案与原来不同**：不是"下推 SQL"（已做），而是
**要么给 `crawled_at` 建索引并改造优先级排序使其可索引**，
**要么改排序语义**（例如改按 `seq` 这类已有索引的键排）。
这属于**中等优先级**——它不影响正确性，只影响延迟；且改动会触及排序契约，
需要重新验证与旧实现的逐行等价（P06 的四态排序契约已有测试）。
若不接受排序变化，则应明确记录"browse 延迟受全表排序限制"而不再列为缺陷。

---

## 三、P14 worker ContextVar：**已被超越，不必再修**

### 现状

- `CrawlTaskContext` 存在（`crawler.py:1776-1805`，frozen dataclass，
  含 `endpoints/ms_instance/sv_instance/cache_root/cache_namespace`）。
- 提交方式正确（`crawler.py:1852-1853`）：**在提交前** `capture()`，
  每个任务 `contextvars.copy_context().run(task.run, ...)`，
  各自独立 Context，不在 worker 内部 capture。
- `task.run`（`crawler.py:1795-1805`）用 `finally` 复位三个隐式变量，
  并用 `_ms_instance_scope`/`_sv_instance_scope` 包裹，**worker 抛异常也恢复**。
- 已有回归测试覆盖复用线程场景：
  `tests/test_p14_crawler_isolation.py:299`、`:313`。

### 判断

**核心隔离缺陷已修**。剩下的只是**代码整洁度**：helper 仍通过
`_current_ms_instance()` 等读取 ContextVar（约 35 处），而非接收显式参数。
但**设置者已经变成每个任务的快照**，不再是调用方遗留的环境状态——
正确性已成立。

**不必再修**。若要动，改动面很大（35 处签名）而收益为零正确性收益，
且会引入回归风险。建议把"helper 仍读 ContextVar"记录为**已知整洁度欠账**，
不列为缺陷，也不作为合并阻塞。

---

## 四、P15 MCP：**一半已完成，剩一半要修**（需拆开记录）

### 已完成的一半

`resources/templates/list` **已实现**（`mcp_server.py:254-259`），
模板数据独立（`mcp_server.py:72-93`），与 `resources/list`（`:248`）分离，
且 `resources/read`（`:260`）拒绝字面模板 URI。
当初"把带变量 URI 当普通资源列出"的缺陷**已修复**（`ebec6d0`）。

### 仍未完成的一半

- `PROTOCOL_VERSION` 仍是 **`"2024-11-05"`**（`mcp_server.py:28`）。
- `initialize`（`mcp_server.py:225-237`）**完全忽略 `params`**：
  不读、不比较、不校验客户端 `protocolVersion`，直接回自己的常量。
  全仓无 `SUPPORTED_VERSIONS`。
  按 MCP 规范，服务端应回显客户端支持的版本或报错；现在**新旧客户端都静默通过**。
- HTTP 侧同样暴露该值（`http_server.py:293,302`）。

### 判断

**要修**，且必须**同时做两件事**：抬高 `PROTOCOL_VERSION` **并**实现
supported-version 校验。**只改字符串等于假升级**（Astra 原文明确反对）。
另注：`MAX_STDIO_LINE_BYTES`（`mcp_server.py:31-33`）自述为"P15 临时预算"，未校准。

**记录建议**：把 P15 拆成两条——`templates/list`（已完成）/ `initialize 协商`（未完成），
不要作为整体关闭或整体挂起。

---

## 五、`status()` 性能：不必修

- 已走 `_cached_aggregate`（`core.py:1087`）。
- **实测**：cold 57.3s，warm **2.19s**，序列化 **366 KB**，**未超** 512 KB 上限
  （`core.py:256`），**所以确实被缓存**。
  （此前有推测认为它会超出上限而永不缓存——实测否证了该推测。）
- 子聚合中 `trust_summary`、`progress` 已各自缓存；
  `freshness`/`term_status`/`news`/`store_stats` 未缓存，
  因此 cold 仍是多次独立读盘。

### 判断

**不必修**。warm 2.19s 已可接受；cold 57s 只在进程首次调用发生。
若要优化，收益明确且低风险的是给那四个子聚合加缓存——
但**优先级远低于 P11/P08，不建议占用当前窗口**。

---

## 六、B8 发布门禁：不"修"，保留为门禁

B8 的缺口（性能、真实客户端互操作、平台矩阵、发布故障点矩阵）
**不是可以靠再写几个补丁关闭的**，它们的性质是"需要外部条件与实测"。
继续维持"未达标"结论，并在 CHECKPOINT 的《明确不能说的》中保留。

---

## 七、合并建议（基于以上核实）

- **技术上可合并**：与 `main` 零冲突、`main` 无新提交、879 测试全绿、
  契约 `drifted: []`、真实 store 只读未改。
- **内容上属于 Astra §3.4 允许的可交付中间态**：未完成的危险写入口
  现在会**明确拒绝**（未知 schema 拒写、无 runtime 的联网调用报错），
  而非"照常写错数据"。
- **合并前已完成的三件事**（`e260602`）：
  1. 分支改名 `b0-b3` → `b0-b6-interim`（原名严重低估真实范围）；
  2. `.gitignore` 随仓库走（原只在 `.git/info/exclude`，不随克隆传播，
     341 MB 索引有误提交风险）；
  3. 修正工具链 sqlite 版本（3.32.2 是系统 CLI，运行时为 3.50.4；
     trigram 索引要求 ≥3.34）。
- **仍建议由人工确认合并**：66 个提交推给消费方是对外可见动作，
  且 B8 未达标意味着不能宣称"发布就绪"。

---

## 附：本轮更正的我自己此前的不准确表述

1. **"web_browse 仍是全表有序扫描"** —— 不准确。过滤/排序/LIMIT 已下推 SQL，
   文本列已排除；真实剩余成本是**排序不可索引**，不是物化。
2. **"P14 worker 仍经 ContextVar 未修"** —— 作为**正确性**判断不准确：
   隔离缺陷已由 `CrawlTaskContext` 修好，剩下的只是传参整洁度。
3. **"status() 可能超缓存上限永不缓存"** —— 被实测否证（366 KB < 512 KB）。

---

## 执行结果回填（2026-09-18，W1/W2/W3 已执行）

本文的"仍要修"判断已按派发清单执行完毕，结果如下。**本文其余部分保留原状**，
作为决策当时的记录；以下为执行后的事实。

| 项 | 执行前判断 | 执行结果 | 提交 |
| :--- | :--- | :--- | :--- |
| **P11 + P08/P09** | 要修，是同一件事 | **已完成**：通道证据行（W1-A）→ 语料 verifier（W1-B）→ `apply_scrub_result`（W1-C）。`--layered` 现在真正写 `term_slots` | `196d425` `0e329c1` `57cdf3f` |
| **P06 browse** | 要修，性质已变 | **已完成**：索引快路径；副本实测 7.66s→0.12s（62x），8 种过滤组合逐行等价 | `ad16469` |
| **P15 MCP** | 要修（剩一半） | **已完成**：`SUPPORTED_VERSIONS=("2025-06-18",)` + initialize 协商 + HTTP 头校验 | `2b4d99a` |
| P14 整洁度 | 不必修 | 未做（维持判断） | — |
| `status()` | 不必修 | 未做（维持判断） | — |
| B8 门禁 | 维持 | 维持"未达标" | — |

**执行中被证伪的三处本文/派单假设**（已逐条记录在对应提交里）：

1. **`apply_scrub_result` 之前还有一道坎**：`cli.py` 的槽提交路径把 `TermRecord`
   传给了按 Mapping 读取的 `decisions_from_record`，`AttributeError` 崩溃 ——
   该分支**从未成功提交过**。由 W1-B 发现并修复；这是我在 `8e7ea48` 引入的缺陷，
   说明此前"P08 已接线"的说法高估了写入侧的完成度。
2. **browse 的成本主体不是排序**：谓词 `count(*)` 单独就 7.5s、最小投影同语句 8.07s，
   主导是扫描与行读取。索引之所以有效，是因为它让查询**根本不读那 747k 行**。
   本文第 16-20 行"剩余成本是排序不可索引"的措辞不准确。
3. **browse 实测值域**：普通 `limit=20` 是 7.2–8.1s（跨会话因页缓存波动到 15.4s），
   本文写的 9.5s 与 CHECKPOINT 的"11–14s"都不是普通用例的值。

**仍未完成（新增记录，勿宣称）**：

- 老库**不会自动获得** `idx_pages_browse`：`_SCHEMA` 只在建新库时执行，
  `ensure_store`/只读路径均已实测不补建。老库保持正确但慢，需显式迁移补建。
- `extract_terms_local` 不写 `source` 到证据行（LLM 路径会写），
  故该路径下非源语言槽仍永远 pending（W1-B 已用测试钉住该边界）。
- browse 带 `source=`/`kind=` 过滤仍受 `_sql_filter_values` 的 5 列 DISTINCT 拖累（~7.6s）。
- P15 仍不可宣称"协议符合 MCP 2025-06-18"（通知 202、GET 行为、真实客户端互操作未做）。
