#!/usr/bin/env python3
"""
FreeCloud Linux CLI.

First version goals:
- prompt for a domain, remote drive folder, local folder, password, and setup method
- optionally upload the FreeCloud web app over FTP
- initialize the remote app if needed
- keep local and remote files in sync by polling

This intentionally uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import ftplib
import getpass
import json
import mimetypes
import os
import posixpath
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent / "server"
LAST_CONFIG_PATH = Path(__file__).resolve().parent / ".freecloud_last_config.json"
DEFAULT_INTERVAL = 10


class FreeCloudApiError(RuntimeError):
    def __init__(self, message: str, url: str, code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.url = url
        self.code = code
        self.body = body


def prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        value = getpass.getpass(f"{label}{suffix}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


def normalize_domain(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Domain is required.")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def normalize_drive_name(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ValueError("Drive folder name is required.")
    bad = {"..", ".", ""}
    parts = [part for part in value.replace("\\", "/").split("/") if part not in bad]
    if len(parts) != 1:
        raise ValueError("Use one public_html folder name, like FreeCloud.")
    return parts[0]


def remote_path(path: str) -> str:
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        parts.append(part)
    return "/".join(parts)


class FreeCloudClient:
    def __init__(self, base_url: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password

    def remember_final_url(self, final_url: str) -> None:
        parsed = urllib.parse.urlparse(final_url)
        if not parsed.scheme or not parsed.netloc:
            return
        if not parsed.path.endswith("/freecloud_api.php"):
            return
        app_path = parsed.path[: -len("/freecloud_api.php")]
        self.base_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, app_path, "", "", "")).rstrip("/")

    def api_url(self, action: str, params: dict[str, Any] | None = None) -> str:
        query = {"action": action}
        if params:
            query.update({key: value for key, value in params.items() if value is not None})
        return self.base_url + "/freecloud_api.php?" + urllib.parse.urlencode(query)

    def request(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        method: str = "GET",
        content_type: str | None = None,
        expect_json: bool = True,
    ) -> Any:
        headers = {"User-Agent": "FreeCloudCLI/1"}
        if self.password:
            headers["X-FreeCloud-Password"] = self.password
        if content_type:
            headers["Content-Type"] = content_type

        url = self.api_url(action, params)
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                self.remember_final_url(response.geturl())
                body = response.read()
                if not expect_json:
                    return body
                try:
                    return json.loads(body.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    preview = body[:500].decode("utf-8", errors="replace")
                    raise FreeCloudApiError(
                        "The server answered, but not with FreeCloud JSON.",
                        url,
                        getattr(response, "status", 0),
                        preview,
                    ) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise FreeCloudApiError(f"HTTP {exc.code} from FreeCloud server.", url, exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise FreeCloudApiError(f"Could not reach FreeCloud server: {exc.reason}", url, 0, "") from exc

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def setup(self, name: str, password: str) -> dict[str, Any]:
        data = urllib.parse.urlencode({"name": name, "password": password}).encode("utf-8")
        return self.request("setup", data=data, method="POST", content_type="application/x-www-form-urlencoded")

    def manifest(self) -> list[dict[str, Any]]:
        data = self.request("manifest")
        return list(data.get("entries", []))

    def mkdir(self, path: str) -> None:
        self.request("mkdir", {"path": remote_path(path)}, data=b"", method="POST")

    def upload(self, local_file: Path, path: str) -> None:
        mtime = int(local_file.stat().st_mtime)
        with local_file.open("rb") as handle:
            body = handle.read()
        self.request(
            "upload",
            {"path": remote_path(path), "mtime": str(mtime)},
            data=body,
            method="POST",
            content_type=mimetypes.guess_type(local_file.name)[0] or "application/octet-stream",
        )

    def download(self, path: str, local_file: Path) -> None:
        headers = {"User-Agent": "FreeCloudCLI/1"}
        if self.password:
            headers["X-FreeCloud-Password"] = self.password
        req = urllib.request.Request(self.api_url("download", {"path": remote_path(path)}), headers=headers)
        local_file.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=120) as response, local_file.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)

    def delete(self, path: str) -> None:
        self.request("delete", {"path": remote_path(path)}, data=b"", method="POST")


def config_path(local_root: Path) -> Path:
    return local_root / ".freecloud_client.json"


def state_path(local_root: Path) -> Path:
    return local_root / ".freecloud_state.json"


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def local_manifest(local_root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    ignored = {".freecloud_client.json", ".freecloud_state.json"}
    for path in local_root.rglob("*"):
        if any(part in ignored for part in path.relative_to(local_root).parts):
            continue
        rel = path.relative_to(local_root).as_posix()
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        entries[rel] = {
            "path": rel,
            "type": "dir" if path.is_dir() else "file",
            "size": 0 if path.is_dir() else stat.st_size,
            "mtime": int(stat.st_mtime),
        }
    return entries


def remote_manifest(client: FreeCloudClient) -> dict[str, dict[str, Any]]:
    return {entry["path"]: entry for entry in client.manifest() if entry.get("path")}


def roughly_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("type") != b.get("type"):
        return False
    if a.get("type") == "dir":
        return True
    return int(a.get("size", -1)) == int(b.get("size", -2)) and abs(int(a.get("mtime", 0)) - int(b.get("mtime", 0))) <= 2


def sync_once(client: FreeCloudClient, local_root: Path, delete_remote: bool = False) -> dict[str, int]:
    local_root.mkdir(parents=True, exist_ok=True)
    previous = load_json(state_path(local_root), {})
    local = local_manifest(local_root)
    remote = remote_manifest(client)
    all_paths = sorted(set(local) | set(remote) | set(previous))
    counts = {"uploaded": 0, "downloaded": 0, "deleted_local": 0, "deleted_remote": 0, "conflicts": 0}

    for path in all_paths:
        local_entry = local.get(path)
        remote_entry = remote.get(path)
        previous_entry = previous.get(path)
        local_file = local_root / path

        if local_entry and local_entry["type"] == "dir":
            if not remote_entry:
                client.mkdir(path)
                counts["uploaded"] += 1
            continue

        if remote_entry and remote_entry["type"] == "dir":
            if not local_entry:
                local_file.mkdir(parents=True, exist_ok=True)
                counts["downloaded"] += 1
            continue

        local_changed = local_entry is not None and (previous_entry is None or not roughly_equal(local_entry, previous_entry))
        remote_changed = remote_entry is not None and (previous_entry is None or not roughly_equal(remote_entry, previous_entry))

        if local_entry and remote_entry and roughly_equal(local_entry, remote_entry):
            continue

        if local_entry and not remote_entry:
            if previous_entry and not local_changed:
                try:
                    local_file.unlink()
                    counts["deleted_local"] += 1
                except FileNotFoundError:
                    pass
            else:
                client.upload(local_file, path)
                counts["uploaded"] += 1
            continue

        if remote_entry and not local_entry:
            if previous_entry and not remote_changed and delete_remote:
                client.delete(path)
                counts["deleted_remote"] += 1
            else:
                client.download(path, local_file)
                if int(remote_entry.get("mtime", 0)) > 0:
                    os.utime(local_file, (int(remote_entry["mtime"]), int(remote_entry["mtime"])))
                counts["downloaded"] += 1
            continue

        if local_entry and remote_entry:
            if local_changed and remote_changed:
                conflict = local_file.with_name(local_file.name + ".conflict-local")
                local_file.rename(conflict)
                client.download(path, local_file)
                counts["conflicts"] += 1
            elif local_changed:
                client.upload(local_file, path)
                counts["uploaded"] += 1
            else:
                client.download(path, local_file)
                if int(remote_entry.get("mtime", 0)) > 0:
                    os.utime(local_file, (int(remote_entry["mtime"]), int(remote_entry["mtime"])))
                counts["downloaded"] += 1

    save_json(state_path(local_root), {**remote_manifest(client), **local_manifest(local_root)})
    return counts


def ftp_mkdirs(ftp: ftplib.FTP, path: str) -> None:
    current = ""
    for part in [p for p in path.split("/") if p]:
        current = posixpath.join(current, part)
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass


def ftp_upload_tree(ftp: ftplib.FTP, source: Path, remote_root: str) -> None:
    if not source.is_dir():
        raise RuntimeError(f"Could not find web app folder: {source}")

    ftp_mkdirs(ftp, remote_root)
    for path in source.rglob("*"):
        rel = path.relative_to(source).as_posix()
        remote = posixpath.join(remote_root, rel)
        if path.is_dir():
            ftp_mkdirs(ftp, remote)
            continue
        ftp_mkdirs(ftp, posixpath.dirname(remote))
        with path.open("rb") as handle:
            ftp.storbinary(f"STOR {remote}", handle)


def explain_connection_failure(error: FreeCloudApiError, drive_name: str, base_url: str) -> None:
    print()
    print("Could not connect to the FreeCloud API.")
    print(f"Checked: {error.url}")
    if error.code:
        print(f"Server response: HTTP {error.code}")
    else:
        print(str(error))

    if error.code == 404:
        print()
        print("The most likely issue is that the files were uploaded to the wrong place.")
        print(f"The contents of this local folder must be directly inside public_html/{drive_name}/:")
        print(f"  {APP_DIR}")
        print()
        print("On the web host it should look like:")
        print(f"  public_html/{drive_name}/index.php")
        print(f"  public_html/{drive_name}/freecloud_api.php")
        print(f"  public_html/{drive_name}/freecloud.php")
        print(f"  public_html/{drive_name}/freecloud_files/")
        print()
        print("It should NOT look like this:")
        print(f"  public_html/{drive_name}/server/freecloud_api.php")

    if error.body:
        preview = error.body.strip().replace("\n", " ")[:300]
        if preview:
            print()
            print(f"Server said: {preview}")

    print()
    print(f"After fixing the upload, test this URL in a browser:")
    print(f"  {base_url}/freecloud_api.php?action=ping")


def run_setup() -> dict[str, Any]:
    print("FreeCloud first-time CLI setup")
    domain = normalize_domain(prompt("Domain", "https://www.example.com"))
    drive_name = normalize_drive_name(prompt("Cloud drive folder name", "FreeCloud"))
    local_root = Path(prompt("Local folder to sync", str(Path.home() / drive_name))).expanduser().resolve()
    password = prompt("FreeCloud password", secret=True)
    setup_method = prompt("Setup method: manual or ftp", "manual").lower()
    base_url = f"{domain}/{urllib.parse.quote(drive_name)}"

    if setup_method == "ftp":
        host = prompt("FTP host", urllib.parse.urlparse(domain).hostname or "")
        user = prompt("FTP username")
        ftp_password = prompt("FTP password", secret=True)
        public_html = prompt("Remote public_html path", "public_html")
        remote_root = posixpath.join(public_html.strip("/"), drive_name)
        print(f"Uploading web app to /{remote_root} ...")
        with ftplib.FTP(host, timeout=60) as ftp:
            ftp.login(user, ftp_password)
            ftp_upload_tree(ftp, APP_DIR, remote_root)
    else:
        print()
        print("Manual setup:")
        print(f"1. Upload the local server folder to your host as public_html/{drive_name}/")
        print(f"   Local folder: {APP_DIR}")
        print(f"2. The web address should be: {base_url}")
        input("Press Enter after the folder is uploaded...")

    client = FreeCloudClient(base_url, password)
    ping: dict[str, Any]
    for attempt in range(3):
        try:
            ping = client.ping()
            break
        except FreeCloudApiError as exc:
            if exc.code == 409:
                ping = {"setup": False}
                break
            if exc.code == 401:
                print()
                print("The remote drive is already set up, but that password did not work.")
                if attempt < 2:
                    password = prompt("Existing FreeCloud password", secret=True)
                    client = FreeCloudClient(base_url, password)
                    continue
            explain_connection_failure(exc, drive_name, base_url)
            raise SystemExit(1) from exc
    else:
        raise SystemExit(1)

    if not ping.get("setup"):
        print("Initializing remote FreeCloud config...")
        try:
            client.setup(drive_name, password)
        except FreeCloudApiError as exc:
            explain_connection_failure(exc, drive_name, base_url)
            raise SystemExit(1) from exc
    else:
        print("Remote FreeCloud already exists. Using it.")

    local_root.mkdir(parents=True, exist_ok=True)
    config = {
        "domain": domain,
        "drive_name": drive_name,
        "base_url": base_url,
        "local_root": str(local_root),
        "password": password,
        "interval": DEFAULT_INTERVAL,
    }
    save_json(config_path(local_root), config)
    save_json(LAST_CONFIG_PATH, config)
    print(f"Saved config: {config_path(local_root)}")
    return config


def load_or_setup(args: argparse.Namespace) -> dict[str, Any]:
    if args.config:
        return load_json(Path(args.config).expanduser().resolve(), {})
    if args.local:
        cfg = load_json(config_path(Path(args.local).expanduser().resolve()), {})
        if cfg:
            return cfg
    cfg = load_json(LAST_CONFIG_PATH, {})
    if cfg:
        return cfg
    return run_setup()


def main() -> int:
    parser = argparse.ArgumentParser(description="FreeCloud Linux CLI sync tool")
    parser.add_argument("--config", help="Path to .freecloud_client.json")
    parser.add_argument("--local", help="Local sync folder")
    parser.add_argument("--once", action="store_true", help="Run one sync pass and exit")
    parser.add_argument("--interval", type=int, help="Polling interval in seconds")
    parser.add_argument("--delete-remote", action="store_true", help="Propagate local deletions to the remote server")
    args = parser.parse_args()

    config = load_or_setup(args)
    if not config:
        print("No config found.", file=sys.stderr)
        return 1

    local_root = Path(config["local_root"]).expanduser().resolve()
    client = FreeCloudClient(config["base_url"], config.get("password", ""))
    interval = max(2, int(args.interval or config.get("interval") or DEFAULT_INTERVAL))

    print(f"Syncing {local_root} <-> {config['base_url']}")
    print(f"Watching for changes every {interval} seconds. Press Ctrl+C to stop.")
    while True:
        try:
            counts = sync_once(client, local_root, delete_remote=args.delete_remote)
            changed = sum(counts.values())
            if changed:
                print(time.strftime("%Y-%m-%d %H:%M:%S"), counts)
            else:
                print(time.strftime("%Y-%m-%d %H:%M:%S"), "No changes.")
        except KeyboardInterrupt:
            print("Stopped.")
            return 0
        except Exception as exc:
            print(f"Sync error: {exc}", file=sys.stderr)

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
