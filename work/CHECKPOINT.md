# 执行 Checkpoint 账本（2026-09-16 18:00 → 23:00）

> 交接用。每条提交后更新。**22:55 必须产出完整交接状态**（即使未完成）。
> 依据：`work/ASTRA_RESPONSE_2026-09-16.md` + `work/ASTRA_EXECUTION_GUIDE_FOR_ZCODE_2026-09-16.md`

---

## 起点状态（18:00）

| 项 | 值 |
| :--- | :--- |
| 基准提交 | `4c07f67` |
| 工作分支 | `longrun/2026-09-16-b0-b3` |
| 测试基线 | **361 项全绿**（我方实测 38.893s；Astra 43.479s） |
| Astra 验证器 | ✅ 可重放（94 叶子键 **0 差异**，见 `work/astra_verify_replay.json`） |
| 真实 store | `store/kb/sekaisync.db` 2.26 GB / web_pages 752,372 行 —— **只读对待** |
| 工具链 | python 3.13.14 / sqlite3 3.32.2 / node v24.19.0 / dotnet 8.0.425 |

**新测试数账本**：基线 **361** → 当前 **563**（+202，全绿）

### 前置验证结论（详见 `work/PLAN_VERIFICATION_2026-09-16.md`）

方案可执行。B1 涉及的 7 项诊断全部经**源码定位 + 动态复现**确认真实：

- **D01** 动态复现：存 `[t1]` 后再存 `[t2]` → terms 表仍为 `['t1','t2']`（确无快照替换语义）
- **D07** 动态复现：meta 置 `'99'` → 调 `ensure_store()` → **静默降级为 `'1'`**
- **D04/D05/D12/D15/P20d** 源码定位确认（`glossary.py:161` `or term.canonical`；`factpacks.py` future 分支仍出全文；`fetcher.py` tar 不拒 symlink；`http_server.py:318` CORS `*`）

**4 处偏差（不影响开工，已记录）**：

1. `tests/test_dbstore.py`、`tests/test_trinity.py` **不存在** —— Astra 的 P07/P19/P20c 验收命令引用它们，**B0 必须新建 `test_dbstore.py`**
2. DSH `tests/backend-contract.test.mjs` **不存在** —— 该验收命令不可用，改用自建契约快照
3. `experiment/` **不在** `.git/info/exclude`（指南红线第 8 条不准确）—— 提交只用显式路径，**禁止 `git add -A`**
4. `llm_client.py:53-73` 无 key 时**仍发请求**（仅不加 Authorization 头）—— P15 的"缺配置零网络"是真实缺口

### B1 并行分组（已核实文件不重叠）

| 执行者 | 项 | 文件 | 测试 |
| :--- | :--- | :--- | :--- |
| 智能体 A | P07 | `dbstore.py`, `cli.py` | 新建 `test_dbstore.py` |
| 智能体 B | P12 | `fetcher.py`, `crawler.py`, `config.py` | `test_fetcher/webcrawler/config_sites.py` |
| 智能体 C | P15+P20d | `tools.py`, `mcp_server.py`, `http_server.py`, `llm_client.py`, `worldlink.py`, `eventalias.py` | `test_mcp/http_mcp/llm_client/worldlink/eventalias.py` |
| 主程序 | P04+P05 | `glossary.py`, `core.py`, `factpacks.py` | `test_glossary/core/factpacks.py` |
| 主程序 | P19 | `integrity.py` | `test_integrity.py` |

---

## 进度追踪

| 批次 | 内容 | 状态 | 提交 | 测试数 | 备注 |
| :--- | :--- | :--- | :--- | ---: | :--- |
| B0 | 契约基线（核实诊断 + 建红测） | ✅ 完成 | 快照在 `work/contract_snapshots/` | 361→ | 观察器 94 键 0 差异 |
| B1 | 最小保护 P07/P12/P15/P20d/P19/P04/P05 | ✅ **7/7 完成** | 见下表 | 563 | 全部已提交并独立验证 |
| B2 | 存储止损与快照 P13/P01/P02 | 🟡 P01/P02 完成；P13 部分 | 见下表 | — | P13 的 raw 代际发布未做（见"未完成"） |
| B3 | 查询去放大 P06 | 🟡 部分 | `517980a` | — | 元数据投影已做；browse/search 未做 |
| CKPT | 22:55 交接检查点 | ✅ 产出（本文档） | — | — | 硬性 |

**测试账本**：基线 **361** → 当前 **563**（+202）。**全绿**。

---

## 已完成项明细

（每项：提交哈希 · 改动文件 · 验收证据）

**本次共 31 个提交**（`git log --oneline 4c07f67..HEAD`）：

| 项 | 提交 | 文件 | 验收证据 | 测试数 |
| :--- | :--- | :--- | :--- | ---: |
| **P05** 时序隔离 | `d1f8f4e` | `factpacks.py`, `tests/test_factpacks.py` | Astra 探针 `future_payload_contains_unreleased_name` **True → False**；future/undated 全文不序列化，改为 withheld 计数 | +10 |
| **P04** 未知显式化 | `8d44c69` | `glossary.py`, `core.py`, `tests/test_glossary.py`, `tests/test_verify_claims.py` | 缺译名 → `target_name=None`/`missing`；无匹配实体与自由文本未命中 → `unknown`（不再是 conflict）；新增显式字段核验 | +17 |
| **P19** 总量/样本分离 | `3fcb4bb` | `integrity.py`, `tests/test_integrity.py` | `str(None)` 折叠为 `"None"` 导致**不同正文被判为镜像**（已修）；`limit` 不再限制总数 | +5 |
| **P02** 读快照与缓存版本 | `9186226` | `core.py`, `dbstore.py`, `tests/test_core_snapshot.py` | 动态复现竞态后修复：`first=old, second=old`（期望 `new`）。版本计算前捕获、计算后校验；`current_revision`/`bump_revision`/`CoreSnapshot`/`ReadView`/`request_view()`；64 键 LRU | +5 |
| **P06** 元数据投影 | `517980a` | `dbstore.py`, `core.py` | 实测：`load_web_index_rows` **37.6→26.0s**；`trust_summary` **40.4→11.6s**；等价性用旧 oracle **证明相等** | — |
| **P07** schema 门禁 | `146767a` | `dbstore.py`, `cli.py`, `tests/test_dbstore.py` | 修复前：`99 → 1` **静默降级**；现 `inspect_schema()` 区分 absent/current/older/newer/corrupt，拒写且文件字节不变 | +17 |
| **P20d** 输入边界 | `19ad672` | `worldlink.py`, `eventalias.py`, tests | `_parse_ordinal('0')→None`、`'1000'→None`、5000 位数字**不抛异常**返回 None；别名可往返 | +79 组内 |
| **P12** 网络/归档边界 | `0da2cb6` | `fetcher.py`, `crawler.py`, `config.py`, tests | `file:`/`data:`/`ftp:`/userinfo 均拒；**symlink 逃逸被拒且外部无字节**；`../`/绝对路径/ADS/保留名全拒；正常归档仍可解 | — |
| **P01** 写契约 | `0baeef1` | `dbstore.py`, `tests/test_terms_write_contract.py` | 快照真替换（`[t1]`→`[t2]` 后仅 `t2`）；`replace []` → count=0 且 0 行；preserve 语义；append 去重不涨 idx；`expected_revision` 冲突拒绝 | +10 |
| **P13** lease + 原子写 | `bf2f7fd` | `fetcher.py`, `filecache.py`, `source_migrate.py`, `tests/test_writer_lease.py` | **真实子进程**持锁时父进程被拒，子进程退出后可获取；迁移 rebuild 失败不再伪装成功 | +10 |
| **P15** 协议/HTTP/LLM 边界 | `ee8f719` | `tools.py`, `mcp_server.py`, `http_server.py`, `llm_client.py`, `crawler.py`, tests | **实起 HTTP 服务**验证：`events/check` GET→**405**；跨源→**403 且无 ACAO**；坏 Host→**403**；无 Origin 本地请求→200。MCP 错误码 -32600/-32601/-32602 正确且错误后合法请求仍成功。无 key 时 `urlopen` **调用数 0** | +126 组内 |

