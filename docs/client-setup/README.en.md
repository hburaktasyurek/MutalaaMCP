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
uv tool install --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.1/mutalaamcp-0.1.1-py3-none-any.whl"
uv tool update-shell
```

This installs the `v0.1.1` release wheel; no PyPI release is required.
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

This installs a background service for your user. Starting with `0.1.1`, `setup` also
installs the Mütalaa skill. With older packages, [add the skill separately](../mutalaa-skill.md).
Add these values in your app’s MCP settings:

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
`setup` attempts clipboard copying but does not edit your app’s MCP settings.
In `0.1.1`, `setup` also installs the skill for Codex/Cursor. For other clients it exports
a skill ZIP; import it through Customize > Skills in Claude Desktop.
See [combined setup and other clients](../mutalaa-skill.md).
These are configuration templates, not a claim that every app version has been tested.
Version `0.1.1` lets multiple stdio clients share one local server. When Claude Desktop
starts separate processes for chat and Cowork, the second connection is no longer
blocked by the single-instance lock; the `serve` command stays the same. Windows validation
with the actual Claude Desktop/Cowork app is still pending. Do not start the HTTP service and stdio together.

## 3. Try your first research

In a new conversation:

> Use Mütalaa to find Law No. 193, the Turkish Income Tax Law, and give its official source link.

Check for a `mutalaamcp` call and a returned source. Verify the [bundled Mütalaa skill](../mutalaa-skill.md), complete any required import, and try a natural legal question in a new conversation.
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
| `already_running` | If the HTTP service is running, use `mutalaamcp service stop` before stdio. On older versions, close the other stdio clients. Close all Mütalaa connections before updates or cache/OCR maintenance. |
| Source unavailable / 429 / 503 | Wait before retrying and respect any reported delay. |

For unresolved issues, use the [support guide](../../SUPPORT.en.md). Do not share tokens, personal information, or case text.

## Update and uninstall

With the HTTP service installed, new releases are verified and installed automatically in the background; no action is needed. To update manually:

```sh
mutalaamcp service stop
mutalaamcp update
mutalaamcp service start
```

For stdio, close all apps connected to Mütalaa and wait a few seconds, then run
`mutalaamcp update`; the apps launch the new version on their next start.
Updates are verified against the SHA-256 digests published on the release channel.

When upgrading from `0.1.0`, close all apps connected to Mütalaa. If you use the HTTP
service, first run `mutalaamcp service stop`. Then install the new package:

```sh
uv tool install --force --refresh --python 3.12 "https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.1/mutalaamcp-0.1.1-py3-none-any.whl"
```

For HTTP, run `mutalaamcp service start` to update the service registration.
For stdio, rerun your client's `setup` command (Claude Desktop:
`mutalaamcp setup --client claude-desktop`) and apply its output to your existing MCP entry.
This makes future updates use the stable launcher; do not add a second MCP entry.

Run your client’s `setup` step again with the updated package to update the skill;
re-import the new ZIP in apps that require imports. Older packages may not bundle skill setup.

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
uv tool install --force --python 3.12 "mutalaamcp[ocr] @ https://github.com/hburaktasyurek/MutalaaMCP/releases/download/v0.1.1/mutalaamcp-0.1.1-py3-none-any.whl"
mutalaamcp ocr install
mutalaamcp ocr status
```

Check for `ready`. For HTTP, reinstall the service with `mutalaamcp setup --client codex --transport http`; for stdio, reopen the app.
OCR models are offered for macOS arm64 and Windows x64. The initial download requires internet access.
