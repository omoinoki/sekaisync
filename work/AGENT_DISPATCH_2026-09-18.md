# 多智能体派发清单（2026-09-18）

> 依据：`work/REMAINING_REVIEW_2026-09-18.md`
> 状态：**已编制，未执行**。需人工确认后再派发。
> 分支：`longrun/2026-09-16-b0-b6-interim` @ `98073f5`

---

## 给所有智能体的共同约束（每段 prompt 都要原样带上）

```
工作区：C:/dsh_projects/sekaisync-handoff-2026-08-14
分支：longrun/2026-09-16-b0-b6-interim

红线（违反即视为任务失败）：
1. 只改本任务"独占文件"表内列出的文件。绝不改动其他文件。
2. 真实 store（store/kb/sekaisync.db，2.3 GB）只读，绝不写入、不迁移、不备份到仓库内。
   所有验证一律用 tempfile 临时库。真实库当前是 schema 1。
3. 不新增任何第三方依赖（requirements 必须为空：见 pyproject.toml dependencies=[]）。
4. 禁止 skipTest / @unittest.skip / expectedFailure 伪造全绿。
5. 禁止 git add -A / git stash / git reset / git checkout -- <file> / git commit --amend。
   提交只能用显式路径：git add -- <具体文件>（work/ 下文件需 git add -f --）。
6. 每个新增测试必须"红绿双向"验证：先确认修复缺失时该测试变红，修复后才算通过。
   把验证过程与输出写进最终报告。
7. 不得放宽 sekaisync/term_slots.py 的 _certificate 语义。它的正确性由
   tests/test_term_slot_migration.py::test_explicit_matching_verifier_evidence_accepts_only_one_slot
   钉住，正在做真实的"反借权威"工作。

完成前必须自查（三条都要有输出）：
  python -X utf8 -m unittest discover -s tests -t .        # 期望 879+ 全 OK
  git diff --check                                          # 期望无输出
  python -X utf8 work/b0_contract_snapshot.py --check        # 期望 {"drifted": []}

报告要求：说明改了哪些文件、新增哪些测试、红绿双向验证的实际输出、
以及"哪些当初记录的说法被本次核实推翻"（文档已被证明系统性落后于代码）。
不要执行 git merge，不要切分支。
```

---

## W1 — P11 + P08/P09 槽落库（最高优先级，内部严格串行）

**独占文件**：`sekaisync/trinity.py`、`sekaisync/term_slots.py`、`sekaisync/cli.py`(仅 826–905 行区段)、
`tests/test_trinity.py`、`tests/test_term_slot_migration.py`

**派发方式**：三个阶段**串行**，每阶段一个智能体（或同一智能体分三轮流）。
后一阶段必须在前一阶段全量回归通过后才启动。

### 阶段 W1-A：通道产出可入库的证据行

**背景（已核实）**：
`scrub_trinity` 已返回 `slot_decisions`（`trinity.py:2371`，由 `24a26fb` 加入），
但六处证据产出点（`trinity.py:803/903/1107/1153/1236/2306`）只给**聚合计数**
（`stories=<count>`、`votes`、`sim`）加**复数** `story_keys=[...]`。
槽证书门要求**每条证据**带**单数** `story_key` + `language` + `source`，
且 `term`/`value`/`sentence` 之一匹配（`term_slots.py:139-142`）。
全仓 `trinity.py` 里单数 `story_key` 只作为 `_Corpus` 方法参数名出现，从未作为证据字段。

**可行性（已核实，写进 prompt 减少摸索）**：
`_Corpus.lines(story_key, language)`（`trinity.py:329`、`:574`）可取按语言的原文行；
translit 通道本就遍历 `corpus.lines(sk, en_lang)`（`trinity.py:1209`），**原句来源就在手边**。

**任务**：新增单一转换函数（建议名 `channel_evidence_rows`），把六处聚合 payload 统一
转成证据行列表（不要六处各写一遍）。证据行需含：单数 `story_key`、`language`、`source`、
`sentence`（或 `term`/`value`）。

**验收**：
- 六处通道均有证据行产出；
- 证据行满足 `term_slots.py:139-142` 的四项条件；
- 新增测试：断言证据行形态（有单数 story_key/language/source/句子），并在转换缺失时变红。