**B1 七项全部完成**（P07/P12/P15/P20d/P19/P04/P05）。
**B2**：P01/P02 完成；P13 完成 raw 代际发布与读取者迁移，但 freshness/文件投影仍在
SQL 事务内写文件、迁移 rebuild 仍删行后重导（见"未完成"，2026-09-17 复审确认）。
**B3**：P06 完成 browse + search。
**B4**：P14/P16/P17 仅有局部修复；**集成契约与发布验收未完成**（P17 news 仍就地写、
P16 事件检查仍写 legacy 布局、P14 运行时上下文未接入真实 worker 路径）。

**上表未列的后续提交**（都是本轮实际改动，按时间序）：

| 提交 | 内容 |
| :--- | :--- |
| `da9495e` | MCP 超长帧被丢弃时写 stderr 日志（P15 follow-up） |
| `5f65db9` | **修 include_text 回归**：投影不含 `text` 导致返回空串 |
| `83b362c` | search 流式 top-k |
| `11c97b9` | **lease 真正接进写路径**（此前实现了但没人用 = 无保护） |
| `0b13754` | 迁移持 lease |
| `807248a` | **修 top-k 堆的两个静默错误**（详见《失败与坑》第 6 条） |
| `e9e8307` | lease 覆盖剩余三个写入口（review / postprocess / news） |
| `54d1fff` | `data_gaps()` 声明「多文件聚合可能混代」（P13 保守半边） |
| `62eabd0` | **P13 raw 代际发布**（prepare + 原子指针提交） |
| `e122cfc` | raw 读取者迁移到代际指针 |
| `93b2ba5` | 收窄 `data_gaps()` 的混代声明（raw 已代际化，news 未） |
| `c55ef5a` | **P17 公告稳定身份**（不再用标题） |
| `8da4472` | **P16 关系行自然键**（137 行不再只剩 1 行） |
| `c95d216` | **P14 显式 RuntimeContext**（端点不再全局串台） |
| `530bf1e` | **P11 冲突结构转换**（不再造出 `SEKAI:translit` 假语言） |
| `0e8aef8` | **P10 一次性裁决日志**（`ordinary_reused: null` → 可复用） |
| `3dae60e` | **P01 调用者迁移完成**（产品代码不再调用旧含糊写接口） |
| `0ab18e3` | **P08 定点修复**（缩写形态≠实体证据；edge-only 不再静默消失） |
| `7e0ea9b` | **P09 官方合并只提升其实际供给的槽**（WrongName/A 组合消除） |

**红测双向验证**（移除修复后确认变红）：
P04/P05 13 处失败 · P19 5 处失败 · P02 竞态复现 · P07 `99→1` 复现 · P01 `t1` 残留复现 ·
P13 迁移伪成功复现 · P06 参数顺序/长度/堆方向共 3 类复现。均加回后全绿。**非伪绿**。

**最终状态**：**648 tests / 全绿** · `git diff --check` 无输出 · 契约快照零漂移 · 工作区干净。

**性能实测（真实 store，只读；均为实测非估算）**：

| 项 | 修复前 | 修复后 |
| :--- | ---: | ---: |
| `load_web_index_rows` | 37.6s | 26.0s |
| `trust_summary` | 40.4s | **11.6s** |
| `status()` | 63.1s | **26.0s** |
| `web_browse(limit=20)` | 50.2s | **~21s** |
| browse 峰值 Python 内存 | 全量对象化（数百 MiB） | **0.88 MiB**（limit=20）/ 1.46 MiB（limit=500） |

**等价性已验证**（这是必做项，不是可选项）：

- `web_browse`：真实库上 **17 种过滤组合**与旧全量扫描算法逐行对比 —— id、顺序、snippet、text_length **全部相等**
- `trust_summary`：真实库上旧逐行算法作 oracle，两个 dict `old == new` → **True**

**诚实说明**：**未达成**方案里期望的 "124s → <1s"。`web_browse` 由 50.2s 降至约 21s，
剩余成本是 752k 行的线性扫描本身；`web_search` 仍是 O(N) 候选。
Astra 原文亦明确要求**不得宣称已获得 0.281s**。
进一步降低需要索引/引擎级改动 + 经过测量的召回等价设计，属明确的后阶段工作，**此处不作宣称**。

**DB 迁移注意（P02）**：`meta.data_revision` 是**新增键**。旧库没有该键时 `current_revision()` 返回 **0**，
即"未建立 revision 语义"。真实 store（2.26 GB）尚未写入该键 —— B2/P13 建立写路径时才会 bump。
接手方若看到 revision 恒为 0，说明写入口还没接上，不是 bug。

**当前全量**：**581 tests / 全绿**（基线 361）。所有提交均可 bisect、各自全绿。
按 Astra 要求，修复行为的红测先在工作分支确认变红，再与对应最小修复一并提交 —— 未用 `skip`/`expectedFailure` 伪造全绿。

