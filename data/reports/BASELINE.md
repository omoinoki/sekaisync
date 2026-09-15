# 数据基线（作者本机实测，2026-09-05）

> 本仓库**不捆绑任何游戏数据**。以下数字来自作者本机一次全量同步+抓取，仅作参考；
> 克隆仓库后需要自行执行 `sync` / `crawl` / `news sync` 重建 store，数字会随数据源
> 演进而变化。

## 数量基线

<!-- BEGIN GENERATED:baseline (refresh via scripts/refresh_baseline.py) -->
<!-- refreshed 2026-09-15T10:11:52+08:00 -->

| 数据层 | 数量 | 说明 |
| --- | --- | --- |
| Registry（跨服实体） | 71,463 | 官方 master DB（管道 A） |
| Glossary（官方/本地化词条） | 71,463 | 五服本地化官方词 |
| Terms（术语索引） | 8,727 | official 标记 105；带正文证据 8,675 |
| 术语五语齐全 | 23 | 单语言覆盖：ja 8,727、en 120、zh_hans 312、ko 1,349、zh_tw 183 |
| Web 正文页 | 752,372 | 含辅助页（管道 B） |
辅助翻译参考| 4,837| auxiliary 辅助页（翻译参考/overlay） |
| 官方公告 | 2,032 | en 640、ja 121、ko 517、zh_hans 238、zh_hant 516 |
<!-- END GENERATED:baseline -->

| 数据层 | 数量 | 说明 |
| --- | --- | --- |
| Web 正文索引 | 638,335 页 | 含辅助页 |
| altsource_ms | 374,074 | Moesekai 镜像正文 |
| altsource_sv | 259,424 | Sekai Viewer 正文 |
| 辅助翻译参考 | 4,837 | altsource_ms_translation 4,730 + altsource_sv_i18n 107 |
| Registry | 66,435 | 跨服实体 |
| Glossary | 59,129 | 官方/本地化词条 |
| Terms | 9,324 | 2026-08-23 清洗污染数据后的实测（清洗前 12,580）；official 标记 113 条、带正文证据 9,264 条 |
| 术语五语覆盖 | 25 条 | 具备 ja / zh_hans / en / zh_tw / ko；单语言覆盖 ja 9,324 / en 341 / zh_hans 192 / ko 146 / zh_tw 63 |
| 官方公告 | 293 | ja 105、zh_hans 188（Sekai Viewer 站内公告已排除） |

## 完整度

- 五服事实层（master 元数据）：100%
- 五服正文层：66%（日服 81%）；剩余缺口均为数据源限制（见 `docs/TODO.md` 的
  「数据源已知缺口」）
- 新活动自动检测：JP 213、EN 177、TC/KR/CN 182，均为 `up_to_date`

## 完整性校验

`sekaisync integrity` 实测：镜像重复 67,707、正文冲突 0、哈希不一致 0、
canonical_missing 0、source_hash 已知 500,146；仅剩 8 条资产/语种错配与 422 条
scenarioId 命名错位（均为数据源异常，口径见 `docs/INTEGRITY.md`）。

## 测试

单元测试 233 项（含事件检测、东京每日限频、事件简称映射、多站点配置、KB v2
布局/迁移等用例）。在正常环境（无沙箱临时目录限制）应为全绿。
