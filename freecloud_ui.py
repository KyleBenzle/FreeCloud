#!/usr/bin/env python3
"""Very small FreeCloud desktop launcher UI for macOS/Linux."""

from __future__ import annotations

import queue
import json
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
from pathlib import Path
from tkinter import messagebox

import freecloud_cli as cli


BASE_DIR = Path(__file__).resolve().parent
CLI_PATH = BASE_DIR / "freecloud_cli.py"
LAST_CONFIG_PATH = BASE_DIR / ".freecloud_last_config.json"


def load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


class FreeCloudUi:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()

        root.title("FreeCloud Sync")
        root.geometry("720x460")
        root.minsize(520, 340)

        self.status = tk.StringVar(value="Stopped")
        self.domain_var = tk.StringVar(value="https://www.example.com")
        self.drive_var = tk.StringVar(value="FreeCloud")
        self.local_var = tk.StringVar(value=str(Path.home() / "FreeCloud"))
        self.password_var = tk.StringVar(value="")

        self.frame = tk.Frame(root, padx=14, pady=14)
        self.frame.pack(fill=tk.BOTH, expand=True)

        title = tk.Label(self.frame, text="FreeCloud Sync", font=("TkDefaultFont", 18, "bold"))
        title.pack(anchor="w")

        note = tk.Label(
            self.frame,
            text="Start syncing your local folder with the web drive. Close this window after stopping sync.",
            anchor="w",
            justify="left",
        )
        note.pack(fill=tk.X, pady=(4, 12))

        self.setup_frame = tk.LabelFrame(self.frame, text="First-time setup", padx=10, pady=10)
        self.build_setup_form()

        self.buttons_frame = tk.Frame(self.frame)
        self.buttons_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_button = tk.Button(self.buttons_frame, text="Start Sync", width=14, command=self.start_sync)
        self.start_button.pack(side=tk.LEFT)

        self.stop_button = tk.Button(self.buttons_frame, text="Stop Sync", width=14, command=self.stop_sync, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        self.once_button = tk.Button(self.buttons_frame, text="Sync Once", width=14, command=self.sync_once)
        self.once_button.pack(side=tk.LEFT, padx=(8, 0))

        self.reset_button = tk.Button(self.buttons_frame, text="Reset Settings", width=14, command=self.reset_settings)
        self.reset_button.pack(side=tk.LEFT, padx=(8, 0))

        status_label = tk.Label(self.buttons_frame, textvariable=self.status, anchor="e")
        status_label.pack(side=tk.RIGHT)

        self.output = tk.Text(self.frame, wrap="word", height=18, state=tk.DISABLED)
        self.output.pack(fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(self.output, command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.drain_output)
        self.refresh_setup_visibility()

    def build_setup_form(self) -> None:
        help_text = (
            "Upload the contents of the local server folder to public_html/<drive folder>/, "
            "then fill this in and click Save Setup."
        )
        tk.Label(self.setup_frame, text=help_text, anchor="w", justify="left", wraplength=650).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8)
        )

        fields = [
            ("Domain", self.domain_var, False),
            ("Cloud drive folder", self.drive_var, False),
            ("Local folder", self.local_var, False),
            ("FreeCloud password", self.password_var, True),
        ]
        for row, (label, variable, secret) in enumerate(fields, start=1):
            tk.Label(self.setup_frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = tk.Entry(self.setup_frame, textvariable=variable, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=3)

        self.setup_button = tk.Button(self.setup_frame, text="Save Setup", command=self.save_setup)
        self.setup_button.grid(row=5, column=1, sticky="w", pady=(8, 0))

        self.setup_frame.columnconfigure(1, weight=1)

    def refresh_setup_visibility(self) -> None:
        if LAST_CONFIG_PATH.is_file():
            self.setup_frame.pack_forget()
            return
        self.setup_frame.pack(fill=tk.X, pady=(0, 12), before=self.buttons_frame)

    def append(self, text: str) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def set_running(self, running: bool) -> None:
        self.status.set("Running" if running else "Stopped")
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.once_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.reset_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def save_setup(self) -> None:
        self.setup_button.configure(state=tk.DISABLED)
        try:
            domain, drive_name, base_url = build_setup_urls(self.domain_var.get(), self.drive_var.get())
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
            cli.save_json(cli.config_path(local_root), config)
            cli.save_json(cli.LAST_CONFIG_PATH, config)
            self.append(f"Saved setup for {client.base_url}\n")
            self.append(f"Local folder: {local_root}\n")
            self.refresh_setup_visibility()
            messagebox.showinfo("FreeCloud Setup", "Setup saved. You can start sync now.")
        except Exception as exc:
            messagebox.showerror("FreeCloud Setup", str(exc))
        finally:
            self.setup_button.configure(state=tk.NORMAL)

    def start_process(self, once: bool = False) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not LAST_CONFIG_PATH.is_file():
            messagebox.showinfo("FreeCloud Sync", "Fill in and save first-time setup before starting sync.")
            self.refresh_setup_visibility()
            return

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
                    self.set_running(False)
                    self.append("\nProcess stopped.\n")
                else:
                    self.append(item)
        except queue.Empty:
            pass
        self.root.after(150, self.drain_output)

    def start_sync(self) -> None:
        self.start_process(once=False)

    def sync_once(self) -> None:
        self.start_process(once=True)

    def stop_sync(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.append("\nStopping sync...\n")

    def reset_settings(self) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("FreeCloud Sync", "Stop sync before resetting settings.")
            return

        config = load_json(LAST_CONFIG_PATH)
        local_root_value = config.get("local_root")
        local_root = Path(str(local_root_value)).expanduser() if local_root_value else None

        paths = [LAST_CONFIG_PATH]
        if local_root is not None:
            paths.append(local_root / ".freecloud_client.json")
            paths.append(local_root / ".freecloud_state.json")
        for default_root in (Path.home() / "FreeCloud", Path.home() / "myCloud"):
            paths.append(default_root / ".freecloud_client.json")
            paths.append(default_root / ".freecloud_state.json")
        paths = list(dict.fromkeys(paths))

        path_list = "\n".join(str(path) for path in paths)
        confirmed = messagebox.askyesno(
            "Reset FreeCloud Settings",
            "Reset local desktop settings?\n\n"
            "This does not delete your synced files and does not reset the website.\n\n"
            f"Files to remove:\n{path_list}",
        )
        if not confirmed:
            return

        removed = []
        missing = []
        for path in paths:
            try:
                path.unlink()
                removed.append(str(path))
            except FileNotFoundError:
                missing.append(str(path))
            except OSError as exc:
                messagebox.showerror("FreeCloud Sync", f"Could not remove:\n{path}\n\n{exc}")
                return

        if removed:
            self.append("\nReset local settings:\n" + "\n".join(removed) + "\n")
        if missing:
            self.append("\nAlready missing:\n" + "\n".join(missing) + "\n")
        self.append("\nNext start will ask first-time setup questions again.\n")
        self.refresh_setup_visibility()
        messagebox.showinfo("FreeCloud Sync", "Local settings were reset.")

    def on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno("FreeCloud Sync", "Sync is still running. Stop it and close?"):
                return
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    FreeCloudUi(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