### 阶段 W1-B：语料感知 verifier，并从提交路径传入

**背景（已核实）**：
- 唯一 verifier `index_verifier`（`term_slots.py:156-210`）只查
  `glossary_terms WHERE official=1`，**永远无法认证 trunk/hub/translit**，
  而后者才是真正产出发现的通道。
- `cli.py:944` 与 `termindex.py:873` 调用 `commit_slot_decisions` 时**都不传 verifier**；
  `_certificate` 在 `term_slots.py:104-105` 遇 `None` 直接返回 `None`，
  导致任何非 L0 决策**静默落入 pending/insufficient_evidence 且不报错**。
- 非官方采纳另需 ≥2 个不同 `story_key`（`term_slots.py:151`）。

**任务**：新增语料感知的 verifier（依据 W1-A 的证据行做判定），
并从 `cli.py:944`、`termindex.py:873` 的调用处**显式传入**。

**验收**：
- 有充分证据的非 L0 决策能真正 `accepted`（不再静默 pending）；
- 无证据时仍 pending（**不得反向放宽**）；
- 新增测试红绿双向，且原证书门钉子测试仍通过。

### 阶段 W1-C：写 `apply_scrub_result`

**背景（已核实）**：
`apply_scrub_result`/`validate_slot_for_commit`/`review_item_from_decision` 在仓库出现 **0 次**。
`--layered` 分支（`cli.py:826-905`）在 `cli.py:843` **丢弃了** `slot_decisions`，
只写 `methodology.json` 与 `review_queue.json`，**全程不碰 SQLite `term_slots`**。
易混淆点（务必写进 prompt）：`cli.py:944` 的 `commit_slot_decisions` 属于
**非 layered** 的 `terms extract` 路径，**不是** layered 落库，别拿它当作已完成的证据。

**任务**：实现 `apply_scrub_result`，把 `slot_decisions` 翻译成 decisions + `evidence_by_id`，
单事务写入槽/证据/待审队列，并接上 `cli.py:843` 的消费点。

**验收**：
- `--layered` 端到端真正写入 `term_slots`；
- 待审守恒（已结算项不重复入队）；
- 新增测试红绿双向。

**重要前提（已核实，写进 prompt）**：
槽写入要求 schema ∈ {2,3}（`term_slots.py:295/468/689`）；真实库是 **schema 1**，
在 v1 上调用 `commit_slot_decisions` 会抛
`ValueError: slot decisions require explicit v2 migration`。
因此**端到端验收必须用临时库 + 显式 migrate_store 到 v2/v3**，
不能在真实库上跑，也不能因为"真实库是 v1"而误判功能未实现。

---

## W2 — P06 `web_browse` 排序成本（中优先级，可与 W1/W3 并行）

**独占文件**：`sekaisync/dbstore.py`(仅 924–1023 `browse_web_rows`)、
`sekaisync/webindex.py`(仅 910–1048 `web_browse`)、`tests/test_web_browse_sql.py`

**背景（已核实，务必纠正旧说法）**：
原缺陷"全库对象化"**早已修好** —— 过滤/排序/LIMIT 已下推 SQL（`dbstore.py:990-995`），
且显式排除 `text` 列（`dbstore.py:989`），两阶段取正文。
**剩余成本是排序不可索引**，实测 `web_browse(limit=20)` 中位数 **9.5s**。

**已核实事实（写进 prompt）**：
- 排序键 `(CASE source 优先级, crawled_at DESC, source, seq)`；
- `web_pages` 索引只有 `idx_pages_kind/lang/aux/canonical/seq`（`dbstore.py:138-142`），
  **无 `crawled_at` 索引**；优先级是**计算出的 CASE，本质不可索引**；
- 基数：`crawled_at` 去重 747,989 / 752,372 行（高基数，建索引理论上有效）；
- **硬约束**：排序是**对外契约**，`tests/test_web_browse_sql.py:205`
  `test_equivalence_with_python_oracle` 钉住与旧实现的等价性，`:120` 保留旧 Python 两趟排序作 oracle。
