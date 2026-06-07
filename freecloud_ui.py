#!/usr/bin/env python3
"""Small FreeCloud desktop launcher UI for macOS/Linux."""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import freecloud_cli as cli


BASE_DIR = Path(__file__).resolve().parent
CLI_PATH = BASE_DIR / "freecloud_cli.py"
LAST_CONFIG_PATH = cli.LAST_CONFIG_PATH
LEGACY_LAST_CONFIG_PATH = cli.LEGACY_LAST_CONFIG_PATH
PID_PATH = cli.STATE_DIR / "sync.pid"
TRAY_PID_PATH = cli.STATE_DIR / "tray.pid"
BACKGROUND_LOG_PATH = cli.STATE_DIR / "sync.log"
TRAY_LOG_PATH = cli.STATE_DIR / "tray.log"
UI_ERROR_LOG_PATH = cli.STATE_DIR / "ui_error.log"
REMOTE_TREE_CACHE_PATH = cli.STATE_DIR / "remote_tree_cache.json"
STARTUP_PROFILE_PATH = cli.STATE_DIR / "startup_profile.log"
LOGO_PATH = BASE_DIR / "logo.png"
ICON_PATH = BASE_DIR / "icon.png"
TRAY_ICON_PATH = BASE_DIR / "FClogo.png" if (BASE_DIR / "FClogo.png").is_file() else ICON_PATH
RUN_SCRIPT_PATH = BASE_DIR / "Run_Mac_Linux.sh"
WINDOWS_CREATION_FLAGS = sum(
    getattr(subprocess, name, 0)
    for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW")
)

COLORS = {
    "bg": "#f4f9ff",
    "panel": "#ffffff",
    "panel_alt": "#f5fbff",
    "hero": "#d8efff",
    "hero_dark": "#7db8df",
    "ink": "#18324a",
    "muted": "#5b7691",
    "line": "#d6e8f6",
    "line_strong": "#c2dced",
    "accent": "#4f9ccf",
    "accent_dark": "#2f7fb5",
    "success": "#2f7d66",
    "danger": "#b25555",
    "console": "#15314b",
    "console_ink": "#edf7ff",
    "button_light": "#f2f8fc",
    "button_light_hover": "#e8f2f8",
    "button_border": "#b8d3e5",
    "button_top": "#ffffff",
    "button_top_blue": "#79b5da",
    "table_line": "#e0eaf5",
    "shadow": "#deebf6",
}

STARTUP_STARTED_AT = time.perf_counter()
MAIN_SCROLLBAR_OVERFLOW_TOLERANCE = 8


def startup_log(message: str) -> None:
    try:
        STARTUP_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        elapsed = time.perf_counter() - STARTUP_STARTED_AT
        with STARTUP_PROFILE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{elapsed:7.3f}s  {message}\n")
    except OSError:
        pass


def autostart_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "autostart" / "freecloud.desktop"


def load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_last_config() -> dict[str, object]:
    """Load setup saved by the new app location, then fall back to old builds.

    Early FreeCloud builds saved `.freecloud_last_config.json` beside the app.
    New Linux packages should use `~/.config/freecloud/last_config.json`, but
    this fallback keeps existing users from losing their setup.
    """
    config = load_json(LAST_CONFIG_PATH)
    if config:
        return config

    legacy_config = load_json(LEGACY_LAST_CONFIG_PATH)
    if legacy_config:
        cli.save_json(LAST_CONFIG_PATH, legacy_config)
    return legacy_config


def build_setup_urls(domain_text: str, drive_text: str) -> tuple[str, str, str]:
    domain = cli.normalize_domain(domain_text)
    drive_name = cli.normalize_drive_name(drive_text)
    parsed = urllib.parse.urlparse(domain)
    domain_path = parsed.path.strip("/")

    if domain_path:
        base_url = domain.rstrip("/")
        if drive_text.strip() in {"", "FreeCloud"}:
            drive_name = domain_path.split("/")[-1]
        return domain, drive_name, base_url

    return domain, drive_name, f"{domain}/{drive_name}"


def load_scaled_image(path: Path, max_width: int, max_height: int) -> tk.PhotoImage | None:
    if not path.is_file():
        return None

    try:
        image = tk.PhotoImage(file=str(path))
    except tk.TclError:
        return None
    scale = max(
        1,
        math.ceil(image.width() / max_width) if max_width > 0 else 1,
        math.ceil(image.height() / max_height) if max_height > 0 else 1,
    )
    return image if scale <= 1 else image.subsample(scale, scale)


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_file_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    if process_is_running(pid):
        return True
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return False


def launch_tray_indicator() -> None:
    if not sys.platform.startswith("linux") or pid_file_running(TRAY_PID_PATH):
        return

    try:
        TRAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRAY_LOG_PATH.open("a", encoding="utf-8") as log_handle:
            kwargs: dict[str, object] = {
                "cwd": str(BASE_DIR),
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
                "start_new_session": True,
            }
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--tray"], **kwargs)
    except OSError:
        return