### P05 的实施边界（重要，勿夸大）

`build_fact_pack_at` **无 `region` 参数**，因此它实现的是**公开时间过滤**，不是逐区服、逐章节的防剧透。
有测试 `test_as_of_without_region_is_not_promised_region_correct` 显式钉住这一点 —— 若将来有人加了 region 支持，该测试会失败并提示重新核对 P03/B6。
对外**不得**宣称"任意剧情进度防剧透"。

### P04 的状态词表变更（消费方需同步）

```text
matched      期望字符串出现在命中实体的 names 中（名称匹配，非事实核验）
supported    显式 field 且存储值一致
conflict     显式 field 且存储值不同 —— 必带 actual + 证据
ambiguous    命中实体但未提供 expected
unknown      查不到实体，或存储不carry该字段 —— 缺失数据不是反证
needs_region_data  多区服实体 + 区服敏感字段 + 未指定 region
```

**`unverified` 状态已不再返回**（改为 `unknown`）。DSH/WinUI 若按旧字符串判断需同步。

---

## 未完成项的精确起点

（Astra 方案编号 + 当前代码状态 + 下一步动作）

### P15 — 协议/HTTP/LLM 边界（B1 的最后一项，**已完成**）

**状态**：已提交 `ee8f719`。B1 的七项至此全部完成。

**已独立验证**（不依赖智能体自述，实起 HTTP 服务）：

```text
health                : 200
GET events/check      : 405        <- 旧 GET 写入路径已关闭
cross-origin GET      : 403  ACAO=None   <- 无宽容 CORS 头
no-Origin GET         : 200  ACAO=None   <- 本地 CLI/Node 仍可用
bad Host              : 403
MCP: -32600 / -32601 / -32602 正确，且错误后合法 tools/list 仍成功
无 key 时 urlopen 调用次数 = 0
OpenAPI 已从 ToolSpec 派生（build_openapi(TOOLS)），手工常量漂移风险已消除
```

**未做**：Streamable HTTP 2025-06-18 完整一致性；真实 MCP 客户端与浏览器互操作测试。

### P17 — 公告身份（**已完成**）

**缺陷（动态复现）**：身份原为 `language:title`，同标题的两条不同公告会合并成一条，**记录静默丢失**。
真实公告里重复标题极多（如"メンテナンスのお知らせ"）。

**修复**：`news_identity` = (backend 命名空间, 上游 id, 语言)。

- 命名空间取 backend：同 backend 的两个实例是已知镜像、共享 id 空间 → 可去重；
  不同 backend 的 id 空间无关，按 Astra"无证据不跨站合并"不合并（moesekai#1 ≠ sekai_viewer#1）
- 上游 id 为主；仅在无 id 时才用规范 URL（否则会误并共享同一 URL 的不同公告）
- 标题只在既无 id 又无 URL 时参与

同时移除了 `_prefer_record` 的"正文越长越新"启发式 —— Astra 明确反对（修订可能删减）。
改由源时间戳决定，回退到 source 优先级，**不**用正文长度、也**不**用输入顺序。

**Astra 验收三项全部通过**（且已验证"移除修复则变红"）：

```text
同标题不同 ID 均保留            -> 2
同 ID 的新短修订胜旧长修订       -> 短
不同后端不因同标题合并           -> 2
```

**注意**：真实库的 news 文件当前为空，故该缺陷尚未造成实际数据丢失（属潜在缺陷）。

**剩余（B4）**：P14（RuntimeContext/线程/缓存）、P16（活动关系与完整性）未做。

---

---

### P09 — 官方合并提升范围（**核心缺陷已修**）

**缺陷（本机复现）**：`merge_terms` 按语言槽"先到先得"保留名称，但把 official/trust/confidence
按**整词**合并：

```text
WrongName (community C) + CorrectName (official A)
-> names.en = WrongName, official = True, trust = A     <-- WrongName/A，正是 Astra 拒绝的组合
```

未验证译名借官方记录获得了 A 级权威。

**修复**（`7e0ea9b`）：

- 官方记录**覆盖**其实际供给的语言槽中的非官方值；被替换值保留进证据（Astra："冲突保留两边证据"）
- `source` 只在官方值实际被保留时跟随官方来源
- `trust` 只在官方值实际被保留时升至官方档——官方记录在所有槽都落败时**不再**向社区值捐赠信任
- 两条社区记录合并（先到先得）行为不变

**Astra 观察器佐证**：`term_authority_and_conflict_shape/merged_name` 由 `WrongName` → **`CorrectName`**，
`merged_source` 由 `llm` → `master_db:jp`。

**同时修（P08 定点，`0ab18e3`）**：

1. **缩写形态规则是候选资格而非实体证据**：`ニーゴ→N25` 与 `ニーゴ→N99` 在
   "同首字母+数字"规则下得分完全相同（0.6）——它无法区分正确别名与邻近负例。
   新增 `romaji.is_abbrev_form_only` 暴露"此匹配仅由缩写规则支撑"，trinity 将其归入
   `translit_low` 而非已确认音译。真音译（セカイ→SEKAI 等）不受影响；
   `is_plausible_translation("ニーゴ","N25") == True` 的门控行为保持不变（N25 仍可经真实对齐证据采纳）。
   没有硬编码 N99 黑名单（Astra 明确禁止）。
2. **edge-only 术语不再静默消失**：`merged_names` 与 `term_conflicts` 均空时的提前
   `continue` 会把"只有边缘档候选"的术语从输出中完全抹掉——不 accepted、不 pending、
   不 conflict，统计与逐槽决定不再守恒。现记入 pending（"看到候选但无法确认"正是审阅队列存在的意义）。

**仍未做**：P08 主体（SlotDecision/term_slots/v2 迁移/逐 (subject_id, language) 决定）、
P09 的 ExtractionContext 显式化。**逐槽溯源仍需 v2。**

---

### P01 — 写契约调用者迁移（**已完成**）

`replace_terms_snapshot` / `upsert_terms` 此前已实现但**产品代码零调用** —— 旧含糊接口
`save_terms_records` 仍是唯一写路径，"快照替换不删除"的缺陷在各处依然活着。

`3dae60e` 把 4 个调用点全部迁移，每个调用点显式声明意图：

