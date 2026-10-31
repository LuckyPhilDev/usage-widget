# Security

Audit of `usage_widget.py`, the build script and git history, 2026-10-31.

## What the widget touches

- **Reads** Claude Code (`~/.claude/.credentials.json`) and Codex (`~/.codex/auth.json`) OAuth tokens, and sends them only to `api.anthropic.com`, `platform.claude.com`, `chatgpt.com` and `auth.openai.com` over HTTPS with certificate checks.
- **Writes** refreshed tokens back to those files when they have expired.
- **Runs** `agy -p /usage` every 5 minutes.
- **Stores** window position and reminders in `%APPDATA%\UsageWidget`, and adds a `Run` registry value for autostart. No tokens, response bodies or URLs with secrets are logged.

The OAuth client IDs in the source are the public IDs of the official CLIs, not secrets. No secrets are in git history.

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | Medium | **Executable planting.** Started from a folder like Downloads, Windows searched the current folder first, so a dropped `agy.bat` ran in place of the real `agy` every 5 minutes. A `notepad.exe` next to `UsageWidget.exe` would run on "Edit settings". Reproduced. | Fixed: `NoDefaultCurrentDirectoryInExePath` is set at startup and Notepad is started by its full System32 path. |
| 2 | Medium | **Credential file race.** A token refresh reads the file, waits on the network, then rewrites it. If Claude Code or Codex logged in or refreshed in that window, the widget overwrote the newer tokens. | Mitigated: the write is skipped when the file changed since it was read. A small window remains, and if both refresh with the same rotating refresh token at once, one side may be signed out. |
| 3 | Low | **Token forwarded on redirect.** `urllib` copies the `Authorization` header to any host a redirect points at. | Fixed: authenticated requests refuse redirects. |
| 4 | Low | **Unpinned build dependencies.** Release exes pulled whatever `pystray`, `pillow` and `pyinstaller` were current. | Fixed: exact versions pinned. Bump them deliberately. |
| 5 | Low | **Paid prompt risk.** If `agy` ever stops treating `/usage` as a command, each poll would be a billed prompt. | Mitigated: after one reply that isn't usage JSON, Antigravity polling stops until restart. |
| 6 | Info | Release exes are unsigned, so users can't verify them. | Open: publish a SHA-256 next to each release (`Get-FileHash dist\UsageWidget.exe`). |
| 7 | Info | `settings.json` `launch` values are opened with `os.startfile`. | Accepted: only the same Windows user can edit it, and they can already run anything. |
| 8 | Info | First run enables autostart without asking. | Accepted: documented in the README, one click to undo in the tray. |
| 9 | Info | Undocumented endpoints are called with the official CLIs' client IDs. | Accepted: noted in the README caveats; check provider terms. |

## Reporting

Open a GitHub issue, or for anything sensitive contact the maintainer privately through their GitHub profile.