- 候选索引**不适用**：`searchindex.candidates` 需要 `query` 参数，browse 无查询串。

**任务（先评估再改，不要盲目动手）**：
1. 先给出两个方案的评估与推荐：
   - 方案 1：建 `crawled_at`（或复合）索引，并把优先级从 CASE 改为可索引形式（如预计算列）；
   - 方案 2：改排序语义（改用已有索引的键，如 `seq`）。
2. 若选方案 2，**必须先确认 `test_equivalence_with_python_oracle` 是否允许该变更**；不允许则只能方案 1。
3. 若两案均不可行：**明确记录"browse 延迟受全表排序限制"，不再列为缺陷，但严禁静默关闭**。

**验收**：改动后 browse 延迟显著下降（给出实测中位数与样本），
且 `test_equivalence_with_python_oracle` 仍通过（或已记录为何契约允许变更）。

---

## W3 — P15 MCP 版本协商（中优先级，可与 W1/W2 并行）

**独占文件**：`sekaisync/mcp_server.py`、`sekaisync/http_server.py`、
`tests/test_mcp.py`、`tests/test_http_mcp.py`

**背景（已核实）**：
- `PROTOCOL_VERSION = "2024-11-05"`（`mcp_server.py:28`）；
- `initialize`（`mcp_server.py:225-237`）**完全忽略 `params`**：
  不读、不比较、不校验客户端 `protocolVersion`，直接回自己的常量；全仓无 `SUPPORTED_VERSIONS`；
- HTTP 侧同样暴露该值（`http_server.py:293,302`）；
- **已完成的一半**：`resources/templates/list` 已实现（`mcp_server.py:254-259`），不要重做。

**任务**：**同时**做两件事 —— 抬高 `PROTOCOL_VERSION` **并**实现 supported-version 校验
（读客户端版本、命中则回显、不命中则按规范报错）。
**只改字符串是 Astra 明确反对的假升级。**

**已核实的关键约束（写进 prompt，避免误判）**：
四个契约快照（`mcp_discovery`/`openapi`/`dsh_query_shapes`/`tool_registry`）
**均不含 `protocolVersion`**，`mcp_discovery` 顶层键也不含它 ——
**改版本不会触发契约漂移**，这一项可以放心做。

**附带（低优先，可选）**：`MAX_STDIO_LINE_BYTES`（`mcp_server.py:31-33`）自述"临时预算"，应校准。

**验收**：
- 客户端版本命中 → 回显该版本；不命中 → 按规范报错（不得静默通过）；
- 新增测试红绿双向；`b0_contract_snapshot --check` 仍 `drifted: []`。

---

## 编排与派发顺序

1. **第一批（并发）**：启动 **W2** 与 **W3**（彼此无文件交集，且都不依赖 W1）。
2. **W1 串行**：W1-A →（全量回归通过后）→ W1-B →（通过）→ W1-C。
   可在 W2/W3 运行期间同时启动 W1-A。
3. 每批回来后：跑 879 全量 + 契约检查 + `--check`，确认无交叉污染再合入下一批。
4. **W1 全部完成后**再评估是否合并 main —— P11 落库是"完整术语闭环"宣称的前提，
   当前 CHECKPOINT 仍禁止该宣称。

## 明确不做（避免智能体自作主张扩展范围）

| 项 | 理由 |
| :--- | :--- |
| P14 helper 传参整洁化（35 处签名） | 隔离缺陷已由 `CrawlTaskContext` + 每任务 `copy_context` 修好并有复用线程回归测试；零正确性收益，只增回归风险 |
| `status()` 子聚合缓存 | 实测 cold 57.3s / **warm 2.19s**、366 KB 在 512 KB 上限内确实被缓存；收益远低于 W1 |
| B8 发布门禁 | 性质是需外部条件与实测（互操作/平台矩阵/故障点矩阵），非补丁可关闭 |

## 派发前需人工确认的两点

1. 是否**现在**就派发（我建议先派 W2+W3，W1-A 同步启动）；
2. 是否接受"W1 完成后才评估合并 main"这个节奏 —— 若你需要尽快合，
   则应先合并当前已验证的 879 全绿状态，W1 作为后续分支再做。
