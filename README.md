# Usage Widget

Desktop card + tray icon answering "which sub should I use right now?" for Claude, Codex and Antigravity. Refreshes every 5 minutes.

Each row: big bar = 5h quota left, thin bar = time left in the 5h window, right = weekly pace (weekly % left minus % of the week left; positive = spare). Hover a row for reset times. The blue row is the suggestion: first of Claude, Codex, AG Claude/GPT, AG Gemini with 20%+ of its 5h left and not more than 10 points behind weekly pace, unless another sub has 50%+ left and resets within an hour. Tune via the constants at the top of the script.

- Run: `pythonw usage_widget.py` (needs `pip install pystray pillow`)
- Check data sources: `python usage_widget.py --check`
- Auto-start: `Usage Widget.lnk` in `shell:startup`. Delete it to stop auto-start.
- Tray: hover to peek at the card (it drops back when the cursor leaves), left-click to bring it to front until you click elsewhere, right-click for Refresh / Quit. Blue icon = there is a suggestion.
- Drag the card to move it; position is saved in `widget.json`.
- Errors go to `usage_widget.log` (no tokens or response bodies are logged).

Sources: Claude `api.anthropic.com/api/oauth/usage`, Codex `chatgpt.com/backend-api/wham/usage` (both using the CLI logins, refreshing expired tokens and saving them back), Antigravity `agy -p /usage --output-format json`.