def tray_pid() -> int | None:
    try:
        return int(TRAY_PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def stop_tray_indicator() -> None:
    pid = tray_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        TRAY_PID_PATH.unlink()
    except FileNotFoundError:
        pass


def saved_config_data() -> dict[str, object]:
    config = load_last_config()
    if str(config.get("mode") or "") == "self_host":
        return {}
    return config


def background_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def background_sync_running() -> bool:
    pid = background_pid()
    if pid is None:
        return False
    if process_is_running(pid):
        return True
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return False


def launch_background_sync_from_saved_config() -> bool:
    if background_sync_running():
        return True
    if not saved_config_data():
        return False

    try:
        BACKGROUND_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BACKGROUND_LOG_PATH.open("a", encoding="utf-8") as log_handle:
            kwargs: dict[str, object] = {
                "cwd": str(BASE_DIR),
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                kwargs["creationflags"] = WINDOWS_CREATION_FLAGS
            else:
                kwargs["start_new_session"] = True

            process = subprocess.Popen([sys.executable, str(CLI_PATH)], **kwargs)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(process.pid), encoding="utf-8")
    except OSError:
        return False
    return True


def run_background_startup() -> int:
    launch_background_sync_from_saved_config()
    launch_tray_indicator()
    return 0


def styled_scrollbar(parent: tk.Widget, orient: str, command: object, width: int) -> tk.Scrollbar:
    return tk.Scrollbar(
        parent,
        orient=orient,
        command=command,
        width=width,
        relief="groove",
        bd=1,
        highlightthickness=0,
        bg="#b7d7ef",
        activebackground=COLORS["accent_dark"],
        troughcolor="#f3f9fe",
    )


class SoftButton(tk.Canvas):
    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: object,
        bg: str,
        fg: str,
        state: str = tk.NORMAL,
        width: int = 150,
        height: int = 42,
        radius: int = 10,
        border: str | None = None,
        hover_bg: str | None = None,
        textvariable: tk.StringVar | None = None,
        highlightbackground: str | None = None,
        activebackground: str | None = None,
        disabledforeground: str | None = None,
        highlightthickness: int = 0,
        font_size: int | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            highlightthickness=highlightthickness,
            bd=0,
            bg=parent.cget("bg"),
            relief="flat",
            cursor="hand2" if state == tk.NORMAL else "",
            **kwargs,
        )
        self.command = command
        self.base_bg = bg
        self.hover_bg = activebackground or hover_bg or bg
        self.fg = fg
        self.hover_fg = fg
        self.disabled_fg = disabledforeground or "#8aa3b8"
        self.state = state
        self.radius = radius
        self.border = highlightbackground or border or bg
        self.text = text
        self.textvariable = textvariable
        self.font_size = font_size
        self._trace_id: str | None = None
        self._hovering = False

        self.bind("<Button-1>", self.on_click)
        self.bind("<Enter>", self.on_enter)
        self.bind("<Leave>", self.on_leave)
        self.bind("<Configure>", self.on_resize)

        if self.textvariable is not None:
            self._trace_id = self.textvariable.trace_add("write", self.on_textvariable_changed)

        self.redraw()

    def on_resize(self, _event: tk.Event[tk.Misc]) -> None:
        self.redraw()

    def on_textvariable_changed(self, *_args: object) -> None:
        self.text = self.textvariable.get()
        self.redraw()

    def on_enter(self, _event: tk.Event[tk.Misc]) -> None:
        if self.state != tk.NORMAL:
            return
        self._hovering = True
        self.redraw()

    def on_leave(self, _event: tk.Event[tk.Misc]) -> None:
        self._hovering = False
        self.redraw()

    def on_click(self, _event: tk.Event[tk.Misc]) -> None:
        if self.state != tk.NORMAL:
            return
        if callable(self.command):
            self.command()

    def configure(self, cnf: dict[str, object] | None = None, **kw: object) -> object:
        options = {}
        if cnf:
            options.update(cnf)
        options.update(kw)

        if "text" in options:
            self.text = str(options.pop("text"))
        if "textvariable" in options:
            variable = options.pop("textvariable")
            if isinstance(variable, tk.StringVar):
                if self.textvariable is not None and self._trace_id is not None:
                    self.textvariable.trace_remove("write", self._trace_id)
                self.textvariable = variable
                self._trace_id = self.textvariable.trace_add("write", self.on_textvariable_changed)
                self.text = self.textvariable.get()
        if "state" in options:
            self.state = str(options.pop("state"))
            super().configure(cursor="hand2" if self.state == tk.NORMAL else "")
        if "bg" in options:
            self.base_bg = str(options.pop("bg"))
        if "fg" in options:
            self.fg = str(options.pop("fg"))
        if "activeforeground" in options:
            self.hover_fg = str(options.pop("activeforeground"))
        if "activebackground" in options:
            self.hover_bg = str(options.pop("activebackground"))
        if "disabledforeground" in options:
            self.disabled_fg = str(options.pop("disabledforeground"))
        if "highlightbackground" in options:
            self.border = str(options.pop("highlightbackground"))
        if "highlightcolor" in options:
            options.pop("highlightcolor")

        result = super().configure(**options)
        self.redraw()
        return result

    config = configure

    def redraw(self) -> None:
        self.delete("all")
        width = max(20, int(self.winfo_width()))
        height = max(20, int(self.winfo_height()))
        radius = min(self.radius, height // 2, width // 2)
        fill = self.hover_bg if self._hovering and self.state == tk.NORMAL else self.base_bg
        outline = self.border if self.border != "" else fill
        text_color = (
            self.hover_fg if self._hovering and self.state == tk.NORMAL else self.fg
        ) if self.state == tk.NORMAL else self.disabled_fg
        self.create_polygon(
            self.rounded_points(1, 1, width - 1, height - 1, radius),
            fill=fill,
            outline=outline,
            width=1,
            smooth=True,
            splinesteps=18,
        )
        lines = self.text.split("\n", 1)
        if len(lines) == 2:
            self.create_text(width // 2, height // 2 - 10, text=lines[0], fill=text_color, font=("TkDefaultFont", 21, "bold"))
            self.create_text(width // 2, height // 2 + 18, text=lines[1], fill=text_color, font=("TkDefaultFont", 9, "bold"))
        else:
            font_size = self.font_size or (21 if len(self.text) <= 2 and height >= 36 else 10)
            self.create_text(width // 2, height // 2, text=self.text, fill=text_color, font=("TkDefaultFont", font_size, "bold"))

    def rounded_points(self, x1: int, y1: int, x2: int, y2: int, r: int) -> list[int]:
        return [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1, x2, y1 + r,
            x2, y2 - r,
            x2, y2, x2 - r, y2,
            x1 + r, y2,
            x1, y2, x1, y2 - r,
            x1, y1 + r,
            x1, y1, x1 + r, y1,
        ]


class FreeCloudUi:
    def __init__(self, root: tk.Tk) -> None:
        startup_log("FreeCloudUi init started")
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.logo_image: tk.PhotoImage | None = None
        self.icon_image: tk.PhotoImage | None = None

        root.title("FreeCloud Sync")
        root.geometry("1280x800")
        root.minsize(980, 640)
        root.configure(bg=COLORS["bg"])

        self.status = tk.StringVar(value="Stopped")
        self.domain_var = tk.StringVar(value="https://www.example.com")
        self.drive_var = tk.StringVar(value="FreeCloud")
        self.local_var = tk.StringVar(value=str(Path.home() / "FreeCloud"))
        self.password_var = tk.StringVar(value="")
        self.password_visible = False
        self.saved_drive_var = tk.StringVar(value="Not saved yet")
        self.saved_server_var = tk.StringVar(value="Not saved yet")
        self.saved_local_var = tk.StringVar(value="Not saved yet")
        self.sync_button_text = tk.StringVar(value="▶\nStart Sync")
        self.remote_path_var = tk.StringVar(value="Cloud path: /")
        self.files_empty_var = tk.StringVar(value="Loading cloud files...")
        self.current_remote_path = ""
        self.remote_back_stack: list[str] = []
        self.remote_forward_stack: list[str] = []
        self.remote_manifest_cache: list[dict[str, object]] = []
        self.remote_manifest_refreshing = False
        self.remote_manifest_refresh_after_id: str | None = None
        self.remote_manifest_refresh_started_at = 0.0
        self.remote_render_generation = 0
        self.last_rendered_folder_key = ""
        self.file_tree_entries: dict[str, dict[str, object]] = {}
        self.drag_source_entry: dict[str, object] | None = None
        self.drag_source_item_id = ""
        self.drag_start_xy: tuple[int, int] | None = None
        self.drag_active = False
        self.drag_source_widget: ttk.Treeview | None = None
        self.remote_move_in_progress = False
        self.remote_tree_rendering = False
        self.remote_tree_paths: dict[str, str] = {}
        self.remote_tree_items: dict[str, str] = {}
        self.remote_tree_signature: tuple[str, ...] = ()
        self.storage_fraction = 0.0
        self.storage_detail_var = tk.StringVar(value="Calculating...")
        self.storage_refreshing = False
        self.storage_last_refreshed_at = 0.0
        self.rendered_cache_notice = False
        self.file_browser_open = True
        self.show_sync_activity_var = tk.BooleanVar(value=False)
        self.closing_for_background = False
        self.editing_settings = False
        self.run_at_startup_var = tk.BooleanVar(value=self.startup_enabled())

        startup_log("loading saved values")
        self.load_saved_values()
        startup_log("saved values loaded")
        self.build_menu_bar()
        startup_log("menu bar built")

        self.outer = tk.Frame(root, bg=COLORS["bg"])
        self.outer.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            self.outer,
            bg=COLORS["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.main_scrollbar = styled_scrollbar(self.outer, tk.VERTICAL, self.canvas.yview, width=18)
        self.main_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.main_scrollbar_visible = True
        self.canvas.configure(yscrollcommand=self.main_scrollbar.set)

        self.frame = tk.Frame(self.canvas, bg=COLORS["bg"], padx=6, pady=6)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")

        self.frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Button-4>", self.on_mousewheel_linux)
        self.canvas.bind_all("<Button-5>", self.on_mousewheel_linux)

        self.build_header()
        startup_log("header built")
        root.after(120, self.load_logo_image)
        root.after(2200, self.load_window_icon)

        self.content = tk.Frame(self.frame, bg=COLORS["bg"])
        self.content.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        self.setup_frame = self.build_setup_card(self.content)
        startup_log("setup card built")
        self.actions_frame = self.build_actions_card(self.content)
        startup_log("actions ribbon built")
        self.files_frame = self.build_files_card(self.content)
        startup_log("files panel built")
        self.output_frame = self.build_output_card(self.content)
        self.output_frame.pack_forget()
        self.summary_frame = tk.Frame(self.content, bg=COLORS["bg"])
        startup_log("output/summary built")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.drain_output)
        root.after(500, launch_tray_indicator)
        root.after(1200, self.refresh_startup_entry_if_enabled)
        has_saved_setup = bool(self.config_data())
        self.refresh_setup_visibility()
        startup_log(f"initial visibility set, saved_setup={has_saved_setup}")
        if has_saved_setup and self.file_browser_open:
            root.after(250, self.load_cached_files_after_startup)
        if has_saved_setup:
            root.after(3500, self.start_sync)
        startup_log("FreeCloudUi init finished")

    def on_frame_configure(self, _event: tk.Event[tk.Misc]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.update_main_scrollbar_visibility()

    def on_canvas_configure(self, event: tk.Event[tk.Misc]) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        self.update_canvas_window_height(event.height)
        self.root.after_idle(self.update_main_scrollbar_visibility)

    def update_main_scrollbar_visibility(self) -> None:
        content_bounds = self.canvas.bbox("all")
        if content_bounds is None:
            needs_scrollbar = False
        else:
            content_height = content_bounds[3] - content_bounds[1]
            overflow = max(0, content_height - self.canvas.winfo_height())
            needs_scrollbar = overflow > MAIN_SCROLLBAR_OVERFLOW_TOLERANCE

        if needs_scrollbar and not self.main_scrollbar_visible:
            self.main_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            self.main_scrollbar_visible = True
        elif not needs_scrollbar and self.main_scrollbar_visible:
            self.main_scrollbar.pack_forget()
            self.main_scrollbar_visible = False
            self.canvas.yview_moveto(0)

    def update_canvas_window_height(self, viewport_height: int | None = None) -> None:
        if viewport_height is None:
            viewport_height = self.canvas.winfo_height()
        has_saved_setup = bool(self.config_data())
        browser_view = has_saved_setup and not self.editing_settings and self.file_browser_open
        target_height = viewport_height if browser_view else max(viewport_height, self.frame.winfo_reqheight())
        self.canvas.itemconfigure(self.canvas_window, height=target_height)

    def on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        canvas = self.scroll_target_for_event(event)
        if event.delta == 0 or canvas is None:
            return
        canvas.yview_scroll(int(-event.delta / 120), "units")

    def on_mousewheel_linux(self, event: tk.Event[tk.Misc]) -> None:
        canvas = self.scroll_target_for_event(event)
        if canvas is None:
            return
        if getattr(event, "num", 0) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", 0) == 5:
            canvas.yview_scroll(1, "units")

    def scroll_target_for_event(self, event: tk.Event[tk.Misc]) -> tk.Widget | None:
        widget = event.widget
        while widget is not None:
            if widget == getattr(self, "files_tree", None):
                return self.files_tree
            if widget == self.canvas:
                return self.canvas
            widget = widget.master
        return self.canvas

    def load_saved_values(self) -> None:
        config = load_last_config()
        if not config or str(config.get("mode") or "") == "self_host":
            return

        self.domain_var.set(str(config.get("base_url") or config.get("domain") or self.domain_var.get()))
        self.drive_var.set(str(config.get("drive_name") or self.drive_var.get()))
        self.local_var.set(str(config.get("local_root") or self.local_var.get()))
        self.password_var.set(str(config.get("password") or ""))
        self.update_summary_text(config)

    def load_logo_image(self) -> None:
        self.logo_image = load_scaled_image(LOGO_PATH, 96, 72)
        logo_label = getattr(self, "logo_display_label", None)
        if self.logo_image is not None and logo_label is not None:
            logo_label.configure(image=self.logo_image, text="")

    def load_window_icon(self) -> None:
        self.icon_image = load_scaled_image(ICON_PATH, 128, 128)
        if self.icon_image is not None:
            try:
                self.root.iconphoto(True, self.icon_image)
            except tk.TclError:
                self.icon_image = None

    def build_menu_bar(self) -> None:
        menu_options = {
            "tearoff": False,
            "bg": "#f3f9fe",
            "fg": COLORS["ink"],
            "activebackground": "#dceeff",
            "activeforeground": COLORS["accent_dark"],
            "selectcolor": COLORS["accent"],
            "relief": "flat",
            "bd": 0,
            "activeborderwidth": 0,
            "font": ("TkDefaultFont", 10),
        }
        menu_bar = tk.Menu(
            self.root,
            tearoff=False,
            bg="#edf6fd",
            fg=COLORS["ink"],
            activebackground="#dceeff",
            activeforeground=COLORS["accent_dark"],
            relief="flat",
            bd=0,
            activeborderwidth=0,
            font=("TkDefaultFont", 10),
        )

        file_menu = tk.Menu(menu_bar, **menu_options)
        file_menu.add_command(label="Open Local Folder", command=self.open_local_folder)
        file_menu.add_command(label="View on Web", command=self.view_on_web)
        file_menu.add_separator()
        file_menu.add_command(label="Run in Background", command=self.exit_keep_syncing)
        file_menu.add_command(label="Sync Now", command=self.sync_once)
        file_menu.add_checkbutton(
            label="Run at Startup",
            variable=self.run_at_startup_var,
            command=self.toggle_startup,
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu_bar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menu_bar, **menu_options)
        edit_menu.add_command(label="Edit Settings", command=self.edit_settings)
        edit_menu.add_separator()
        edit_menu.add_command(label="New Folder", command=self.create_remote_folder)
        edit_menu.add_command(label="Upload", command=self.upload_remote_files)
        edit_menu.add_command(label="Refresh", command=self.refresh_files)
        edit_menu.add_separator()
        edit_menu.add_checkbutton(
            label="Show Sync Activity",
            variable=self.show_sync_activity_var,
            command=self.toggle_sync_activity,
        )
        menu_bar.add_cascade(label="Edit", menu=edit_menu)

        help_menu = tk.Menu(menu_bar, **menu_options)
        help_menu.add_command(label="About FreeCloud", command=self.show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menu_bar)

    def startup_enabled(self) -> bool:
        if sys.platform.startswith("linux"):
            return autostart_path().is_file()
        return False

    def toggle_startup(self) -> None:
        try:
            if self.run_at_startup_var.get():
                self.enable_startup()
            else:
                self.disable_startup()
        except Exception as exc:
            self.run_at_startup_var.set(self.startup_enabled())
            messagebox.showerror("Run at Startup", str(exc))

    def enable_startup(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Run at Startup is currently implemented for Linux desktop sessions.")

        target = autostart_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        exec_target = RUN_SCRIPT_PATH if RUN_SCRIPT_PATH.is_file() else Path("/usr/bin/freecloud")
        exec_text = '"' + str(exec_target).replace('"', '\\"') + '"'
        exec_args = " --background"
        target.write_text(
            "\n".join(
                [
                    "[Desktop Entry]",
                    "Type=Application",
                    "Name=FreeCloud Sync",
                    "Comment=Start FreeCloud when you log in",
                    f"Exec={exec_text}{exec_args}",
                    "Icon=freecloud",
                    "Terminal=false",
                    "X-GNOME-Autostart-enabled=true",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def disable_startup(self) -> None:
        try:
            autostart_path().unlink()
        except FileNotFoundError:
            pass

    def refresh_startup_entry_if_enabled(self) -> None:
        if not self.run_at_startup_var.get():
            return
        try:
            self.enable_startup()
        except Exception:
            pass

    def toggle_sync_activity(self) -> None:
        if self.show_sync_activity_var.get():
            self.output_frame.pack(fill=tk.X, pady=(0, 0), after=self.files_frame)
        else:
            self.output_frame.pack_forget()

    def build_header(self) -> None:
        header = tk.Frame(
            self.frame,
            bg=COLORS["panel"],
            bd=0,
            highlightthickness=0,
            highlightbackground=COLORS["line"],
            padx=12,
            pady=6,
        )
        header.pack(fill=tk.X)

        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=0)

        brand = tk.Frame(header, bg=COLORS["panel"])
        brand.grid(row=0, column=0, sticky="w")

        self.logo_display_label = tk.Label(
            brand,
            text="☁",
            bg=COLORS["panel"],
            fg=COLORS["accent"],
            font=("TkDefaultFont", 34),
        )
        self.logo_display_label.pack(side=tk.LEFT, anchor="n", padx=(2, 14))

        tk.Label(
            brand,
            text="Stop paying twice for hosting space.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="left",
            font=("TkDefaultFont", 10),
        ).pack(side=tk.LEFT, anchor="n", pady=(2, 0))

        self.header_setup_frame = tk.Frame(header, bg=COLORS["panel"])
        self.header_setup_frame.grid(row=0, column=1, sticky="e", padx=(18, 24))
        tk.Label(self.header_setup_frame, text="⚙  Saved Setup", bg=COLORS["panel"], fg=COLORS["ink"], font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        for row, (label, value) in enumerate((
            ("Drive:", self.saved_drive_var),
            ("Server:", self.saved_server_var),
            ("Local:", self.saved_local_var),
        ), start=1):
            tk.Label(self.header_setup_frame, text=label, bg=COLORS["panel"], fg=COLORS["ink"], font=("TkDefaultFont", 9, "bold")).grid(row=row, column=0, sticky="w", padx=(0, 8))
            tk.Label(self.header_setup_frame, textvariable=value, bg=COLORS["panel"], fg=COLORS["ink"], font=("TkDefaultFont", 9), width=28, anchor="w").grid(row=row, column=1, sticky="w")

        status_box = tk.Frame(header, bg=COLORS["panel"], highlightthickness=0)
        status_box.grid(row=0, column=2, sticky="e", padx=(14, 4))
        tk.Label(status_box, text="Status", bg=COLORS["panel"], fg=COLORS["ink"], font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.header_status_label = tk.Label(status_box, textvariable=self.status, bg=COLORS["panel"], fg=COLORS["danger"], font=("TkDefaultFont", 18, "bold"))
        self.header_status_label.pack(anchor="w", pady=(3, 0))

    def build_setup_card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=0, bd=0)
        card.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        layout = tk.Frame(card, bg=COLORS["panel"])
        layout.pack(fill=tk.BOTH, expand=True, padx=38, pady=28)
        layout.grid_columnconfigure(0, weight=3)
        layout.grid_columnconfigure(1, weight=2)
        layout.grid_rowconfigure(0, weight=1)

        form = tk.Frame(layout, bg=COLORS["panel"])
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 44))
        form.grid_columnconfigure(0, weight=1)

        tk.Label(
            form,
            text="FreeCloud Setup",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 22, "bold"),
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            form,
            text="Connect your computer to your cloud drive.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            font=("TkDefaultFont", 11),
        ).grid(row=1, column=0, sticky="ew", pady=(6, 20))
        tk.Frame(form, bg=COLORS["line"], height=1).grid(row=2, column=0, sticky="ew", pady=(0, 24))

        tk.Label(
            form,
            text="Website Address",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=3, column=0, sticky="ew", pady=(0, 7))
        self.website_entry = self.setup_entry(form, self.domain_var)
        self.website_entry.grid(row=4, column=0, sticky="ew", ipady=10)
        self.domain_placeholder = "https://yourdomain.com/FreeCloud"
        if self.domain_var.get() in {"", "https://www.example.com"}:
            self.domain_var.set(self.domain_placeholder)
            self.website_entry.configure(fg="#8a9aaa")
        self.website_entry.bind("<FocusIn>", self.on_website_focus_in)
        self.website_entry.bind("<FocusOut>", self.on_website_focus_out)

        tk.Label(
            form,
            text="Local Folder",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=5, column=0, sticky="ew", pady=(24, 7))
        local_row = tk.Frame(form, bg=COLORS["panel"])
        local_row.grid(row=6, column=0, sticky="ew")
        local_row.grid_columnconfigure(0, weight=1)
        self.local_entry = self.setup_entry(local_row, self.local_var)
        self.local_entry.grid(row=0, column=0, sticky="ew", ipady=10, padx=(0, 10))
        tk.Button(
            local_row,
            text="Browse...",
            command=self.browse_local_folder,
            relief="flat",
            bd=0,
            bg=COLORS["button_light"],
            fg=COLORS["ink"],
            activebackground=COLORS["button_light_hover"],
            cursor="hand2",
            font=("TkDefaultFont", 10, "bold"),
            padx=18,
            pady=10,
        ).grid(row=0, column=1)

        tk.Label(
            form,
            text="Password (Optional)",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=7, column=0, sticky="ew", pady=(24, 7))
        password_row = tk.Frame(form, bg=COLORS["panel"])
        password_row.grid(row=8, column=0, sticky="ew")
        password_row.grid_columnconfigure(0, weight=1)
        self.password_entry = self.setup_entry(password_row, self.password_var, show="*")
        self.password_entry.grid(row=0, column=0, sticky="ew", ipady=10)
        self.password_toggle_button = tk.Button(
            password_row,
            text="Show",
            command=self.toggle_password_visibility,
            relief="flat",
            bd=0,
            bg=COLORS["panel"],
            fg=COLORS["accent_dark"],
            activebackground=COLORS["button_light_hover"],
            cursor="hand2",
            font=("TkDefaultFont", 9, "bold"),
            padx=10,
        )
        self.password_toggle_button.grid(row=0, column=1, sticky="e")

        self.setup_button = SoftButton(
            form,
            text="Connect to FreeCloud",
            command=self.save_setup,
            bg=COLORS["accent"],
            fg="white",
            highlightbackground=COLORS["accent"],
            activebackground=COLORS["accent_dark"],
            width=230,
            height=48,
            radius=9,
        )
        self.setup_button.grid(row=9, column=0, sticky="w", pady=(28, 8))

        help_card = tk.Frame(layout, bg="#f7fbff", highlightthickness=1, highlightbackground=COLORS["line"])
        help_card.grid(row=0, column=1, sticky="nsew")
        help_body = tk.Frame(help_card, bg="#f7fbff")
        help_body.pack(fill=tk.BOTH, expand=True, padx=24, pady=24)
        help_body.grid_columnconfigure(1, weight=1)
        tk.Label(
            help_body,
            text="?  Need Help?",
            bg="#f7fbff",
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))

        steps = (
            "Upload FreeCloud server files to your web host.",
            "Open your FreeCloud website.",
            "Enter the website address.",
            "Choose a local folder.",
            "Click Connect to FreeCloud.",
        )
        for index, text in enumerate(steps, start=1):
            tk.Label(
                help_body,
                text=str(index),
                bg=COLORS["accent"],
                fg="white",
                width=2,
                font=("TkDefaultFont", 9, "bold"),
            ).grid(row=index, column=0, sticky="n", padx=(0, 12), pady=7)
            tk.Label(
                help_body,
                text=text,
                bg="#f7fbff",
                fg=COLORS["ink"],
                anchor="w",
                justify="left",
                wraplength=280,
                font=("TkDefaultFont", 10),
            ).grid(row=index, column=1, sticky="ew", pady=7)

        security_row = len(steps) + 1
        tk.Frame(help_body, bg=COLORS["line"], height=1).grid(
            row=security_row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(24, 18),
        )
        tk.Label(
            help_body,
            text="🔒  Security",
            bg="#f7fbff",
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=security_row + 1, column=0, columnspan=2, sticky="ew")
        tk.Label(
            help_body,
            text="Your password is stored locally on this computer and is never sent anywhere except your FreeCloud server.",
            bg="#f7fbff",
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=310,
            font=("TkDefaultFont", 10),
        ).grid(row=security_row + 2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        return card

    def setup_entry(self, parent: tk.Widget, variable: tk.StringVar, show: str = "") -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            highlightcolor=COLORS["accent"],
            bd=0,
            bg="#ffffff",
            fg=COLORS["ink"],
            insertbackground=COLORS["ink"],
            font=("TkDefaultFont", 11),
        )

    def browse_local_folder(self) -> None:
        initial = Path(self.local_var.get()).expanduser()
        selected = filedialog.askdirectory(
            title="Choose Local FreeCloud Folder",
            initialdir=str(initial if initial.is_dir() else initial.parent),
        )
        if selected:
            self.local_var.set(selected)

    def on_website_focus_in(self, _event: tk.Event[tk.Misc]) -> None:
        if self.domain_var.get() == self.domain_placeholder:
            self.domain_var.set("")
            self.website_entry.configure(fg=COLORS["ink"])

    def on_website_focus_out(self, _event: tk.Event[tk.Misc]) -> None:
        if not self.domain_var.get().strip():
            self.domain_var.set(self.domain_placeholder)
            self.website_entry.configure(fg="#8a9aaa")

    def toggle_password_visibility(self) -> None:
        self.password_visible = not self.password_visible
        self.password_entry.configure(show="" if self.password_visible else "*")
        self.password_toggle_button.configure(text="Hide" if self.password_visible else "Show")

    def build_summary_card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        card.pack(fill=tk.BOTH, expand=False, pady=(0, 14))

        tk.Label(
            card,
            text="Saved Setup",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            font=("TkDefaultFont", 14, "bold"),
            padx=18,
            pady=16,
        ).pack(anchor="w")

        summary = tk.Frame(card, bg=COLORS["panel_alt"], highlightthickness=1, highlightbackground=COLORS["line"])
        summary.pack(fill=tk.X, padx=18, pady=(0, 18))

        rows = [
            ("Drive", self.saved_drive_var),
            ("Server", self.saved_server_var),
            ("Local Folder", self.saved_local_var),
        ]

        for label_text, value_var in rows:
            row = tk.Frame(summary, bg=COLORS["panel_alt"])
            row.pack(fill=tk.X, anchor="w", padx=18, pady=6)
            row.grid_columnconfigure(1, weight=1)

            tk.Label(
                row,
                text=f"{label_text}:",
                bg=COLORS["panel_alt"],
                fg=COLORS["ink"],
                font=("TkDefaultFont", 11, "bold"),
                anchor="w",
                width=12,
            ).grid(row=0, column=0, sticky="w")

            if label_text == "Server":
                server_entry = tk.Entry(
                    row,
                    textvariable=value_var,
                    relief="flat",
                    bd=0,
                    readonlybackground=COLORS["panel_alt"],
                    bg=COLORS["panel_alt"],
                    fg=COLORS["ink"],
                    highlightthickness=0,
                    insertbackground=COLORS["ink"],
                    font=("TkDefaultFont", 10),
                )
                server_entry.grid(row=0, column=1, sticky="ew", padx=10)
                server_entry.configure(state="readonly")
            else:
                tk.Label(
                    row,
                    textvariable=value_var,
                    bg=COLORS["panel_alt"],
                    fg=COLORS["ink"],
                    anchor="w",
                    justify="left",
                    wraplength=620,
                    padx=10,
                ).grid(row=0, column=1, sticky="w")
        return card

    def build_files_card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=0)
        card.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        body = tk.Frame(card, bg=COLORS["panel"])
        body.pack(fill=tk.BOTH, expand=True, padx=4, pady=(8, 6))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        browser_panes = tk.PanedWindow(
            body,
            orient=tk.HORIZONTAL,
            bg=COLORS["panel"],
            bd=0,
            relief="flat",
            sashwidth=6,
            sashrelief="flat",
            showhandle=False,
            opaqueresize=True,
        )
        browser_panes.grid(row=0, column=0, sticky="nsew")

        sidebar = tk.Frame(browser_panes, bg="#fbfdff", highlightthickness=0, bd=0)
        sidebar.grid_rowconfigure(1, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        tk.Label(
            sidebar,
            text="☁  FreeCloud",
            bg="#fbfdff",
            fg=COLORS["ink"],
            anchor="w",
            padx=14,
            pady=12,
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=0, column=0, sticky="ew")

        tree_shell = tk.Frame(sidebar, bg="#fbfdff")
        tree_shell.grid(row=1, column=0, sticky="nsew", padx=(6, 0))
        tree_shell.grid_rowconfigure(0, weight=1)
        tree_shell.grid_columnconfigure(0, weight=1)

        tree_style = ttk.Style(self.root)
        tree_style.configure(
            "FreeCloud.Treeview",
            background="#fbfdff",
            fieldbackground="#fbfdff",
            foreground=COLORS["ink"],
            borderwidth=0,
            relief="flat",
            rowheight=30,
            font=("TkDefaultFont", 10),
        )
        tree_style.map(
            "FreeCloud.Treeview",
            background=[("selected", "#dceeff")],
            foreground=[("selected", COLORS["accent_dark"])],
        )
        self.remote_tree = ttk.Treeview(
            tree_shell,
            show="tree",
            selectmode="browse",
            style="FreeCloud.Treeview",
            padding=(4, 4),
        )
        self.remote_tree.grid(row=0, column=0, sticky="nsew")
        self.remote_tree_scrollbar = styled_scrollbar(tree_shell, tk.VERTICAL, self.remote_tree.yview, width=12)
        self.remote_tree_scrollbar_visible = False
        self.remote_tree.configure(yscrollcommand=self.on_remote_tree_scroll)
        self.remote_tree.bind("<Configure>", self.update_remote_tree_scrollbar, add="+")
        self.remote_tree.bind("<<TreeviewSelect>>", self.on_remote_tree_select)
        self.remote_tree.bind("<ButtonPress-1>", self.on_remote_tree_button_press)
        self.remote_tree.bind("<B1-Motion>", self.on_remote_tree_drag_motion)
        self.remote_tree.bind("<ButtonRelease-1>", self.on_remote_tree_button_release)

        tk.Frame(sidebar, bg=COLORS["line"], height=1).grid(row=2, column=0, sticky="ew", padx=10)
        tk.Label(
            sidebar,
            text="♲  Trash",
            bg="#fbfdff",
            fg=COLORS["muted"],
            anchor="w",
            padx=14,
            pady=12,
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=3, column=0, sticky="ew")

        storage = tk.Frame(sidebar, bg="#fbfdff")
        storage.grid(row=4, column=0, sticky="ew", padx=14, pady=(4, 14))
        storage.grid_columnconfigure(0, weight=1)
        tk.Label(
            storage,
            text="Storage",
            bg="#fbfdff",
            fg=COLORS["ink"],
            anchor="w",
            font=("TkDefaultFont", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.storage_bar = tk.Canvas(storage, height=8, bg="#fbfdff", highlightthickness=0, bd=0)
        self.storage_bar.grid(row=1, column=0, sticky="ew")
        self.storage_bar.bind("<Configure>", self.draw_storage_bar)
        tk.Label(
            storage,
            textvariable=self.storage_detail_var,
            bg="#fbfdff",
            fg=COLORS["muted"],
            anchor="w",
            font=("TkDefaultFont", 9),
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        main = tk.Frame(body, bg=COLORS["panel"])
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        browser_panes.add(sidebar, minsize=180, width=238, stretch="never")
        browser_panes.add(main, minsize=560, stretch="always")

        toolbar = tk.Frame(main, bg=COLORS["panel"])
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        toolbar.grid_columnconfigure(3, weight=1)

        self.back_button = self.nav_bubble_button(toolbar, "←", self.go_back_remote_folder)
        self.back_button.grid(row=0, column=0, padx=(0, 3))
        self.up_button = self.nav_bubble_button(toolbar, "↑", self.go_up_remote_folder, font_size=16)
        self.up_button.grid(row=0, column=1, padx=(0, 3))
        self.forward_button = self.nav_bubble_button(toolbar, "→", self.go_forward_remote_folder)
        self.forward_button.grid(row=0, column=2, padx=(0, 8))

        self.path_entry = tk.Entry(
            toolbar,
            textvariable=self.remote_path_var,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            highlightcolor=COLORS["accent"],
            bg="#ffffff",
            fg=COLORS["ink"],
            font=("TkDefaultFont", 11),
        )
        self.path_entry.grid(row=0, column=3, sticky="ew", ipady=7, padx=(0, 12))
        self.path_entry.bind("<Return>", self.go_to_path_from_entry)

        self.toolbar_button(toolbar, "📁  New Folder", self.create_remote_folder, 118).grid(row=0, column=4, padx=(0, 6))
        self.toolbar_button(toolbar, "↑  Upload", self.upload_remote_files, 100).grid(row=0, column=5, padx=(0, 6))
        self.toolbar_button(toolbar, "☷  ˅", self.show_files_menu, 62).grid(row=0, column=6)

        list_shell = tk.Frame(main, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        list_shell.grid(row=1, column=0, sticky="nsew")
        list_shell.grid_rowconfigure(0, weight=1)
        list_shell.grid_columnconfigure(0, weight=1)

        file_style = ttk.Style(self.root)
        file_style.configure(
            "FreeCloud.Files.Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["ink"],
            borderwidth=0,
            relief="flat",
            rowheight=40,
            font=("TkDefaultFont", 10),
        )
        file_style.configure(
            "FreeCloud.Files.Treeview.Heading",
            background="#f8fbff",
            foreground=COLORS["ink"],
            relief="flat",
            borderwidth=0,
            font=("TkDefaultFont", 10, "bold"),
        )
        file_style.map(
            "FreeCloud.Files.Treeview",
            background=[("selected", "#e4f1ff")],
            foreground=[("selected", COLORS["ink"])],
        )
        self.files_tree = ttk.Treeview(
            list_shell,
            columns=("name", "size", "type", "modified"),
            show="headings",
            selectmode="browse",
            style="FreeCloud.Files.Treeview",
        )
        self.files_tree.heading("name", text="Name", anchor="w")
        self.files_tree.heading("size", text="Size", anchor="w")
        self.files_tree.heading("type", text="Type", anchor="w")
        self.files_tree.heading("modified", text="Modified", anchor="w")
        self.files_tree.column("name", width=330, minwidth=180, stretch=True, anchor="w")
        self.files_tree.column("size", width=90, minwidth=70, stretch=False, anchor="w")
        self.files_tree.column("type", width=140, minwidth=100, stretch=False, anchor="w")
        self.files_tree.column("modified", width=190, minwidth=150, stretch=False, anchor="w")
        self.files_tree.grid(row=0, column=0, sticky="nsew")
        self.files_tree.bind("<ButtonPress-1>", self.on_file_tree_button_press)
        self.files_tree.bind("<B1-Motion>", self.on_file_tree_drag_motion)
        self.files_tree.bind("<ButtonRelease-1>", self.on_file_tree_button_release)
        self.files_tree.bind("<Button-3>", self.show_file_tree_item_menu)

        files_scrollbar = styled_scrollbar(list_shell, tk.VERTICAL, self.files_tree.yview, width=16)
        files_scrollbar.grid(row=0, column=1, sticky="ns")
        self.files_tree.configure(yscrollcommand=files_scrollbar.set)

        self.files_footer_var = tk.StringVar(value="")
        footer = tk.Frame(card, bg=COLORS["panel"])
        footer.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Label(footer, textvariable=self.files_footer_var, bg=COLORS["panel"], fg=COLORS["muted"], font=("TkDefaultFont", 9)).pack(side=tk.LEFT)

        self.render_remote_tree()
        return card

    def tighten_card_header(self, card: tk.Frame) -> None:
        children = card.winfo_children()
        if len(children) < 2:
            return
        helper = children[1]
        if isinstance(helper, tk.Label):
            helper.configure(pady=0)

    def build_actions_card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bg="#fbfdff", highlightthickness=0)
        card.pack(fill=tk.X, pady=(2, 5))
        button_width = 152
        button_gap = 8

        self.open_local_button = self.make_action_button(
            card,
            "🗂\nOpen Local Folder",
            self.open_local_folder,
            "#fbfdff",
            COLORS["ink"],
            width=button_width,
            height=60,
        )
        self.open_local_button.pack(side=tk.LEFT, padx=(0, button_gap))

        self.local_folder_button = self.make_action_button(
            card,
            "📁\nNew Folder",
            self.create_local_folder,
            "#fbfdff",
            COLORS["ink"],
            width=button_width,
            height=60,
        )
        self.local_folder_button.pack(side=tk.LEFT, padx=(0, button_gap))

        self.view_web_button = self.make_action_button(
            card,
            "🌐\nView on Web",
            self.view_on_web,
            "#fbfdff",
            COLORS["ink"],
            width=button_width,
            height=60,
        )
        self.view_web_button.pack(side=tk.LEFT, padx=(0, button_gap))

        self.sync_button = self.make_action_button(
            card,
            "▶\nStart Sync",
            self.toggle_sync,
            "#fbfdff",
            COLORS["success"],
            width=button_width,
            height=60,
        )
        self.sync_button.configure(textvariable=self.sync_button_text)
        self.sync_button.pack(side=tk.LEFT, padx=(0, button_gap))

        self.settings_button = self.make_action_button(
            card,
            "⚙\nSettings",
            self.edit_settings,
            "#fbfdff",
            COLORS["ink"],
            width=button_width,
            height=60,
        )
        self.settings_button.pack(side=tk.LEFT, padx=(0, button_gap))

        self.exit_button = self.make_action_button(
            card,
            "▣\nRun in Background",
            self.exit_keep_syncing,
            "#fbfdff",
            COLORS["ink"],
            width=button_width,
            height=60,
        )
        self.exit_button.pack(side=tk.LEFT)

        return card

    def build_output_card(self, parent: tk.Widget) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        card.pack(fill=tk.X, pady=(0, 0))
        tk.Label(card, text="Sync Activity", bg=COLORS["panel"], fg=COLORS["ink"], font=("TkDefaultFont", 10, "bold"), padx=12, pady=8).pack(anchor="w")
        shell = tk.Frame(card, bg=COLORS["console"], highlightthickness=1, highlightbackground="#224763")
        shell.pack(fill=tk.X, padx=12, pady=(0, 12))

        output_wrap = tk.Frame(shell, bg=COLORS["console"])
        output_wrap.pack(fill=tk.X, expand=False, padx=10, pady=10)

        self.output = tk.Text(
            output_wrap,
            wrap="word",
            height=5,
            state=tk.DISABLED,
            relief="flat",
            bd=0,
            bg=COLORS["console"],
            fg=COLORS["console_ink"],
            insertbackground=COLORS["console_ink"],
            selectbackground="#3e7daf",
            font=("TkFixedFont", 11),
        )
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(output_wrap, command=self.output.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.output.configure(yscrollcommand=scrollbar.set)

        return card

    def card(self, parent: tk.Widget, title: str, body: str) -> tk.Frame:
        card = tk.Frame(parent, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        card.pack(fill=tk.BOTH, expand=False, pady=(0, 14))

        tk.Label(
            card,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            font=("TkDefaultFont", 15, "bold"),
            padx=18,
            pady=15,
        ).pack(anchor="w")

        tk.Label(
            card,
            text=body,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=780,
            padx=18,
            font=("TkDefaultFont", 10),
        ).pack(anchor="w", fill=tk.X, pady=(0, 14))

        return card

    def make_action_button(
        self,
        parent: tk.Widget,
        text: str,
        command: object,
        bg: str,
        fg: str,
        state: str = tk.NORMAL,
        width: int = 146,
        height: int = 42,
    ) -> SoftButton:
        return SoftButton(
            parent,
            text=text,
            command=command,
            state=state,
            bg=bg,
            fg=fg,
            activebackground=COLORS["accent_dark"] if bg == COLORS["accent"] else COLORS["button_light_hover"],
            disabledforeground="#8aa3b8",
            highlightthickness=1 if bg == COLORS["accent"] else 0,
            highlightbackground=COLORS["accent"] if bg == COLORS["accent"] else str(parent.cget("bg")),
            highlightcolor=COLORS["accent"] if bg == COLORS["accent"] else str(parent.cget("bg")),
            border=COLORS["accent"] if bg == COLORS["accent"] else "",
            width=width,
            height=height,
            radius=13,
        )

    def toolbar_button(self, parent: tk.Widget, text: str, command: object, width: int) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command if callable(command) else None,
            width=max(2, width // 10),
            relief="flat",
            bd=0,
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            activebackground=COLORS["button_light_hover"],
            activeforeground=COLORS["accent_dark"],
            cursor="hand2",
            font=("TkDefaultFont", 10, "bold"),
            padx=8,
            pady=8,
        )

    def nav_bubble_button(self, parent: tk.Widget, text: str, command: object, font_size: int | None = None) -> SoftButton:
        return SoftButton(
            parent,
            text=text,
            command=command,
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            activebackground=COLORS["button_light_hover"],
            highlightbackground=COLORS["panel"],
            disabledforeground="#9fb5c7",
            width=40,
            height=38,
            radius=8,
            font_size=font_size,
        )

    def draw_clouds(self, canvas: tk.Canvas) -> None:
        cloud_color = "#f9fdff"
        shadow = "#b7d7ef"
        canvas.create_oval(18, 54, 78, 94, fill=shadow, outline="")
        canvas.create_oval(54, 38, 122, 98, fill=shadow, outline="")
        canvas.create_oval(98, 54, 166, 96, fill=shadow, outline="")
        canvas.create_rectangle(46, 66, 136, 96, fill=shadow, outline="")

        canvas.create_oval(12, 46, 72, 86, fill=cloud_color, outline="")
        canvas.create_oval(50, 28, 118, 90, fill=cloud_color, outline="")
        canvas.create_oval(94, 46, 162, 88, fill=cloud_color, outline="")
        canvas.create_rectangle(40, 58, 132, 88, fill=cloud_color, outline="")

        canvas.create_oval(138, 26, 180, 54, fill="#f4fbff", outline="")
        canvas.create_oval(164, 18, 214, 62, fill="#f4fbff", outline="")
        canvas.create_oval(196, 30, 238, 56, fill="#f4fbff", outline="")
        canvas.create_rectangle(160, 38, 208, 58, fill="#f4fbff", outline="")

    def refresh_setup_visibility(self) -> None:
        self.files_frame.pack_forget()
        self.setup_frame.pack_forget()
        has_saved_setup = bool(self.config_data())
        settings_selected = not has_saved_setup or self.editing_settings
        if settings_selected:
            self.header_setup_frame.grid_remove()
        else:
            self.header_setup_frame.grid()
        if has_saved_setup and self.editing_settings:
            self.settings_button.command = self.show_dashboard
            self.settings_button.configure(text="▦\nDashboard")
        else:
            self.settings_button.command = self.edit_settings
            self.settings_button.configure(text="⚙\nSettings")
        self.settings_button.configure(
            bg="#e5f1ff" if settings_selected else "#fbfdff",
            fg=COLORS["accent_dark"] if settings_selected else COLORS["ink"],
            activebackground="#d9eaff" if settings_selected else COLORS["button_light_hover"],
            highlightbackground="#e5f1ff" if settings_selected else "#fbfdff",
            highlightcolor="#e5f1ff" if settings_selected else "#fbfdff",
        )

        if has_saved_setup and not self.editing_settings:
            self.setup_frame.pack_forget()
            if self.file_browser_open:
                self.files_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
            self.root.after_idle(self.update_canvas_window_height)
            return

        if has_saved_setup and self.editing_settings:
            self.setup_frame.pack(fill=tk.X, pady=(0, 14), after=self.actions_frame)
            self.root.after_idle(self.update_canvas_window_height)
            return

        self.file_browser_open = True
        self.setup_frame.pack(fill=tk.X, pady=(0, 14), after=self.actions_frame)
        self.root.after_idle(self.update_canvas_window_height)

    def update_summary_text(self, config: dict[str, object]) -> None:
        base_url = str(config.get("base_url") or "")
        local_root = str(config.get("local_root") or "")
        drive_name = str(config.get("drive_name") or "")
        self.saved_drive_var.set(drive_name or "FreeCloud")
        self.saved_server_var.set(base_url or "Not saved yet")
        self.saved_local_var.set(local_root or "Not saved yet")

    def config_data(self) -> dict[str, object]:
        config = load_last_config()
        if str(config.get("mode") or "") == "self_host":
            return {}
        return config

    def current_client(self) -> cli.FreeCloudClient:
        config = self.config_data()
        base_url = str(config.get("base_url") or "")
        if not base_url:
            raise RuntimeError("Save setup before browsing cloud files.")
        return cli.FreeCloudClient(base_url, str(config.get("password") or ""))

    def load_remote_tree_cache(self) -> bool:
        data = load_json(REMOTE_TREE_CACHE_PATH)
        config = self.config_data()
        if not data:
            return False
        if str(data.get("base_url") or "") != str(config.get("base_url") or ""):
            return False
        entries = data.get("entries")
        if not isinstance(entries, list):
            return False
        self.remote_manifest_cache = [entry for entry in entries if isinstance(entry, dict)]
        return bool(self.remote_manifest_cache)

    def cached_remote_tree_entries(self, base_url: str) -> list[dict[str, object]]:
        data = load_json(REMOTE_TREE_CACHE_PATH)
        if not data:
            return []
        if str(data.get("base_url") or "") != base_url:
            return []
        entries = data.get("entries")
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def load_cached_files_after_startup(self) -> None:
        startup_log("cache load scheduled task started")
        base_url = str(self.config_data().get("base_url") or "")
        if not base_url:
            startup_log("cache load skipped: no base_url")
            return

        def worker() -> None:
            started_at = time.perf_counter()
            entries = self.cached_remote_tree_entries(base_url)
            elapsed = time.perf_counter() - started_at
            startup_log(f"cache file read on worker, entries={len(entries)}, elapsed={elapsed:.3f}s")
            self.root.after(0, lambda: self.finish_cached_files_after_startup(entries))

        threading.Thread(target=worker, daemon=True).start()

    def finish_cached_files_after_startup(self, entries: list[dict[str, object]]) -> None:
        startup_log(f"cache load finish on UI thread, entries={len(entries)}")
        if entries:
            self.remote_manifest_cache = entries
            self.update_storage_display(None)
            self.render_cached_files_on_startup()
            self.schedule_folder_refresh(5000)
            self.schedule_storage_refresh()
            return
        self.files_empty_var.set("Loading cloud files...")
        self.schedule_folder_refresh(1500)

    def save_remote_tree_cache(self) -> None:
        config = self.config_data()
        cli.save_json(
            REMOTE_TREE_CACHE_PATH,
            {
                "saved_at": int(datetime.now().timestamp()),
                "base_url": str(config.get("base_url") or ""),
                "drive_name": str(config.get("drive_name") or "FreeCloud"),
                "entries": self.remote_manifest_cache,
            },
        )

    def entries_for_folder(self, folder_path: str) -> list[dict[str, object]]:
        clean_folder = cli.remote_path(folder_path)
        prefix = f"{clean_folder}/" if clean_folder else ""
        entries: list[dict[str, object]] = []
        for entry in self.remote_manifest_cache:
            path = cli.remote_path(str(entry.get("path") or ""))
            if not path:
                continue
            if clean_folder:
                if not path.startswith(prefix):
                    continue
                remainder = path[len(prefix):]
            else:
                remainder = path
            if remainder and "/" not in remainder:
                entries.append(entry)
        return sorted(entries, key=lambda item: (str(item.get("type") or "") != "dir", str(item.get("name") or item.get("path") or "").lower()))

    def render_remote_tree(self) -> None:
        tree = getattr(self, "remote_tree", None)
        if tree is None:
            return

        folder_paths: set[str] = set()
        for entry in self.remote_manifest_cache:
            path = cli.remote_path(str(entry.get("path") or ""))
            if not path:
                continue
            parts = path.split("/")
            limit = len(parts) if str(entry.get("type") or "") == "dir" else len(parts) - 1
            for index in range(1, limit + 1):
                folder_paths.add("/".join(parts[:index]))

        drive_name = str(self.config_data().get("drive_name") or "FreeCloud")
        sorted_folder_paths = sorted(folder_paths, key=lambda path: (path.count("/"), path.lower()))
        signature = (drive_name, *sorted_folder_paths)
        if signature == self.remote_tree_signature and self.remote_tree_items:
            self.select_current_remote_tree_item()
            return

        open_paths = {
            path
            for item_id, path in self.remote_tree_paths.items()
            if tree.exists(item_id) and bool(tree.item(item_id, "open"))
        }
        self.remote_tree_rendering = True
        try:
            root_items = tree.get_children("")
            if root_items:
                tree.delete(*root_items)
            self.remote_tree_paths = {}
            self.remote_tree_items = {}
            root_id = tree.insert("", "end", text=f"☁  {drive_name}", open=True)
            self.remote_tree_paths[root_id] = ""
            self.remote_tree_items[""] = root_id
            path_to_id = {"": root_id}

            for folder_path in sorted_folder_paths:
                parent_path, _, name = folder_path.rpartition("/")
                parent_id = path_to_id.get(parent_path, root_id)
                item_id = tree.insert(
                    parent_id,
                    "end",
                    text=f"📁  {name}",
                    open=folder_path in open_paths or self.current_remote_path.startswith(folder_path + "/"),
                )
                path_to_id[folder_path] = item_id
                self.remote_tree_paths[item_id] = folder_path
                self.remote_tree_items[folder_path] = item_id

            self.remote_tree_signature = signature
            self.select_current_remote_tree_item()
        finally:
            self.remote_tree_rendering = False
            self.root.after_idle(self.update_remote_tree_scrollbar)

    def select_current_remote_tree_item(self) -> None:
        tree = getattr(self, "remote_tree", None)
        if tree is None:
            return
        selected_id = self.remote_tree_items.get(self.current_remote_path)
        if selected_id is None or not tree.exists(selected_id):
            return

        parent_id = tree.parent(selected_id)
        while parent_id:
            tree.item(parent_id, open=True)
            parent_id = tree.parent(parent_id)

        self.remote_tree_rendering = True
        try:
            if tree.selection() != (selected_id,):
                tree.selection_set(selected_id)
            tree.focus(selected_id)
            tree.see(selected_id)
        finally:
            self.remote_tree_rendering = False
        self.root.after_idle(self.update_remote_tree_scrollbar)

    def on_remote_tree_scroll(self, first: str, last: str) -> None:
        self.remote_tree_scrollbar.set(first, last)
        self.update_remote_tree_scrollbar(first=first, last=last)

    def update_remote_tree_scrollbar(
        self,
        _event: tk.Event[tk.Misc] | None = None,
        *,
        first: str | None = None,
        last: str | None = None,
    ) -> None:
        scrollbar = getattr(self, "remote_tree_scrollbar", None)
        tree = getattr(self, "remote_tree", None)
        if scrollbar is None or tree is None:
            return
        if first is None or last is None:
            first, last = tree.yview()
        needs_scrollbar = float(first) > 0.0 or float(last) < 1.0
        if needs_scrollbar and not self.remote_tree_scrollbar_visible:
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.remote_tree_scrollbar_visible = True
        elif not needs_scrollbar and self.remote_tree_scrollbar_visible:
            scrollbar.grid_remove()
            self.remote_tree_scrollbar_visible = False

    def on_remote_tree_select(self, _event: tk.Event[tk.Misc]) -> None:
        if self.remote_tree_rendering:
            return
        selected = self.remote_tree.selection()
        if not selected:
            return
        path = self.remote_tree_paths.get(selected[0])
        if path is not None and path != self.current_remote_path:
            self.navigate_remote_folder(path)

    def render_current_folder_from_cache(self) -> bool:
        if not self.remote_manifest_cache:
            self.render_remote_tree()
            return False
        started_at = time.perf_counter()
        self.remote_path_var.set(f"/{self.current_remote_path}" if self.current_remote_path else "/")
        self.up_button.configure(state=tk.NORMAL if self.current_remote_path else tk.DISABLED)
        self.back_button.configure(state=tk.NORMAL if self.remote_back_stack else tk.DISABLED)
        self.forward_button.configure(state=tk.NORMAL if self.remote_forward_stack else tk.DISABLED)
        self.render_remote_entries(self.entries_for_folder(self.current_remote_path))
        self.render_remote_tree()
        startup_log(
            f"cached folder rendered: /{self.current_remote_path}, "
            f"elapsed={time.perf_counter() - started_at:.3f}s"
        )
        return True

    def view_on_web(self) -> None:
        config = self.config_data()
        base_url = str(config.get("base_url") or "").strip()
        if not base_url:
            messagebox.showinfo("View on Web", "Save setup before opening the web drive.")
            return
        try:
            self.open_url_in_default_browser(base_url)
        except Exception as exc:
            messagebox.showerror("View on Web", str(exc))

    def open_local_folder(self) -> None:
        config = self.config_data()
        local_root = str(config.get("local_root") or "").strip()
        if not local_root:
            messagebox.showinfo("Open Local Folder", "Save setup before opening the local folder.")
            return

        path = Path(local_root).expanduser()
        if not path.exists():
            messagebox.showerror("Open Local Folder", f"Local folder was not found:\n{path}")
            return

        try:
            self.open_path_with_default_app(path)
        except Exception as exc:
            messagebox.showerror("Open Local Folder", str(exc))

    def create_local_folder(self) -> None:
        config = self.config_data()
        local_root = str(config.get("local_root") or "").strip()
        if not local_root:
            messagebox.showinfo("New Folder", "Save setup before creating a local folder.")
            return

        name = self.simple_text_prompt("New Folder", "Folder name")
        if not name:
            return
        if name in {".", ".."} or Path(name).name != name:
            messagebox.showerror("New Folder", "Use a folder name without slashes.")
            return

        try:
            root = Path(local_root).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            folder = root / name
            folder.mkdir()
            self.append(f"Created local folder: {folder}\n")
        except FileExistsError:
            messagebox.showerror("New Folder", f"A file or folder named {name} already exists.")
        except Exception as exc:
            messagebox.showerror("New Folder", str(exc))

    def toggle_files_browser(self) -> None:
        if not self.config_data():
            messagebox.showinfo("FreeCloud Files", "Save setup before browsing cloud files.")
            return

        self.file_browser_open = not self.file_browser_open
        self.refresh_setup_visibility()
        if self.file_browser_open:
            self.schedule_files_refresh()

    def schedule_files_refresh(self, delay_ms: int = 180) -> None:
        self.root.after(delay_ms, self.refresh_files)

    def render_cached_files_on_startup(self) -> None:
        startup_log("cached render started")
        started_at = time.perf_counter()
        if self.render_current_folder_from_cache() and not self.rendered_cache_notice:
            elapsed = time.perf_counter() - started_at
            self.append(f"Loaded cached cloud file list in {elapsed:.2f}s.\n")
            self.rendered_cache_notice = True
            startup_log(f"cached render queued, elapsed={elapsed:.3f}s")

    def schedule_folder_refresh(self, delay_ms: int = 0) -> None:
        if self.remote_manifest_refreshing or self.remote_manifest_refresh_after_id is not None:
            return
        self.remote_manifest_refresh_after_id = self.root.after(
            delay_ms,
            lambda: self.refresh_current_folder_in_background(show_errors=False),
        )

    def refresh_files(self, force_live: bool = False) -> None:
        loaded_from_cache = self.render_current_folder_from_cache()
        if not loaded_from_cache and self.load_remote_tree_cache():
            loaded_from_cache = self.render_current_folder_from_cache()
            if loaded_from_cache and not self.rendered_cache_notice:
                self.append("Loaded cached cloud file list.\n")
                self.rendered_cache_notice = True

        if force_live or not loaded_from_cache:
            self.refresh_current_folder_in_background(show_errors=not loaded_from_cache)
        else:
            self.schedule_folder_refresh(2500)

    def refresh_current_folder_in_background(self, show_errors: bool = False) -> None:
        if self.remote_manifest_refreshing:
            return
        self.remote_manifest_refresh_after_id = None
        self.remote_manifest_refreshing = True
        self.remote_manifest_refresh_started_at = time.perf_counter()
        folder_path = self.current_remote_path
        had_cached_entries = bool(self.entries_for_folder(folder_path))
        startup_log(f"live folder refresh started: /{folder_path}")

        def worker() -> None:
            try:
                client = self.current_client()
                entries = client.list(folder_path)
            except Exception as exc:
                self.root.after(0, lambda: self.finish_folder_refresh_error(exc, show_errors))
                return
            self.root.after(0, lambda: self.finish_folder_refresh(folder_path, entries, had_cached_entries, show_errors))

        threading.Thread(target=worker, daemon=True).start()

    def replace_cached_folder_entries(self, folder_path: str, entries: list[dict[str, object]]) -> None:
        clean_folder = cli.remote_path(folder_path)
        prefix = f"{clean_folder}/" if clean_folder else ""

        def is_direct_child(entry: dict[str, object]) -> bool:
            path = cli.remote_path(str(entry.get("path") or ""))
            if not path:
                return False
            if clean_folder:
                if not path.startswith(prefix):
                    return False
                remainder = path[len(prefix):]
            else:
                remainder = path
            return remainder != "" and "/" not in remainder

        kept = [entry for entry in self.remote_manifest_cache if not is_direct_child(entry)]
        seen = {cli.remote_path(str(entry.get("path") or "")) for entry in kept}
        for entry in entries:
            path = cli.remote_path(str(entry.get("path") or ""))
            if path and path not in seen:
                kept.append(entry)
                seen.add(path)
        self.remote_manifest_cache = kept

    def finish_folder_refresh(
        self,
        folder_path: str,
        entries: list[dict[str, object]],
        had_cached_entries: bool = False,
        show_errors: bool = False,
    ) -> None:
        self.remote_manifest_refreshing = False
        if had_cached_entries and not entries and not show_errors:
            elapsed = time.perf_counter() - self.remote_manifest_refresh_started_at if self.remote_manifest_refresh_started_at else 0
            startup_log(f"ignored empty background folder refresh, path=/{folder_path}, elapsed={elapsed:.3f}s")
            return
        self.replace_cached_folder_entries(folder_path, entries)
        if cli.remote_path(folder_path) == self.current_remote_path:
            self.render_current_folder_from_cache()
        elapsed = time.perf_counter() - self.remote_manifest_refresh_started_at if self.remote_manifest_refresh_started_at else 0
        self.append(f"Updated current folder in {elapsed:.2f}s.\n")
        startup_log(f"live folder refresh finished, path=/{folder_path}, entries={len(entries)}, elapsed={elapsed:.3f}s")
        threading.Thread(target=self.save_remote_tree_cache, daemon=True).start()
        self.schedule_storage_refresh()

    def finish_folder_refresh_error(self, exc: Exception, show_error: bool) -> None:
        self.remote_manifest_refreshing = False
        startup_log(f"live folder refresh error: {exc}")
        if show_error:
            messagebox.showerror("FreeCloud Files", str(exc))
        else:
            self.append(f"Could not refresh cloud file list: {exc}\n")

    def schedule_storage_refresh(self, minimum_interval: int = 300) -> None:
        if self.storage_refreshing:
            return
        if time.monotonic() - self.storage_last_refreshed_at < minimum_interval:
            return
        self.storage_refreshing = True

        def worker() -> None:
            storage: dict[str, object] | None = None
            try:
                response = self.current_client().storage()
                raw_storage = response.get("storage")
                if isinstance(raw_storage, dict):
                    storage = raw_storage
            except Exception:
                pass
            self.root.after(0, lambda: self.finish_storage_refresh(storage))

        threading.Thread(target=worker, daemon=True).start()

    def finish_storage_refresh(self, storage: dict[str, object] | None) -> None:
        self.storage_refreshing = False
        self.storage_last_refreshed_at = time.monotonic()
        self.update_storage_display(storage)

    def update_storage_display(self, storage: dict[str, object] | None) -> None:
        if storage is None:
            used_bytes = sum(
                int(entry.get("size") or 0)
                for entry in self.remote_manifest_cache
                if str(entry.get("type") or "") == "file"
            )
            self.storage_fraction = 0.0
            self.storage_detail_var.set(f"{self.format_bytes(used_bytes)} used")
            self.draw_storage_bar()
            return

        used_bytes = max(0, int(storage.get("used_bytes") or 0))
        capacity_value = storage.get("capacity_bytes")
        capacity_bytes = max(0, int(capacity_value or 0))
        self.storage_fraction = min(1.0, used_bytes / capacity_bytes) if capacity_bytes > 0 else 0.0
        if capacity_bytes > 0:
            percent = min(100, max(0, round(self.storage_fraction * 100)))
            self.storage_detail_var.set(
                f"{percent}%  ·  {self.format_bytes(used_bytes)} of {self.format_bytes(capacity_bytes)}"
            )
        else:
            self.storage_detail_var.set(f"{self.format_bytes(used_bytes)} used")
        self.draw_storage_bar()

    def draw_storage_bar(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        canvas = getattr(self, "storage_bar", None)
        if canvas is None:
            return
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#dfe8f1", outline="")
        fill_width = round(width * self.storage_fraction)
        if fill_width > 0:
            canvas.create_rectangle(0, 0, fill_width, height, fill=COLORS["accent"], outline="")

    def render_remote_entries(self, entries: list[dict[str, object]]) -> None:
        folder_key_parts = []
        for entry in entries:
            folder_key_parts.append(
                "|".join(
                    (
                        cli.remote_path(str(entry.get("path") or "")),
                        str(entry.get("type") or ""),
                        str(entry.get("size") or ""),
                        str(entry.get("mtime") or ""),
                    )
                )
            )
        folder_key = "\n".join(folder_key_parts)
        if folder_key == self.last_rendered_folder_key:
            return
        self.last_rendered_folder_key = folder_key

        existing_items = self.files_tree.get_children("")
        if existing_items:
            self.files_tree.delete(*existing_items)
        self.file_tree_entries = {}

        if not entries:
            self.files_empty_var.set("This cloud folder is empty.")
            self.files_tree.insert("", "end", values=("This cloud folder is empty.", "", "", ""))
            self.files_footer_var.set("0 items")
            return

        folder_count = sum(1 for entry in entries if str(entry.get("type") or "") == "dir")
        file_count = len(entries) - folder_count
        self.files_footer_var.set(f"{len(entries)} items,  {folder_count} folders, {file_count} files")
        for entry in entries:
            is_dir = str(entry.get("type") or "") == "dir"
            name = str(entry.get("name") or entry.get("path") or "")
            item_id = self.files_tree.insert(
                "",
                "end",
                values=(
                    f"{self.file_kind_badge(name, is_dir)}  {name}",
                    "—" if is_dir else self.format_bytes(int(entry.get("size") or 0)),
                    "Folder" if is_dir else self.file_type_label(name),
                    self.format_mtime(entry.get("mtime")),
                ),
            )
            self.file_tree_entries[item_id] = entry

    def on_file_tree_button_press(self, event: tk.Event[tk.Misc]) -> None:
        if self.files_tree.identify_region(event.x, event.y) != "cell":
            self.clear_remote_drag()
            return
        item_id = self.files_tree.identify_row(event.y)
        entry = self.file_tree_entries.get(item_id)
        if entry is None:
            return
        self.drag_source_entry = entry
        self.drag_source_item_id = item_id
        self.drag_start_xy = (event.x_root, event.y_root)
        self.drag_active = False
        self.drag_source_widget = self.files_tree

    def on_file_tree_drag_motion(self, event: tk.Event[tk.Misc]) -> None:
        self.on_remote_drag_motion(event)

    def on_remote_tree_button_press(self, event: tk.Event[tk.Misc]) -> None:
        item_id = self.remote_tree.identify_row(event.y)
        path = self.remote_tree_paths.get(item_id)
        if not path:
            self.clear_remote_drag()
            return
        self.drag_source_entry = {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "type": "dir",
        }
        self.drag_source_item_id = item_id
        self.drag_start_xy = (event.x_root, event.y_root)
        self.drag_active = False
        self.drag_source_widget = self.remote_tree

    def on_remote_tree_drag_motion(self, event: tk.Event[tk.Misc]) -> None:
        self.on_remote_drag_motion(event)

    def on_remote_drag_motion(self, event: tk.Event[tk.Misc]) -> None:
        if self.drag_source_entry is None or self.drag_start_xy is None:
            return
        start_x, start_y = self.drag_start_xy
        if abs(event.x_root - start_x) + abs(event.y_root - start_y) < 8:
            return
        if not self.drag_active:
            self.drag_active = True
            if self.drag_source_widget is not None:
                self.drag_source_widget.selection_set(self.drag_source_item_id)
                self.drag_source_widget.configure(cursor="fleur")

    def on_file_tree_button_release(self, event: tk.Event[tk.Misc]) -> None:
        self.finish_remote_drag(event, open_file_on_click=True)

    def on_remote_tree_button_release(self, event: tk.Event[tk.Misc]) -> None:
        self.finish_remote_drag(event, open_file_on_click=False)

    def finish_remote_drag(self, event: tk.Event[tk.Misc], open_file_on_click: bool) -> None:
        try:
            if self.drag_active and self.drag_source_entry is not None:
                target_folder = self.drop_target_folder(event.x_root, event.y_root)
                if target_folder is not None:
                    self.move_dragged_remote_item(self.drag_source_entry, target_folder)
                return
            if open_file_on_click:
                self.open_file_tree_item(event)
        finally:
            self.clear_remote_drag()

    def clear_remote_drag(self) -> None:
        source_widget = self.drag_source_widget
        self.drag_source_entry = None
        self.drag_source_item_id = ""
        self.drag_start_xy = None
        self.drag_active = False
        self.drag_source_widget = None
        if source_widget is not None and not self.remote_move_in_progress:
            source_widget.configure(cursor="")

    def open_file_tree_item(self, event: tk.Event[tk.Misc]) -> None:
        if self.files_tree.identify_region(event.x, event.y) != "cell":
            return
        item_id = self.files_tree.identify_row(event.y)
        entry = self.file_tree_entries.get(item_id)
        if entry is None:
            return
        path = str(entry.get("path") or "")
        name = str(entry.get("name") or path)
        if str(entry.get("type") or "") == "dir":
            self.open_remote_folder(path)
        else:
            self.open_local_file(path, name)

    def drop_target_folder(self, root_x: int, root_y: int) -> str | None:
        widget = self.root.winfo_containing(root_x, root_y)
        while widget is not None:
            if widget == self.remote_tree:
                local_y = root_y - self.remote_tree.winfo_rooty()
                item_id = self.remote_tree.identify_row(local_y)
                return self.remote_tree_paths.get(item_id)
            if widget == self.files_tree:
                local_y = root_y - self.files_tree.winfo_rooty()
                item_id = self.files_tree.identify_row(local_y)
                entry = self.file_tree_entries.get(item_id)
                if entry is not None and str(entry.get("type") or "") == "dir":
                    return cli.remote_path(str(entry.get("path") or ""))
                return self.current_remote_path
            widget = widget.master
        return None

    def move_dragged_remote_item(self, entry: dict[str, object], target_folder: str) -> None:
        if self.remote_move_in_progress:
            return
        source_path = cli.remote_path(str(entry.get("path") or ""))
        if not source_path:
            return
        name = source_path.rsplit("/", 1)[-1]
        clean_target_folder = cli.remote_path(target_folder)
        target_path = "/".join(part for part in (clean_target_folder, name) if part)
        source_parent = source_path.rpartition("/")[0]
        if target_path == source_path or clean_target_folder == source_parent:
            return
        if str(entry.get("type") or "") == "dir" and (
            clean_target_folder == source_path or clean_target_folder.startswith(source_path + "/")
        ):
            messagebox.showerror("Move Cloud Item", "Cannot move a folder into itself.")
            return
        client = self.current_client()
        self.remote_move_in_progress = True
        self.files_tree.configure(cursor="watch")
        self.remote_tree.configure(cursor="watch")

        def worker() -> None:
            try:
                client.move(source_path, target_path)
            except Exception as exc:
                self.root.after(0, lambda error=exc: self.finish_remote_move_error(error))
                return
            self.root.after(0, lambda: self.finish_remote_move(source_path, target_path))

        threading.Thread(target=worker, daemon=True).start()

    def finish_remote_move(self, source_path: str, target_path: str) -> None:
        self.remote_move_in_progress = False
        self.files_tree.configure(cursor="")
        self.remote_tree.configure(cursor="")
        self.apply_remote_move_to_cache(source_path, target_path)
        self.last_rendered_folder_key = ""
        self.render_current_folder_from_cache()
        self.append(f"Moved cloud item: {source_path} -> {target_path}\n")
        threading.Thread(target=self.save_remote_tree_cache, daemon=True).start()

    def finish_remote_move_error(self, exc: Exception) -> None:
        self.remote_move_in_progress = False
        self.files_tree.configure(cursor="")
        self.remote_tree.configure(cursor="")
        messagebox.showerror("Move Cloud Item", str(exc))

    def apply_remote_move_to_cache(self, source_path: str, target_path: str) -> None:
        source_path = cli.remote_path(source_path)
        target_path = cli.remote_path(target_path)
        updated = []
        for entry in self.remote_manifest_cache:
            path = cli.remote_path(str(entry.get("path") or ""))
            if path == source_path or path.startswith(source_path + "/"):
                suffix = path[len(source_path):]
                moved = dict(entry)
                moved["path"] = target_path + suffix
                if path == source_path:
                    moved["name"] = target_path.rsplit("/", 1)[-1]
                updated.append(moved)
            else:
                updated.append(entry)
        self.remote_manifest_cache = updated
        self.current_remote_path = self.rewrite_moved_path(
            self.current_remote_path, source_path, target_path
        )
        self.remote_back_stack = [
            self.rewrite_moved_path(path, source_path, target_path)
            for path in self.remote_back_stack
        ]
        self.remote_forward_stack = [
            self.rewrite_moved_path(path, source_path, target_path)
            for path in self.remote_forward_stack
        ]
        self.remote_tree_signature = ()

    @staticmethod
    def rewrite_moved_path(path: str, source_path: str, target_path: str) -> str:
        clean_path = cli.remote_path(path)
        if clean_path == source_path or clean_path.startswith(source_path + "/"):
            return target_path + clean_path[len(source_path):]
        return clean_path

    def show_file_tree_item_menu(self, event: tk.Event[tk.Misc]) -> None:
        item_id = self.files_tree.identify_row(event.y)
        entry = self.file_tree_entries.get(item_id)
        if entry is None:
            return
        self.files_tree.selection_set(item_id)
        path = str(entry.get("path") or "")
        name = str(entry.get("name") or path)
        self.show_remote_item_menu(path, name, str(entry.get("type") or "") == "dir")

    def open_remote_folder(self, path: str) -> None:
        self.navigate_remote_folder(path)

    def navigate_remote_folder(self, path: str, remember_history: bool = True) -> None:
        clean_path = cli.remote_path(path)
        if remember_history and clean_path != self.current_remote_path:
            self.remote_back_stack.append(self.current_remote_path)
            self.remote_forward_stack.clear()
        self.current_remote_path = clean_path
        self.last_rendered_folder_key = ""
        if not self.render_current_folder_from_cache():
            self.refresh_files(force_live=True)

    def go_up_remote_folder(self) -> None:
        if not self.current_remote_path:
            return
        parts = self.current_remote_path.split("/")
        self.navigate_remote_folder("/".join(parts[:-1]))

    def go_back_remote_folder(self) -> None:
        if not self.remote_back_stack:
            return
        self.remote_forward_stack.append(self.current_remote_path)
        self.current_remote_path = self.remote_back_stack.pop()
        self.last_rendered_folder_key = ""
        if not self.render_current_folder_from_cache():
            self.refresh_files(force_live=True)

    def go_forward_remote_folder(self) -> None:
        if not self.remote_forward_stack:
            return
        self.remote_back_stack.append(self.current_remote_path)
        self.current_remote_path = self.remote_forward_stack.pop()
        self.last_rendered_folder_key = ""
        if not self.render_current_folder_from_cache():
            self.refresh_files(force_live=True)

    def go_to_path_from_entry(self, _event: tk.Event[tk.Misc]) -> None:
        self.navigate_remote_folder(self.remote_path_var.get())

    def create_remote_folder(self) -> None:
        if not self.config_data():
            messagebox.showinfo("New Folder", "Save setup before creating cloud folders.")
            return
        name = self.simple_text_prompt("New Folder", "Folder name")
        if not name:
            return
        target = "/".join(part for part in (self.current_remote_path, name) if part)
        try:
            self.current_client().mkdir(target)
            self.append(f"Created remote folder: {target}\n")
            self.refresh_files(force_live=True)
        except Exception as exc:
            messagebox.showerror("New Folder", str(exc))

    def simple_text_prompt(self, title: str, label: str) -> str:
        value = simpledialog.askstring(title, label, parent=self.root)
        return "" if value is None else value.strip()

    def upload_remote_files(self) -> None:
        if not self.config_data():
            messagebox.showinfo("Upload", "Save setup before uploading cloud files.")
            return
        filenames = filedialog.askopenfilenames(title="Upload Files to FreeCloud")
        if not filenames:
            return
        client = self.current_client()
        uploaded = 0
        try:
            for filename in filenames:
                local_file = Path(filename)
                target = "/".join(part for part in (self.current_remote_path, local_file.name) if part)
                client.upload(local_file, target)
                uploaded += 1
            self.append(f"Uploaded {uploaded} file(s) to /{self.current_remote_path}\n")
            self.refresh_files(force_live=True)
        except Exception as exc:
            messagebox.showerror("Upload", str(exc))

    def show_files_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Refresh", command=lambda: self.refresh_files(force_live=True))
        menu.add_command(label="Upload Files", command=self.upload_remote_files)
        menu.add_command(label="New Folder", command=self.create_remote_folder)
        menu.add_separator()
        menu.add_command(label="Open Local Folder", command=self.open_local_folder)
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def show_about(self) -> None:
        messagebox.showinfo(
            "About FreeCloud",
            "FreeCloud Sync\n\nYour hosting. Your cloud. Always in sync.",
        )

    def show_remote_item_menu(self, path: str, name: str, is_dir: bool) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        if is_dir:
            menu.add_command(label="Open", command=lambda: self.open_remote_folder(path))
        else:
            menu.add_command(label="Open Local Copy", command=lambda: self.open_local_file(path, name))
        menu.add_command(label="Download", command=lambda: self.download_remote_item(path, name, is_dir))
        menu.add_command(label="Delete", command=lambda: self.delete_remote_item(path, name))
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def delete_remote_item(self, path: str, name: str) -> None:
        if not messagebox.askyesno("Delete Cloud Item", f"Delete {name} from the cloud drive?"):
            return
        try:
            self.current_client().delete(path)
            self.append(f"Deleted remote item: {path}\n")
            self.refresh_files(force_live=True)
        except Exception as exc:
            messagebox.showerror("FreeCloud Files", str(exc))

    def download_remote_item(self, path: str, name: str, is_dir: bool) -> None:
        config = self.config_data()
        base_url = str(config.get("base_url") or "")
        password = str(config.get("password") or "")
        if not base_url:
            messagebox.showerror("FreeCloud Files", "Save setup before downloading cloud files.")
            return

        default_name = f"{name}.zip" if is_dir else name
        target_path = filedialog.asksaveasfilename(
            title="Save Cloud Download",
            initialfile=default_name,
        )
        if not target_path:
            return

        url = base_url.rstrip("/") + "/freecloud_download.php?path=" + urllib.parse.quote(cli.remote_path(path))
        request = urllib.request.Request(url, headers={"User-Agent": "FreeCloudUI/1"})
        if password:
            request.add_header("X-FreeCloud-Password", password)

        try:
            with urllib.request.urlopen(request, timeout=300) as response, Path(target_path).open("wb") as handle:
                handle.write(response.read())
            self.append(f"Downloaded remote item: {path} -> {target_path}\n")
        except Exception as exc:
            messagebox.showerror("FreeCloud Files", str(exc))

    def open_local_file(self, remote_path: str, name: str) -> None:
        config = self.config_data()
        local_root = str(config.get("local_root") or "").strip()
        if not local_root:
            messagebox.showinfo("Open File", "Save setup before opening synced files.")
            return

        local_path = Path(local_root).expanduser() / Path(cli.remote_path(remote_path))
        if not local_path.is_file():
            messagebox.showinfo(
                "Open File",
                "That file is not available locally yet.\n\n"
                "Let sync finish first, or use Download if you want a manual copy.",
            )
            return

        try:
            self.open_path_with_default_app(local_path)
        except Exception as exc:
            messagebox.showerror("Open File", str(exc))

    def bind_row_open(self, widget: tk.Widget, command: object, exclude: tuple[tk.Widget, ...] = ()) -> None:
        def handle_click(_event: tk.Event[tk.Misc]) -> None:
            if callable(command):
                command()

        widget.bind("<Button-1>", handle_click)
        if not exclude:
            return
        for child in widget.winfo_children():
            if child in exclude:
                continue
            self.bind_row_open(child, command)

    def file_kind_badge(self, name: str, is_dir: bool) -> str:
        if is_dir:
            return "📁"

        ext = Path(name).suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}:
            return "🖼"
        if ext in {".mp4", ".mov", ".webm", ".m4v", ".ogg", ".ogv"}:
            return "🎥"
        if ext in {".mp3", ".wav", ".flac", ".aac", ".m4a"}:
            return "🎵"
        if ext in {".zip", ".tar", ".gz", ".bz2", ".7z", ".rar"}:
            return "📦"
        if ext == ".pdf":
            return "📕"
        if ext in {".txt", ".md", ".json", ".csv", ".ini", ".log", ".py", ".php", ".js", ".html", ".css", ".xml", ".yml", ".yaml"}:
            return "📄"
        return "📄"

    def file_type_label(self, name: str) -> str:
        ext = Path(name).suffix.lower().lstrip(".")
        if not ext:
            return "File"
        labels = {
            "pdf": "PDF File",
            "txt": "Text Document",
            "md": "Markdown File",
            "json": "JSON File",
            "csv": "CSV File",
            "jpg": "Image",
            "jpeg": "Image",
            "png": "Image",
            "gif": "Image",
            "webp": "Image",
            "mp4": "Video",
            "mov": "Video",
            "mp3": "Audio",
            "wav": "Audio",
            "zip": "Compressed Folder",
        }
        return labels.get(ext, f"{ext.upper()} File")

    def badge_colors(self, badge: str) -> tuple[str, str]:
        if badge == "📁":
            return ("#fff2c9", "#7a5a00")
        if badge == "🖼":
            return ("#d9f3ff", "#1a5f84")
        if badge == "🎥":
            return ("#e7ddff", "#5d3a9b")
        if badge == "🎵":
            return ("#e0f7ea", "#246b49")
        if badge == "📦":
            return ("#ffe6d8", "#8a4e1b")
        if badge == "📕":
            return ("#ffe0e0", "#8c3030")
        if badge == "📄":
            return ("#edf1f5", "#4b5e72")
        return ("#edf1f5", "#4b5e72")

    def format_bytes(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = float(size)
        for unit in ("KB", "MB", "GB", "TB"):
            value /= 1024.0
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        return f"{size} B"

    def format_mtime(self, value: object) -> str:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp <= 0:
            return "—"
        dt = datetime.fromtimestamp(timestamp)
        hour = dt.hour % 12 or 12
        return f"{dt.strftime('%b')} {dt.day}, {dt.year} {hour}:{dt.minute:02d} {dt.strftime('%p')}"

    def append(self, text: str) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def set_running(self, running: bool) -> None:
        self.status.set("Running" if running else "Stopped")
        status_color = COLORS["success"] if running else COLORS["danger"]
        for label_name in ("header_status_label",):
            label = getattr(self, label_name, None)
            if label is not None:
                label.configure(fg=status_color)
        self.sync_button_text.set("🛑\nStop Sync" if running else "▶\nStart Sync")
        self.sync_button.configure(
            bg="#f3dede" if running else "#fbfdff",
            fg=COLORS["danger"] if running else COLORS["success"],
            activebackground="#ecd0d0" if running else COLORS["button_light_hover"],
            activeforeground=COLORS["danger"] if running else COLORS["success"],
            highlightbackground="#f3dede" if running else "#fbfdff",
            highlightcolor="#f3dede" if running else "#fbfdff",
        )

    def save_setup(self) -> None:
        self.setup_button.configure(state=tk.DISABLED)
        try:
            if self.domain_var.get().strip() == self.domain_placeholder:
                raise ValueError("Enter the full address of your FreeCloud website.")
            website_address = cli.normalize_domain(self.domain_var.get())
            parsed_address = urllib.parse.urlparse(website_address)
            path_parts = [part for part in parsed_address.path.split("/") if part]
            if path_parts:
                drive_name = path_parts[-1]
                domain = urllib.parse.urlunparse(
                    (parsed_address.scheme, parsed_address.netloc, "", "", "", "")
                ).rstrip("/")
                base_url = website_address
            else:
                drive_name = cli.normalize_drive_name(self.drive_var.get() or "FreeCloud")
                domain = website_address
                base_url = f"{domain}/{urllib.parse.quote(drive_name)}"
            local_root = Path(self.local_var.get()).expanduser().resolve()
            password = self.password_var.get()
            client = cli.FreeCloudClient(base_url, password)
            try:
                ping = client.ping()
            except cli.FreeCloudApiError as exc:
                if exc.code == 409:
                    ping = {"setup": False}
                elif exc.code == 401:
                    messagebox.showerror(
                        "FreeCloud Setup",
                        "The remote drive is already set up, but that password did not work.",
                    )
                    return
                elif exc.code == 404:
                    messagebox.showerror(
                        "FreeCloud Setup",
                        "Could not find freecloud_api.php.\n\n"
                        f"Checked URL:\n{exc.url}\n\n"
                        f"Upload the contents of:\n{cli.APP_DIR}\n\n"
                        f"into public_html/{drive_name}/ on your host.",
                    )
                    return
                else:
                    messagebox.showerror("FreeCloud Setup", f"Could not contact the server:\n{exc}")
                    return

            if not ping.get("setup"):
                client.setup(drive_name, password)

            local_root.mkdir(parents=True, exist_ok=True)
            config = {
                "domain": domain,
                "drive_name": drive_name,
                "base_url": client.base_url,
                "local_root": str(local_root),
                "password": password,
                "interval": cli.DEFAULT_INTERVAL,
            }
            self.append(f"Saved setup for {client.base_url}\n")
            self.append(f"Local folder: {local_root}\n")

            cli.save_json(cli.config_path(local_root), config)
            cli.save_json(cli.LAST_CONFIG_PATH, config)
            self.update_summary_text(config)
            self.editing_settings = False
            self.refresh_setup_visibility()
            self.schedule_files_refresh()
            self.append("Starting automatically...\n")
            self.start_sync()
        except Exception as exc:
            messagebox.showerror("FreeCloud Setup", str(exc))
        finally:
            self.setup_button.configure(state=tk.NORMAL)

    def start_process(self, once: bool = False) -> None:
        startup_log(f"start_process called, once={once}")
        if self.process is not None and self.process.poll() is None:
            startup_log("start_process skipped: foreground process already running")
            return
        if not once and self.background_sync_running():
            self.append("FreeCloud sync is already running in the background.\n")
            self.set_running(True)
            startup_log("start_process skipped: background sync already running")
            return
        if not self.config_data():
            messagebox.showinfo("FreeCloud Sync", "Fill in and save first-time setup before starting sync.")
            self.refresh_setup_visibility()
            startup_log("start_process skipped: no config")
            return

        self.closing_for_background = False
        command = [sys.executable, str(CLI_PATH)]
        if once:
            command.append("--once")

        self.append("$ " + " ".join(command) + "\n")
        self.process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.set_running(True)
        startup_log("foreground sync process started")

        thread = threading.Thread(target=self.read_output, daemon=True)
        thread.start()

    def read_output(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.output_queue.put(line)
        self.process.wait()
        self.output_queue.put(None)

    def drain_output(self) -> None:
        try:
            while True:
                item = self.output_queue.get_nowait()
                if item is None:
                    if not self.closing_for_background:
                        self.set_running(False)
                        self.append("\nProcess stopped.\n")
                    self.process = None
                else:
                    self.append(item)
        except queue.Empty:
            pass
        self.root.after(150, self.drain_output)

    def start_sync(self) -> None:
        self.start_process(once=False)

    def sync_once(self) -> None:
        self.start_process(once=True)

    def toggle_sync(self) -> None:
        running = (self.process is not None and self.process.poll() is None) or self.background_sync_running()
        if running:
            self.stop_sync()
        else:
            self.start_sync()

    def stop_sync(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.append("\nStopping sync...\n")
            self.root.after(4000, self.force_stop_process_if_needed)
            return

        if self.background_sync_running():
            pid = self.background_pid()
            if pid is not None:
                self.stop_background_process(pid)
                self.append("\nStopping background sync...\n")
                self.set_running(False)
            return

        self.set_running(False)

    def force_stop_process_if_needed(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.kill()
        self.append("Sync did not stop cleanly. Forced exit.\n")

    def edit_settings(self) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("FreeCloud Sync", "Stop sync before editing settings.")
            return

        if self.background_sync_running():
            messagebox.showinfo("FreeCloud Sync", "Stop background sync before editing settings.")
            return

        self.load_saved_values()
        self.editing_settings = True
        self.refresh_setup_visibility()
        self.append("\nEditing saved settings.\n")

    def show_dashboard(self) -> None:
        self.editing_settings = False
        self.refresh_setup_visibility()

    def background_pid(self) -> int | None:
        try:
            return int(PID_PATH.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return None

    def background_sync_running(self) -> bool:
        pid = self.background_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            try:
                PID_PATH.unlink()
            except FileNotFoundError:
                pass
            return False
        return True

    def launch_background_sync(self) -> bool:
        if self.background_sync_running():
            return True
        if not self.config_data():
            return False

        BACKGROUND_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BACKGROUND_LOG_PATH.open("a", encoding="utf-8") as log_handle:
            kwargs: dict[str, object] = {
                "cwd": str(BASE_DIR),
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                kwargs["creationflags"] = WINDOWS_CREATION_FLAGS
            else:
                kwargs["start_new_session"] = True

            process = subprocess.Popen([sys.executable, str(CLI_PATH)], **kwargs)
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(process.pid), encoding="utf-8")
        return True

    def stop_background_process(self, pid: int) -> None:
        try:
            if os.name == "nt":
                os.kill(pid, signal.SIGTERM)
            else:
                os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass

    def exit_keep_syncing(self) -> None:
        if self.process is not None and self.process.poll() is None:
            if self.launch_background_sync():
                launch_tray_indicator()
                self.closing_for_background = True
                self.process.terminate()
                self.root.after(150, self.root.destroy)
                return
            messagebox.showerror("FreeCloud Sync", "Could not start background sync.")
            return

        if not self.background_sync_running() and self.config_data():
            self.launch_background_sync()
        if self.background_sync_running():
            launch_tray_indicator()
        self.root.destroy()

    def on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            keep_running = messagebox.askyesno(
                "FreeCloud Sync",
                "Continue running in Background?\n\nYes: minimize to tray\nNo: close everything",
            )
            if keep_running:
                self.exit_keep_syncing()
                return
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
            stop_tray_indicator()
        elif self.background_sync_running():
            keep_running = messagebox.askyesno(
                "FreeCloud Sync",
                "Continue running in Background?\n\nYes: minimize to tray\nNo: close everything",
            )
            if not keep_running:
                pid = self.background_pid()
                if pid is not None:
                    self.stop_background_process(pid)
                stop_tray_indicator()
            else:
                launch_tray_indicator()
        else:
            stop_tray_indicator()
        self.root.destroy()

    def open_path_with_default_app(self, path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], cwd=str(path.parent if path.is_file() else path))
        else:
            subprocess.Popen(["xdg-open", str(path)], cwd=str(path.parent if path.is_file() else path))

    def open_url_in_default_browser(self, url: str) -> None:
        if os.name == "nt":
            os.startfile(url)
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], cwd=str(BASE_DIR))
            return
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], cwd=str(BASE_DIR))
            return
        if not webbrowser.open(url):
            raise RuntimeError(f"Could not open browser for:\n{url}")


def tray_background_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def tray_background_running() -> bool:
    pid = tray_background_pid()
    if pid is None:
        return False
    if process_is_running(pid):
        return True
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return False


def tray_stop_background_sync() -> None:
    pid = tray_background_pid()
    if pid is None:
        return
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass


def run_tray_indicator() -> int:
    if not sys.platform.startswith("linux"):
        return 1
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return 1

    try:
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator  # type: ignore[import-not-found]
        except (ImportError, ValueError):
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppIndicator  # type: ignore[import-not-found]
        from gi.repository import GLib, Gtk  # type: ignore[import-not-found]
    except Exception:
        TRAY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRAY_LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            TRAY_PID_PATH.unlink()
        except FileNotFoundError:
            pass
        return 1

    initialized, _args = Gtk.init_check(sys.argv[:1])
    if not initialized:
        return 1

    try:
        TRAY_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRAY_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    category = AppIndicator.IndicatorCategory.APPLICATION_STATUS
    tray_icon_name = TRAY_ICON_PATH.stem if TRAY_ICON_PATH.is_file() else "freecloud"
    if hasattr(AppIndicator.Indicator, "new_with_path") and TRAY_ICON_PATH.is_file():
        indicator = AppIndicator.Indicator.new_with_path(
            "freecloud-sync",
            tray_icon_name,
            category,
            str(TRAY_ICON_PATH.parent),
        )
    else:
        indicator = AppIndicator.Indicator.new("freecloud-sync", tray_icon_name, category)
    indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    if TRAY_ICON_PATH.is_file() and hasattr(indicator, "set_icon_theme_path"):
        indicator.set_icon_theme_path(str(TRAY_ICON_PATH.parent))
    if hasattr(indicator, "set_icon_full"):
        indicator.set_icon_full(tray_icon_name, "FreeCloud")

    menu = Gtk.Menu()
    status_item = Gtk.MenuItem(label="FreeCloud sync is running")
    status_item.set_sensitive(False)
    menu.append(status_item)
    menu.append(Gtk.SeparatorMenuItem())

    open_item = Gtk.MenuItem(label="Open FreeCloud")
    stop_item = Gtk.MenuItem(label="Stop Sync")
    quit_item = Gtk.MenuItem(label="Quit FreeCloud")
    menu.append(open_item)
    menu.append(stop_item)
    menu.append(quit_item)

    def refresh_status() -> bool:
        running = tray_background_running()
        status_item.set_label("FreeCloud sync is running" if running else "FreeCloud sync is stopped")
        stop_item.set_sensitive(running)
        return True

    def open_ui(_item: object) -> None:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop_sync(_item: object) -> None:
        tray_stop_background_sync()
        refresh_status()

    def quit_freecloud(_item: object) -> None:
        tray_stop_background_sync()
        Gtk.main_quit()

    open_item.connect("activate", open_ui)
    stop_item.connect("activate", stop_sync)
    quit_item.connect("activate", quit_freecloud)
    menu.show_all()
    indicator.set_menu(menu)
    refresh_status()
    GLib.timeout_add_seconds(5, refresh_status)

    try:
        Gtk.main()
    finally:
        try:
            TRAY_PID_PATH.unlink()
        except FileNotFoundError:
            pass
    return 0


def main() -> int:
    if "--tray" in sys.argv[1:]:
        return run_tray_indicator()
    if "--background" in sys.argv[1:]:
        return run_background_startup()

    try:
        root = tk.Tk()
        FreeCloudUi(root)
        root.mainloop()
        return 0
    except Exception:
        try:
            UI_ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            UI_ERROR_LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
