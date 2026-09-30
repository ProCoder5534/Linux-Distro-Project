#!/usr/bin/env python3
"""
PyDock - a magnifying dock for X11, built with GTK 3 and libwnck 3.

* Single translucent ARGB window, composited by picom (no blur needed)
* Smooth cosine-curve magnification, launch bounce, urgency bounce
* Pinned apps + running apps, grouped by desktop entry, with running dots
* Drag to reorder, drag off the dock to unpin, right-click menus
* Optional auto-hide / intelligent hide, hides itself for fullscreen windows
* Reserves screen space via _NET_WM_STRUT_PARTIAL (when auto-hide is off)

Config: ~/.config/pydock/config.json  (created on first run)
"""

import json
import math
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("GDK_BACKEND", "x11")  # this dock is X11-only

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Wnck", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import (  # noqa: E402
    Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango, PangoCairo, Wnck,
)
import cairo  # noqa: E402

APP_ID = "org.pydock.Dock"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pydock"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "pinned": [],                 # desktop-file ids, e.g. "firefox.desktop"
    "icon_size": 48,              # resting icon size in px
    "zoom": 1.7,                  # max magnification (1.0 = off)
    "zoom_radius": 150,           # px over which magnification falls off
    "spacing": 6,                 # gap between icons
    "padding": 10,                # panel inner padding
    "bottom_margin": 12,          # gap between panel and screen edge
    "corner_radius": 22,
    "autohide": "none",           # "none" | "intellihide" | "always"
    "monitor": -1,                # -1 = primary
    "show_labels": True,
    "launcher_command": "auto",   # "auto", "off", or a command such as "rofi -show drun"
    "panel_color": [0.09, 0.10, 0.13, 0.60],   # r, g, b, a
    "accent_color": [0.50, 0.72, 1.00],
}

# First run: pin the first installed app from each group.
FIRST_RUN_GROUPS = [
    ["org.gnome.Nautilus", "thunar", "pcmanfm", "org.kde.dolphin", "nemo", "caja"],
    ["firefox", "org.mozilla.firefox", "chromium", "google-chrome", "brave-browser",
     "org.gnome.Epiphany"],
    ["org.gnome.Terminal", "xfce4-terminal", "kitty", "Alacritty", "org.wezfurlong.wezterm",
     "org.gnome.Console", "xterm"],
    ["code", "org.gnome.TextEditor", "mousepad", "org.gnome.gedit"],
]
LAUNCHER_CANDIDATES = [["rofi", "-show", "drun"], ["xfce4-appfinder"], ["ulauncher-toggle"]]


# --------------------------------------------------------------------------- helpers

def load_config():
    cfg = dict(DEFAULTS)

    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (OSError, ValueError) as err:
            print(f"pydock: ignoring unreadable config ({err})", file=sys.stderr)

    else:
        for group in FIRST_RUN_GROUPS:
            for name in group:
                try:
                    info = Gio.DesktopAppInfo.new(name + ".desktop")
                except (TypeError, GLib.Error):
                    info = None

                if info is not None:
                    cfg["pinned"].append(info.get_id())
                    break

        save_config(cfg)

    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError as err:
        print(f"pydock: could not save config ({err})", file=sys.stderr)


def spawn(argv):
    try:
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
    except GLib.Error as err:
        print(f"pydock: cannot run {argv[0]}: {err.message}", file=sys.stderr)


def rrect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def ease(k, rate, dt):
    return 1.0 - math.exp(-rate * dt) if k is None else k + 0.0


def approach(cur, target, rate, dt):
    return cur + (target - cur) * (1.0 - math.exp(-rate * dt))


# --------------------------------------------------------------------------- app lookup

class AppIndex:
    """Maps window class names / executables to .desktop entries."""

    def __init__(self):
        self.strong, self.weak = {}, {}
        self.refresh()
        Gio.AppInfoMonitor.get().connect("changed", lambda *_: self.refresh())

    def refresh(self):
        strong, weak = {}, {}
        for info in Gio.AppInfo.get_all():
            if not isinstance(info, Gio.DesktopAppInfo) or not info.get_id():
                continue
            stem = info.get_id()[:-8].lower()
            for k in {stem, stem.split(".")[-1]}:
                strong.setdefault(k, info)
            wm = info.get_startup_wm_class()
            if wm:
                strong.setdefault(wm.lower(), info)
            exe = info.get_executable()
            if exe:
                weak.setdefault(os.path.basename(exe).lower(), info)
            weak.setdefault(info.get_name().lower(), info)
        self.strong, self.weak = strong, weak

    def for_window(self, win):
        keys = [win.get_class_group_name(), win.get_class_instance_name()]
        try:
            keys.append(os.path.basename(os.readlink(f"/proc/{win.get_pid()}/exe")))
        except (OSError, TypeError):
            pass
        keys = [k.lower() for k in keys if k]
        keys += [k.replace(" ", "-") for k in keys if " " in k]
        for table in (self.strong, self.weak):
            for k in keys:
                if k in table:
                    return table[k]
        return None


