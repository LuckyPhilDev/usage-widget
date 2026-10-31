# Usage Widget

A small Windows desktop card and tray icon that answers "which AI subscription should I use right now?" for Claude Code, Codex and Antigravity.

## Install

Download `UsageWidget.exe` from Releases and run it. No installer: it starts with Windows by default (untick "Start with Windows" in the tray menu to stop). Windows may show a SmartScreen warning because the exe is unsigned.

It reads the logins of whichever tools you already use on this PC; tools you don't have are hidden:

- Claude Code: `%USERPROFILE%\.claude\.credentials.json`
- Codex: `%USERPROFILE%\.codex\auth.json`
- Antigravity: the `agy` CLI on your PATH

## Reading it

Each row: big bar = 5 hour quota left, thin bar = time left in the 5 hour window, right = weekly pace (weekly % left minus % of the week left; positive means spare). Hover a row for reset times.

The blue row is the suggestion: the first sub in your favourite order with 20%+ of its 5 hour quota left and not more than 10 points behind weekly pace, unless another sub has 50%+ left and resets within an hour.

Notes on a bar: `back 18:45` = when a skipped sub becomes suggestible again if left idle; `out ~16:56` = at your last 30 minutes of use, the 5 hour quota runs out before it resets.

- Click a row to open that tool. Right-click for "Remind me when it refills" (a Windows notification once it's back to 99%+).
- Tray: hover to peek at the card, left-click to bring it to the front, right-click for Refresh, Start with Windows, Edit settings, Quit.
- Drag the card to move it.

## Settings

Tray > Edit settings opens `%APPDATA%\UsageWidget\settings.json`:

- `order`: favourite order for the suggestion (`Claude`, `Codex`, `AG Claude/GPT`, `AG Gemini`)
- `launch`: Start menu AppID opened when you click a row (find them with PowerShell `Get-StartApps`)

Changes apply on the next refresh. Position, reminders and an error log live in the same folder.

## Caveats

Usage comes from undocumented endpoints the official tools use (`api.anthropic.com/api/oauth/usage`, `chatgpt.com/backend-api/wham/usage`, `agy -p /usage`), so it can break without notice. When a Claude Code or Codex login has expired, the widget refreshes it and saves it back to that tool's credentials file, as the tool itself would (skipped if the tool changed the file meanwhile). See [SECURITY.md](SECURITY.md). Check the providers' terms before relying on it.

## Development

```powershell
python -m pip install -r requirements.txt
python usage_widget.py --check   # logic self-check + one live fetch
pythonw usage_widget.py          # run without a console
.\build.ps1                      # builds dist\UsageWidget.exe
```
