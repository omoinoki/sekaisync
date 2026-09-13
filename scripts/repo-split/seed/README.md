# SekaiSync Desktop

> English | [简体中文](README.zh-CN.md)

Windows desktop front-end for [SekaiSync](https://github.com/omoinoki/sekaisync), built with WinUI 3.

The main view is a read-only browser for the local knowledge base (`store/kb/sekaisync.db`). Four side pages cover the glossary, agent integration, sync tasks, and settings. The UI follows WinUI 3 conventions: a NavigationView shell, Mica backdrop, content extended into the title bar, and MVVM through x:Bind plus CommunityToolkit.Mvvm.

## Status

- Builds and runs locally on .NET SDK 8 with Windows App SDK 1.8 (MSVC v14x and Windows 11 SDK 10.0.26100).
- The database is opened read-only. SQLite WAL keeps browsing safe while a crawler or a CLI session runs. The sync page warns you when `store/crawl.lock` exists.

## Features

| Page | What it does |
| --- | --- |
| Database (home) | Pick one of six tables with row counts, LIKE search, paging with adjustable page size, column headers that scroll in sync, row detail with pretty-printed `*_json` columns |
| Glossary | Side-by-side view of `glossary_terms` and `terms` across five languages (ja / en / zh_hans / zh_hant / ko), with name JSON and sentence-level `term_evidence` |
| Agent | Copy MCP stdio config JSON for Claude Desktop or Cursor, start and stop the HTTP service, health check, endpoint cheat sheet |
| Sync | Run `sync`, `news sync`, `status`, `progress`, and `integrity` from the GUI. The crawler entry needs an explicit TOS checkbox. Output streams live |
| Settings | Store directory, Python interpreter, HTTP port, page size, theme (system / light / dark). Stored in `%LOCALAPPDATA%\SekaiSyncDesktop\settings.json` |

## Build

You need .NET SDK 8 or newer, MSVC v14x build tools, and the Windows 11 SDK. Install them as individual components through the Visual Studio Installer. No IDE workload is required. Windows App SDK arrives through NuGet.

```powershell
dotnet build SekaiSync.Desktop.csproj -p:Platform=x64
.\bin\x64\Debug\net8.0-windows10.0.19041.0\SekaiSync.Desktop.exe
```

The output is unpackaged and self-contained for Windows App SDK (`WindowsPackageType=None` with `WindowsAppSDKSelfContained`), so a target machine does not need the Windows App Runtime installed separately.

> Known issue: under `dotnet build`, the standalone XAML compiler can die with a `WMC9999` internal error at the moment it wants to report a binding problem, which hides the real message. Bisect with a minimal page. In practice the cause has been an `x:Bind` to a member that does not exist, or a theme resource key that WinUI does not define (for example `SystemAccentColorLight2Brush`). The second case kills the process with a XamlParseException at runtime.

## Where the store lives

The app walks up from its own executable looking for `sekaisync/cli.py` or `store/kb/sekaisync.db`, and treats the first directory that matches as the repository root. That works when this repository sits at `frontends/WinUI3` inside a SekaiSync checkout, which is the usual layout.

Cloned on its own, the probe finds nothing. Set the store directory by hand on the Settings page and it is remembered.

## Layout

```text
SekaiSync.Desktop.csproj     net8.0-windows10.0.19041.0, UseWinUI, unpackaged
App.xaml(.cs)                entry point, global exception handling, diagnostic log in %TEMP%\sekaisync_desktop.log
MainWindow.xaml(.cs)         NavigationView shell with Mica and ExtendsContentIntoTitleBar
Models/                      database meta-model: table, column, page result, detail field
Services/
  AppEnvironment.cs          repo root, store, database, and Python path discovery
  AppSettings.cs             settings persistence under %LOCALAPPDATA%
  DatabaseService.cs         read-only SQLite: table catalog, paged LIKE queries, row detail, term evidence
  ProcessLauncherService.cs  python -m sekaisync subprocess with streamed output
  AppServices.cs             composition root
ViewModels/                  Database / Glossary / Agent / Sync / Settings
Views/                       the five pages
```

## Boundary

- The front-end reads the local knowledge base and drives the CLI. It does not download images, audio, Live2D, or video.
- Check the target site's TOS and robots rules before importing story text. The crawler entry on the sync page stays behind an explicit TOS checkbox.

## License

MIT. See [LICENSE](LICENSE).