| 调用点 | 意图 | 新调用 |
| :--- | :--- | :--- |
| `core.merge_zhfirst_terms` | 名称更新 + 证据保留 | `upsert_terms`（无 evidence_updates = preserve） |
| `cli.cmd_terms_init` | 全量快照（init/reset） | `replace_terms_snapshot`（缺席 id 删除） |
| `cli.cmd_terms` 抽取 | 局部增量 | `upsert_terms` + 显式逐 id 证据替换 |
| `dbstore.import_legacy_domains` | legacy JSON 全量导入 | `replace_terms_snapshot` |

`save_terms_records` 已标记 deprecated（docstring 说明其无法表达的三种意图并指向新 API），
**保留可用** —— Astra 离线观察器用它记录基线缺陷。产品代码零调用。

**Astra 观察器佐证**：迁移后 `review_lifecycle/ordinary_requeued/skipped_settled` 由 0 变 **1**
—— P10 的"重新 enqueue 同项不打扰"经由新的裁决日志生效，这是跨项联动的直接证据。

---

### P10 — 一次性裁决持久化（**核心缺陷已修**）

**缺陷（Astra 探针证据 `ordinary_reused: null`，本机复现）**：不带 `generalize` 的
accept 裁决**只计数不落盘** —— `submit_judgments` 返回 `accepted: 1`，但方法论没有写入任何条目，
`consult` 永远返回 `None`。结果：同样的术语每轮刮削都重新打扰智能体，"干预量随时间递减"的核心机制失效。

**修复**：新增**独立的一次性裁决日志** `kb/terms/review_decisions.json`，与方法论刻意分离：

| 存储 | 职责 |
| :--- | :--- |
| `review_queue.json` | 待审项 |
| `methodology.json` | **可复用规则**（generalize 沉淀，pair/pattern） |
| `review_decisions.json`（新增） | **裁决本身**（一次性，不带 generalize 也持久化） |

**边界（被既有测试逼出来的三个不变量）**：

1. `consult` 只在**方法论文件不存在**时查决策日志 —— 方法论一旦存在就拥有裁决权：
   损坏文件 fail-closed（不被旧决策绕过）；退休规则不被"产生它的那个决策"复活
2. `enqueue` 把日志命中计为 `skipped_settled` —— 重新入队同项不再打扰智能体
3. `decision_id` 用内容哈希 —— 重复提交幂等（`decisions_added: 0`）

**Astra 观察器佐证**：`review_lifecycle/ordinary_reused` 由 `null` 变为完整可复用决策
（`accept / Good / source: decision`）。**第三方证据，非自述。**

**新增 4 项测试**：复用、作用域隔离（不同术语不串）、幂等、skip-settled。

**仍未做（P10 其余部分）**：v2 三表设计（SQLite 中的 review_queue/decisions/rules，
含 scope 与证据版本）——本提交在**现有文件格式内**落地了"裁决必须持久化"的保证。

---

### P11 — layered 冲突数据契约（**契约缺陷已修，落库未做**）

**缺陷（用真实生产者输出复现）**：`arbitrate` 返回的冲突形如

```text
{"lang": "en", "candidates": {"SEKAI": ["translit"], "SEKAI2": ["translit"]}}
```

语言在**外层** `lang`，`candidates` 的键是**候选译名**、值是通道。
`_names_from_conflict` 却把 `candidates` 当成 `{language: candidates}` 遍历：

```text
旧输出 -> {'SEKAI': 'translit', 'SEKAI2': 'translit'}   <-- 假语言
新输出 -> {'en': ['SEKAI', 'SEKAI2']}                  <-- 保留真实语言槽
```

**Astra 观察器佐证**：其探针 `term_authority_and_conflict_shape/cli_conflict_proposal`
基线记录 `{"SEKAI": "translit", ...}`，现为 `{"en": ["SEKAI","SEKAI2"]}`。

**同时修掉两处一致性问题**：

1. 只取首候选提案，但入队检查用完整候选集合 → 可能出现"既未采纳也不入队"。
   Astra 要求 consult/enqueue 使用**同一候选集合**。
2. `_proposals_from_rows` 文档写 `{term: {lang: [candidates]}}` 却只 append 单值；
   现同时接受 `{lang: value}` 与 `{lang: [values]}`，冲突路径不再丢候选。

**新建 `tests/test_trinity.py`**：Astra 指出该模块**零直接覆盖**，缺陷因此长期未被发现。
新测试直接驱动 `arbitrate` 本体（非手写 fixture），含真实生产者 → CLI 转换的端到端往返。新增 12 项。

**仍未做（P11 主体，属 B5）**：`apply_scrub_result` 单事务写 term_slots/证据/待审队列、
`candidate_tiers.validate_slot_for_commit` 唯一资格门、按 `(subject_id, language)` 结算、
`TermRecord` 落库。**layered 仍未真正持久化，不要宣称已落库。**

---

### P14 — 显式 RuntimeContext（**核心缺陷已修**）

**缺陷（动态复现）**：外部端点存在**进程级全局快照**里，`configure_endpoints` 整体替换它，
于是"最后配置者获胜"对**所有人**生效，包括已在执行中的工作：

```text
配置 A 之后              : https://AAA.example
B 配置之后 A 的值        : https://BBB.example    <-- A 被静默替换
```

**修复**：新增 `sekaisync/runtime.py`。

| 能力 | 说明 |
| :--- | :--- |
| `RuntimeContext`（frozen dataclass） | 不可变端点快照 + sites + fetcher + source_ids |
| `build_runtime(config)` | 从 config 的 sites 派生快照，**不碰全局状态** |
| `runtime.activate()` | 上下文作用域安装端点，退出时恢复（**异常也恢复**） |
| `require_endpoints(runtime)` | 缺 runtime 时抛 `RuntimeNotConfiguredError`，**不**回退全局快照 |
| crawl 入口 | 从**本次调用自己的 settings** 派生快照，交错/嵌套互不泄漏 |

**刻意不改**：`SekaiSyncCore(store_root)` 仍可无 runtime 使用 —— 纯本地读取不应要求联网配置；
现有 CLI 入口也继续可用。改变的是"**持有 runtime 的调用方不再依赖全局状态**"。

**已验证**：A/B 交错隔离（含 B 嵌套在 A 内）、两线程各 100 次迭代互不串台、
异常后上下文恢复、派生快照不改动全局。新增 14 项测试。

**未做（P14 其余部分）**：线程任务的 `instance_id`/`cache_namespace` 显式参数化、
缓存键纳入 backend+instance+完整 URL+解析器版本、`CrawlContext` 直接传 worker 取代 ContextVar。
**不要宣称 P14 全部完成。**

