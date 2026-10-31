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
- Run `python usage_widget.py --check` (logic self-check plus one live fetch) before committing.

## Security invariants (see SECURITY.md)

- Authenticated requests never follow redirects.
- Refreshed tokens are written back only if the credentials file is unchanged since it was read.
- External programs run by full path with an argument list, never a shell string.
- Antigravity polling stops after any reply that isn't usage JSON.
- Never log tokens, response bodies or URLs containing secrets.

## Releases

- Only when asked. Build with `build.ps1` (Windows), attach the binary, and put its SHA-256 in the release notes.
