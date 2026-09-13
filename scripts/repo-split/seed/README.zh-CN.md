# SekaiSync Desktop

> [English](README.md) | 简体中文

[SekaiSync](https://github.com/omoinoki/sekaisync) 的 Windows 桌面前端，基于 WinUI 3。

主界面主体为**本地数据库阅览**（`store/kb/sekaisync.db`，只读），侧边导航提供**用语表**、**智能体接入**、**同步**与**设置**四个功能页。UI 严格遵循 WinUI 3 规范：NavigationView 导航壳、Mica 背景材质、标题栏内容延伸、x:Bind + CommunityToolkit.Mvvm 的 MVVM 结构。

## 当前状态

- 已在本机编译通过并运行验证（.NET SDK 8 + Windows App SDK 1.8，VS2026 MSVC v14x + Windows 11 SDK 10.0.26100 工具链）。
- 数据库为只读访问（SQLite WAL 并发安全），可与爬虫 / CLI 会话并存；同步页会提示 `store/crawl.lock` 存在时避免并发写入。

## 功能

| 页面 | 说明 |
| --- | --- |
| 数据库阅览（主页） | 表选择（6 张表带行数）、LIKE 搜索、分页（可调每页行数）、横向滚动表头同步、行详情（`*_json` 列自动美化 + JSON 徽标） |
| 用语表 | `glossary_terms` / `terms` 的五语（ja/en/zh_hans/zh_hant/ko）对照浏览，条目详情含名称 JSON 与 `term_evidence` 句级证据 |
| 智能体接入 | MCP stdio 配置 JSON 一键复制（Claude Desktop / Cursor）、HTTP 服务启动停止与健康检查、端点速查 |
| 同步 | `sync` / `news sync` / `status` / `progress` / `integrity` 图形化执行，爬虫需显式勾选 TOS；输出实时回显 |
| 设置 | store 目录、Python 解释器、HTTP 端口、每页行数、主题（跟随系统/浅/深）；配置存于 `%LOCALAPPDATA%\SekaiSyncDesktop\settings.json` |

## 构建

需要 .NET SDK 8+、MSVC v14x 生成工具、Windows 11 SDK。三者都可以在 Visual Studio Installer 里按单个组件安装，不需要勾选 IDE 工作负载。Windows App SDK 由 NuGet 自动还原。

```powershell
dotnet build SekaiSync.Desktop.csproj -p:Platform=x64
.\bin\x64\Debug\net8.0-windows10.0.19041.0\SekaiSync.Desktop.exe
```

部署形态为**未打包 + 自包含 Windows App SDK**（`WindowsPackageType=None` + `WindowsAppSDKSelfContained`），目标机器无需单独安装 Windows App Runtime。

> 已知问题：`dotnet build` 下 XAML 编译器（独立 exe 模式）在报绑定错误时，其自身错误文案资源缺失会以 `WMC9999` 内部错误形式崩溃、掩盖真实错误。排查 XAML 绑定问题时，若遇到 WMC9999，先用最小页面二分定位（实战经验：多为 x:Bind 引用了不存在的成员，或使用了 WinUI 中不存在的主题资源键，如 `SystemAccentColorLight2Brush`；后者会在运行时以 XamlParseException 直接杀掉进程）。

## store 目录怎么找

程序从自身可执行文件所在目录逐级向上查找 `sekaisync/cli.py` 或 `store/kb/sekaisync.db`，把第一个命中的目录当作仓库根。本仓库位于 SekaiSync 主仓的 `frontends/WinUI3` 时（也就是常见的 submodule 布局），这一步能自动完成。

单独 clone 本仓库时，向上探测找不到任何标志文件。此时在**设置**页手工填写 store 目录即可，配置会被记住。

## 项目结构

```text
SekaiSync.Desktop.csproj     net8.0-windows10.0.19041.0, UseWinUI, unpackaged
App.xaml(.cs)                入口 + 全局异常处理 + 诊断日志（%TEMP%\sekaisync_desktop.log）
MainWindow.xaml(.cs)         NavigationView 导航壳（Mica、ExtendsContentIntoTitleBar）
Models/                      数据库元模型（表/列/页结果/详情字段）
Services/
  AppEnvironment.cs          仓库根 / store / 数据库 / Python 路径探测
  AppSettings.cs             %LOCALAPPDATA% 设置持久化
  DatabaseService.cs         只读 SQLite：表目录、分页 LIKE 查询、行详情、术语证据
  ProcessLauncherService.cs  python -m sekaisync 子进程流式输出
  AppServices.cs             组合根
ViewModels/                  Database / Glossary / Agent / Sync / Settings
Views/                       对应五个页面
```

## 边界

- 前端只读访问本地知识库与 CLI；不下载图片、音频、Live2D、视频。
- 导入正文前请确认目标站点 TOS / robots；同步页的爬虫入口需要显式勾选 TOS 复选框。

## 许可

MIT，见 [LICENSE](LICENSE)。
