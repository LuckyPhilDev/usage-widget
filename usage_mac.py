"""macOS menu bar app suggesting which AI subscription to use right now (Claude, Codex, Antigravity)."""
import fcntl
import logging
import plistlib
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps
from AppKit import (NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSImage, NSMakeRect, NSMakeSize)
from Foundation import NSAttributedString

import usage_core as core
from usage_core import (APP_NAME, DATA_DIR, REFILLED_AT, REFRESH_SECONDS, SESSION_SECONDS, SETTINGS_FILE, STATE_FILE, ago, check,
                        clock, countdown, installed_providers, load_settings, load_state, pace_delta, rank,
                        record_sample, runs_out_at, save_setting, suggest, time_left, usable_again_at, write_json)

LABEL = "com.luckyphil.usagewidget"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
OPEN = "/usr/bin/open"
REDRAW_SECONDS = 30
# Same palette and bar geometry as the Windows card.
GREEN, AMBER, RED, ACCENT, TIME_BAR, INK = "#4caf50", "#f0a030", "#e05050", "#4a9eff", "#9aa3b2", "#101114"
NAME_W, BAR_W, BAR_H, TIME_H, PACE_W, ROW_H = 112, 150, 14, 3, 44, 26


# ---------- startup ----------

def program_arguments():
    if getattr(sys, "frozen", False):  # PyInstaller app
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def starts_at_login():
    return LAUNCH_AGENT.exists()


def set_start_at_login(on):
    if on:
        LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
        LAUNCH_AGENT.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": program_arguments(),
                                                 "RunAtLoad": True, "LimitLoadToSessionType": "Aqua"}))
    else:
        LAUNCH_AGENT.unlink(missing_ok=True)


def already_running():
    global lock_file
    lock_file = open(DATA_DIR / "lock", "w")  # deliberately kept open: the lock lasts until the process exits
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True


def notify(title, text):
    # rumps.notification needs a bundle id and is unreliable on recent macOS; osascript always works.
    # Text goes in as arguments so a sub name can't break out of the AppleScript.
    subprocess.Popen(["/usr/bin/osascript", "-e", "on run argv",
                      "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                      "-e", "end run", title, text])


def color(hex_rgb):
    r, g, b = (int(hex_rgb[k:k + 2], 16) / 255 for k in (1, 3, 5))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1)


def color_for(pct):
    return GREEN if pct > 50 else AMBER if pct > 20 else RED


def pace_color(delta):
    return GREEN if delta >= 5 else RED if delta <= -10 else AMBER if delta <= -3 else None


def fill(x, y, w, h, fill_color):
    fill_color.setFill()
    NSBezierPath.fillRect_(NSMakeRect(x, y, w, h))


def text(s, x, mid, size, text_color, bold=False, right=False):
    """Draw s vertically centred on mid, starting at x (or ending at x if right); returns its width."""
    font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    string = NSAttributedString.alloc().initWithString_attributes_(
        s, {NSFontAttributeName: font, NSForegroundColorAttributeName: text_color})
    w, h = string.size()
    string.drawAtPoint_((x - w if right else x, mid - h / 2))
    return w


def row_image(s, stale, picked, reminded, note, warn, delta, now):
    """One card row (name, 5h bar with time-left bar under it, pace) as an image for a menu item."""
    def draw(_):
        # Drawn on each display, so light/dark mode is resolved here, not when the image is made.
        dark = NSAppearance.currentDrawingAppearance().bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]) == NSAppearanceNameDarkAqua
        label, dim = NSColor.labelColor(), NSColor.secondaryLabelColor()
        track = color("#33363d" if dark else "#dcdde1")
        mid = ROW_H / 2
        if picked:
            fill(0, 4, 3, ROW_H - 8, color(ACCENT))
        w = text(s["name"], 11, mid, 13, dim if stale else color(ACCENT) if picked else label, bold=True)
        if reminded:
            text("\U0001F514", 11 + w + 4, mid, 10, label)
        x, top = 11 + NAME_W, (ROW_H - BAR_H - 3 - TIME_H) / 2
        fill(x, top, BAR_W, BAR_H, track)
        fill(x, top, BAR_W * s["h"] / 100, BAR_H, dim if stale else color(color_for(s["h"])))
        on_fill = track if stale else color(INK)
        text(f"{s['h']:.0f}%", x + 6, top + BAR_H / 2, 10, on_fill if s["h"] >= 20 else label)
        if note:
            # Backed by a track-coloured chip so it reads the same over the fill or the empty track.
            note_w = NSAttributedString.alloc().initWithString_attributes_(
                note, {NSFontAttributeName: NSFont.systemFontOfSize_(10)}).size()[0]
            fill(x + BAR_W - 9 - note_w, top + 1, note_w + 8, BAR_H - 2, track)
            text(note, x + BAR_W - 5, top + BAR_H / 2, 10, color(AMBER) if warn else dim, right=True)
        time_y = top + BAR_H + 3
        fill(x, time_y, BAR_W, TIME_H, track)
        if s["h_reset"] is not None:
            fill(x, time_y, BAR_W * time_left(s["h_reset"], SESSION_SECONDS, now) / 100, TIME_H, color(TIME_BAR))
        pace = pace_color(delta)
        text(f"{delta:+d}%", x + BAR_W + 8 + PACE_W, mid, 13, dim if stale else color(pace) if pace else label,
             right=True)
        return True
    return NSImage.imageWithSize_flipped_drawingHandler_(
        NSMakeSize(11 + NAME_W + BAR_W + 8 + PACE_W, ROW_H), True, draw)


