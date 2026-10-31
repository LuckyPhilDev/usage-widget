"""Platform-neutral part of Usage Widget: fetching usage and deciding which subscription to suggest."""
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

MAC = sys.platform == "darwin"
APP_NAME = "UsageWidget"
HOME = Path.home()

if MAC:
    DATA_DIR = HOME / "Library" / "Application Support" / APP_NAME
    # Apps started from Finder or launchd get a minimal PATH without the places agy is installed.
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("PATH", "/usr/bin:/bin"), "/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local" / "bin")])
else:
    DATA_DIR = Path(os.environ.get("APPDATA", HOME)) / APP_NAME
    # Launched from Downloads, Windows would otherwise run a planted agy.bat from the current folder.
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "widget.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
REFRESH_SECONDS = 300
SESSION_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86400

# Favourite first: the suggestion goes to the first usable sub in this order.
# ORDER and LAUNCH are defaults; settings.json overrides them (see load_settings).
ORDER = ["Claude", "Codex", "AG Claude/GPT", "AG Gemini"]
MIN_SESSION_LEFT = 20      # % of 5h window; below this a sub is not worth suggesting
MAX_BEHIND_PACE = -10      # weekly pace delta; further behind than this and the sub is skipped
EXPIRY_OVERRIDE_LEFT = 50  # % of 5h left that jumps the queue when ...
EXPIRY_OVERRIDE_SECS = 3600  # ... it resets within this long
RUNWAY_LOOKBACK = 30 * 60   # burn rate is measured over this much recent history ...
RUNWAY_MIN_SPAN = 9 * 60    # ... once at least this much exists
REFILLED_AT = 99            # % of 5h left that counts as refilled for reminders

if MAC:  # app names for `open -a`
    LAUNCH = {"Claude": "Claude", "Codex": "ChatGPT", "AG Claude/GPT": "Antigravity", "AG Gemini": "Antigravity"}
else:  # Start menu AppIDs (PowerShell: Get-StartApps) opened by clicking a row.
    LAUNCH = {"Claude": "Claude_pzs8sxrjxfjjc!Claude", "Codex": "OpenAI.Codex_2p2nqsd0c76g0!App",
              "AG Claude/GPT": "Google.Antigravity", "AG Gemini": "Google.Antigravity"}

# Optional notes on each row; off by default, toggled from the tray menu.
SHOW_BACK_TIMES = False  # "back 18:45": when a skipped sub becomes suggestible again
SHOW_RUNWAY = False      # "out ~19:26": 5h quota runs out before its reset at the recent pace

CLAUDE_CREDS = HOME / ".claude" / ".credentials.json"
CLAUDE_KEYCHAIN = "Claude Code-credentials"  # macOS: Claude Code keeps the same JSON in the login Keychain
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CODEX_AUTH = HOME / ".codex" / "auth.json"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

SECURITY = "/usr/bin/security"

logging.basicConfig(filename=DATA_DIR / "usage_widget.log", level=logging.WARNING,
                    format="%(asctime)s %(message)s")


# ---------- fetching ----------
# Each fetcher returns subs: {"name", "h", "h_reset", "w", "w_reset"} where h/w are % left
# of the 5h and weekly windows and resets are epoch seconds (None = window not started).

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # urllib would forward the Authorization header to whatever host it redirects to


OPENER = urllib.request.build_opener(NoRedirect)


