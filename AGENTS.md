# Repository Agent Guide

This is the single development guide for the connected SekaiSync workspaces. DSH, WinUI3 and UX root `AGENTS.md` files are reading pointers to this file, not separate policy copies. The members-only `memories-of-melody` repository is the shared knowledge base for Project OMNK's Project SEKAI-related projects; it owns the experience-library read/write protocol in its own `AGENTS.md`.

For agents answering Project Sekai questions through the product, use [agents/rules/AGENTS.md](agents/rules/AGENTS.md); those are product query rules, not the development workflow.

## Connected Workspaces

Resolve these five folders from the current project's configured paths before working across repositories. When present, read the ignored local path map at `work/agent-context/connected-workspaces.local.json`; it records checkout locations, not additional rules. Confirm that its destinations still exist before using it.

| Folder | Responsibility | Guidance |
| --- | --- | --- |
| `sekaisync-cli` | Backend and shared development workflow | This file |
| `dsh-sekaisync-connect` | DSH plugin | Pointer to this file |
| `sekaisync-winui3` | Windows desktop client | Pointer to this file |
| `sekaisync-ux` | Shared UX/design workspace; temporarily non-public until complete | Pointer to this file |
| `memories-of-melody` | Shared knowledge base for Project OMNK's Project SEKAI-related projects; Project OMNK members only | Its own `AGENTS.md` read/write protocol |

The plugin and desktop submodules in this checkout are not additional connected repositories. Distinguish their pinned copies from the independent checkouts before editing. The official `deepseek-harness` upstream is a compatibility reference, not one of these five project folders.

## Project Map

- `sekaisync/`: Python backend, synchronization, extraction, storage, search and fact packs.
- `sekaisync/cli.py`, `sekaisync/mcp_server.py`, `sekaisync/http_server.py`: public interfaces consumed by clients.
- `tests/`: backend regression tests; `.github/workflows/ci.yml`: supported CI commands and Python versions.
- `agents/deepseek-harness/`: SekaiSync Connect plugin, maintained in a separate Git repository and pinned here as a submodule.
- `frontends/WinUI3/`: Windows desktop client, also a separate Git repository and submodule.
- `scripts/`: development, evaluation, build and migration helpers.
- `work/`: ignored local experiments, logs, screenshots and release evidence. `store/` contains local data, not source code.

## Working Approach

1. Inventory every folder connected to the current project, not just the primary checkout or `.gitmodules`. Distinguish project-owned repositories, mounted submodule copies and upstream reference directories; identify the authoritative checkout for each edit and synchronization destination.
2. Inspect Git status, relevant source and tests before changing files. Preserve unrelated edits, including changes inside submodules.
3. Keep fixes focused on the requested behavior. When a bug reveals a shared failure pattern, inspect adjacent extraction paths and client consumers, and add regression coverage for the affected cases.
4. Use current code, manifests and CI as the source of truth. Handoff notes and checkpoint documents describe historical snapshots; verify their commands, versions and results before relying on them.
5. Preserve Python 3.10 compatibility and the backend's zero-third-party-runtime-dependency design. Follow existing patterns rather than introducing a new framework for a local fix.
6. Use explicit file lists when staging. Commit submodule changes in their own repository, then update the parent gitlink to the intended verified commit when the task calls for it.
7. For local workspace moves, preview the plan and confirm the new project path before applying a resumable journal. Preserve checkout/submodule revisions, existing edits and stores; update only current path references, verify integrity, and remove the old root only when confirmed empty. Inventory executable helpers, environment/editor settings and filesystem link targets; verify path discovery in both independent checkouts and mounted copies rather than hardcoding a local folder name. A locked empty root can remain pending cleanup.

## Data And Interface Contracts

- Keep entity identity, region, language, source and version distinctions intact. Resolve relationships using the appropriate region and source generation rather than assuming IDs from different tables are interchangeable.
- Preserve descriptive fields through extraction, storage, lookup and fact-pack output. Test the complete read path, not only the extractor.
- Keep missing content, fallback language and conflicting regional evidence distinguishable in results. Derive facts from source data rather than filling gaps with model knowledge.
- Check CLI, MCP, HTTP, DSH and WinUI3 consumers when changing shared schemas or response fields. WinUI3 also reads SQLite directly: check its table, column and stored-JSON contracts rather than testing only HTTP. Prefer compatible additions; document intentional breaking changes and their upgrade path.
- Preserve endpoint validation, configuration precedence, timeout/cancellation behavior and actionable error reporting.

## Verification

Run focused regression tests first. For backend changes, run the full suite before delivery:

```sh
python -m unittest discover -s tests -t .
```

CI covers Python 3.10 through 3.13, then builds and installs a wheel. Packaging checks use:

```sh
python -m pip wheel . --no-deps --wheel-dir dist
```

Use isolated environments for wheel installation and temporary stores for smoke tests. Match the CLI smoke commands in `.github/workflows/ci.yml`, with `--no-event-check` and an explicit test `--store` before the subcommand.

Tests should work from a clean checkout without private endpoints, a production store or live network data. Use local fixtures and temporary directories. Run online synchronization, crawling or whole-corpus evaluations only when they are part of the requested task; keep them separate from ordinary unit tests.