# ---------- app ----------

class App(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="AI usage", quit_button=None)
        load_settings()
        self.reminders = set(load_state().get("reminders", []))  # sub names to notify about when they refill
        self.save_state()
        self.providers = installed_providers()
        # provider -> {"subs": [...], "ok_at": ts, "error": str|None}
        self.data = {name: {"subs": [], "ok_at": None, "error": None} for name in self.providers}
        self.events = queue.Queue()
        self.samples = {}  # sub name -> [(time, 5h % left)] for burn rate
        # rumps timers fire once on start, so this also does the first refresh and render.
        self.timers = [rumps.Timer(self.pump, 0.5), rumps.Timer(lambda _: self.render(), REDRAW_SECONDS),
                       rumps.Timer(lambda _: self.refresh(), REFRESH_SECONDS)]
        for timer in self.timers:
            timer.start()

    def save_state(self):
        write_json(STATE_FILE, {"reminders": sorted(self.reminders)})

    # --- refresh ---
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

    def pump(self, _):
        """Main-thread side of the fetch threads and reminder timers."""
        while not self.events.empty():
            kind, *args = self.events.get()
            if kind == "data":
                name, subs, error = args
                entry = self.data[name]
                entry["error"] = error
                if subs is not None:
                    entry["subs"], entry["ok_at"] = subs, time.time()
                    for s in subs:
                        record_sample(self.samples, s, time.time())
                    self.check_reminders(subs)
                self.render()
            elif kind == "refresh":
                self.refresh()

    # --- drawing ---
    def render(self):
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

        menu = [self.row(s, stale, s["name"] == pick, now) for s, stale in rows]
        if footnotes:
            menu.append(None)
        for note, stale in footnotes:
            menu.append(rumps.MenuItem(note if isinstance(note, str) else f"{note['name']} weekly {note['w']:.0f}%"))
        menu += [None, rumps.MenuItem("Refresh", callback=lambda _: self.refresh()),
                 self.toggle("Show refill times", "show_back_times", core.SHOW_BACK_TIMES),
                 self.toggle("Show run-out warnings", "show_runway", core.SHOW_RUNWAY),
                 self.toggle("Start at Login", None, starts_at_login()),
                 rumps.MenuItem("Edit settings", callback=lambda _: subprocess.Popen([OPEN, "-t", SETTINGS_FILE])),
                 rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application())]
        self.menu.clear()
        self.menu.update(menu)
        for menu_item in menu[:len(rows)]:
            menu_item._menuitem.setTitle_("")  # the image is the whole row; the title was only rumps' key
        picked = next((s for s, stale in rows if s["name"] == pick), None)
        self.title = f"{pick} {picked['h']:.0f}%" if picked else "AI usage"

    def toggle(self, title, key, on):
        def flip(_):
            if key:
                save_setting(key, not on)
                load_settings()
            else:
                set_start_at_login(not on)
            self.render()
        menu_item = rumps.MenuItem(title, callback=flip)
        menu_item.state = int(on)
        return menu_item

    def row(self, s, stale, picked, now):
        back_at = usable_again_at(s, now)
        out_at = runs_out_at(self.samples.get(s["name"], []))
        running_out = back_at is None and out_at is not None and s["h_reset"] and out_at < s["h_reset"]
        note = (f"back {clock(back_at)}" if back_at and core.SHOW_BACK_TIMES
                else f"out ~{clock(out_at)}" if running_out and core.SHOW_RUNWAY else "")
        reminded = s["name"] in self.reminders
        delta = pace_delta(s, now)
        details = [f"5 hour: {s['h']:.0f}% left, resets {countdown(s['h_reset'])}",
                   "Weekly not reported" if s["w"] is None else
                   f"Weekly: {s['w']:.0f}% left, resets {countdown(s['w_reset'])}",
                   f"Weekly pace: {delta:+d}% vs an even spend"]
        if back_at:
            details.append(f"Suggestible again at {clock(back_at)} if left idle")
        if out_at:
            details.append(f"At your recent pace, 5 hour runs out ~{clock(out_at)}")
        row = rumps.MenuItem(s["name"])
        image = row_image(s, stale, picked, reminded, note, running_out, delta, now)
        image.setAccessibilityDescription_(f"{s['name']}: {s['h']:.0f}% of 5 hour left, pace {delta:+d}%")
        row._menuitem.setImage_(image)
        row._menuitem.setToolTip_("\n".join(details))
        for line in details:
            row.add(rumps.MenuItem(line))
        row.add(rumps.separator)
        if s["name"] in core.LAUNCH:
            row.add(rumps.MenuItem("Open", callback=lambda _, n=s["name"]: subprocess.Popen(
                [OPEN, "-a", core.LAUNCH[n]])))
        if reminded:
            row.add(rumps.MenuItem("Cancel refill reminder", callback=lambda _: self.set_reminder(s, False)))
        elif s["h"] < REFILLED_AT:
            row.add(rumps.MenuItem(f"Remind me when it refills ({clock(s['h_reset'])})" if s["h_reset"]
                                   else "Remind me when it refills", callback=lambda _: self.set_reminder(s, True)))
        return row

    # --- reminders ---
    def set_reminder(self, s, on):
        (self.reminders.add if on else self.reminders.discard)(s["name"])
        if on and s["h_reset"]:
            # Fetch right after the reset rather than waiting for the next 5 minute refresh.
            timer = threading.Timer(max(0, s["h_reset"] - time.time() + 30), self.events.put, [("refresh",)])
            timer.daemon = True
            timer.start()
        self.save_state()
        self.render()

    def check_reminders(self, subs):
        refilled = [s for s in subs if s["name"] in self.reminders and s["h"] is not None and s["h"] >= REFILLED_AT]
        for s in refilled:
            self.reminders.discard(s["name"])
            notify(f"{s['name']} refilled", f"{s['name']} is back to {s['h']:.0f}% of its 5 hour limit.")
        if refilled:
            self.save_state()


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif not already_running():
        # First run: start at login by default, untick in the menu. Rewriting it on later runs
        # keeps it pointing at this copy if the app was moved.
        if not STATE_FILE.exists() or starts_at_login():
            set_start_at_login(True)
        App().run()
