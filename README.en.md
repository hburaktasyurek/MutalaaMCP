# Mütalaa MCP

![Mütalaa MCP promotional graphic in Turkish: Turkish legal sources in your AI app. Free, open source, and local.](docs/assets/social/mutalaa-mcp-tanitim.png)

**Research Turkish legal sources from the AI app you already use.**
Free, open source, and running on your computer.

[Setup guide](docs/client-setup/README.en.md) · [Türkçe](README.md) · [Mütalaa](https://mutalaa.tr/)

## What can you do?

- Search legislation, judicial decisions, and Constitutional Court decisions.
- Read the relevant article or document and open its official source.
- Start with everyday questions such as “What shared expenses must a tenant pay?”

## Install with your AI

Copy this prompt into your AI app. With access to your computer, it can perform setup; otherwise, it will guide you step by step.

```text
Help me install Mütalaa MCP on my computer.

First read the official setup guide:
https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/client-setup/README.en.md
Companion skill guide:
https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/mutalaa-skill.md

Identify my operating system and intended AI app from context; ask if unknown. If you cannot access the guide, say so and ask me for its contents. Do not invent commands or compatibility claims.

If you can run commands on my computer, inspect the existing installation and follow the appropriate documented steps. Preserve existing app settings and other MCP connections. Otherwise, guide me one short step at a time without assuming technical knowledge. Explain if my app does not support local MCP.

Direct me to the sign-in screen when needed. Never ask me to paste passwords or access tokens into chat. Install the Mütalaa companion skill if supported.

Finally, verify the connection with a real Mütalaa tool call: find the Turkish Income Tax Law and return its source link. Do not substitute web search for this verification. If tools are unavailable, guide me to the required new conversation or app and mark verification as pending.

Briefly state what is complete and what I still need to do. Do not claim the installation is verified until a tool call succeeds.
```

## Manual setup

You need a **Mütalaa account**, an internet connection, and an **AI app that supports local MCP connections**.

1. Follow the [setup guide](docs/client-setup/README.en.md) for your computer. No repository clone or coding is required.
2. Add Mütalaa to your app and sign in.
3. Add the [Mütalaa skill](docs/mutalaa-skill.md) to help the app select the tools for natural legal questions.
4. Try this in a new conversation:

   > Use Mütalaa to find Law No. 193, the Turkish Income Tax Law, and give its official source link.

A `mutalaamcp` tool call shows that Mütalaa was used. If you only see web searches, follow [troubleshooting](docs/client-setup/README.en.md#troubleshooting).

**Installation:** Use the [terminal guide](docs/client-setup/README.en.md) to install a specific release. No unsigned macOS installer is offered; the Windows CMD script is experimental.

## Data and cost

Local use is free, with no commercial-use quota. Your AI app may charge separately.
Processing and the research cache stay on your computer; source requests go to the relevant official services.
Your Mütalaa account is used for sign-in and activation; legal queries and document text are not sent to the activation service.
Your AI app processes your question and tool results under its own data policy.

Mütalaa product announcements may appear separately at the end of an answer. [How announcements work](docs/announcements.md).

## For developers

Eight tools in one local server: research entry point, legislation/decision/Constitutional Court search,
document retrieval, article retrieval, search within legislation, and article outlines.
Inputs and responses are defined in the [tool contract](contracts/tool-surface-v1.json).

- [Contributing and forks](CONTRIBUTING.md)
- [Support and bug reports](SUPPORT.en.md)
- [Reporting vulnerabilities](SECURITY.en.md)

MIT licensed. [License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.en.md) · [Trademark policy](TRADEMARK_POLICY.en.md)

Mütalaa MCP is legal-research software. Verify results against primary sources and consult a qualified professional about a specific matter.
