# Security Policy

> **Authoritative version:** This is an English translation. The authoritative Turkish policy is [SECURITY.md](SECURITY.md).

## Reporting a vulnerability

Do **not** report a suspected vulnerability in a public issue, discussion, or
chat. Submit it through GitHub Private Vulnerability Reporting for this
repository:

<https://github.com/hburaktasyurek/MutalaaMCP/security/advisories/new>

If private reporting is unavailable through this link, do not post sensitive details publicly.

A useful report identifies the affected revision or package artifact, the
preconditions and reproducible steps, observed and expected behavior, and a
clear impact assessment. Redact credentials, access tokens, refresh tokens,
personal data, legal queries, document text, and other sensitive material. Do
not attach real secrets.

## Scope

Report vulnerabilities in MutalaaMCP source, its official package artifacts,
and its documented local setup. Examples include credential exposure, unsafe
local file handling, request-routing flaws, dependency or packaging issues, and
behaviors that could expose local research data.

The upstream legal-information services used by the client are outside this
repository's control. Report a defect in an upstream service or source document
to that service through its official channel. A vulnerability in the way
MutalaaMCP calls an upstream service remains in scope here.

## Handling expectations

This repository makes no response-time, remediation-time, or disclosure-date
commitment. Maintainers need a private report and reproducible evidence before
assessing impact or coordinating a fix. Public issue reports are appropriate
for non-sensitive defects; see [`SUPPORT.en.md`](SUPPORT.en.md) for support boundaries.