---

### P16 — 活动关系行身份（**核心缺陷已修**）

**缺陷（动态复现 + 真实库量化）**：`_merge_event_rows` 用 `str(record.get("id"))` 作键。
真实 `eventMusics.json` **137 行全部没有 `id`**，于是全部落到同一个键 `"None"`，只有第一行存活：

```text
真实 eventMusics 行数    : 137
旧算法保留              : 1        <-- 丢失 136 条关系
```

**下游影响**：`_build_jp_box_map` 判定箱活**必须有专属曲目**，关系丢失后第一个活动之外的事件都没有歌，
于是全部无法判为箱活。测试里事件 5 被判为 `other` **正是数据丢失的症状**，不是该事件的真实属性 ——
拿到歌曲行后它正确判为箱活（marathon 类型 + rarity_4 卡 + 专属曲 + 单一人类 unit）。

**修复**：`_row_identity` 为每类表定义自然键。

- 关系表（`eventMusics`/`eventCards`/`eventDeckBonus`）用真实关系字段组合
- **`seq` 刻意排除在身份之外** —— 它是排序/版本，不是身份；否则同一关系换个 seq 就能重复追加
- 实体表用 `id`
- **无任何可用键的行用整行内容作身份**：不同行绝不碰撞，完全相同的行仍去重
  （Astra："去掉完整行相同的重复"），而不是一起塌成 `None`

`_merge_table_records` 有反向的同类陷阱：`if key:` 守卫会**直接丢弃**无 id 的行。
当前该路径上的表都有 id，故暂无损失，但已一并改为同一套键，避免以后把 `eventMusics` 接过去时复活该缺陷。

**Astra 观察器佐证**：其自带探针 `event_relationship_identity` 基线记录 `merged_relationships: 1`（缺陷证据），
现在为 **3**，且原先被丢弃的两行可见。这是第三方证据，非我方自述。

**验证**：真实库只读复核 137 行进 / 137 行出、零键碰撞；新增 8 项验收测试；
P16 验收命令组（event_detection/eventalias/worldlink/registry/core）**100 测试全绿**。

**仍未做（P16 其余部分，属较大工程）**：`TableRead` 的 missing/invalid/valid_empty/valid 四态区分、
"缺表不输出 high"、CLI/Core 共用同一发布流程、按完整快照判定 up_to_date。当前 `up_to_date` 仍可能
在关联表缺失时短路。**不要宣称 P16 全部完成。**

---

### P13 — 不可变 raw 代际与跨文件发布指针（**已完成** ✅）

这是 B2 里**唯一没有完成**的部分，也是 Astra 明确划为"必须重构"的边界。

**已全部完成**（`bf2f7fd` + `bc1b15f` + `8713905` + `e9e8307` + `62eabd0` + `e122cfc`）：

| 能力 | 状态 |
| :--- | :--- |
| `WriterLease` / `store_writer_lock`（跨进程，Windows msvcrt / POSIX flock） | ✅ 已实现并**已被使用** |
| 实际持锁的写入口 | ✅ **全部 7 个**：`sync`、`crawl_altsource_ms`、`crawl_altsource_sv`、`rename_legacy_source_ids`（非 dry-run）、`agent_review.submit_judgments`、`postprocess.mark_untranslated_pages`、`news.sync_news` |
| `write_json_atomic`（fsync + os.replace，失败不动原文件） | ✅ 已实现 |
| 迁移 rebuild 失败不再伪装成功 | ✅ `errors` + `ok` 标志 |

**代际发布已实现**（`62eabd0`）：

| 能力 | 状态 |
| :--- | :--- |
| `prepare_raw_generation`：新代际目录 `raw/generations/<id>/<region>/source` | ✅ 完全不动已发布状态，失败即清理 |
| `publish_generation`：BEGIN IMMEDIATE 内提交索引 + 指针 + freshness + revision | ✅ 指针翻转与提交是同一事件 |
| 指针存于 SQL `meta.active_raw_generation`（而非文件） | ✅ 才能与索引原子提交 |
| 逐区服指针独立（只同步某服不影响其他服） | ✅ 实测：同步 en 后 jp 指针与 418 个文件不变 |
| 旧代际保留不回收 | ✅ 首轮不做 GC |
| 读取者迁移（registry/progress/event_detection/eventalias/worldlink） | ✅ `e122cfc` |

**实测验证**（用真实库的 jp / en 树作本地镜像，无网络）：

```text
发布后：62,551 实体入库，指针已设，代际目录已填充，revision=1
失败注入 1（prepare 中）：指针不变、半成品目录已清理、读者仍见完整旧代
失败注入 2（提交前）：   指针回滚、索引回滚、实体数不变
worldlink / eventalias / progress / event_detection 均解析到代际目录，找到 419 个表
```

**剩余（明确不属于本项，Astra 亦标注为后续）**：

| 缺口 | 说明 |
| :--- | :--- |
| **kb/news 的代际** | 本项只做了 raw；news 仍是就地写 —— `data_gaps()` 的声明仍然有效 |
| 旧代际回收 | 首轮保留，需显式维护时再处理 |
| 断电持久性 | Astra 明确：不能凭原子 rename 声称所有硬件断电场景已验证 |

**为什么重要**：Astra B2 明确要求 —— 在代际发布完成前，多文件聚合不得宣称整代一致。
**接手方至少应先把这一点补上（保守化），再实现代际。**

**下一步动作**（Astra P13 原文顺序）：

```text
1. layout 新增 generations 路径与"从指定快照选 raw 路径"的函数
2. 迁移所有 raw 读取者（registry / progress / event_detection / eventalias / worldlink）
   —— 一次请求固定同一指针集合，不能每张表重查"当前"
3. 写路径：prepare 到 raw/generations/<uuid>/<region>/ → 验证全部文件 →
   BEGIN IMMEDIATE → 写 active_raw_generation + freshness → bump revision → COMMIT
4. 旧 raw/<region>/source 保留为已验证基线，不自动删除
5. 给上表三个未加锁写入口补 lease（照抄 cursor 的做法即可）
6. 给多文件聚合加 not_ready/unknown 降级声明
```

**验收**：每个故障点（prepare 前/索引写前/提交前/提交后）读者只见完整旧代或完整新代。

### P06 — 查询去放大（**browse 与 search 均已完成**）

**已完成**：元数据投影 + trust SQL 聚合（`517980a`）+ browse 的 SQL 下推（`b5eb614`）
+ search 的流式 top-k（`23167c3`）。

