# Changelog

English | [中文](CHANGELOG.zh-CN.md)

## 0.4.2-alpha - Unreleased

### Fixed

- Restore character-profile descriptions and character-name lookup. Fixes [#1](https://github.com/omoinoki/sekaisync/issues/1).
- Retain card titles and skill names, mission descriptions and item flavor text.
- Support lookup by complete entity ID as well as character names and description text.
- Fix offline test configuration and Python 3.10 test compatibility.

### Added

- Select a region for FactPacks through CLI, MCP and HTTP.
- Include region, body language, source, version and content status in FactPacks; support Chinese language aliases.
- Add occurrence-level senses, exact text anchors and adaptive context review to the scraper.
- Update the bundled [SekaiSync Connect plugin](https://github.com/omoinoki/dsh-sekaisync-connect/releases/tag/v0.3.9-alpha.1) to 0.3.9-alpha.1, with regional results, clearer tool errors and safer deployment-path saving.

### Upgrading

Run sync again with the updated backend to restore descriptions missing from an older store, then restart MCP and HTTP services. Local raw master data can also be used for offline recovery. See the [recovery guide](docs/ISSUE_1_PROFILE_RECOVERY_261003.md).
