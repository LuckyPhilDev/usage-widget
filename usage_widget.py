"""Desktop widget suggesting which AI subscription to use right now (Claude, Codex, Antigravity)."""
import base64
import ctypes
import json
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

HOME = Path.home()
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "widget.json"
REFRESH_SECONDS = 300
REDRAW_MS = 30_000
PEEK_POLL_MS = 150
PEEK_RADIUS = 24  # px the cursor can drift from the tray icon before the peek ends
SESSION_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86400

# Favourite first: the suggestion goes to the first usable sub in this order.
ORDER = ["Claude", "Codex", "AG Claude/GPT", "AG Gemini"]
MIN_SESSION_LEFT = 20      # % of 5h window; below this a sub is not worth suggesting
MAX_BEHIND_PACE = -10      # weekly pace delta; further behind than this and the sub is skipped
EXPIRY_OVERRIDE_LEFT = 50  # % of 5h left that jumps the queue when ...
EXPIRY_OVERRIDE_SECS = 3600  # ... it resets within this long

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
BAR_W, BAR_H, TIME_H = 150, 14, 3

logging.basicConfig(filename=HERE / "usage_widget.log", level=logging.WARNING,
                    format="%(asctime)s %(message)s")


# ---------- fetching ----------
# Each fetcher returns subs: {"name", "h", "h_reset", "w", "w_reset"} where h/w are % left
# of the 5h and weekly windows and resets are epoch seconds (None = window not started).