| 函数 | 变化 | 内存 |
| :--- | :--- | :--- |
| `web_browse` | 过滤/排序/LIMIT 下推到 SQL，正文只为 K 条命中读取 | **0.88 MiB**（limit=20，原为数百 MiB 级全量对象化） |
| `web_search` | 流式读取 `title` + `substr(text,1,20000)`（与基线评分窗口一致），固定 K 的 heap | O(limit + batch) |

**search 为什么保留 Python 打分**：Astra 明确**拒绝**改成 `LIKE '%query%' LIMIT K` ——
那会改变召回与来源优先级，不是提速。评分循环**仍是线性**的，本次收益是内存与消除全页中间列表，
**不是**算法级加速。

**search 的真实成本（实测，勿粉饰）**：查询 `活动剧情` 耗时 **约 195s**。
其中流式取行只有 **41s**，其余 **~154s 是模糊评分本身**（实测 ~3.4ms/行 × 747k 行），
这部分**在本次改动之前就存在**，只是被全量加载掩盖了。
**不要**把这项宣称为性能提升；收益是内存与正确性。

**⚠️ 本轮由验证抓出的真实缺陷（已修，`5dcf513`）**：
流式 top-k 的第一次实现有两个**静默**错误 —— 都返回"看起来合理但不对"的结果：

1. **堆保留的是前 K 个而非最好的 K 个**（min-heap 的根实为最优项，替换条件写反）
2. **同分顺序反了**：旧实现用稳定排序，同分保持库内顺序；新堆用升序 arrival 作 tiebreak，
   导致**同分时淘汰最旧而非最新**。真实库上这个查询 **30 个候选里有 26 个同分**，所以 tiebreak 决定结果。

**这两个都不是单测能发现的**：第一版与 oracle 在前 2 行一致、之后分歧。
**接手方修改排序/限额逻辑时，务必用真实库的 oracle 逐行对比，不要只跑单测。**

**`text_hash` 行为变更**：不再由正文重算（流式路径拿不到完整正文），改为读存储列。
`save_web_pages` 本就会为空白 hash 填充该列，所以结果不变 —— 已用
`sha256_hex(text)` 对比验证过。

**等价性验证结果**：

| 项 | 方法 | 结果 |
| :--- | :--- | :--- |
| `web_browse` | 真实库 17 组过滤 vs 旧全量扫描 | ✅ id/顺序/snippet/text_length **全部相等** |
| `web_search` | 真实库单查询 vs 旧全量扫描（`work/_search_equiv2.py`） | ✅ **修复后相等**（8 行返回、30 候选） |
| `trust_summary` | 真实库旧逐行算法作 oracle | ✅ `old == new` → True |

**诚实说明**：**未达成**方案里期望的 "124s → <1s"。`web_browse` 由 50.2s 降至约 21s，
剩余成本是 752k 行的线性扫描本身。Astra 原文明确要求**不得宣称已获得 0.281s**。
进一步的降低需要索引/引擎级改动 + 经过测量的召回等价设计，属明确的后阶段工作。

### B6 / P03 — 逐区服事实（未开始，本轮范围外）

`facts` 仍是单份无区服归属的 dict，所以 `verify_claims` 对多区服实体的区服敏感字段返回
`needs_region_data`（已实现该保守行为），`build_fact_pack_at` **没有 `region` 参数**。
有测试钉住这两点，加了 region 支持会失败并提示回来核对。**在此之前不得宣称逐区服正确或防剧透。**

---

## 消费方同步状态

### DSH（`C:\dsh_projects\dsh-sekaisync-connect`，**非 git 仓库**）

**已做的改动**（1 处，`lib/backend.js` 的 `compactResolve`）：

旧代码 `${r.target_name || ''}` 会把新的 `null` 渲染成空白 —— P04 之后 `null` 表示
"该语言未覆盖"，是**有意义的信息**，渲染成空白就丢了这个含义。现改为：

```text
target_name == null 且 translation_status == 'missing'  →  未覆盖（规范名 …）
target_name == null 其他情况                          →  未知
target_name 有值                                      →  原样显示
```

**已验证**（`node --check` 通过 + 实测渲染输出）：

```text
missing  : character:1 [character] Hoshino Ichika → 未覆盖（规范名 Hoshino Ichika）（official=true trust=A score=1）
available: character:1 [character] Hoshino Ichika → 星乃一歌（official=true trust=A score=1）
legacy   : x [k] A → B（official=false trust=C score=0.5）      ← 旧响应仍兼容
```

**注意**：DSH 不调用 `verify`，也不传 `as_of`（已确认九个工具中无此调用），
所以 P04 的 `unknown` 状态词表变更与 P05 的时序参数**不影响 DSH 现有路径**。

### WinUI（`frontends/WinUI3`，主仓 submodule）

**本轮未改动**。需要接手方注意的契约变更：

1. `resolve_name` 结果的 `target_name` 现在可能为 **`null`**（此前总是字符串）。
   若 WinUI 按字符串处理会显示空白或 `null` —— 应显示"未覆盖"，并用 `canonical_name` 做展示名。
2. `verify_claims` 的状态词表变更：`unverified` **不再返回**（改为 `unknown`），
   新增 `supported` / `needs_region_data`。按旧字符串判断的地方需同步。
3. `integrity` 的 `issues` 语义变更：现在是**总问题数**，样本数在 `issues_sample_count`，
   并有 `*_truncated` 标志。

### 消费方契约快照（B0 建立，防漂移）

```bash
python -B -X utf8 work/b0_contract_snapshot.py --check   # 漂移则为 exit 1
```

本轮检测到并**逐条核对过**的漂移：仅 `event_check` 的 `http_method` `GET → POST`（P15 有意为之）。
工具数仍 24，`mcp_name`/`http_path`/`wrap`/参数**零变化** —— DSH 九工具不受影响。

## 独立验收证据：Astra 观察器复跑对比

这是**最强的一条证据** —— 用 Astra 自己的观察器（不是我的测试）复跑，看缺陷是否真的变了。

```bash
python -B -X utf8 work/astra_verify_2026_09_16.py   # 输出见 work/ASTRA_VERIFY_AFTER_FIXES_2026-09-16.json
```

**关键结果**（`baseline → now`，全部是缺陷转为正确行为，**无一项变坏**）：