Report the commands actually run and their results. Distinguish unit tests, build checks, contract tests and real GUI verification. For documentation-only edits, check diffs, links, examples and rendering instead of rerunning unrelated long suites.

## Client Compatibility

### DSH Plugin

For compatibility decisions, consult the latest official `deepseek-harness` repository and documentation, and verify the installed runtime. Base version gates and plugin API usage on that evidence, not only older plugin notes. Record the upstream revision/runtime used in local verification evidence.

Read the plugin's README and `package.json`. Run `npm test` from its repository for relevant changes; use `npm run test:integration -- <backend-root>` for backend integration. Additional `npm run verify` checks require the installed DSH/ASAR environment and local verification prerequisites described by their scripts, so inspect those first rather than treating them as clean-checkout tests. When changing panel configuration or lifecycle behavior, use Computer Use to exercise saving, reloading, connecting and tool execution with an isolated profile/store.

Verify tools against the actual installed official ToolRuntime as well as plugin tests. Keep version/profile discovery, editable configuration layers, cache/process invalidation after configuration changes, failed-tool results and cancellation propagation consistent. Prepare verification prerequisites separately rather than changing working configuration to satisfy a test. Keep evidence in a confirmed ignored directory; the plugin does not automatically ignore its own `work/` directory.

### WinUI3

Read the client's README and current project file for build requirements. Check backend process invocation, store discovery and response deserialization when changing backend contracts. Its documented Windows build command is:

```powershell
dotnet build SekaiSync.Desktop.csproj -p:Platform=x64
```

Run this from the desktop repository root with the required Windows/.NET toolchain. `dotnet run --project tools/regression/Regression.csproj` exercises synthetic data-layer fixtures; `dotnet run --project tools/smoke/Smoke.csproj -- <store-path>` reads a populated store and requires the story bodies used by its checks. Exercise affected screens when GUI behavior changes; a successful build is a build result, not a GUI test.

### UX Workspace

`sekaisync-ux` is a design-resource and static-preview workspace, not an application package or a Git repository. Read its `UX-HANDBOOK.md`; shared tokens, preview files and assets are the design source. Preserve archive and research provenance, and keep drafts separate from product assets.

For changes there, run `node --check fragments.js` and parse `tokens.json` and `assets/manifest.json` from the UX workspace root. No application build/test command is currently defined. Inspect `preview.html` in a browser for visual changes, including narrow screens, themes, keyboard interaction and the accessibility modes relevant to the change; syntax and JSON validation do not replace visual checks.

## Public Documentation

- Write README, changelog and release notes for people installing and using the product. Describe concrete changes, examples and necessary upgrade actions.
- Follow the preceding version's structure and tone. Keep unrelated existing wording and promises unchanged unless revising them is part of the task.
- Keep internal instructions, authorization records, conversations, local paths, acceptance logs and audit hashes in `work/`, not in product introductions or release notes.
- Explain relevant prerequisites or functional constraints once, where they help the reader act. Avoid repetitive caveats and process narration.
- Keep each Markdown paragraph and list item on one physical line in GitHub release bodies; let the renderer wrap text. Maintain English/Chinese parity and verify links, code examples and rendered output.

## Release Work

Inspect the actual remote commits, tags, releases and assets before continuing a release. Build and verify artifacts from the intended release commit, and rebuild when packaged documentation or metadata changes.

Use package-ecosystem-valid versions, explicit artifact lists and checksum verification. Preserve published tags and assets; moving a published tag or replacing an asset requires an explicit request. Distinguish a source push, a GitHub Release and a package-registry publication when reporting delivery.

Keep credentials, private configuration, local stores and generated game-data exports out of commits and release packages. Preserve tracked contract fixtures intentionally included by the repository rather than treating all `work/` paths as disposable.

## Learning And Cross-Repository Sync

At task start, locate the connected `memories-of-melody` folder and read its `AGENTS.md`, the relevant project profile, index and matching lessons. Consult the library again before release, migration or other consequential operations. Follow its protocol rather than inventing a parallel memory format.

As a routine task wrap-up, automatically capture reusable lessons from confirmed bugs, failed approaches and successful fixes in that library, without waiting for a separate reminder. Check for duplicates first, append verified evidence to an existing entry when it already covers the lesson, and maintain the required registry/index/project metadata for new entries. State the cause, prevention and verification method; omit chat transcripts and transient test-count ledgers.

When a lesson changes future agent behavior, update the applicable rule here. Keep DSH, WinUI3 and UX root guidance as pointers only; verify those pointers instead of copying rules into them. Component-specific instructions also belong here, under their component. Replace superseded rules and deduplicate guidance so the main guide and experience protocol remain the only substantive instruction sources.

Keep private experience entries in the private library. Public repository guidance may contain the resulting non-sensitive action rule, not private transcripts, local configuration or library excerpts. Use authoritative checkouts, and follow the task's delivery scope for commits, pushes and gitlink updates.

Verify the updated files, library metadata and all three reading pointers, then report the actual destinations updated. If a connected path or write permission is unavailable, identify the missing destination rather than claiming full synchronization.