def http_json(url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "usage-widget/1.0", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def write_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def jwt_exp(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]


def iso_to_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sub(name, h, h_reset, w, w_reset):
    return {"name": name, "h": h, "h_reset": h_reset, "w": w, "w_reset": w_reset}


def fetch_claude():
    creds = json.loads(CLAUDE_CREDS.read_text())
    oauth = creds["claudeAiOauth"]
    if oauth["expiresAt"] / 1000 < time.time() + 60:
        new = http_json(CLAUDE_TOKEN_URL, body={
            "grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
            "client_id": CLAUDE_CLIENT_ID, "scope": " ".join(oauth.get("scopes", []))})
        oauth["accessToken"] = new["access_token"]
        oauth["refreshToken"] = new.get("refresh_token", oauth["refreshToken"])
        oauth["expiresAt"] = int((time.time() + new["expires_in"]) * 1000)
        write_json(CLAUDE_CREDS, creds)
    data = http_json(CLAUDE_USAGE_URL, {
        "Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"})

    def window(w):
        return 100 - w["utilization"], iso_to_epoch(w["resets_at"]) if w.get("resets_at") else None

    # Opus/Sonnet weekly caps only matter when they are tighter than the overall weekly one.
    weeklies = [window(data[k]) for k in ("seven_day", "seven_day_opus", "seven_day_sonnet") if data.get(k)]
    weekly = min(weeklies, key=lambda pct_reset: pct_reset[0])
    return [sub("Claude", *window(data["five_hour"]), *weekly)]


def fetch_codex():
    auth = json.loads(CODEX_AUTH.read_text())
    tokens = auth["tokens"]
    if jwt_exp(tokens["access_token"]) < time.time() + 60:
        new = http_json(CODEX_TOKEN_URL, body={
            "client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "scope": "openid profile email"})
        for key in ("access_token", "id_token", "refresh_token"):
            if new.get(key):
                tokens[key] = new[key]
        auth["last_refresh"] = datetime.utcnow().isoformat() + "Z"
        write_json(CODEX_AUTH, auth)
    data = http_json(CODEX_USAGE_URL, {
        "Authorization": f"Bearer {tokens['access_token']}", "ChatGPT-Account-Id": tokens["account_id"]})
    limits = data["rate_limit"]
    session, week = limits["primary_window"], limits["secondary_window"]
    return [sub("Codex", 100 - session["used_percent"], session["reset_at"],
                100 - week["used_percent"], week["reset_at"])]


def fetch_antigravity():
    # Argument list, no shell: a shell (Git Bash) can mangle "/usage" into a path, which agy sends as a paid prompt.
    out = subprocess.run([shutil.which("agy") or "agy", "-p", "/usage", "--output-format", "json"],
                         capture_output=True, text=True, timeout=120,
                         creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout
    names = {"Gemini Models": "AG Gemini", "Claude and GPT models": "AG Claude/GPT", "Gemini 4 Argon": "Argon"}
    subs = []
    for group in json.loads(out)["command"]["data"]["groups"]:
        windows = {"5h": (None, None), "weekly": (None, None)}
        for b in group["buckets"]:
            pct = b["remaining_fraction"] * 100
            # At 100% the API reports reset as now+window, which is meaningless.
            windows[b["window"]] = (pct, None if pct >= 100 else iso_to_epoch(b["reset_time"]))
        subs.append(sub(names.get(group["name"], group["name"]), *windows["5h"], *windows["weekly"]))
    return subs


PROVIDERS = {"Claude": fetch_claude, "Codex": fetch_codex, "Antigravity": fetch_antigravity}


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


def suggest(subs, now):
    usable = [s for s in subs if s["h"] is not None and s["h"] >= MIN_SESSION_LEFT
              and pace_delta(s, now) > MAX_BEHIND_PACE]
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
    at = datetime.fromtimestamp(reset)
    clock = at.strftime("%H:%M") if secs < 86400 else at.strftime("%a %H:%M")
    return f"{left} ({clock})"


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
        self.body = tk.Frame(self.root, bg=BG, padx=12, pady=10)
        self.body.pack()
        self.tooltip = Tooltip(self.root)

        # provider -> {"subs": [...], "ok_at": ts, "error": str|None}
        self.data = {name: {"subs": [], "ok_at": None, "error": None} for name in PROVIDERS}
        self.events = queue.Queue()
        self.popped = False
        self.peek_anchor = None  # cursor position while hover-peeking from the tray
        self.drag_from = None

        self.root.bind_all("<ButtonPress-1>", self.drag_start)
        self.root.bind_all("<B1-Motion>", self.drag_move)
        self.root.bind_all("<ButtonRelease-1>", self.drag_end)
        self.root.bind("<FocusOut>", lambda e: self.pop_down())

        self.tray = TrayIcon("usage_widget", self.tray_image(False), "AI usage", menu=pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("popup",)), default=True, visible=False),
            pystray.MenuItem("Refresh", lambda: self.events.put(("refresh",))),
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

    def save_position(self):
        write_json(STATE_FILE, {"x": self.root.winfo_x(), "y": self.root.winfo_y()})

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
        self.drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        dx, dy = self.drag_from
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def drag_end(self, e):
        self.save_position()
        if not self.popped:
            self.to_bottom()

    # --- refresh ---
    def refresh(self):
        for name, fetch in PROVIDERS.items():
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
        tk.Frame(self.body, bg=ACCENT if picked else BG, width=3, height=26).grid(row=r, column=0, padx=(0, 8), pady=3)
        name = tk.Label(self.body, text=s["name"], font=FONT_BOLD, width=13, anchor="w", bg=BG,
                        fg=DIM if stale else ACCENT if picked else FG)
        name.grid(row=r, column=1, sticky="w")

        bar = tk.Canvas(self.body, width=BAR_W, height=BAR_H + 3 + TIME_H, bg=BG, highlightthickness=0)
        bar.create_rectangle(0, 0, BAR_W, BAR_H, fill=TRACK, width=0)
        bar.create_rectangle(0, 0, BAR_W * s["h"] / 100, BAR_H, fill=DIM if stale else color_for(s["h"]), width=0)
        bar.create_text(6, BAR_H / 2, text=f"{s['h']:.0f}%", anchor="w", font=FONT_SMALL,
                        fill=INK if s["h"] >= 20 else FG)
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

        weekly = "weekly not reported" if s["w"] is None else \
            f"Weekly: {s['w']:.0f}% left, resets {countdown(s['w_reset'])}"
        tip = f"5 hour: {s['h']:.0f}% left, resets {countdown(s['h_reset'])}\n{weekly}\n" \
              f"Weekly pace: {delta:+d}% vs an even spend"
        for widget in (name, bar, pace):
            widget.bind("<Enter>", lambda e, t=tip: self.tooltip.show(t, e.x_root, e.y_root))
            widget.bind("<Leave>", lambda e: self.tooltip.hide())

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
    print("self-check ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        self_check()
        now, subs = time.time(), []
        for name, fetch in PROVIDERS.items():
            try:
                subs += fetch()
            except Exception as exc:
                print(f"{name:12} FAILED {type(exc).__name__}: {exc}")
        for s in sorted(subs, key=lambda s: rank(s["name"])):
            h = "    -" if s["h"] is None else f"{s['h']:5.1f}%"
            print(f"{s['name']:14} 5h {h} {countdown(s['h_reset']):>20}  pace {pace_delta(s, now):+d}%")
        print("suggest:", suggest([s for s in subs if s["h"] is not None], now))
    else:
        Widget().run()