# --------------------------------------------------------------------------- slot model

class Slot:
    """One thing in the dock: an app, the separator, or the launcher button."""

    def __init__(self, kind, key=None, app=None, pinned=False):
        self.kind, self.key, self.app, self.pinned = kind, key or kind, app, pinned
        self.name, self.icon, self.windows = "", None, []
        self.scale = self.target_scale = 1.0
        self.presence = 0.0
        self.removing = False
        self.active_a = 0.0
        self.x = self.w = self.size = 0.0
        self.launch_until = self.anim_t0 = 0.0


# --------------------------------------------------------------------------- the dock

class Dock(Gtk.Window):
    def __init__(self, app, cfg):
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title="PyDock")
        app.add_window(self)
        self.app, self.cfg = app, cfg

        # geometry
        self.icon_sz = int(cfg["icon_size"])
        self.zoom = max(1.0, float(cfg["zoom"]))
        self.zoom_radius = float(cfg["zoom_radius"])
        self.spacing = int(cfg["spacing"])
        self.pad = int(cfg["padding"])
        self.margin = int(cfg["bottom_margin"])
        self.radius = float(cfg["corner_radius"])
        self.autohide = cfg["autohide"]
        self.panel_h = self.icon_sz + 2 * self.pad
        self.label_h = 36 if cfg["show_labels"] else 0
        headroom = max(self.icon_sz * (self.zoom - 1) + self.label_h, self.icon_sz * 0.5) + 8
        self.win_h = int(math.ceil(self.margin + self.panel_h + headroom))
        self.win_w = 800
        self.panel_l, self.panel_w = 0.0, 0.0
        self.mon_geo = None
        self.accent = tuple(cfg["accent_color"][:3])
        self.panel_rgba = tuple(cfg["panel_color"])

        # state
        self.mouse_x = self.mouse_y = None
        self.hover_slot = self.label_slot = None
        self.hover_active = False
        self.poll_id = self.tick_id = 0
        self.last_t = None
        self.label_a = 0.0
        self.hide_t, self.hide_target, self.hide_count = 0.0, 0.0, 0
        self.fs_hidden = False
        self.menu_open = False
        self.press_slot = self.drag_slot = None
        self.press_xy = (0, 0)
        self.dragging = False
        self.drag_x = self.drag_y = 0.0
        self.active_xid = 0
        self._rgn_key = None
        self.missing_pinned = []

        self.apps = AppIndex()
        self.theme = Gtk.IconTheme.get_default()
        self.theme.connect("changed", lambda *_: self.reload_icons())

        self.pinned, self.running = [], []
        self.sep = Slot("sep")
        cmd = self.find_launcher_command()
        self.launcher_cmd = cmd
        self.launcher = Slot("launcher", pinned=False) if cmd else None
        if self.launcher:
            self.launcher.name = "Applications"

        self.setup_window()
        self.place()
        self.load_pinned()
        self.setup_wnck()

        GLib.timeout_add(200, self.policy_tick)
        self.show_all()

    # ------------------------------------------------------------------ window setup

    def setup_window(self):
        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            print("pydock: no compositor detected - start picom first for transparency.",
                  file=sys.stderr)
        if visual:
            self.set_visual(visual)
        self.set_name("pydock")
        css = Gtk.CssProvider()
        css.load_from_data(b"#pydock, #pydock-area { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(
            screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.stick()
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        try:
            self.set_wmclass("pydock", "PyDock")  # lets picom match class_g = 'PyDock'
        except Exception:
            pass

        self.area = Gtk.DrawingArea()
        self.area.set_name("pydock-area")
        self.area.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("enter-notify-event", self.on_enter)
        self.area.connect("leave-notify-event", self.on_leave)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.add(self.area)

        self.connect("map-event", lambda *_: self.apply_struts())
        self.connect("realize", lambda *_: self.update_input_region())
        screen.connect("monitors-changed", lambda *_: self.place())
        screen.connect("size-changed", lambda *_: self.place())

    def pick_monitor(self):
        display = Gdk.Display.get_default()
        idx = int(self.cfg["monitor"])
        if 0 <= idx < display.get_n_monitors():
            return display.get_monitor(idx)
        return display.get_primary_monitor() or display.get_monitor(0)

    def place(self):
        mon = self.pick_monitor()
        self.mon_geo = g = mon.get_geometry()
        self.win_w = g.width
        self.icon_sf = max(1, mon.get_scale_factor())
        self.set_size_request(g.width, self.win_h)
        self.resize(g.width, self.win_h)
        self.move(g.x, g.y + g.height - self.win_h)
        if self.get_realized():
            self.apply_struts()
            self.reload_icons()
        self.kick()

    def set_cardinals(self, name, values):
        gwin = self.get_window()
        if gwin is None:
            return
        try:
            Gdk.property_change(
                gwin, Gdk.Atom.intern(name, False), Gdk.Atom.intern("CARDINAL", False),
                32, Gdk.PropMode.REPLACE, values, len(values))
        except Exception:
            subprocess.run(["xprop", "-id", str(gwin.get_xid()), "-f", name, "32c",
                            "-set", name, ",".join(str(v) for v in values)],
                           check=False, stderr=subprocess.DEVNULL)

    def apply_struts(self):
        """Popup dock: never reserve screen space."""
        self.set_cardinals("_NET_WM_STRUT_PARTIAL", [0] * 12)
        self.set_cardinals("_NET_WM_STRUT", [0, 0, 0, 0])
    # ------------------------------------------------------------------ input region
    # The window spans the monitor width so magnification never has to resize it.
    # Its transparent parts must not swallow clicks, so the input shape follows the
    # panel: tight at rest, taller while hovering, a thin strip when hidden.

    def update_input_region(self):
        if not self.get_realized():
            return
        H = self.win_h
        pl, pw = int(self.panel_l), int(self.panel_w)
        if self.fs_hidden:
            rect = None
        elif self.hide_t > 0.5 and not self.hover_active:
            rect = (pl, H - 10, pw, 10)
        elif self.hover_active or self.dragging or self.menu_open:
            rect = (max(0, pl - 24), 0, pw + 48, H)
        else:
            rect = (pl, H - self.margin - self.panel_h, pw, self.margin + self.panel_h)
        if rect == self._rgn_key:
            return
        self._rgn_key = rect
        if rect is None or rect[2] <= 0:
            region = cairo.Region()
        else:
            region = cairo.Region(cairo.RectangleInt(*rect))
        self.input_shape_combine_region(region)

    # ------------------------------------------------------------------ icons

    def find_launcher_command(self):
        cmd = str(self.cfg["launcher_command"]).strip()
        if cmd == "off":
            return None
        if cmd and cmd != "auto":
            return shlex.split(cmd)
        for cand in LAUNCHER_CANDIDATES:
            if GLib.find_program_in_path(cand[0]):
                return cand
        return None

    def icon_px(self):
        return int(math.ceil(self.icon_sz * self.zoom)) * getattr(self, "icon_sf", 1)

    def load_gicon(self, gicon):
        px = self.icon_px()
        sf = getattr(self, "icon_sf", 1)
        flags = Gtk.IconLookupFlags.FORCE_SIZE
        if gicon is not None:
            try:
                info = self.theme.lookup_by_gicon_for_scale(gicon, px // sf, sf, flags)
                if info:
                    return info.load_icon()
            except GLib.Error:
                pass
        for name in ("application-x-executable", "application-default-icon"):
            try:
                return self.theme.load_icon_for_scale(name, px // sf, sf, flags)
            except GLib.Error:
                continue
        return GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, px, px)

    def window_icon(self, win):
        pb = win.get_icon()
        if pb is None:
            return self.load_gicon(None)
        px = self.icon_px()
        return pb.scale_simple(px, px, GdkPixbuf.InterpType.HYPER)

    def fill_slot(self, s):
        if s.kind != "app":
            return
        if s.app:
            s.name = s.app.get_display_name()
            s.icon = self.load_gicon(s.app.get_icon())
        elif s.windows:
            w = s.windows[0]
            s.name = w.get_class_group_name() or w.get_name()
            s.icon = self.window_icon(w)

    def reload_icons(self):
        for s in self.pinned + self.running:
            self.fill_slot(s)
        self.kick()

    # ------------------------------------------------------------------ pinned apps

    def load_pinned(self):
        for did in self.cfg["pinned"]:
            did = did if did.endswith(".desktop") else did + ".desktop"
            info = Gio.DesktopAppInfo.new(did)
            if info is None:
                self.missing_pinned.append(did)
                continue
            s = Slot("app", key=info.get_id(), app=info, pinned=True)
            self.fill_slot(s)
            s.presence = 1.0
            self.pinned.append(s)

    def save_pinned(self):
        self.cfg["pinned"] = [s.key for s in self.pinned] + self.missing_pinned
        save_config(self.cfg)

    def pin(self, s):
        if s.app is None:
            return
        if s in self.running:
            self.running.remove(s)
        s.pinned, s.removing = True, False
        self.pinned.append(s)
        self.save_pinned()
        self.kick()

    def unpin(self, s):
        if s in self.pinned:
            self.pinned.remove(s)
        s.pinned = False
        s.removing = not s.windows
        self.running.append(s)
        self.save_pinned()
        self.kick()

    # ------------------------------------------------------------------ wnck

    def setup_wnck(self):
        Wnck.set_client_type(Wnck.ClientType.PAGER)
        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()
        self.screen.connect("window-opened", self.on_window_opened)
        self.screen.connect("window-closed", lambda *_: self.queue_rebuild())
        self.screen.connect("active-window-changed", lambda *_: self.queue_rebuild())
        self.screen.connect("active-workspace-changed", lambda *_: self.queue_rebuild())
        for w in self.screen.get_windows():
            self.watch_window(w)
        self.rebuild()

    def watch_window(self, w):
        for sig in ("state-changed", "class-changed", "icon-changed", "workspace-changed"):
            try:
                w.connect(sig, lambda *_: self.queue_rebuild())
            except TypeError:
                pass

    def on_window_opened(self, _screen, w):
        self.watch_window(w)
        self.queue_rebuild()

    def queue_rebuild(self):
        if not getattr(self, "_rebuild_pending", False):
            self._rebuild_pending = True
            GLib.idle_add(self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_pending = False
        self.rebuild()
        return False

    @staticmethod
    def is_task(w):
        return (w.get_window_type() in (Wnck.WindowType.NORMAL, Wnck.WindowType.DIALOG)
                and not w.is_skip_tasklist())

    def rebuild(self):
        groups = {}
        for w in self.screen.get_windows():
            if not self.is_task(w):
                continue
            info = self.apps.for_window(w)
            key = info.get_id() if info else \
                "wm:" + (w.get_class_group_name() or w.get_name() or "?").lower()
            groups.setdefault(key, (info, []))[1].append(w)

        by_key = {s.key: s for s in self.pinned + self.running}
        for key, (info, wins) in groups.items():
            wins.sort(key=lambda w: w.get_xid())
            s = by_key.get(key)
            if s is None:
                s = Slot("app", key=key, app=info)
                self.running.append(s)
            s.windows, s.removing = wins, False
            if s.icon is None or s.app is None:
                self.fill_slot(s)
        for s in self.pinned + self.running:
            if s.key not in groups:
                s.windows = []
                if not s.pinned:
                    s.removing = True

        active = self.screen.get_active_window()
        self.active_xid = active.get_xid() if active else 0
        self.kick()

    def all_slots(self):
        out = ([self.launcher] if self.launcher else []) + self.pinned + [self.sep] + self.running
        return out

    def sequence(self):
        seq = []
        if self.launcher:
            seq.append(self.launcher)
        seq += self.pinned
        if self.sep.presence > 0.004:
            seq.append(self.sep)
        seq += [s for s in self.running if s.presence > 0.004 or not s.removing]
        return seq

    # ------------------------------------------------------------------ actions

    def launch_context(self):
        ctx = Gdk.Display.get_default().get_app_launch_context()
        ctx.set_timestamp(Gtk.get_current_event_time())
        return ctx

    def launch(self, s):
        if s.app is None:
            return
        try:
            s.app.launch([], self.launch_context())
            s.launch_until = GLib.get_monotonic_time() / 1e6 + 8
            s.anim_t0 = GLib.get_monotonic_time() / 1e6
            self.kick()
        except GLib.Error as err:
            print(f"pydock: launch failed: {err.message}", file=sys.stderr)

    def raise_window(self, w):
        ts = Gtk.get_current_event_time()
        if w.is_minimized():
            w.unminimize(ts)
        w.activate(ts)

    def click(self, s):
        if s.kind == "launcher":
            spawn(self.launcher_cmd)
            return
        if not s.windows:
            self.launch(s)
            return
        active = next((w for w in s.windows if w.get_xid() == self.active_xid), None)
        if active is not None:
            if len(s.windows) == 1:
                active.minimize()
            else:  # cycle through this app's windows
                i = s.windows.index(active)
                self.raise_window(s.windows[(i + 1) % len(s.windows)])
        else:
            stacked = {w.get_xid(): i for i, w in enumerate(self.screen.get_windows_stacked())}
            self.raise_window(max(s.windows, key=lambda w: stacked.get(w.get_xid(), -1)))

    # ------------------------------------------------------------------ menus

    def popup_menu(self, s, event):
        menu = Gtk.Menu()

        def add(label, cb, sensitive=True, bold=False):
            item = Gtk.MenuItem.new_with_label(label)
            if bold:
                item.get_child().set_markup("<b>%s</b>" % GLib.markup_escape_text(label))
            item.set_sensitive(sensitive)
            if cb:
                item.connect("activate", lambda *_: cb())
            menu.append(item)

        def sep():
            menu.append(Gtk.SeparatorMenuItem())

        if s is None or s.kind == "sep":
            add("Edit Settings", lambda: spawn(["xdg-open", str(CONFIG_FILE)]))
            add("Restart Dock", lambda: os.execv(sys.executable, [sys.executable] + sys.argv))
            sep()
            add("Quit Dock", self.app.quit)
        elif s.kind == "launcher":
            add("Show Applications", lambda: spawn(self.launcher_cmd))
        else:
            add(s.name, None, sensitive=False, bold=True)
            if s.windows:
                sep()
                for w in s.windows:
                    title = w.get_name()
                    title = title if len(title) <= 42 else title[:41] + "…"
                    add(title, lambda w=w: self.raise_window(w))
            sep()
            if s.app:
                add("New Window" if s.windows else "Open", lambda: self.launch(s))
                for action in s.app.list_actions():
                    add(s.app.get_action_name(action),
                        lambda a=action: s.app.launch_action(a, self.launch_context()))
                sep()
                add("Remove from Dock" if s.pinned else "Keep in Dock",
                    (lambda: self.unpin(s)) if s.pinned else (lambda: self.pin(s)))
            if s.windows:
                add("Close All Windows" if len(s.windows) > 1 else "Close",
                    lambda: [w.close(Gtk.get_current_event_time()) for w in s.windows])

        self.menu_open = True
        menu.connect("deactivate", lambda *_: setattr(self, "menu_open", False))
        menu.show_all()
        rect = Gdk.Rectangle()
        if s is not None and s.kind != "sep":
            rect.x, rect.y = int(s.x), int(self.baseline() - s.size - 6)
            rect.width, rect.height = max(1, int(s.w)), 1
        else:
            rect.x, rect.y, rect.width, rect.height = int(event.x), int(self.baseline()), 1, 1
        menu.popup_at_rect(self.get_window(), rect, Gdk.Gravity.NORTH, Gdk.Gravity.SOUTH, event)

    # ------------------------------------------------------------------ layout

    def baseline(self):
        return self.win_h - self.margin - self.pad

    def _layout(self, seq, magnified):
        gap = self.spacing
        xs, ws, total = [], [], 0.0
        for s in seq:
            if s.kind == "sep":
                w = 1.0 * s.presence
            else:
                w = self.icon_sz * (s.scale if magnified else 1.0) * s.presence
            ws.append(w)
            total += w + gap * s.presence
        if seq:
            total -= gap * seq[-1].presence
        panel_w = total + 2 * self.pad
        panel_l = (self.win_w - panel_w) / 2
        x = panel_l + self.pad
        for s, w in zip(seq, ws):
            xs.append(x)
            x += w + gap * s.presence
        return xs, ws, panel_l, panel_w

    def apply_layout(self):
        seq = self.sequence()
        xs, ws, self.panel_l, self.panel_w = self._layout(seq, True)
        for s, x, w in zip(seq, xs, ws):
            s.x, s.w = x, w
            s.size = self.icon_sz * s.scale * (0.55 + 0.45 * min(1.0, s.presence))
        return seq

    def slot_at(self, x, y):
        half = self.spacing / 2
        for s in self.sequence():
            if s.kind == "sep":
                continue
            if s.x - half <= x <= s.x + s.w + half and \
                    self.baseline() - s.size - 8 <= y <= self.win_h:
                return s
        return None

    # ------------------------------------------------------------------ animation

    def kick(self):
        if not self.tick_id and self.get_realized():
            self.tick_id = self.area.add_tick_callback(self.on_tick)

    def on_tick(self, _w, clock):
        now = clock.get_frame_time() / 1e6
        dt = min(0.05, now - self.last_t) if self.last_t else 1 / 60
        self.last_t = now
        busy = self.step(dt, now)
        self.area.queue_draw()
        if not busy:
            self.tick_id, self.last_t = 0, None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def update_targets(self, seq):
        magnify = (self.mouse_x is not None and not self.dragging and self.hide_t < 0.5
                   and self.zoom > 1.0)
        xs, ws, _, _ = self._layout(seq, False)
        for s, x, w in zip(seq, xs, ws):
            s.target_scale = 1.0
            if magnify and s.kind != "sep":
                d = abs(self.mouse_x - (x + w / 2))
                if d < self.zoom_radius:
                    s.target_scale = 1 + (self.zoom - 1) * 0.5 * (1 + math.cos(math.pi * d / self.zoom_radius))

    def step(self, dt, now):
        busy = False
        self.sep.removing = not (any(not p.removing for p in self.pinned)
                                 and any(not r.removing for r in self.running))
        self.update_targets(self.sequence())

        for s in self.all_slots():
            target_p = 0.0 if s.removing else 1.0
            s.scale = approach(s.scale, s.target_scale, 22, dt)
            s.presence = approach(s.presence, target_p, 9, dt)
            is_active = any(w.get_xid() == self.active_xid for w in s.windows)
            s.active_a = approach(s.active_a, 1.0 if is_active else 0.0, 12, dt)
            if (abs(s.scale - s.target_scale) > 0.002 or abs(s.presence - target_p) > 0.003
                    or abs(s.active_a - (1.0 if is_active else 0.0)) > 0.01
                    or self.bounce_offset(s, now) > 0):
                busy = True

        self.running = [r for r in self.running if not (r.removing and r.presence < 0.01)]

        self.hide_t = approach(self.hide_t, self.hide_target, 9, dt)
        if abs(self.hide_t - self.hide_target) > 0.004:
            busy = True
        want_label = self.hover_slot is not None or (self.dragging and self.drag_outside())
        if self.hover_slot is not None:
            self.label_slot = self.hover_slot
        self.label_a = approach(self.label_a, 1.0 if want_label else 0.0, 16, dt)
        if abs(self.label_a - (1.0 if want_label else 0.0)) > 0.01:
            busy = True

        self.apply_layout()
        self.update_input_region()
        return busy

    def bounce_offset(self, s, now):
        if s.kind != "app":
            return 0.0
        urgent = any(w.needs_attention() for w in s.windows)
        launching = s.launch_until > now and not s.windows
        if not (urgent or launching):
            return 0.0
        return abs(math.sin((now - s.anim_t0) * 5.5)) * self.icon_sz * 0.42

    # ------------------------------------------------------------------ hover & policy

    def activate_hover(self):
        if not self.hover_active:
            self.hover_active = True
            if self.hide_target == 1.0 and not self.fs_hidden:
                self.hide_target, self.hide_count = 0.0, 0
            if not self.poll_id:
                self.poll_id = GLib.timeout_add(100, self.pointer_check)
            self.update_input_region()
            self.kick()

    def pointer_check(self):
        """Hover ends when the pointer leaves the (tall) hover region."""
        if self.dragging:
            return True
        gwin = self.get_window()
        inside = False
        if gwin:
            ptr = Gdk.Display.get_default().get_default_seat().get_pointer()
            _, x, y, _ = gwin.get_device_position(ptr)
            inside = (self.panel_l - 24 <= x <= self.panel_l + self.panel_w + 24
                      and 0 <= y <= self.win_h)
        if inside:
            return True
        self.hover_active = False
        self.mouse_x = self.mouse_y = None
        self.hover_slot = None
        self.poll_id = 0
        self.update_input_region()
        self.kick()
        return False

    def overlaps_windows(self):
        g = self.mon_geo
        left, right = g.x + self.panel_l, g.x + self.panel_l + self.panel_w
        top = g.y + g.height - self.margin - self.panel_h - 4
        ws = self.screen.get_active_workspace()
        for w in self.screen.get_windows():
            if not self.is_task(w) or w.is_minimized():
                continue
            if ws is not None and not w.is_on_workspace(ws) and not w.is_pinned():
                continue
            x, y, ww, hh = w.get_geometry()
            if x < right and x + ww > left and y + hh > top and y < g.y + g.height:
                return True
        return False

    def policy_tick(self):
        active = self.screen.get_active_window()
        fs = bool(active and active.is_fullscreen())
        if fs != self.fs_hidden:
            self.fs_hidden = fs
            self.update_input_region()
        want = fs
        if not fs and self.autohide != "none" and not (
                self.hover_active or self.menu_open or self.dragging):
            want = True if self.autohide == "always" else self.overlaps_windows()
        self.hide_count = self.hide_count + 1 if want else 0
        target = 1.0 if (fs or (want and self.hide_count >= 2)) else 0.0
        if target != self.hide_target:
            self.hide_target = target
            self.kick()
        return True

    # ------------------------------------------------------------------ pointer events

    def on_enter(self, _w, ev):
        self.mouse_x, self.mouse_y = ev.x, ev.y
        self.activate_hover()

    def on_leave(self, _w, _ev):
        if not self.dragging and self.poll_id:
            GLib.timeout_add(30, lambda: (self.pointer_check(), False)[1])

    def on_motion(self, _w, ev):
        self.activate_hover()
        self.mouse_x, self.mouse_y = ev.x, ev.y
        ps = self.press_slot
        if (ps and not self.dragging and ps.pinned and ps.kind == "app"
                and (abs(ev.x - self.press_xy[0]) > 8 or abs(ev.y - self.press_xy[1]) > 8)):
            self.dragging, self.drag_slot = True, ps
            self.update_input_region()
        if self.dragging:
            self.drag_x, self.drag_y = ev.x, ev.y
            self.reorder_drag()
            self.hover_slot = None
        else:
            self.hover_slot = self.slot_at(ev.x, ev.y)
        self.kick()
        return True

    def on_press(self, _w, ev):
        s = self.slot_at(ev.x, ev.y)
        if ev.button == 1:
            self.press_slot, self.press_xy = s, (ev.x, ev.y)
        elif ev.button == 2 and s and s.kind == "app":
            self.launch(s)
        elif ev.button == 3:
            self.popup_menu(s, ev)
        return True

    def on_release(self, _w, ev):
        if ev.button != 1:
            return True
        if self.dragging:
            self.finish_drag()
        else:
            s = self.slot_at(ev.x, ev.y)
            if s is not None and s is self.press_slot:
                self.click(s)
        self.press_slot = None
        return True

    # ------------------------------------------------------------------ drag & drop

    def drag_outside(self):
        return self.drag_y < self.win_h - self.margin - self.panel_h - 60

    def reorder_drag(self):
        s = self.drag_slot
        others = [p for p in self.pinned if p is not s]
        idx = sum(1 for p in others if p.x + p.w / 2 < self.drag_x)
        new = others[:idx] + [s] + others[idx:]
        if new != self.pinned:
            self.pinned = new

    def finish_drag(self):
        s = self.drag_slot
        self.dragging, self.drag_slot = False, None
        if s is not None:
            if self.drag_outside():
                self.unpin(s)
            else:
                self.save_pinned()
        self.update_input_region()
        self.kick()

    # ------------------------------------------------------------------ drawing

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        now = GLib.get_monotonic_time() / 1e6
        ease_h = self.hide_t * self.hide_t * (3 - 2 * self.hide_t)
        cr.translate(0, ease_h * (self.panel_h + self.margin + 10))

        self.draw_panel(cr)
        base = self.baseline()
        for s in self.sequence():
            if s is not self.drag_slot or not self.dragging:
                self.draw_slot(cr, s, now, base)
        if self.dragging and self.drag_slot:
            outside = self.drag_outside()
            self.draw_icon(cr, self.drag_slot.icon, self.drag_x - self.icon_sz * 0.55,
                           self.drag_y - self.icon_sz * 0.55, self.icon_sz * 1.1,
                           0.45 if outside else 0.9)
            if outside:
                self.draw_label(cr, "Remove from Dock", self.drag_x,
                                self.drag_y - self.icon_sz * 0.55 - 8, 1.0)
        elif self.label_a > 0.01 and self.label_slot and self.cfg["show_labels"]:
            s = self.label_slot
            self.draw_label(cr, s.name, s.x + s.w / 2,
                            base - s.size - self.bounce_offset(s, now) - 10, self.label_a)
        return False

    def draw_panel(self, cr):
        x, w, h = self.panel_l, self.panel_w, self.panel_h
        y = self.win_h - self.margin - h
        r = min(self.radius, h / 2)

        # soft shadow, only outside the panel so the glass stays clean
        cr.save()
        cr.rectangle(-50, -50, self.win_w + 100, self.win_h + 100)
        rrect(cr, x, y, w, h, r)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()
        for i in range(1, 11):
            rrect(cr, x - i, y - i + 4, w + 2 * i, h + 2 * i, r + i)
            cr.set_source_rgba(0, 0, 0, 0.026)
            cr.fill()
        cr.restore()

        pr, pg, pb, pa = self.panel_rgba
        grad = cairo.LinearGradient(0, y, 0, y + h)
        grad.add_color_stop_rgba(0, min(1, pr + 0.07), min(1, pg + 0.07), min(1, pb + 0.07),
                                 min(1, pa + 0.05))
        grad.add_color_stop_rgba(1, pr, pg, pb, pa)
        rrect(cr, x, y, w, h, r)
        cr.set_source(grad)
        cr.fill()

        edge = cairo.LinearGradient(0, y, 0, y + h)
        edge.add_color_stop_rgba(0, 1, 1, 1, 0.30)
        edge.add_color_stop_rgba(1, 1, 1, 1, 0.07)
        rrect(cr, x + 0.5, y + 0.5, w - 1, h - 1, r - 0.5)
        cr.set_source(edge)
        cr.set_line_width(1)
        cr.stroke()

    def draw_icon(self, cr, pixbuf, x, y, size, alpha):
        if pixbuf is None or size <= 0.5:
            return
        cr.save()
        cr.translate(x, y)
        cr.scale(size / pixbuf.get_width(), size / pixbuf.get_height())
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BEST)
        cr.paint_with_alpha(max(0.0, min(1.0, alpha)))
        cr.restore()

    def draw_launcher_glyph(self, cr, x, y, size, alpha):
        rrect(cr, x, y, size, size, size * 0.23)
        g = cairo.LinearGradient(0, y, 0, y + size)
        g.add_color_stop_rgba(0, 0.30, 0.34, 0.48, alpha)
        g.add_color_stop_rgba(1, 0.14, 0.16, 0.26, alpha)
        cr.set_source(g)
        cr.fill()
        cell = size * 0.15
        gap = size * 0.09
        start = (size - (3 * cell + 2 * gap)) / 2
        cr.set_source_rgba(1, 1, 1, 0.92 * alpha)
        for row in range(3):
            for col in range(3):
                rrect(cr, x + start + col * (cell + gap), y + start + row * (cell + gap),
                      cell, cell, cell * 0.3)
                cr.fill()

    def draw_slot(self, cr, s, now, base):
        a = min(1.0, s.presence)
        if s.kind == "sep":
            cr.set_source_rgba(1, 1, 1, 0.22 * a)
            cr.rectangle(round(s.x), base - self.icon_sz * 0.75, 1, self.icon_sz * 0.75)
            cr.fill()
            return

        size = s.size
        x = s.x + (s.w - size) / 2
        y = base - size - self.bounce_offset(s, now)

        if s.active_a > 0.01:
            rrect(cr, x - 4, y - 4, size + 8, size + 8, size * 0.24)
            cr.set_source_rgba(1, 1, 1, 0.11 * s.active_a)
            cr.fill()

        if s.kind == "launcher":
            self.draw_launcher_glyph(cr, x, y, size, a)
            return
        self.draw_icon(cr, s.icon, x, y, size, a)

        if s.windows:
            urgent = any(w.needs_attention() for w in s.windows)
            n = min(len(s.windows), 3)
            cx, cy = s.x + s.w / 2, base + self.pad / 2 + 1
            for i in range(n):
                dx = (i - (n - 1) / 2) * 7
                if urgent:
                    cr.set_source_rgba(1.0, 0.62, 0.20, a)
                elif s.active_a > 0.5:
                    cr.set_source_rgba(*self.accent, a)
                else:
                    cr.set_source_rgba(1, 1, 1, 0.72 * a)
                cr.arc(cx + dx, cy, 2.2, 0, 2 * math.pi)
                cr.fill()

    def draw_label(self, cr, text, cx, bottom, alpha):
        font = Pango.FontDescription.from_string(
            Gtk.Settings.get_default().get_property("gtk-font-name") or "Sans 10")
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(font)
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        w, h = tw + 20, th + 10
        x = max(4, min(self.win_w - w - 4, cx - w / 2))
        y = max(2, bottom - h)
        rrect(cr, x, y, w, h, h / 2.6)
        cr.set_source_rgba(0.06, 0.07, 0.09, 0.82 * alpha)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.12 * alpha)
        cr.set_line_width(1)
        cr.stroke()
        cr.move_to(x + 10, y + 5)
        cr.set_source_rgba(1, 1, 1, 0.95 * alpha)
        PangoCairo.show_layout(cr, layout)


# --------------------------------------------------------------------------- application

class DockApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.dock = None

    def do_activate(self):
        if self.dock is None:
            self.dock = Dock(self, load_config())


def main():
    GLib.set_prgname("pydock")
    GLib.set_application_name("PyDock")
    display = Gdk.Display.get_default()
    if display is None or not isinstance(display, GdkX11.X11Display):
        sys.exit("pydock: needs an X11 session (Wayland is not supported).")
    app = DockApp()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: app.quit() or True)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: app.quit() or True)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())