| Astra 记录的缺陷证据 | 基线值 | 现在 |
| :--- | :--- | :--- |
| `aggregate_version_race.after_version_bump.value` | `old` | **`new`** ✅ 竞态已修（P02） |
| `temporal_and_language_contract.future_payload_contains_unreleased_name` | `True` | **`False`** ✅ 泄露已关（P05） |
| `temporal_and_language_contract.undated_past_nonempty` | `True` | **`False`** ✅ undated 也扣下（P05） |
| `missing_translation_fallback.target_name` | `JapaneseOnly` | **`None`** ✅（P04） |
| `verification_absence_as_conflict.missing_fact_status` | `conflict` | **`unknown`** ✅（P04） |
| `unsupported_schema_version.version_after_ensure` | `1` | **`99`** ✅ 不再降级（P07） |
| `unsupported_schema_version.refused_instead_of_downgraded` | （无） | **`True`** ✅ |
| `web_read_filter_and_provenance.lookup_calls_full_load` | `True` | **`False`** ✅（P06） |
| `integrity_missing_hash.different_text_conflict_groups` | `0` | **`1`** ✅ 不同正文不再误判镜像（P19） |
| `integrity_missing_hash.mirror_duplicates` | `1` | **`0`** ✅ |
| `activity_inputs_and_unknowns.long_input` | `ValueError` | **`no error`** ✅ 超长输入不再抛（P20d） |
| `activity_inputs_and_unknowns.wl0[2]` / `wl1000[2]` | `1` | **`None`** ✅ 显式 0/1000 不再变成 1（P20d） |
| `web_read_filter.metadata_query` | 含 `text` 列 | **不含 `text`**，改用 `length(text)` ✅（P06） |

`source_inventory` 的行数/hash 变化属预期（代码改动）。

### ⚠️ 观察器本身需要一处适配（已改）

`probe_schema` 原实现直接调 `ensure_store()` 去**观察**降级行为。P07 修好后该调用会**抛
`SchemaVersionError`**，导致整个观察器在第 331 行中断、后续探针全部不执行。
我已把它改成捕获异常并**报告**结果：

```python
"refused_instead_of_downgraded": refused is not None,   # True = P07 生效
"refusal_error": refused,                              # 如 "SchemaVersionError"
```

**接手方注意**：这处修改是**观察器适配**，不是产品代码。若把 `refused_instead_of_downgraded`
改回 `False`，说明 P07 门禁**回退了**。

---

## 失败与坑

（记录尝试失败的方案与原因，避免接手方重走）

### 1. `_PAGE_COLUMNS` 里就有 `text` —— 只改 SELECT 是无效的

P06 第一次修改时我仍用 `', '.join(_PAGE_COLUMNS)` 拼 SQL，只是把 `len(text)` 换成 SQL 的 `length(text)`。
实测 40.4s → 37.4s，几乎没变。**根因是 `_PAGE_COLUMNS` 本身包含 `text`**，必须显式排除：

```python
index_columns = tuple(c for c in _PAGE_COLUMNS if c != "text")
```

**教训**：优化前先用 `cProfile` 定位，不要凭直觉改。我第一次改完只快了 3 秒，profile 才发现
真正的成本是 `_row_tuple_to_dict` **每行新建一个类对象**（752,400 次 `__build_class__`），
比 SQL 查询本身还贵。

### 2. 逐行 oracle 对比是验证等价性的唯一可靠办法

P06 改信任统计时，正确性风险是"计数静默漂移"—— 比慢更糟。
我没有只跑单测，而是在真实库上把**旧算法逐行原样跑一遍**当作 oracle，比较两个 dict：

```python
old == new   # True
```

**接手方改这段代码时请沿用这个方法**，不要只依赖 `tests/`。

### 3. 并发智能体改同一文件会丢更新

`dbstore.py` 同时被 P07（智能体）和 P01/P02/P06（我）改。我在自己写入前发现智能体已读过旧版本，
发了明确警告要求它**重新读取并只用定向 Edit**（不要整文件 Write）。

另外提交时用 `git diff > patch` + 正则切分 hunk，只暂存属于本项的改动，
保证每个提交可 bisect。**不要用 `git add -A`** —— `experiment/` 并未被 git 排除（见 PLAN_VERIFICATION）。

### 4. 不要用 `skip` / 整文件 Write 求快

`tests/test_dbstore.py` 在我实现 P07 期间**故意保持红色**（1 个失败），
实现完成后才随修复一起提交。这是 Astra 明确要求的做法：
"针对修复后行为的新测试先在本地工作分支确认会变红，再与对应最小修复一起进入可交付提交"。

### 5. 性能目标未达成，如实记录

方案 §2.1 写的是 "124s → 预期 <1s"。实际 `web_browse` 50.2s → 约 21s。
**Astra 自己也写了不得宣称 0.281s**。不要因为数字不好看就把这一项标成完成。

### 6. ⚠️ 最贵的教训：top-k 堆的两个静默错误（单测发现不了）

`web_search` 改成固定 K 的 heap 后，我写了 6 个单测**全绿**，但真实库 oracle 对比显示
**结果不对**。两个错误都属于"返回看起来合理但不对的行"：

1. **保留了前 K 个而非最好的 K 个**：`heapq` 是 min-heap，我按质量顺序
   `(rank, -score, arrival)` 作 key，于是 `heap[0]` 是**最优**项，`entry < heap[0]` 永不成立，
   替换从不发生。正确做法是让 key **随质量下降而变小**（`-rank`、`+score`），
   根节点才是"最差的那个"，替换条件用 `entry > heap[0]`。

2. **同分顺序反了**：旧实现用**稳定排序**，同分保持库内顺序。堆的 tiebreak 必须
   让"更晚到达"成为**更差**（`-arrival`），否则同分时淘汰最旧而非最新。
   真实库上这个查询 **30 个候选里 26 个同分** —— tiebreak 直接决定结果。

**为什么单测没抓到**：这两个错误只有在候选数量 > limit **且** 存在同分时才显形，
而我最初的 fixture 太小、同分不够。第一版与 oracle **在前 2 行一致、之后分歧**。

**交接建议**：任何排序/限额/去重逻辑的改动，**必须用真实库 oracle 逐行对比**
（脚本范式见 `work/_search_equiv2.py`），不要只跑单测。

## 接手方验证命令

