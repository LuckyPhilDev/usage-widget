"""Desktop widget suggesting which AI subscription to use right now (Claude, Codex, Antigravity)."""
import ctypes
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import usage_core as core
from usage_core import (APP_NAME, REFILLED_AT, REFRESH_SECONDS, SESSION_SECONDS, SETTINGS_FILE, STATE_FILE,
                        ago, check, clock, countdown, installed_providers, load_settings, load_state, pace_delta,
                        rank, record_sample, runs_out_at, save_setting, suggest, time_left, usable_again_at,
                        write_json)

NOTEPAD = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REDRAW_MS = 30_000
PEEK_POLL_MS = 150
PEEK_RADIUS = 24  # px the cursor can drift from the tray icon before the peek ends
CLICK_SLOP = 4    # px of movement before a press is a drag, not a click

BG, FG, DIM, TRACK, INK = "#1b1d22", "#e6e6e6", "#6b6f78", "#33363d", "#101114"
GREEN, AMBER, RED, ACCENT, TIME_BAR, NEUTRAL = "#4caf50", "#f0a030", "#e05050", "#4a9eff", "#9aa3b2", "#c8ccd4"
FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_ICON = ("Segoe MDL2 Assets", 8)
BELL = ""  # Segoe MDL2 "Ringer"
BAR_W, BAR_H, TIME_H = 150, 14, 3

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


def color_for(pct):
    return GREEN if pct > 50 else AMBER if pct > 20 else RED


def pace_color(delta):
    return GREEN if delta >= 5 else RED if delta <= -10 else AMBER if delta <= -3 else NEUTRAL


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
        state = load_state()
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
            pystray.MenuItem("Show refill times", self.toggle_setting("show_back_times"),
                             checked=lambda item: core.SHOW_BACK_TIMES),
            pystray.MenuItem("Show run-out warnings", self.toggle_setting("show_runway"),
                             checked=lambda item: core.SHOW_RUNWAY),
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

    def toggle_setting(self, key):
        def toggle(icon, item):
            save_setting(key, not item.checked)
            load_settings()
            icon.update_menu()
            self.events.put(("redraw",))
        return toggle

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
            elif kind == "redraw":
                self.render()
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
        plans = {s["name"]: s["plan"] for s, _ in rows if s.get("plan")}
        pick_label = f"{pick} {plans[pick]}" if pick in plans else pick
        self.tray.title = f"Use {pick_label}" if pick else "AI usage: nothing suggested"

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
        note = (f"back {clock(back_at)}" if back_at and core.SHOW_BACK_TIMES
                else f"out ~{clock(out_at)}" if running_out and core.SHOW_RUNWAY else "")

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
        if name in core.LAUNCH:
            os.startfile(f"shell:AppsFolder\\{core.LAUNCH[name]}")

    def row_menu(self, e, s):
        self.tooltip.hide()
        menu = tk.Menu(self.root, tearoff=False)
        if s["name"] in self.reminders:
            menu.add_command(label="Cancel refill reminder", command=lambda: self.set_reminder(s, False))
        elif s["h"] < REFILLED_AT:
            menu.add_command(label=f"Remind me when it refills ({clock(s['h_reset'])})" if s["h_reset"]
                             else "Remind me when it refills", command=lambda: self.set_reminder(s, True))
        if s["name"] in core.LAUNCH:
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
        for s in subs:
            record_sample(self.samples, s, time.time())

    @staticmethod
    def tray_image(has_pick):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=ACCENT if has_pick else DIM)
        return img

    def run(self):
        self.root.after(REFRESH_SECONDS * 1000, self.auto_refresh)
        self.root.mainloop()


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif not already_running():
        if not STATE_FILE.exists():  # first run: start with Windows by default, untick in the tray menu
            set_start_with_windows(True)
        Widget().run()
