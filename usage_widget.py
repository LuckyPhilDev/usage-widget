"""Desktop widget suggesting which AI subscription to use right now (Claude, Codex, Antigravity)."""
import base64
import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

# Launched from Downloads, Windows would otherwise run a planted agy.bat from the current folder.
os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"

APP_NAME = "UsageWidget"
HOME = Path.home()
NOTEPAD = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
DATA_DIR = Path(os.environ.get("APPDATA", HOME)) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "widget.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REFRESH_SECONDS = 300
REDRAW_MS = 30_000
PEEK_POLL_MS = 150
PEEK_RADIUS = 24  # px the cursor can drift from the tray icon before the peek ends
SESSION_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86400

# Favourite first: the suggestion goes to the first usable sub in this order.
# ORDER and LAUNCH are defaults; settings.json overrides them (see load_settings).
ORDER =["Claude", "Codex", "AG Claude/GPT", "AG Gemini"]
MIN_SESSION_LEFT = 20      # % of 5h window; below this a sub is not worth suggesting
MAX_BEHIND_PACE = -10      # weekly pace delta; further behind than this and the sub is skipped
EXPIRY_OVERRIDE_LEFT = 50  # % of 5h left that jumps the queue when ...
EXPIRY_OVERRIDE_SECS = 3600  # ... it resets within this long
RUNWAY_LOOKBACK = 30 * 60   # burn rate is measured over this much recent history ...
RUNWAY_MIN_SPAN = 9 * 60    # ... once at least this much exists
REFILLED_AT = 99            # % of 5h left that counts as refilled for reminders
CLICK_SLOP = 4              # px of movement before a press is a drag, not a click

# Start menu AppIDs (PowerShell: Get-StartApps) opened by clicking a row.
LAUNCH = {"Claude": "Claude_pzs8sxrjxfjjc!Claude", "Codex": "OpenAI.Codex_2p2nqsd0c76g0!App",
          "AG Claude/GPT": "Google.Antigravity", "AG Gemini": "Google.Antigravity"}

CLAUDE_CREDS = HOME / ".claude" / ".credentials.json"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CODEX_AUTH = HOME / ".codex" / "auth.json"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

BG, FG, DIM, TRACK, INK = "#1b1d22", "#e6e6e6", "#6b6f78", "#33363d", "#101114"
GREEN, AMBER, RED, ACCENT, TIME_BAR, NEUTRAL = "#4caf50", "#f0a030", "#e05050", "#4a9eff", "#9aa3b2", "#c8ccd4"
FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_ICON = ("Segoe MDL2 Assets", 8)
BELL = ""  # Segoe MDL2 "Ringer"
BAR_W, BAR_H, TIME_H = 150, 14, 3

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


def jwt_exp(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]


def iso_to_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sub(name, h, h_reset, w, w_reset):
    return {"name": name, "h": h, "h_reset": h_reset, "w": w, "w_reset": w_reset}


def fetch_claude():
    raw = CLAUDE_CREDS.read_text()
    creds = json.loads(raw)
    oauth = creds["claudeAiOauth"]
    if oauth["expiresAt"] / 1000 < time.time() + 60:
        new = http_json(CLAUDE_TOKEN_URL, body={
            "grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
            "client_id": CLAUDE_CLIENT_ID, "scope": " ".join(oauth.get("scopes", []))})
        oauth["accessToken"] = new["access_token"]
        oauth["refreshToken"] = new.get("refresh_token", oauth["refreshToken"])
        oauth["expiresAt"] = int((time.time() + new["expires_in"]) * 1000)
        write_back(CLAUDE_CREDS, raw, creds)
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
                         creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout
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
    "Claude": (fetch_claude, CLAUDE_CREDS.exists),
    "Codex": (fetch_codex, CODEX_AUTH.exists),
    "Antigravity": (fetch_antigravity, lambda: shutil.which("agy") is not None),
}


def installed_providers():
    return {name: fetch for name, (fetch, installed) in ALL_PROVIDERS.items() if installed()}


# ---------- settings and startup ----------