```bash
cd "C:/dsh_projects/sekaisync-handoff-2026-08-14"

# 1. 全量测试（当前状态健康）
python -X utf8 -m unittest discover -s tests -t .
#    期望：Ran 563 tests / OK        （基线为 361）

# 2. 提交整洁性
git diff --check
#    期望：无输出

# 3. Astra 诊断基线（原始缺陷证据；修复后部分项已变化，属预期）
python -B -X utf8 work/astra_verify_2026_09_16.py

# 4. 消费方契约漂移检测（B0 建立）
python -B -X utf8 work/b0_contract_snapshot.py --check
#    期望：{"drifted": []}

# 5. 已修复项的独立复验（不经测试框架）
python -B -X utf8 -c "
from sekaisync.factpacks import build_fact_pack_at
from sekaisync.models import Entity
import json
e=Entity(id='c:f',type='character',region='demo',regions=['demo'],
         names={'en':'Unreleased'},facts={'startAt':1_800_000_000_000},
         source='master_db:demo',demo=True)
p=build_fact_pack_at(e,as_of=1_700_000_000_000)
print('future name leaked?', 'Unreleased' in json.dumps(p,ensure_ascii=False), '(期望 False)')
"

# 6. 查看全部提交
git log --oneline 4c07f67..HEAD

# 7. 查看本项方案原文
#    work/ASTRA_RESPONSE_2026-09-16.md        第 445-1440 行为逐项方案（P01-P21）
#    work/ASTRA_EXECUTION_GUIDE_FOR_ZCODE_2026-09-16.md   快速索引与批次表
#    work/PLAN_VERIFICATION_2026-09-16.md     本轮开工前的独立验证与 4 处材料偏差
```

---

## 关于"能否发布"的诚实结论

Astra §3.4 说 B1–B3 可先发布一个**明确标识为安全/可用性修正**的小版本，
但**不得**宣称完成完整术语闭环、全区服事实或防剧透。

**本轮完成度**：B0–B4 主体完成。**2026-09-17 下午：三个被配额中断的子智能体
（P15 协议恢复、P13 发布原子性、P17 news 代际）已恢复并完成**，遗留的 22 项失败全部
清零后分 10 个提交落库（738421f/fee6b4d/d26e1b9/be361a4/48ec0e5/72dede7/3cf3cd0/
e0996ba/d44c720/0872418）：
- **P17**：kb/news 以不可变代际发布（manifest sha256 绑定 SQL 指针、与 revision 同事务原子翻转、
  损坏即 fail-closed），身份改为 upstream namespace + id + language；
  `Core.news` 请求级作用域、单次加载、items 与 summary 同源（回归 `test_core_news_snapshot`）。
- **P15**：MCP/HTTP/REST 序列化失败在任何传输字节写出前降级为净化的 internal error；
  stdio 超长帧按字节预算有界排水，无换行流无法逼出无界读（12 项回归）。
- **P13**：freshness 与派生文件投影只在 SQL 提交后写出；source_migrate 改为 DB→DB 改名
  （保留 DB-only 行与未知元数据、冲突不删行、陈旧 JSON 永不覆盖 DB，8 项故障注入回归）。
- **P03/P18/P09/P08/P20**：region_facts 逐区服事实、progress 四态表状态、
  ExtractionContext 显式上下文、提案校验、候选信号、integrity 样本语义，各带回归。
- **两个真缺陷由验证器残差定位并修复**：`metadata_preserves_source_type`（投影丢
  source_type/instance，trust 重算降级 B→C，48ec0e5）；`empty_replacement_actual_count`
  （弃用写入器空证据替换不清行，d44c720）。探针复跑分别转为
  `true` / `actual_count=0`（`work/ASTRA_VERIFY_AFTER_2026-09-17.json`）。
（当前测试 **788 项全绿**，2026-09-17）

**独立验收**：Astra 观察器复跑（2026-09-17 最终复跑：14 项探针全部可运行，exit 0；
此前三项残差全部处置——`empty_replacement_actual_count` 1→0 为真修复，
`metadata_preserves_source_type` false→true 为真修复，
`distinct_ids_same_title_collide` 为探针过时：现同时报告废弃标题键仍碰撞（仅旧快照读取）
与当前身份契约不碰撞 `distinct_ids_current_identity_collide=false`）。
（详见 `work/ASTRA_VERIFY_AFTER_2026-09-17.json`）。
这是比"我们的测试全绿"更强的证据 —— 它用的是 Astra 自己的探针。

**因此可以对外说的**：

- 术语写入语义已分离（快照/增量/证据），空证据不再谎报
- 未知 schema 版本会被拒绝而非静默改写
- 网络与归档边界受约束；协议边界校验；默认仅监听 loopback
- 缺数据返回 `unknown` 而非伪装成答案；未来/无日期内容不外泄
- 完整性审计的"总数"不再是样本数；不同正文不再被误判为镜像
- 并发写入有单写者 lease（**7 个写入口全部覆盖**）；单文件 JSON 原子写
- **raw 主数据表以不可变代际发布**，读取者按代际指针读取（仅同步某服不影响其他服）
- **kb/news 也以不可变代际发布**（P17，2026-09-17）：manifest 绑定 SQL 指针、原子翻转、
  fail-closed；含 news 的聚合与 items/summary 单次加载同源
- 公告按**上游 namespace + id + 语言**标识，不再因标题相同而丢失记录
- 缓存不再把旧计算结果当成新版本；Web 元数据读取不再拉全文且保留 source_type/instance
- 传输层序列化失败可恢复；stdio 超长帧有界排水

**明确不能说的**（接手方务必遵守）：

- ❌ 不说"layered 已可靠落库" —— P08 的 SlotDecision/term_slots/v2 迁移**代码已实现**
  （`term_slots.py` + `dbstore.migrate_store`，2026-09-17；18 项迁移/事务测试绿），
  但 **与 termindex/core 消费者的接线（v3 持久化路径）未完成**——不要宣称端到端逐槽权威闭环
- ❌ 不说"协议已符合 MCP 2025-06-18" —— 边界校验与序列化恢复已做，未做完整一致性
- ❌ 不说"P14/P16 全部完成" —— P14 RuntimeContext 核心已修但线程任务的显式参数化未做；
  P16 关系行自然键已修但 TableRead 四态区分未做（progress.py 的四态是 P18 的
  compute_progress，不是 P16 的 TableRead）
- ❌ 不说"性能已达标" —— `web_browse` 约 21s（原 50.2s），`web_search` 仍约 195s；均未达方案预期的 <1s
- ❌ 不说"逐区服事实防剧透已完整" —— region_facts 已实现，但 `build_fact_pack_at` 仍无 region 参数，

**真实 store 未被修改**：全部破坏性验证都在临时库上完成；真实库仅做过只读查询与性能测量。
