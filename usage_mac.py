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
from AppKit import NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName
from Foundation import NSAttributedString

import usage_core as core
from usage_core import (APP_NAME, DATA_DIR, REFILLED_AT, REFRESH_SECONDS, SETTINGS_FILE, STATE_FILE, ago, check,
                        clock, countdown, duration, installed_providers, load_settings, load_state, pace_delta, rank,
                        record_sample, runs_out_at, save_setting, suggest, usable_again_at, write_json)

LABEL = "com.luckyphil.usagewidget"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
OPEN = "/usr/bin/open"
REDRAW_SECONDS = 30
BAR_CELLS = 10
MONO = NSFont.monospacedSystemFontOfSize_weight_(13, 0)


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


def item(title, callback=None, stale=False):
    """Menu item in a monospaced font so the bars and numbers line up."""
    menu_item = rumps.MenuItem(title, callback=callback)
    attrs = {NSFontAttributeName: MONO}
    if stale:
        attrs[NSForegroundColorAttributeName] = NSColor.secondaryLabelColor()
    menu_item._menuitem.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
    return menu_item


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
            menu.append(item(note if isinstance(note, str) else f"{note['name']} weekly {note['w']:.0f}%"))
        menu += [None, rumps.MenuItem("Refresh", callback=lambda _: self.refresh()),
                 self.toggle("Show refill times", "show_back_times", core.SHOW_BACK_TIMES),
                 self.toggle("Show run-out warnings", "show_runway", core.SHOW_RUNWAY),
                 self.toggle("Start at Login", None, starts_at_login()),
                 rumps.MenuItem("Edit settings", callback=lambda _: subprocess.Popen([OPEN, "-t", SETTINGS_FILE])),
                 rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application())]
        self.menu.clear()
        self.menu.update(menu)
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
        filled = round(s["h"] / 100 * BAR_CELLS)
        bar = "█" * filled + "░" * (BAR_CELLS - filled)
        left = duration(s["h_reset"] - now) if s["h_reset"] else "-"
        delta = pace_delta(s, now)
        title = f"{s['name']:<14}{bar} {s['h']:3.0f}%  {left:>7}  {delta:+4d}%  {note}{' 🔔' if reminded else ''}"
        row = item(title.rstrip(), stale=stale)
        row.state = int(picked)

        details = [f"5 hour: {s['h']:.0f}% left, resets {countdown(s['h_reset'])}",
                   "Weekly not reported" if s["w"] is None else
                   f"Weekly: {s['w']:.0f}% left, resets {countdown(s['w_reset'])}",
                   f"Weekly pace: {delta:+d}% vs an even spend"]
        if back_at:
            details.append(f"Suggestible again at {clock(back_at)} if left idle")
        if out_at:
            details.append(f"At your recent pace, 5 hour runs out ~{clock(out_at)}")
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
