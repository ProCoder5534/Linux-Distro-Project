"""Thin GTK3/X11 status panel for PyDock."""

import datetime
import locale
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _configure_locale():
    try:
        locale.setlocale(locale.LC_ALL, "")
        return
    except locale.Error:
        pass
    for candidate in ("C.UTF-8", "C.utf8", "C"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            os.environ["LC_ALL"] = candidate
            os.environ["LANG"] = candidate
            return
        except locale.Error:
            continue


_configure_locale()
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

ICON_SIZE = Gtk.IconSize.MENU

CSS = b"""
/* ---------- Panel (frosted glass) ---------- */
#pydock-status {
    background-color: rgba(22, 26, 38, 0.45);
    background-image: linear-gradient(to bottom,
                      rgba(255, 255, 255, 0.10), rgba(255, 255, 255, 0.02));
    border-top: 1px solid rgba(255, 255, 255, 0.22);
}
#pydock-status label,
#pydock-status image {
    color: rgba(246, 248, 252, 0.96);
}
#pydock-status label {
    font-size: 12px;
    font-weight: 500;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
}

/* Glass "chips" that group each indicator */
#pydock-status .chip {
    background-color: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 0 12px;
    min-height: 22px;
}
#pydock-status .chip image { margin-right: 6px; }

/* Battery states */
#pydock-status .chip.charging label,
#pydock-status .chip.charging image { color: #9fe3b5; }
#pydock-status .chip.low label,
#pydock-status .chip.low image { color: #ff9a9a; }

/* Clock (clickable chip) */
#pydock-status button.clock {
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    box-shadow: none;
    padding: 0 14px;
    min-height: 22px;
    transition: background-color 140ms ease;
}
#pydock-status button.clock:hover  { background: rgba(138, 180, 248, 0.28); }
#pydock-status button.clock:active { background: rgba(138, 180, 248, 0.40); }

/* Mute button inside the volume chip */
#pydock-status button.icon-btn {
    background: transparent;
    border: 0;
    border-radius: 999px;
    box-shadow: none;
    padding: 0 4px;
    min-width: 0;
    min-height: 0;
}
#pydock-status button.icon-btn:hover { background: rgba(255, 255, 255, 0.16); }
#pydock-status button.icon-btn image { margin-right: 0; }

/* Volume slider: slim track, thumb appears on hover */
#pydock-status scale { padding: 0; margin: 0 2px 0 4px; }
#pydock-status scale trough {
    min-height: 4px;
    border: 0;
    border-radius: 2px;
    background-color: rgba(255, 255, 255, 0.22);
}
#pydock-status scale highlight {
    border: 0;
    border-radius: 2px;
    background: #a7c8ff;
}
#pydock-status scale slider {
    min-width: 12px;
    min-height: 12px;
    border: 0;
    border-radius: 50%;
    background: #ffffff;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.45);
    opacity: 0;
    transition: opacity 120ms ease;
}
#pydock-status scale:hover slider,
#pydock-status scale:active slider { opacity: 1; }

/* ---------- Calendar popup (frosted glass) ---------- */
#pydock-calendar {
    background-color: rgba(26, 30, 42, 0.58);
    background-image: linear-gradient(to bottom right,
                      rgba(255, 255, 255, 0.14), rgba(255, 255, 255, 0.03));
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-radius: 16px;
}
#pydock-calendar label {
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
}
#pydock-calendar label.cal-time {
    color: #f6f8fc;
    font-size: 30px;
    font-weight: 300;
}
#pydock-calendar label.cal-date {
    color: rgba(246, 248, 252, 0.72);
    font-size: 12px;
}
#pydock-calendar separator {
    background-color: rgba(255, 255, 255, 0.14);
    min-height: 1px;
}
#pydock-calendar calendar {
    color: #f6f8fc;
    background: transparent;
    border: 0;
    padding: 2px 0 0 0;
    font-size: 12px;
}
#pydock-calendar calendar:selected {
    color: #0e1420;
    background-color: #a7c8ff;
    border-radius: 8px;
}
#pydock-calendar calendar:indeterminate { color: rgba(246, 248, 252, 0.32); }
#pydock-calendar calendar.header {
    background: transparent;
    border: 0;
    color: rgba(246, 248, 252, 0.98);
}
#pydock-calendar calendar.highlight { color: rgba(246, 248, 252, 0.60); }
#pydock-calendar calendar.button {
    color: rgba(246, 248, 252, 0.85);
    background: transparent;
    border: 0;
    border-radius: 6px;
}
#pydock-calendar calendar.button:hover { background: rgba(138, 180, 248, 0.24); }
"""


def _chip(icon_name=None):
    """Build a rounded indicator chip: [icon] label."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.get_style_context().add_class("chip")
    image = Gtk.Image.new_from_icon_name(icon_name or "image-missing", ICON_SIZE)
    label = Gtk.Label()
    box.pack_start(image, False, False, 0)
    box.pack_start(label, False, False, 0)
    return box, image, label


class StatusBar(Gtk.Window):
    """Full-width bottom panel that reserves its height from Openbox workspaces."""

    HEIGHT = 36
    POPUP_WIDTH = 300
    POPUP_GAP = 8

    def __init__(self, app, cfg):
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title="PyDock Status Bar")
        app.add_window(self)
        self.monitor_index = int(cfg.get("monitor", -1))
        self._volume_backend = "pactl" if shutil.which("pactl") else (
            "amixer" if shutil.which("amixer") else None)
        self._volume_guard = False
        self._volume_timer = 0
        self._popup_hidden_at = 0
        self._blur_sizes = {}
        self.app = app

        self.set_name("pydock-status")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_keep_above(True)
        self.stick()
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_app_paintable(True)

        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(12)
        bar.set_margin_end(14)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        self.add(bar)

        # Controls fill the bar but hug the right edge.
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.END)
        bar.pack_start(controls, True, True, 0)

        # Network chip
        self.network_chip, self.network_icon, self.network_label = _chip(
            "network-offline-symbolic")
        self.network_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.network_label.set_max_width_chars(18)
        controls.pack_start(self.network_chip, False, False, 0)

        # Battery chip (hidden on desktops without a battery)
        self.battery_chip, self.battery_icon, self.battery_label = _chip(
            "battery-level-100-symbolic")
        self.battery_chip.set_no_show_all(True)
        controls.pack_start(self.battery_chip, False, False, 0)

        # Volume chip: mute button + slider
        volume_chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        volume_chip.get_style_context().add_class("chip")
        volume_chip.set_margin_end(0)

        self.volume_button = Gtk.Button()
        self.volume_button.get_style_context().add_class("icon-btn")
        self.volume_button.set_relief(Gtk.ReliefStyle.NONE)
        self.volume_button.set_focus_on_click(False)
        self.volume_button.set_tooltip_text("Mute or unmute audio")
        self.volume_icon = Gtk.Image.new_from_icon_name(
            "audio-volume-high-symbolic", ICON_SIZE)
        self.volume_button.add(self.volume_icon)
        self.volume_button.connect("clicked", self.toggle_mute)
        volume_chip.pack_start(self.volume_button, False, False, 0)

        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_draw_value(False)
        self.volume.set_size_request(104, 18)
        self.volume.set_tooltip_text("System volume")
        self.volume.set_sensitive(self._volume_backend is not None)
        self.volume.connect("value-changed", self.on_volume_changed)
        volume_chip.pack_start(self.volume, False, False, 0)
        controls.pack_start(volume_chip, False, False, 0)

        # Clock button
        self.clock_button = Gtk.Button()
        self.clock_button.get_style_context().add_class("clock")
        self.clock_button.set_relief(Gtk.ReliefStyle.NONE)
        self.clock_button.set_focus_on_click(False)
        self.clock_button.set_tooltip_text("Open calendar")
        self.clock_label = Gtk.Label()
        self.clock_button.add(self.clock_label)
        self.clock_button.connect("clicked", self.show_calendar)
        controls.pack_start(self.clock_button, False, False, 0)

        self._build_calendar_popup(app, screen)

        self.set_size_request(1, self.HEIGHT)
        self.connect("realize", lambda *_: self.apply_struts())
        self.connect("size-allocate", self.apply_blur)
        self.connect("map-event", self.on_map)
        screen.connect("monitors-changed", lambda *_: self.place())
        screen.connect("size-changed", lambda *_: self.place())
        self.place()
        self.show_all()
        self.update()
        GLib.timeout_add_seconds(2, self.update)

    # ------------------------------------------------------------------ popup

    def _build_calendar_popup(self, app, screen):
        popup = Gtk.Window(type=Gtk.WindowType.TOPLEVEL, title="PyDock Calendar")
        self.calendar_popup = popup
        app.add_window(popup)
        popup.set_name("pydock-calendar")
        popup.set_decorated(False)
        popup.set_resizable(False)
        popup.set_skip_taskbar_hint(True)
        popup.set_skip_pager_hint(True)
        popup.set_accept_focus(True)
        popup.set_focus_on_map(True)
        popup.set_keep_above(True)
        popup.stick()
        popup.set_transient_for(self)
        popup.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        popup.set_app_paintable(True)
        visual = screen.get_rgba_visual()
        if visual:
            popup.set_visual(visual)
        popup.connect("size-allocate", self.apply_blur)
        popup.connect("key-press-event", self.on_calendar_key)
        popup.connect("focus-out-event", self.on_calendar_focus_out)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_size_request(self.POPUP_WIDTH, -1)
        content.set_margin_top(16)
        content.set_margin_bottom(12)
        content.set_margin_start(18)
        content.set_margin_end(18)
        popup.add(content)

        self.cal_time = Gtk.Label(xalign=0)
        self.cal_time.get_style_context().add_class("cal-time")
        self.cal_date = Gtk.Label(xalign=0)
        self.cal_date.get_style_context().add_class("cal-date")
        content.pack_start(self.cal_time, False, False, 0)
        content.pack_start(self.cal_date, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(12)
        sep.set_margin_bottom(6)
        content.pack_start(sep, False, False, 0)

        self.calendar = Gtk.Calendar()
        content.pack_start(self.calendar, False, False, 0)

    def _refresh_popup_header(self, now=None):
        now = now or datetime.datetime.now().astimezone()
        self.cal_time.set_text(now.strftime("%H:%M"))
        self.cal_date.set_text(now.strftime("%A, %d %B %Y"))

    # ---------------------------------------------------------------- placing

    def selected_monitor(self):
        display = Gdk.Display.get_default()
        if display is None or display.get_n_monitors() == 0:
            return None
        if 0 <= self.monitor_index < display.get_n_monitors():
            return display.get_monitor(self.monitor_index)
        return display.get_primary_monitor() or display.get_monitor(0)

    def place(self):
        mon = self.selected_monitor()
        if mon is None:
            return
        g = mon.get_geometry()
        self.set_default_size(g.width, self.HEIGHT)
        self.set_size_request(g.width, self.HEIGHT)
        if self.get_realized():
            self.resize(g.width, self.HEIGHT)
        self.move(g.x, g.y + g.height - self.HEIGHT)
        if self.get_realized():
            self.apply_struts(g)

    def on_map(self, *_args):
        self.place()
        return False

    def apply_struts(self, geometry=None):
        """Reserve the panel's bottom strip so maximized Openbox windows stop above it."""
        mon = self.selected_monitor()
        if mon is None:
            return
        g = geometry or mon.get_geometry()
        root = Gdk.Screen.get_default()
        bottom = max(0, root.get_height() - (g.y + g.height - self.HEIGHT))
        values = [0, 0, 0, bottom, 0, 0, 0, 0, 0, 0,
                  max(0, g.x), max(0, g.x + g.width - 1)]
        self.set_cardinals("_NET_WM_STRUT_PARTIAL", values)
        self.set_cardinals("_NET_WM_STRUT", [0, 0, 0, bottom])

    def apply_blur(self, widget, alloc):
        """Ask the compositor (picom/KWin) to blur whatever is behind this window."""
        gdk_window = widget.get_window()
        if gdk_window is None or alloc.width <= 1:
            return
        size = (alloc.width, alloc.height)
        if self._blur_sizes.get(widget) == size:
            return
        self._blur_sizes[widget] = size
        self.set_cardinals("_KDE_NET_WM_BLUR_BEHIND_REGION",
                           [0, 0, alloc.width, alloc.height], gdk_window)

    def set_cardinals(self, name, values, gdk_window=None):
        gdk_window = gdk_window or self.get_window()
        if gdk_window is None:
            return
        atom = Gdk.Atom.intern(name, False)
        try:
            Gdk.property_change(gdk_window, atom, Gdk.Atom.intern("CARDINAL", False),
                                32, Gdk.PropMode.REPLACE, values, len(values))
        except Exception:
            pass
        # GDK's format-32 marshaling differs between PyGObject builds. Write the
        # property through xprop as well when available so Openbox sees exact CARDINALs.
        xid = gdk_window.get_xid()
        if shutil.which("xprop"):
            try:
                subprocess.run(["xprop", "-id", str(xid), "-f", name, "32c",
                                "-set", name, ",".join(str(v) for v in values)],
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass

    # ----------------------------------------------------------- status data

    @staticmethod
    def network_status():
        """Return (icon_name, text) for the active default route."""
        route_iface = None
        try:
            for line in Path("/proc/net/route").read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 1:
                    route_iface = fields[0]
                    break
        except (OSError, ValueError):
            pass
        if not route_iface:
            return "network-offline-symbolic", "Offline"
        iface = Path("/sys/class/net") / route_iface
        if route_iface.startswith(("tun", "tap", "wg", "ppp")):
            return "network-vpn-symbolic", "VPN"
        if (iface / "wireless").exists():
            return "network-wireless-symbolic", "Wi-Fi"
        return "network-wired-symbolic", "Ethernet"

    @staticmethod
    def battery_status():
        """Return (icon_name, text, css_state) or None if no battery is present."""
        for supply in Path("/sys/class/power_supply").glob("*"):
            try:
                if (supply / "type").read_text().strip().lower() != "battery":
                    continue
                capacity = supply / "capacity"
                if capacity.exists():
                    percent = int(capacity.read_text().strip())
                else:
                    now_key = "energy_now" if (supply / "energy_now").exists() else "charge_now"
                    full_key = "energy_full" if now_key == "energy_now" else "charge_full"
                    now = int((supply / now_key).read_text().strip())
                    full = int((supply / full_key).read_text().strip())
                    if full <= 0:
                        continue
                    percent = round(100 * now / full)
                percent = max(0, min(100, percent))
                status_file = supply / "status"
                status = status_file.read_text().strip().lower() if status_file.exists() else ""
                level = int(round(percent / 10.0)) * 10
                if status == "charging":
                    return f"battery-level-{level}-charging-symbolic", f"{percent}%", "charging"
                if status == "full":
                    return "battery-level-100-charged-symbolic", f"{percent}%", ""
                state = "low" if percent <= 15 else ""
                return f"battery-level-{level}-symbolic", f"{percent}%", state
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def volume_icon_name(value, muted):
        if muted or not value:
            return "audio-volume-muted-symbolic"
        if value < 34:
            return "audio-volume-low-symbolic"
        if value < 67:
            return "audio-volume-medium-symbolic"
        return "audio-volume-high-symbolic"

    def read_volume(self):
        if self._volume_backend == "pactl":
            try:
                out = subprocess.run(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    check=True, text=True, capture_output=True, timeout=0.7).stdout
                match = re.search(r"(\d+)%", out)
                muted_out = subprocess.run(
                    ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                    check=True, text=True, capture_output=True, timeout=0.7).stdout
                return (int(match.group(1)) if match else None, "yes" in muted_out.lower())
            except (OSError, subprocess.SubprocessError):
                return None, False
        if self._volume_backend == "amixer":
            try:
                out = subprocess.run(
                    ["amixer", "-D", "pulse", "sget", "Master"],
                    check=True, text=True, capture_output=True, timeout=0.7).stdout
                match = re.search(r"\[(\d+)%\]", out)
                muted = "[off]" in out.lower()
                return (int(match.group(1)) if match else None, muted)
            except (OSError, subprocess.SubprocessError):
                return None, False
        return None, False

    def set_volume(self):
        self._volume_timer = 0
        value = int(self.volume.get_value())
        try:
            if self._volume_backend == "pactl":
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=0.7)
            elif self._volume_backend == "amixer":
                subprocess.run(["amixer", "-D", "pulse", "sset", "Master", f"{value}%"],
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=0.7)
        except (OSError, subprocess.SubprocessError):
            pass
        return GLib.SOURCE_REMOVE

    def on_volume_changed(self, _scale):
        if self._volume_guard or self._volume_backend is None:
            return
        value = int(self.volume.get_value())
        self.volume.set_tooltip_text(f"Volume {value}%")
        self.volume_icon.set_from_icon_name(self.volume_icon_name(value, False), ICON_SIZE)
        if self._volume_timer:
            GLib.source_remove(self._volume_timer)
        self._volume_timer = GLib.timeout_add(120, self.set_volume)

    def toggle_mute(self, *_args):
        try:
            if self._volume_backend == "pactl":
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=0.7)
            elif self._volume_backend == "amixer":
                subprocess.run(["amixer", "-D", "pulse", "sset", "Master", "toggle"],
                               check=False, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=0.7)
        except (OSError, subprocess.SubprocessError):
            pass
        self.update()

    # --------------------------------------------------------------- calendar

    def show_calendar(self, *_args):
        popup = self.calendar_popup
        if popup.get_visible():
            popup.hide()
            return
        # If the popup just closed because the click moved focus away, don't reopen it.
        if GLib.get_monotonic_time() - self._popup_hidden_at < 250_000:
            return

        now = datetime.datetime.now().astimezone()
        self._refresh_popup_header(now)
        self.calendar.select_month(now.month - 1, now.year)
        self.calendar.select_day(now.day)

        mon = self.selected_monitor()
        if mon is None:
            return
        g = mon.get_geometry()

        popup.show_all()
        _min, natural = popup.get_preferred_size()
        width, height = natural.width, natural.height

        bar_x, bar_y = self.get_position()
        coords = self.clock_button.translate_coordinates(self, 0, 0)
        bx = coords[0] if coords else self.get_allocated_width() - 200
        right_edge = bar_x + bx + self.clock_button.get_allocated_width()
        x = max(g.x + self.POPUP_GAP,
                min(g.x + g.width - width - self.POPUP_GAP, right_edge - width))
        y = max(g.y, bar_y - height - self.POPUP_GAP)
        popup.move(x, y)
        popup.present()

    def on_calendar_key(self, _window, event):
        if event.keyval == Gdk.KEY_Escape:
            self.calendar_popup.hide()
            return True
        return False

    def on_calendar_focus_out(self, *_args):
        self.calendar_popup.hide()
        self._popup_hidden_at = GLib.get_monotonic_time()
        return False

    # ----------------------------------------------------------------- update

    def update(self):
        now = datetime.datetime.now().astimezone()

        icon, text = self.network_status()
        self.network_icon.set_from_icon_name(icon, ICON_SIZE)
        self.network_label.set_text(text)

        battery = self.battery_status()
        if battery:
            icon, text, state = battery
            self.battery_icon.set_from_icon_name(icon, ICON_SIZE)
            self.battery_label.set_text(text)
            ctx = self.battery_chip.get_style_context()
            for cls in ("charging", "low"):
                ctx.remove_class(cls)
            if state:
                ctx.add_class(state)
            self.battery_chip.show_all()
        else:
            self.battery_chip.hide()

        self.clock_label.set_markup(
            f"<b>{now.strftime('%H:%M')}</b>"
            f"  <span alpha='65%'>{now.strftime('%a %d %b')}</span>")
        if self.calendar_popup.get_visible():
            self._refresh_popup_header(now)

        value, muted = self.read_volume()
        if value is not None:
            self.volume_icon.set_from_icon_name(self.volume_icon_name(value, muted), ICON_SIZE)
            self._volume_guard = True
            self.volume.set_value(max(0, min(100, value)))
            self._volume_guard = False
            # on_volume_changed was skipped by the guard, so refresh the tooltip here
            self.volume.set_tooltip_text(f"Volume {value}%" + (" (muted)" if muted else ""))
        return GLib.SOURCE_CONTINUE


def main():
    """Allow launching the bar by itself, as well as through main.py."""
    app = Gtk.Application(application_id="org.pydock.StatusBar")

    def activate(application):
        if not hasattr(application, "status_bar"):
            application.status_bar = StatusBar(application, {"monitor": -1})

    app.connect("activate", activate)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())