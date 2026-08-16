# ChatGPT Work / Web developer-mode MCP setup

As of July 2026, the recommended remote path for ChatGPT is a custom MCP app created through Developer Mode, not an OpenAPI Action.

## Steps

1. Start SekaiSync with the HTTP server:

   ```powershell
   & $env:PYTHON -m sekaisync serve-http --host 0.0.0.0 --port 8787
   ```

2. Expose port 8787 over HTTPS. ChatGPT web requires a public HTTPS URL. Use OpenAI Secure MCP Tunnel, a reverse proxy, or another tunnel that forwards `https://your-host/mcp` to `http://127.0.0.1:8787/mcp`.

3. In ChatGPT, open Settings > Plugins or `chatgpt.com/plugins`. Enable Developer Mode where your workspace exposes it, then create a custom MCP app and enter the HTTPS endpoint URL ending in `/mcp`.

4. Review the exposed `sekaisync_*` tools. Keep the app read-only for normal lookup and verification; enable write actions only if you later add a writing workflow.

5. Use the plugin directory as the distribution path when the workspace supports plugins. A plugin can carry the SekaiSync skill plus the MCP-backed app.

## Notes

- Developer Mode availability depends on plan and workspace settings. Full MCP support with write actions is rolling out to Business, Enterprise, and Edu plans; Plus/Pro users can connect MCPs with read/fetch permissions in Developer Mode.
- Local ChatGPT Desktop should use the shared Codex MCP configuration (`agents/mcp-stdio/codex.config.toml`) instead, because it can spawn a stdio MCP server directly.
- The legacy OpenAPI document remains at `/openapi.json` for manual Actions-based integration only (see `agents/rest-openapi/`).
