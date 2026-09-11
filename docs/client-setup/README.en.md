# Setup and first use

**Want help from your AI?** [Copy the setup prompt](https://hburaktasyurek.github.io/MutalaaMCP/en/#install-with-ai) and paste it into your AI app.


[Türkçe](README.md) · [Home](../../README.en.md)

This guide covers macOS and Windows. You need a Mütalaa account and an AI app with local MCP support.
In-app sign-in has been exercised on macOS; real-device validation of the Windows service is still pending.
MCP availability depends on your app version and account.

## 1. Install from a terminal

No unsigned double-click installer is offered for macOS. The Windows CMD script is experimental; its real download and first-launch experience has not been verified. Use the main installation method below.

**uv** manages the Python environment for you. You do not need to clone or maintain a repository folder.
If uv is not installed, follow its [official installation guide](https://docs.astral.sh/uv/getting-started/installation/), then reopen your terminal.

Open **Terminal** on macOS or **PowerShell** on Windows and run:

```sh
uv tool install --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
uv tool update-shell
```

This installs the published `v0.1.0` release wheel; no PyPI release is required.
Reopen the terminal and check:

```sh
mutalaamcp --help
```

A command list confirms installation. Git and a preinstalled Python are not required; uv downloads Python if needed.
[How uv tool installation works](https://docs.astral.sh/uv/guides/tools/).

## 2. Connect your app

### Local URL and in-app sign-in

For apps supporting local HTTP MCP and OAuth:

```sh
mutalaamcp setup --client codex --transport http
```

This installs a background service for your user. Add these values in your app’s MCP settings:

| Field | Value |
| --- | --- |
| Name | `mutalaamcp` |
| URL | `http://127.0.0.1:18769/mcp` |
| Bearer token / headers | Leave empty |

Replace any old Mütalaa `command`/`args` entry instead of adding a duplicate.
For Codex TOML configuration, the equivalent is:

```toml
[mcp_servers.mutalaamcp]
url = "http://127.0.0.1:18769/mcp"
```

Enable the connection, restart the app if needed, and select **Authenticate**.
Grant access on the browser connection page and complete Mütalaa sign-in and device confirmation in the window that opens.
Keep the first connection page open. Return to the app when complete.

Do not also run `auth login` for this flow. `127.0.0.1` is your own computer; it cannot be added to a cloud service that requires a remotely accessible server.
An MCP settings screen need not display individual tools for the connection to work.

### Other apps: command-based connection (stdio)

Use this alternative if your app launches a command instead of connecting to a local URL.
If you previously installed the HTTP service, run `mutalaamcp service stop` first.

```sh
mutalaamcp auth login
```

Complete browser sign-in and device approval, then choose your app:

| App | Command |
| --- | --- |
| Claude Desktop | `mutalaamcp setup --client claude-desktop` |
| Cursor | `mutalaamcp setup --client cursor` |
| Codex | `mutalaamcp setup --client codex` |
| Google Antigravity | `mutalaamcp setup --client google-antigravity` |
| Zcode | `mutalaamcp setup --client zcode` |
| OpenCode Desktop | `mutalaamcp setup --client opencode-desktop` |
| Witsy | `mutalaamcp setup --client witsy` |
| Hermes Agent | `mutalaamcp setup --client hermes-agent` |
| Cherry Studio | `mutalaamcp setup --client cherry-studio` |

Add the output to your app’s MCP configuration. Witsy and Cherry Studio use separate command-path and `serve` argument fields.
`setup` attempts clipboard copying but does not edit your app’s settings.
These are configuration templates, not a claim that every app version has been tested.
Run one server per user; do not start HTTP and stdio together.

## 3. Try your first research

In a new conversation:

> Use Mütalaa to find Law No. 193, the Turkish Income Tax Law, and give its official source link.

Check for a `mutalaamcp` call and a returned source. Then [add the Mütalaa skill](../mutalaa-skill.md) and try a natural legal question in a new conversation.
The skill helps tool selection; it does not guarantee it.

## Troubleshooting

| Problem | Action |
| --- | --- |
| `uv` not found | Complete uv installation and open a new terminal. |
| `mutalaamcp` not found | Run `uv tool update-shell` and open a new terminal. |
| No Authenticate button | Check the URL. Run `mutalaamcp service status`; if necessary, run `mutalaamcp service start` and reopen the app. |
| Expired code / `invalid_grant` | Start a new authentication from the app for HTTP, or run `mutalaamcp auth login` for stdio. Do not reuse the old code. |
| Email or terms confirmation required | Complete the step in your Mütalaa account, then sign in again. |
| Only web searches appear | Explicitly ask to “use Mütalaa” in a new conversation. Check skill installation. Without an MCP call, Mütalaa was not used. |
| `already_running` | Close the other server/client. Do not launch stdio while the HTTP service runs. |
| Source unavailable / 429 / 503 | Wait before retrying and respect any reported delay. |

For unresolved issues, use the [support guide](../../SUPPORT.en.md). Do not share tokens, personal information, or case text.

## Update and uninstall

To update, replace the package URL below with the new release wheel URL. Reusing the same URL reinstalls the same version. Update the skill separately.


**For this guide’s uv installation**, stop the HTTP service or close your stdio client before updating:

```sh
mutalaamcp service stop
uv tool install --force --refresh --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
mutalaamcp setup --client codex --transport http
```

For stdio, skip the first service command and replace the final command with your own `setup --client ...`.
Reopen the app. Update the skill separately if you installed it.

To uninstall, remove the MCP entry from your app, then run:

```sh
mutalaamcp service stop
mutalaamcp auth logout
uv tool uninstall mutalaamcp
```

Also remove the service registration so it does not start at the next sign-in.

macOS:

```sh
rm ~/Library/LaunchAgents/tr.mutalaa.mcp.plist
```

Windows PowerShell:

```powershell
schtasks /Delete /TN tr.mutalaa.mcp /F
```

Skip service steps for stdio. Uninstalling does not delete research caches; if desired, run `mutalaamcp cache clear` before removing the package.

## Optional OCR

Stop the HTTP service or close your stdio client first:

```sh
uv tool install --force --python 3.12 "mutalaamcp[ocr] @ https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.0/mutalaamcp-0.1.0-py3-none-any.whl"
mutalaamcp ocr install
mutalaamcp ocr status
```

Check for `ready`. For HTTP, reinstall the service with `mutalaamcp setup --client codex --transport http`; for stdio, reopen the app.
OCR models are offered for macOS arm64 and Windows x64. The initial download requires internet access.
