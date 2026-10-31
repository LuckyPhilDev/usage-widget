# Agent guidelines

This repo is public. Everything committed here is published.

## What belongs in the repo

- Product files only: source, README.md, SECURITY.md, LICENSE, build scripts, requirements files.
- Never commit handoffs, plans, session notes, scratch files, screenshots, or anything about the maintainer's setup or preferences. Keep those outside the repo.
- Never commit tokens, credential files, `widget.json`, `settings.json` or logs.

## Git

- Work on a branch per change and merge to `main` through a pull request. Keep commits small and focused.
- No co-author or attribution lines in commit messages.
- Ask before pushing, and never `--force` unless the maintainer asks for it.
- Before pushing, check `git diff origin/main --stat` lists only product files.

## Code

- Keep it small and plain: stdlib first, pin any new dependency in requirements.
- Comments explain why, not what.
- No em dashes in user-facing text (UI, README, release notes).
- Shared logic lives in `usage_core.py`; `usage_widget.py` is the Windows UI and `usage_mac.py` the macOS menu bar UI. Keep platform code out of the core except for small `sys.platform` choices (paths, credential stores).
- Run the logic self-check plus one live fetch before committing: `python usage_widget.py --check` on Windows, `python3 usage_core.py` on macOS. Changes to `usage_core.py` must be checked on both before merging.

## Security invariants (see SECURITY.md)

- Authenticated requests never follow redirects.
- Refreshed tokens are written back only if the credentials file (or macOS Keychain item) is unchanged since it was read.
- Secrets never go on a command line: Keychain writes pass the value as hex on stdin to `security -i`.
- External programs run by full path with an argument list, never a shell string.
- Antigravity polling stops after any reply that isn't usage JSON.
- Never log tokens, response bodies or URLs containing secrets.

## Releases

- Only when asked. Build with `build.ps1` on Windows (`UsageWidget.exe`) and `build.sh` on macOS (`UsageWidget-mac.zip`, built for that Mac's architecture), attach the binaries, and put each SHA-256 in the release notes.
