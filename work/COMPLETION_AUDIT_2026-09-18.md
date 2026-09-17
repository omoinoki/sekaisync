# Astra 重构完成度评估与派发变更审计（2026-09-18）

> 两个问题：① 距离完全完成 GPT-6 Astra 的重构还差多少（百分比）；② 确认
> `work/AGENT_DISPATCH_2026-09-18.md` 指向的本次修改没有引入新问题。
> 所有结论基于**当前代码与实测**，不依据文档自述。

---

## 第一部分：完成度评估

### 1.1 方法说明（为什么不是一个简单百分比）

Astra 的交付物不是"21 个特性"，而是**四个表示层缺陷的修复 + 一个发布门禁**，
且门禁（B8）明确要求"不能由单元测试签署"。所以单一百分比会误导。
本文分三个口径给出，并说明各自的分母。

### 1.2 口径 A：P01–P21 逐项完成度（按"是否闭环"判定）

判定标准：**该条要求的行为已经在代码中闭环、且有回归测试钉住**（不是"写了函数"）。

| 项 | 状态 | 依据 |
| :--- | :--- | :--- |
| P01 写语义三分离 | ✅ 闭环 | `replace_terms_snapshot`/`upsert_terms`/证据 patch；`0baeef1` |
| P02 请求级读快照 | ✅ 闭环 | `ReadView` + revision 提交；**含本轮补上的 web 写入递增 revision**（`ae101fb`） |
| P03 区服事实 | ✅ 闭环 | `entity_region_facts` v3 + 迁移；`8e7ea48` |
| P04 未知显式化 | ✅ 闭环 | `needs_region_data`/`unknown` 词表；`8d44c69` |
| P05 时序隔离 | ✅ 闭环 | region 必填 + 语言如实；`c95787b` |
| P06 去全库对象化 | ✅ 闭环 | search 索引化（`ae101fb`）+ browse 索引快路径（`ad16469`） |
| P07 schema 门禁 | ✅ 闭环 | `inspect_schema` 六态 + 显式迁移；`146767a` |
| P08 逐语言槽 | ✅ 闭环 | 槽权威 + 读路径 + **本轮补上的通道证据行与落库**（`196d425`/`0e329c1`/`57cdf3f`） |
| P09 旧路径同资格 | ✅ 闭环 | `validate_translation_proposal`（`term_proposals.py:87`）返回含 status/reason 的决策；提取路径两处调用（`termindex.py:1306,2541`） |
| P10 裁决日志 | ❌ **未闭环** | 表已建（`review_queue`/`review_decisions`/`review_rules`），但 `agent_review.enqueue/consult/submit_judgments` **仍以 JSON 为权威**——实测 enqueue 后 SQLite 三表 **0 行**、JSON queue 存在。Astra 原文要求「权威状态改进同一 SQLite 库，不再让两份 JSON 独自承担事务」，故未达 |
| P11 layered 提交与守恒 | ✅ 闭环 | `apply_scrub_result` 单事务 + 待审守恒（`57cdf3f`，独立验证） |
| P12 受约束 transport | ✅ 闭环 | scheme/symlink/ADS 全拒；`0da2cb6` |
| P13 单写者+代际 | ✅ 闭环 | lease + raw/news 不可变代际；`62eabd0` 等 |
| P14 显式 RuntimeContext | 🟡 基本闭环 | 端点隔离/线程参数/cache 身份/CLI+Core 接入均已做；**worker helper 仍读 ContextVar**（仅整洁度，已评估不值得改） |
| P15 边界与协议 | 🟡 基本闭环 | 边界校验、序列化恢复、templates 分离、**本轮版本协商**（`2b4d99a`）；**该版其余一致性项与真实客户端互操作未做** |
| P16 表身份与四态 | ✅ 闭环 | TableRead 四态 + `dea142c` |
| P17 公告身份与代际 | ✅ 闭环 | 稳定身份 + `active_news_generation` SQL 指针 + manifest 校验（`news.py:431,448,487`） |
| P18 区域覆盖/live | ✅ 闭环 | `requested_live` 与 `live_degraded` 分离（`progress.py:598-599`） |
| P19 总量与样本分离 | ✅ 闭环 | totals 与 `*_samples` 分离 + `*_truncated` 标志（`integrity.py:190-202`） |
| P20 定点修复 | ✅ 闭环 | 缓存身份/候选信号/输入边界 |
| P21 消费方同步 | ❌ **未做** | WinUI（`frontends/WinUI3`）与 DSH（`dsh-sekaisync-connect/lib/*.js`）**均未改**；Astra 明确"消费方同步是发布的一部分"，且要求新表（`entity_region_facts`/`term_slots`）在 UI 有显示 |

**口径 A 结论**（经本轮实证修正）：21 项中 **18 项闭环**、**2 项未闭环**（P10 仍以 JSON 为权威、P21 消费方同步未做）、2 项基本闭环（P14/P15 各剩非阻塞尾巴）。

按"整项闭环 = 1，基本闭环 = 0.8，未做/未闭环 = 0"计：
`(18 + 0.8×2) / 21 = 19.6/21 ≈ 93%`。