def http_json(url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "usage-widget/1.0", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    with OPENER.open(req, timeout=30) as resp:
        return json.load(resp)


def write_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def write_back(path, original_text, obj):
    """Save refreshed tokens unless the owning tool rewrote the file while we were refreshing."""
    if path.read_text() == original_text:
        write_json(path, obj)


def keychain_account(service):
    """Account of a login Keychain item, or None if there is no such item. Reads no secret."""
    out = subprocess.run([SECURITY, "find-generic-password", "-s", service], capture_output=True, text=True)
    match = re.search(r'"acct"<blob>="(.*)"', out.stdout)
    return match.group(1) if out.returncode == 0 and match else None


def keychain_read(service):
    return subprocess.run([SECURITY, "find-generic-password", "-s", service, "-w"],
                          capture_output=True, text=True, check=True).stdout.rstrip("\n")


def keychain_write_back(service, original_text, obj):
    """Keychain twin of write_back. The secret goes in as hex on stdin, so it never shows in `ps`."""
    if keychain_read(service) != original_text:
        return
    account = keychain_account(service)
    command = f'add-generic-password -U -a "{account}" -s "{service}" -X {json.dumps(obj).encode().hex()}\n'
    subprocess.run([SECURITY, "-i"], input=command, capture_output=True, text=True, check=True)


def jwt_exp(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]


def iso_to_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sub(name, h, h_reset, w, w_reset):
    return {"name": name, "h": h, "h_reset": h_reset, "w": w, "w_reset": w_reset}


def claude_in_keychain():
    return MAC and keychain_account(CLAUDE_KEYCHAIN) is not None


def fetch_claude():
    if claude_in_keychain():
        raw = keychain_read(CLAUDE_KEYCHAIN)
        save = lambda obj: keychain_write_back(CLAUDE_KEYCHAIN, raw, obj)
    else:
        raw = CLAUDE_CREDS.read_text()
        save = lambda obj: write_back(CLAUDE_CREDS, raw, obj)
    creds = json.loads(raw)
    oauth = creds["claudeAiOauth"]
    if oauth["expiresAt"] / 1000 < time.time() + 60:
        new = http_json(CLAUDE_TOKEN_URL, body={
            "grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
            "client_id": CLAUDE_CLIENT_ID, "scope": " ".join(oauth.get("scopes", []))})
        oauth["accessToken"] = new["access_token"]
        oauth["refreshToken"] = new.get("refresh_token", oauth["refreshToken"])
        oauth["expiresAt"] = int((time.time() + new["expires_in"]) * 1000)
        save(creds)
    data = http_json(CLAUDE_USAGE_URL, {
        "Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"})

    def window(w):
        return 100 - w["utilization"], iso_to_epoch(w["resets_at"]) if w.get("resets_at") else None

    # Opus/Sonnet weekly caps only matter when they are tighter than the overall weekly one.
    weeklies = [window(data[k]) for k in ("seven_day", "seven_day_opus", "seven_day_sonnet") if data.get(k)]
    weekly = min(weeklies, key=lambda pct_reset: pct_reset[0])
    return [sub("Claude", *window(data["five_hour"]), *weekly)]


def fetch_codex():
    raw = CODEX_AUTH.read_text()
    auth = json.loads(raw)
    tokens = auth["tokens"]
    if jwt_exp(tokens["access_token"]) < time.time() + 60:
        new = http_json(CODEX_TOKEN_URL, body={
            "client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "scope": "openid profile email"})
        for key in ("access_token", "id_token", "refresh_token"):
            if new.get(key):
                tokens[key] = new[key]
        auth["last_refresh"] = datetime.utcnow().isoformat() + "Z"
        write_back(CODEX_AUTH, raw, auth)
    data = http_json(CODEX_USAGE_URL, {
        "Authorization": f"Bearer {tokens['access_token']}", "ChatGPT-Account-Id": tokens["account_id"]})
    limits = data["rate_limit"]
    session, week = limits["primary_window"], limits["secondary_window"]
    return [sub("Codex", 100 - session["used_percent"], session["reset_at"],
                100 - week["used_percent"], week["reset_at"])]


agy_trusted = True  # cleared when agy's reply isn't usage JSON: "/usage" may have run as a paid prompt


def fetch_antigravity():
    global agy_trusted
    if not agy_trusted:
        raise RuntimeError("skipped after unexpected agy output; restart to retry")
    # Argument list, no shell: a shell (Git Bash) can mangle "/usage" into a path, which agy sends as a paid prompt.
    out = subprocess.run([shutil.which("agy"), "-p", "/usage", "--output-format", "json"],
                         capture_output=True, text=True, timeout=120,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True).stdout
    try:
        groups = json.loads(out)["command"]["data"]["groups"]
    except (ValueError, KeyError, TypeError):
        agy_trusted = False
        raise
    names = {"Gemini Models": "AG Gemini", "Claude and GPT models": "AG Claude/GPT", "Gemini 4 Argon": "Argon"}
    subs = []
    for group in groups:
        windows = {"5h": (None, None), "weekly": (None, None)}
        for b in group["buckets"]:
            pct = b["remaining_fraction"] * 100
            # At 100% the API reports reset as now+window, which is meaningless.
            windows[b["window"]] = (pct, None if pct >= 100 else iso_to_epoch(b["reset_time"]))
        subs.append(sub(names.get(group["name"], group["name"]), *windows["5h"], *windows["weekly"]))
    return subs


ALL_PROVIDERS = {  # name -> (fetcher, is it installed?)
    "Claude": (fetch_claude, lambda: CLAUDE_CREDS.exists() or claude_in_keychain()),
    "Codex": (fetch_codex, CODEX_AUTH.exists),
    "Antigravity": (fetch_antigravity, lambda: shutil.which("agy") is not None),
}


def installed_providers():
    return {name: fetch for name, (fetch, installed) in ALL_PROVIDERS.items() if installed()}


# ---------- settings ----------

def load_settings():
    """User-editable settings; written with defaults on first run."""
    global ORDER, LAUNCH, SHOW_BACK_TIMES, SHOW_RUNWAY
    if not SETTINGS_FILE.exists():
        write_json(SETTINGS_FILE, {"order": ORDER, "launch": LAUNCH,
                                   "show_back_times": SHOW_BACK_TIMES, "show_runway": SHOW_RUNWAY})
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        ORDER, LAUNCH = settings.get("order", ORDER), settings.get("launch", LAUNCH)
        SHOW_BACK_TIMES = bool(settings.get("show_back_times", False))
        SHOW_RUNWAY = bool(settings.get("show_runway", False))
    except (OSError, ValueError) as exc:  # keep the previous settings until the file is fixed
        logging.warning("settings.json: %s", exc)


def save_setting(key, value):
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        settings[key] = value
        write_json(SETTINGS_FILE, settings)
    except (OSError, ValueError) as exc:  # a broken file is left for the user to fix via Edit settings
        logging.warning("settings.json: %s", exc)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


# ---------- deciding ----------

def time_left(reset, window_seconds, now):
    """% of the window still to run; a window that hasn't started has all of it."""
    if reset is None:
        return 100.0
    return max(0.0, min(100.0, (reset - now) / window_seconds * 100))


def pace_delta(s, now):
    """Weekly % left minus % of the week left: positive means spare quota."""
    if s["w"] is None:
        return 0
    return round(s["w"] - time_left(s["w_reset"], WEEK_SECONDS, now))


def is_usable(s, now):
    return s["h"] >= MIN_SESSION_LEFT and pace_delta(s, now) > MAX_BEHIND_PACE


def usable_again_at(s, now):
    """When a sub that is too low becomes suggestible again if left idle; None if usable now."""
    if is_usable(s, now):
        return None
    waits = []
    if s["h"] < MIN_SESSION_LEFT:
        waits.append(s["h_reset"] or now)
    delta = pace_delta(s, now)
    if delta <= MAX_BEHIND_PACE:
        # Idle quota holds steady while the week runs down, closing the gap at 100% per week.
        catch_up = now + (MAX_BEHIND_PACE + 1 - delta) / 100 * WEEK_SECONDS
        waits.append(min(catch_up, s["w_reset"] or catch_up))
    return max(waits)


def runs_out_at(samples):
    """Epoch when 5h quota hits 0 at the recent burn rate; samples are (time, % left), oldest first."""
    if len(samples) < 2:
        return None
    t1, p1 = samples[-1]
    t0, p0 = next((t, p) for t, p in samples if t1 - t <= RUNWAY_LOOKBACK)
    if t1 - t0 < RUNWAY_MIN_SPAN or p0 <= p1:
        return None
    return t1 + p1 / ((p0 - p1) / (t1 - t0))


def record_sample(samples, s, now):
    """Append a 5h reading to samples (sub name -> [(time, % left)]) for the burn rate."""
    if s["h"] is None:
        return
    history = samples.setdefault(s["name"], [])
    if history and s["h"] > history[-1][1] + 1:  # window reset; old burn rate no longer applies
        history.clear()
    history.append((now, s["h"]))
    del history[:-(RUNWAY_LOOKBACK // (REFRESH_SECONDS // 2))]


def suggest(subs, now):
    usable = [s for s in subs if s["h"] is not None and is_usable(s, now)]
    expiring = [s for s in usable if s["h_reset"] and s["h"] > EXPIRY_OVERRIDE_LEFT
                and s["h_reset"] - now < EXPIRY_OVERRIDE_SECS]
    pool = expiring or usable
    return min(pool, key=lambda s: rank(s["name"]))["name"] if pool else None


def rank(name):
    return ORDER.index(name) if name in ORDER else len(ORDER)


# ---------- formatting ----------

def countdown(reset):
    if reset is None:
        return "not started"
    return f"{duration(reset - time.time())} ({clock(reset)})"


def duration(secs):
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M" if ts - time.time() < 86400 else "%a %H:%M")


def ago(ts):
    mins = int((time.time() - ts) // 60)
    return "just now" if mins < 1 else f"{mins}m ago"


def self_check():
    now = 1_000_000.0
    week_half = now + WEEK_SECONDS / 2

    def s(name, h, h_in, w=50):
        return sub(name, h, now + h_in if h_in else None, w, week_half)

    assert suggest([s("Claude", 91, 4 * 3600), s("Codex", 80, 3600)], now) == "Claude", "favourite wins"
    assert suggest([s("Claude", 10, 4 * 3600), s("Codex", 80, 3600)], now) == "Codex", "low session skipped"
    assert suggest([s("Claude", 91, 4 * 3600, w=30), s("Codex", 80, 7200)], now) == "Codex", "behind pace skipped"
    assert suggest([s("Claude", 91, 4 * 3600), s("AG Gemini", 70, 1800)], now) == "AG Gemini", "expiry override"
    assert suggest([s("Claude", 5, 3600)], now) is None, "nothing usable"
    assert pace_delta(sub("x", 1, None, 100, None), now) == 0, "unstarted week is on pace"

    assert usable_again_at(s("Claude", 91, 3600), now) is None, "usable now"
    assert usable_again_at(s("Codex", 10, 3600), now) == now + 3600, "low session back at reset"
    behind = usable_again_at(s("Codex", 80, 3600, w=35), now)  # pace -15: needs 6 points of the week to pass
    assert abs(behind - (now + 0.06 * WEEK_SECONDS)) < 1, behind

    assert runs_out_at([(now, 80)]) is None, "one sample has no rate"
    assert runs_out_at([(now - 600, 80), (now, 70)]) == now + 70 * 60, "10%/10min, 70% left = 70 min"
    assert runs_out_at([(now - 600, 70), (now, 70)]) is None, "idle never runs out"
    assert runs_out_at([(now - 300, 80), (now, 70)]) is None, "too little history"

    assert NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None, "no redirect"
    with tempfile.TemporaryDirectory() as tmp:
        creds = Path(tmp) / "creds.json"
        creds.write_text("newer login")
        write_back(creds, "stale", {"token": "ours"})
        assert creds.read_text() == "newer login", "tool's newer file kept"
        write_back(creds, "newer login", {"token": "ours"})
        assert json.loads(creds.read_text()) == {"token": "ours"}, "unchanged file updated"
    print("self-check ok")


def check():
    """Self-check plus one live fetch from every installed tool, printed."""
    self_check()
    now, subs = time.time(), []
    load_settings()
    providers = installed_providers()
    print("installed:", ", ".join(providers) or "none")
    for name, fetch in providers.items():
        try:
            subs += fetch()
        except Exception as exc:
            print(f"{name:12} FAILED {type(exc).__name__}: {exc}")
    for s in sorted(subs, key=lambda s: rank(s["name"])):
        h = "    -" if s["h"] is None else f"{s['h']:5.1f}%"
        print(f"{s['name']:14} 5h {h} {countdown(s['h_reset']):>20}  pace {pace_delta(s, now):+d}%")
    print("suggest:", suggest([s for s in subs if s["h"] is not None], now))


if __name__ == "__main__":
    check()