def load_settings():
    """User-editable favourite order and launch targets; written with defaults on first run."""
    global ORDER, LAUNCH
    if not SETTINGS_FILE.exists():
        write_json(SETTINGS_FILE, {"order": ORDER, "launch": LAUNCH})
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        ORDER, LAUNCH = settings.get("order", ORDER), settings.get("launch", LAUNCH)
    except (OSError, ValueError) as exc:  # keep the previous settings until the file is fixed
        logging.warning("settings.json: %s", exc)


def launch_command():
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}"'
    return f'"{Path(sys.executable).with_name("pythonw.exe")}" "{Path(__file__).resolve()}"'


def starts_with_windows():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_start_with_windows(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            winreg.DeleteValue(key, APP_NAME)


def already_running():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The handle is deliberately never closed: it marks this instance until the process exits.
    kernel32.CreateMutexW(None, False, f"{APP_NAME}SingleInstance")
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


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


def suggest(subs, now):
    usable = [s for s in subs if s["h"] is not None and is_usable(s, now)]
    expiring = [s for s in usable if s["h_reset"] and s["h"] > EXPIRY_OVERRIDE_LEFT
                and s["h_reset"] - now < EXPIRY_OVERRIDE_SECS]
    pool = expiring or usable
    return min(pool, key=lambda s: rank(s["name"]))["name"] if pool else None


def rank(name):
    return ORDER.index(name) if name in ORDER else len(ORDER)


# ---------- formatting ----------

def color_for(pct):
    return GREEN if pct > 50 else AMBER if pct > 20 else RED


def pace_color(delta):
    return GREEN if delta >= 5 else RED if delta <= -10 else AMBER if delta <= -3 else NEUTRAL


def countdown(reset):
    if reset is None:
        return "not started"
    secs = max(0, int(reset - time.time()))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    left = f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"
    return f"{left} ({clock(reset)})"


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M" if ts - time.time() < 86400 else "%a %H:%M")


def ago(ts):
    mins = int((time.time() - ts) // 60)
    return "just now" if mins < 1 else f"{mins}m ago"


# ---------- window ----------

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", ctypes.c_ulong)]


def cursor_pos():
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def work_area(x, y):
    """Usable area (minus taskbar) of the monitor containing (x, y)."""
    MONITOR_DEFAULTTONEAREST = 2
    monitor = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


class TrayIcon(pystray.Icon):
    """pystray ignores mouse-over on the tray icon; forward it so hovering can peek at the widget."""
    WM_MOUSEMOVE = 0x0200

    def __init__(self, *args, on_hover, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_hover = on_hover

    def _on_notify(self, wparam, lparam):
        if lparam == self.WM_MOUSEMOVE:
            self.on_hover()
        return super()._on_notify(wparam, lparam)


class Tooltip:
    OFFSET = 12

    def __init__(self, root):
        self.root, self.win = root, None

    def show(self, text, x, y):
        self.hide()
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        tk.Label(self.win, text=text, font=FONT, fg=FG, bg="#2a2d34", justify="left",
                 padx=8, pady=5).pack()
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = work_area(x, y)
        # Below-right of the cursor, flipped to the other side when that would leave the screen.
        tx = x + self.OFFSET if x + self.OFFSET + w <= right else x - self.OFFSET - w
        ty = y + self.OFFSET if y + self.OFFSET + h <= bottom else y - self.OFFSET - h
        self.win.geometry(f"+{max(left, tx)}+{max(top, ty)}")

    def hide(self):
        if self.win:
            self.win.destroy()
            self.win = None


class Widget:
    def __init__(self):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", 0.92)
        state = self.load_state()
        self.root.geometry(f"+{state.get('x', 40)}+{state.get('y', 40)}")
        self.reminders = set(state.get("reminders", []))  # sub names to notify about when they refill
        self.body = tk.Frame(self.root, bg=BG, padx=12, pady=10)
        self.body.pack()
        self.tooltip = Tooltip(self.root)

        load_settings()
        self.providers = installed_providers()
        # provider -> {"subs": [...], "ok_at": ts, "error": str|None}
        self.data = {name: {"subs": [], "ok_at": None, "error": None} for name in self.providers}
        self.events = queue.Queue()
        self.popped = False
        self.peek_anchor = None  # cursor position while hover-peeking from the tray
        self.drag_from = None
        self.press_at = None
        self.samples = {}  # sub name -> [(time, 5h % left)] for burn rate

        self.root.bind_all("<ButtonPress-1>", self.drag_start)
        self.root.bind_all("<B1-Motion>", self.drag_move)
        self.root.bind_all("<ButtonRelease-1>", self.drag_end)
        self.root.bind("<FocusOut>", lambda e: self.pop_down())

        self.tray = TrayIcon("usage_widget", self.tray_image(False), "AI usage", menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("popup",)), default=True, visible=False),
            pystray.MenuItem("Refresh", lambda: self.events.put(("refresh",))),
            pystray.MenuItem("Start with Windows", self.toggle_autostart, checked=lambda item: starts_with_windows()),
            pystray.MenuItem("Edit settings", lambda: subprocess.Popen([NOTEPAD, SETTINGS_FILE])),
            pystray.MenuItem("Quit", lambda: self.events.put(("quit",)))),
            on_hover=lambda: self.events.put(("peek",)))
        self.tray.run_detached()

        self.render()
        self.root.after(100, self.to_bottom)
        self.root.after(200, self.pump)
        self.root.after(REDRAW_MS, self.redraw_loop)
        self.refresh()

    # --- state ---
    def load_state(self):
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self):
        write_json(STATE_FILE, {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                                "reminders": sorted(self.reminders)})

    # --- z-order ---
    def hwnd(self):
        return ctypes.windll.user32.GetParent(self.root.winfo_id())

    def to_bottom(self):
        HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
        self.root.attributes("-topmost", False)
        ctypes.windll.user32.SetWindowPos(self.hwnd(), HWND_BOTTOM, 0, 0, 0, 0,
                                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def pop_up(self):
        self.popped = True
        self.root.attributes("-topmost", True)
        self.root.focus_force()

    def pop_down(self):
        if self.popped:
            self.popped = False
            self.to_bottom()

    def peek(self):
        starting = self.peek_anchor is None
        self.peek_anchor = cursor_pos()
        if starting and not self.popped:
            self.root.attributes("-topmost", True)
            self.root.after(PEEK_POLL_MS, self.peek_check)

    def peek_check(self):
        """Keep peeking while the cursor stays near the tray icon or over the widget."""
        x, y = cursor_pos()
        ax, ay = self.peek_anchor
        wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
        over_widget = wx <= x < wx + self.root.winfo_width() and wy <= y < wy + self.root.winfo_height()
        near_tray = abs(x - ax) <= PEEK_RADIUS and abs(y - ay) <= PEEK_RADIUS
        if over_widget or near_tray:
            self.root.after(PEEK_POLL_MS, self.peek_check)
            return
        self.peek_anchor = None
        if not self.popped:
            self.to_bottom()

    # --- dragging ---
    def drag_start(self, e):
        self.tooltip.hide()
        self.press_at = (e.x_root, e.y_root)
        self.drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        dx, dy = self.drag_from
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def drag_end(self, e):
        self.save_state()
        if not self.popped:
            self.to_bottom()

    # --- refresh ---
    @staticmethod
    def toggle_autostart(icon, item):
        set_start_with_windows(not starts_with_windows())
        icon.update_menu()

    def refresh(self):
        load_settings()  # pick up edits to settings.json without a restart
        for name, fetch in self.providers.items():
            threading.Thread(target=self.fetch_one, args=(name, fetch), daemon=True).start()

    def fetch_one(self, name, fetch):
        try:
            self.events.put(("data", name, fetch(), None))
        except Exception as exc:  # any failure keeps the last good values on screen
            logging.warning("%s: %s: %s", name, type(exc).__name__, exc)
            self.events.put(("data", name, None, f"{type(exc).__name__}"))

    def pump(self):
        while not self.events.empty():
            kind, *args = self.events.get()
            if kind == "data":
                name, subs, error = args
                entry = self.data[name]
                entry["error"] = error
                if subs is not None:
                    entry["subs"], entry["ok_at"] = subs, time.time()
                    self.record_samples(subs)
                    self.check_reminders(subs)
                self.render()
            elif kind == "popup":
                self.pop_up()
            elif kind == "peek":
                self.peek()
            elif kind == "refresh":
                self.refresh()
            elif kind == "quit":
                self.tray.stop()
                self.root.destroy()
                return
        self.root.after(200, self.pump)

    def redraw_loop(self):
        self.render()
        self.root.after(REDRAW_MS, self.redraw_loop)

    def auto_refresh(self):
        self.refresh()
        self.root.after(REFRESH_SECONDS * 1000, self.auto_refresh)

    # --- drawing ---
    def render(self):
        self.tooltip.hide()
        for child in self.body.winfo_children():
            child.destroy()
        now = time.time()
        rows, footnotes = [], []
        for provider, entry in self.data.items():
            stale = entry["error"] is not None
            for s in entry["subs"]:
                (rows if s["h"] is not None else footnotes).append((s, stale))
            if entry["ok_at"] is None:
                footnotes.append((f"{provider}: {'failed' if stale else 'loading...'}", stale))
            elif stale:
                footnotes.append((f"{provider}: failed, showing {ago(entry['ok_at'])}", stale))
        if not self.providers:
            footnotes.append(("No supported tools found. Sign in to Claude Code, Codex or Antigravity.", False))
        rows.sort(key=lambda r: rank(r[0]["name"]))
        pick = suggest([s for s, stale in rows if not stale], now)

        for r, (s, stale) in enumerate(rows):
            self.draw_row(r, s, stale, s["name"] == pick, now)
        for r, (note, stale) in enumerate(footnotes, start=len(rows)):
            text = note if isinstance(note, str) else f"{note['name']} weekly {note['w']:.0f}%"
            tk.Label(self.body, text=text, font=FONT_SMALL, fg=DIM, bg=BG, anchor="w").grid(
                row=r, column=0, columnspan=4, sticky="w", pady=(4 if r == len(rows) else 0, 0))

        self.tray.icon = self.tray_image(pick is not None)
        self.tray.title = f"Use {pick}" if pick else "AI usage: nothing suggested"

    def draw_row(self, r, s, stale, picked, now):
        stripe = tk.Frame(self.body, bg=ACCENT if picked else BG, width=3, height=26)
        stripe.grid(row=r, column=0, padx=(0, 8), pady=3)
        label = tk.Frame(self.body, bg=BG)
        label.grid(row=r, column=1, sticky="w")
        name = tk.Label(label, text=s["name"], font=FONT_BOLD, bg=BG, fg=DIM if stale else ACCENT if picked else FG)
        name.pack(side="left")
        reminded = s["name"] in self.reminders
        bell = tk.Label(label, text=BELL if reminded else "", font=FONT_ICON, bg=BG, fg=ACCENT)
        bell.pack(side="left", padx=(4, 0))
        self.body.grid_columnconfigure(1, minsize=112)

        back_at = usable_again_at(s, now)
        out_at = runs_out_at(self.samples.get(s["name"], []))
        running_out = back_at is None and out_at is not None and s["h_reset"] and out_at < s["h_reset"]
        note = f"back {clock(back_at)}" if back_at else f"out ~{clock(out_at)}" if running_out else ""

        bar = tk.Canvas(self.body, width=BAR_W, height=BAR_H + 3 + TIME_H, bg=BG, highlightthickness=0)
        fill_w = BAR_W * s["h"] / 100
        bar.create_rectangle(0, 0, BAR_W, BAR_H, fill=TRACK, width=0)
        bar.create_rectangle(0, 0, fill_w, BAR_H, fill=DIM if stale else color_for(s["h"]), width=0)
        bar.create_text(6, BAR_H / 2, text=f"{s['h']:.0f}%", anchor="w", font=FONT_SMALL,
                        fill=INK if s["h"] >= 20 else FG)
        if note:
            # Backed by a track-coloured chip so it reads the same over the fill or the empty track.
            text = bar.create_text(BAR_W - 5, BAR_H / 2, text=note, anchor="e", font=FONT_SMALL,
                                   fill=AMBER if running_out else NEUTRAL)
            x0, _, x1, _ = bar.bbox(text)
            bar.tag_lower(bar.create_rectangle(x0 - 4, 1, BAR_W - 1, BAR_H - 1, fill=TRACK, width=0), text)
        time_y = BAR_H + 3
        bar.create_rectangle(0, time_y, BAR_W, time_y + TIME_H, fill=TRACK, width=0)
        if s["h_reset"] is not None:
            bar.create_rectangle(0, time_y, BAR_W * time_left(s["h_reset"], SESSION_SECONDS, now) / 100,
                                 time_y + TIME_H, fill=TIME_BAR, width=0)
        bar.grid(row=r, column=2, padx=(8, 8))

        delta = pace_delta(s, now)
        pace = tk.Label(self.body, text=f"{delta:+d}%", font=FONT, width=5, anchor="e", bg=BG,
                        fg=DIM if stale else pace_color(delta))
        pace.grid(row=r, column=3, sticky="e")

        lines = [f"5 hour: {s['h']:.0f}% left, resets {countdown(s['h_reset'])}",
                 "Weekly not reported" if s["w"] is None else
                 f"Weekly: {s['w']:.0f}% left, resets {countdown(s['w_reset'])}",
                 f"Weekly pace: {delta:+d}% vs an even spend"]
        if back_at:
            lines.append(f"Suggestible again at {clock(back_at)} if left idle")
        if out_at:
            lines.append(f"At your recent pace, 5 hour runs out ~{clock(out_at)}")
        if reminded:
            lines.append("Refill reminder set")
        lines.append("Click to open, right-click for a refill reminder")
        tip = "\n".join(lines)

        for widget in (stripe, label, name, bell, bar, pace):
            widget.configure(cursor="hand2")
            widget.bind("<Enter>", lambda e, t=tip: self.tooltip.show(t, e.x_root, e.y_root))
            widget.bind("<Leave>", lambda e: self.tooltip.hide())
            widget.bind("<ButtonRelease-1>", lambda e, n=s["name"]: self.click_row(e, n))
            widget.bind("<Button-3>", lambda e, sub=s: self.row_menu(e, sub))

    # --- row actions ---
    def click_row(self, e, name):
        px, py = self.press_at or (e.x_root, e.y_root)
        if abs(e.x_root - px) <= CLICK_SLOP and abs(e.y_root - py) <= CLICK_SLOP:
            self.open_tool(name)

    @staticmethod
    def open_tool(name):
        if name in LAUNCH:
            os.startfile(f"shell:AppsFolder\\{LAUNCH[name]}")

    def row_menu(self, e, s):
        self.tooltip.hide()
        menu = tk.Menu(self.root, tearoff=False)
        if s["name"] in self.reminders:
            menu.add_command(label="Cancel refill reminder", command=lambda: self.set_reminder(s, False))
        elif s["h"] < REFILLED_AT:
            menu.add_command(label=f"Remind me when it refills ({clock(s['h_reset'])})" if s["h_reset"]
                             else "Remind me when it refills", command=lambda: self.set_reminder(s, True))
        if s["name"] in LAUNCH:
            menu.add_command(label="Open", command=lambda: self.open_tool(s["name"]))
        menu.tk_popup(e.x_root, e.y_root)

    def set_reminder(self, s, on):
        (self.reminders.add if on else self.reminders.discard)(s["name"])
        if on and s["h_reset"]:
            # Fetch right after the reset rather than waiting for the next 5 minute refresh.
            self.root.after(int(max(0, s["h_reset"] - time.time() + 30) * 1000), self.refresh)
        self.save_state()
        self.render()

    def check_reminders(self, subs):
        refilled = [s for s in subs if s["name"] in self.reminders and s["h"] is not None and s["h"] >= REFILLED_AT]
        for s in refilled:
            self.reminders.discard(s["name"])
            self.tray.notify(f"{s['name']} is back to {s['h']:.0f}% of its 5 hour limit.", f"{s['name']} refilled")
        if refilled:
            self.save_state()

    def record_samples(self, subs):
        now = time.time()
        for s in subs:
            if s["h"] is None:
                continue
            history = self.samples.setdefault(s["name"], [])
            if history and s["h"] > history[-1][1] + 1:  # window reset; old burn rate no longer applies
                history.clear()
            history.append((now, s["h"]))
            del history[:-(RUNWAY_LOOKBACK // (REFRESH_SECONDS // 2))]

    @staticmethod
    def tray_image(has_pick):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=ACCENT if has_pick else DIM)
        return img

    def run(self):
        self.root.after(REFRESH_SECONDS * 1000, self.auto_refresh)
        self.root.mainloop()


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


if __name__ == "__main__":
    if "--check" in sys.argv:
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
    elif not already_running():
        if not STATE_FILE.exists():  # first run: start with Windows by default, untick in the tray menu
            set_start_with_windows(True)
        Widget().run()