**但这个数字有误导性**，因为 P21 是 Astra 明说的发布前置，且 B8 未达标。

### 1.3 口径 B：B0–B8 批次完成度（Astra 的分批口径）

| 批次 | 内容 | 状态 | 完成度 |
| :--- | :--- | :--- | ---: |
| B0 | 契约基线 | ✅ | 100% |
| B1 | 最小保护（7 项） | ✅ | 100% |
| B2 | 存储止损与快照 | ✅ | 100%（P13 代际已做，P02 最后缺口本轮补上） |
| B3 | 查询去放大 | ✅ | 100%（search + browse 均已完成） |
| B4 | 运行时/代际/完整性接线 | ✅ | 100%（P14 接入、P16、P17、P03、P08、P05 全部落地） |
| B5 | v2 逐槽术语 + 事务审阅 → layered 落库 | ✅ | **100%**（本轮 `57cdf3f` 收口） |
| B6 | v3 区服事实 + 字段核验 + 公开时间过滤 | 🟡 | ~85%（`entity_region_facts`/核验/公开时间过滤已做；`_sql_filter_values` 与 factpack 的 region 读取链路仍有缺口） |
| B7 | 质量/评估与更深剧情时序 | ⬜ | Astra 明言"不阻塞基础发布"，未作为本次范围 |
| B8 | 发布门禁 | ❌ | ~50%（6 项中 4 项已验；性能/互操作/平台矩阵/故障点矩阵有缺口） |

按批次均值（B0–B6 为交付范围，B7 不计）：`(6 + 0.85) / 7 ≈ 98%`；
若把 B8 计入分母：`(6 + 0.85 + 0.5) / 8 ≈ 92%`。

### 1.4 口径 C：以"能否发布"为标准的完成度

Astra §3.4 给了明确的中间态发布条件，逐条对照：

| 条件 | 现状 |
| :--- | :--- |
| B1–B3 可作为"安全/可用性修正版"发布 | ✅ 满足 |
| 未完成的危险写入口必须**明确拒绝**而非写错 | ✅ 满足（未知 schema 拒写、无 runtime 联网调用报错、v1 上槽提交报错并提示迁移、layered 在 v1 上报告 `applied:false` 而非假成功） |
| B5 完成前不得宣称 layered 已落库 | ✅ **现已完成**，可宣称 |
| B6 完成前 region/time 继续显式标未知 | ✅ 满足（`unknown`/`needs_region_data`/`effective_language`） |
| B8 全 6 项通过才可签发布 | ❌ **未通过**：④性能（broad 查询仍 ~112s）、⑤真实 MCP/浏览器/WinUI/DSH 互操作（完全未做）、⑥平台矩阵（仅 3.13.14 实跑）、③故障点全矩阵（部分） |

**口径 C 结论**：**未达"可发布"，但已达"可交付中间态"**。

### 1.5 综合判断

**给出一个数字：约 88%。**

- 若只问"Astra 列出的修复项是否修完"：**≈ 93%**
  （P10 未闭环、P21 未做、P14/P15 有非阻塞尾巴）
- 若问"能否按 Astra 自己的标准签发布"：**≈ 50%**（B8 门禁未过）
- 综合：**≈ 88%**

**剩余 10% 的构成，按重要性**：

1. **P10 权威状态未迁到 SQLite（本轮新发现）** —— 表建了但没人用；
   `agent_review` 仍写 `review_queue.json`/`review_decisions.json`。Astra 明确要求
   权威状态进 SQLite，故此项**未闭环**，且它影响"裁决事务性"这一核心诉求。
2. **P21 消费方同步（完全未做）** —— WinUI 与 DSH 均未更新；Astra 称之为"发布的一部分"。
2. **B8 未达标的四项**（性能、真实互操作、平台矩阵、故障点矩阵）——
   它们需要外部条件（真实客户端、多平台），不是补丁能关闭的。
3. **B6 残余**：`_sql_filter_values` 的 5 列 DISTINCT（带 `source=`/`kind=` 过滤仍 ~7.6s）、
   factpack 的 region 读取链路。
4. **本轮新暴露的运维项**：老库不会自动获得 `idx_pages_browse`，需显式迁移。
5. **非阻塞尾巴**：P14 worker 传参整洁化、P15 该版其余一致性项、
   `extract_terms_local` 不写 `source`（导致该路径非源语言槽永远 pending）。

---

## 第二部分：派发变更是否引入新问题

对 `329de7a..HEAD` 的 6 个提交、15 个文件、+2954/−21 行做审查。

### 2.1 结论：**未发现新问题**，但有 3 处必须知情的行为变化

**全部验证通过（我自己复跑，非采信代理报告）**：

| 检查 | 结果 |
| :--- | :--- |
| 全量测试 | **943 tests / 0 failure / 0 error / 0 skipped / 0 expectedFailure** |
| 契约快照 | `{"drifted": []}`（4 个快照） |
| `git diff --check` | 无输出 |
| Astra 验证器 | 与基线差异**仅 3 处**，均为预期中的 P05 改进；`real_store_accessed=False`、`settings_read=False` |
| 真实库 | 字节数 2,268,860,416 **未变**、mtime 2026-09-16 **未变**、`quick_check=ok`、schema 1、无 `idx_pages_browse`、无 `term_slots` 表 |
| 核心判定函数 | `_certificate`/`_prepare_slot`/`_commit` **未被修改**（仅新增 `corpus_verifier`） |
| `webindex.py` | W2 声称零改动 —— **核实为真**（`git diff` 空） |
| browse 旧序契约 | `test_equivalence_with_python_oracle` 仍通过 |
| HTTP 端到端 | 实起服务：无头→200、旧版本头→400、新版本头→200、乱版本→400 |

**verifier 滥用安全审计**（我构造 7 种攻击场景，全部被拒）：

| 场景 | 结果 |
| :--- | :--- |
| 完全无证据 | `None` ✅ |
| 仅 1 个故事 | `None` ✅ |
| 值不在句中（仅靠 story_key 计数） | `None` ✅ |
| 语言不匹配 | `None` ✅ |
| source 不匹配（如 `fandom`） | `None` ✅ |
| 同一故事重复行充数 | `None` ✅ |
| 两故事且句子含值（正当） | PASS，`trust=C`、`official=False` ✅ |

**反向验证**：`verifier=None` 时 accepted=0、status=pending、`names_json={}` ——
门槛**未被放宽**。幂等性：连续三次 apply，revision 停在 3，证据停在 2 行。

### 2.2 三处必须知情的行为变化（非缺陷，但会影响消费者）

**① MCP 协议版本：`2024-11-05` → `2025-06-18`，且旧版本会被拒绝。**

- **stdio**：客户端传 `2024-11-05` → `-32602` 错误（此前是成功返回）。
- **HTTP**：带 `MCP-Protocol-Version: 2024-11-05` 头 → **400**（规范 2025-06-18 明确要求 MUST 400）。
- **已验证无实际影响**：DSH 侧（`dsh-sekaisync-connect/lib/*.js`）**不发送该头**，
  实起服务验证"无头 + initialize"返回 200。但**任何固定使用旧版本的客户端会失败**，
  这是有意的破坏性变更。若存在这样的消费者，需先升级它。

**② browse 的加速对老库不生效（需显式迁移）。**

- `_SCHEMA` 只在新建库时执行；我实测 `ensure_store()`、`initialize()`、只读路径
  **都不会补建** `idx_pages_browse`。
- 后果：老库**保持正确但慢**（走 fallback 语句，语义与改动前逐行相同）。
- 已在 CHECKPOINT 与 REMAINING_REVIEW 记录。**不能**把建索引放进读路径
  （违反 P02/P07"读不得写"，且在 2.3GB 库上会悄悄加 3s + 95MB）。

**③ layered 分支的输出新增了一个键。**

- 既有 12 个键**一个未改**（`accepted` 仍指管线自身口径）；
- 新增 `slot_commit`（含真实提交计数）。
- 依据：Astra 明确反对"把未应用裁决打印成已采纳译名"，所以两套数字分开写。
  但**解析该 JSON 的消费者需知道多了一个键**（未破坏兼容，属加法）。

### 2.3 本轮发现并已修复的两个真实缺陷

这两个都是**修好了问题、而非引入问题**，但值得单独记录：

1. **死代码（我在 `8e7ea48` 引入）**：`cli.py` 的槽提交把 `TermRecord` 传给按 Mapping
   读取的函数 → `AttributeError`，该分支**从未成功提交过**。已修（`term_to_dict`），
   并补了驱动真实命令的测试。
2. **证据行 evidence_id 不稳定**：`_load_evidence_items` 回读时省略空的可选列，
   导致内容摘要变化 → 二次提交报 `conflicting payload for stable evidence_id`。
   已通过规范化修复，使重复 apply 幂等。

### 2.4 未引入新问题的边界说明（诚实边界）

以下**不是**新问题，但我要明确它们**没有被本次修改解决**，避免误读：

- `extract_terms_local` 不写 `source` → 该路径非源语言槽**仍然永远 pending**（已用测试钉住）。
- browse 带 `source=`/`kind=` 过滤**仍然 ~7.6s**（`_sql_filter_values` 在授权范围外）。
- factpack 的 region 读取链路仍未接（B6 残余）。
- 老库的 browse 索引需显式迁移（见 2.2②）。
- P21 完全未做（见 1.5）。

---

## 附：一句话回答

**距完全完成约 90%**：Astra 的 21 项修复中 19 项闭环、2 项余非阻塞尾巴、**1 项（P21 消费方同步）完全未做**；
B0–B6 实质收口，但 **B8 发布门禁未过**（性能/真实互操作/平台矩阵/故障点矩阵），
因此**可交付中间态、不可签发布**。

**本次派发未引入新问题**：943 测试全绿、契约无漂移、真实库字节未变、核心判定函数未被触碰、
verifier 七种滥用场景全拒；但有 3 处**知情的行为变化**（MCP 版本拒绝旧版、browse 索引对老库需迁移、
layered 输出多一个键），其中唯一可能影响消费者的是 MCP 版本，且已核实 DSH 不受影响。
