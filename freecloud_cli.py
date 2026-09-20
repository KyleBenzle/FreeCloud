#!/usr/bin/env python3
"""
FreeCloud Linux CLI.

First version goals:
- prompt for a domain, remote drive folder, local folder, password, and setup method
- optionally upload the FreeCloud web app over encrypted FTPS
- initialize the remote app if needed
- keep local and remote files in sync by polling

This intentionally uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import ftplib
import getpass
import hashlib
import json
import mimetypes
import os
import posixpath
import shlex
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent / "server"
LEGACY_LAST_CONFIG_PATH = Path(__file__).resolve().parent / ".freecloud_last_config.json"
DEFAULT_INTERVAL = 10
APP_NAME = "freecloud"
VERSION = "2.1.1"
PROTECTED_NAMES = {
    ".freecloud_client.json",
    ".freecloud_last_config.json",
    ".freecloud_state.json",
    "freecloud_background.log",
    "freecloud_sync.pid",
}
PROTECTED_DIR_NAMES = {".git", "__pycache__"}
PROTECTED_SERVER_DIR_NAMES = {"freecloud_files", "freecloud_sessions", "freecloud_meta"}
PROTECTED_SERVER_FILES = {"config.json"}


def app_config_dir() -> Path:
    """Return the per-user config folder used by the desktop/CLI app.

    Linux packages should not write settings beside files installed in
    `/usr/share/freecloud`, so new installs use the standard XDG location.
    """
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "FreeCloud"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME


def app_state_dir() -> Path:
    """Return the per-user state/log folder."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "FreeCloud"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / APP_NAME


CONFIG_DIR = app_config_dir()
STATE_DIR = app_state_dir()
LAST_CONFIG_PATH = CONFIG_DIR / "last_config.json"
DEVICE_CONFIG_PATH = CONFIG_DIR / "device.json"
NODE_RULES_PATH = CONFIG_DIR / "sync_rules.json"
NODE_STATE_PATH = STATE_DIR / "node_state.json"
NODE_LOG_PATH = STATE_DIR / "freecloud-node.log"


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
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("FreeCloud requires HTTPS. Plain HTTP is allowed only for local development.")
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


def validate_password(value: str) -> str:
    if len(value) < 8:
        raise ValueError("Use a FreeCloud password with at least 8 characters.")
    return value


def remote_path(path: str) -> str:
    # Keep client-side paths predictable before they reach the PHP normalizer.
    # A parent segment backs up one clean segment instead of becoming a name.
    parts = []
    for part in path.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


class FreeCloudClient:
    def __init__(self, base_url: str, password: str) -> None:
        self.base_url = normalize_domain(base_url)
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

    def post_json(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        return dict(
            self.request(
                action,
                data=data,
                method="POST",
                content_type="application/json",
            )
        )

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def setup(self, name: str, password: str) -> dict[str, Any]:
        data = urllib.parse.urlencode({"name": name, "password": password}).encode("utf-8")
        return self.request("setup", data=data, method="POST", content_type="application/x-www-form-urlencoded")

    def manifest(self) -> list[dict[str, Any]]:
        data = self.request("manifest", {"_": time.time_ns()})
        return list(data.get("entries", []))

    def list(self, path: str = "") -> list[dict[str, Any]]:
        data = self.request("list", {"path": remote_path(path)})
        return list(data.get("entries", []))

    def storage(self) -> dict[str, Any]:
        return dict(self.request("storage"))

    def mkdir(self, path: str) -> None:
        self.request("mkdir", {"path": remote_path(path)}, data=b"", method="POST")

    def move(self, from_path: str, to_path: str) -> None:
        self.request(
            "move",
            {"from": remote_path(from_path), "to": remote_path(to_path)},
            data=b"",
            method="POST",
        )

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

    def register_device(
        self,
        name: str,
        device_type: str,
        device_id: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.post_json(
            "register_device",
            {
                "device_id": device_id,
                "name": name,
                "type": device_type,
                "capabilities": capabilities or [],
            },
        )

    def heartbeat(self, device_id: str) -> dict[str, Any]:
        return self.post_json("heartbeat", {"device_id": device_id})

    def publish_manifest(self, device_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post_json("publish_manifest", {"device_id": device_id, "entries": entries})

    def metadata_manifest(self) -> list[dict[str, Any]]:
        data = self.request("metadata_manifest", {"_": time.time_ns()})
        return list(data.get("entries", []))

    def metadata_list(self, path: str = "") -> list[dict[str, Any]]:
        data = self.request("metadata_list", {"path": remote_path(path)})
        return list(data.get("entries", []))

    def get_sync_rules(self, device_id: str) -> list[dict[str, Any]]:
        data = self.request("get_sync_rules", {"device_id": device_id})
        return list(data.get("rules", []))

    def set_sync_rules(self, device_id: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post_json("set_sync_rules", {"device_id": device_id, "rules": rules})

    def poll_transfers(self, device_id: str) -> list[dict[str, Any]]:
        data = self.post_json("poll_transfers", {"device_id": device_id})
        return list(data.get("transfers", []))

    def upload_transfer(self, device_id: str, transfer_id: str, local_file: Path) -> dict[str, Any]:
        headers = {
            "User-Agent": "FreeCloudCLI/1",
            "X-FreeCloud-Device": device_id,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(local_file.stat().st_size),
        }
        if self.password:
            headers["X-FreeCloud-Password"] = self.password
        with local_file.open("rb") as source:
            request = urllib.request.Request(
                self.api_url("upload_transfer", {"transfer_id": transfer_id, "device_id": device_id}),
                headers=headers,
                data=source,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return dict(json.loads(response.read().decode("utf-8")))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise FreeCloudApiError(f"HTTP {exc.code} uploading transfer.", request.full_url, exc.code, body) from exc

    def fail_transfer(self, device_id: str, transfer_id: str, error: str) -> None:
        self.post_json(
            "fail_transfer",
            {"device_id": device_id, "transfer_id": transfer_id, "error": error[:500]},
        )


def config_path(local_root: Path) -> Path:
    return local_root / ".freecloud_client.json"


def state_path(local_root: Path) -> Path:
    return local_root / ".freecloud_state.json"


def default_device_name() -> str:
    hostname = socket.gethostname().strip()
    return hostname or "FreeCloud Node"


def new_device_id() -> str:
    return uuid.uuid4().hex


def build_setup_urls(domain_text: str, drive_text: str) -> tuple[str, str, str]:
    domain = normalize_domain(domain_text)
    drive_name = normalize_drive_name(drive_text)
    parsed = urllib.parse.urlparse(domain)
    domain_path = parsed.path.strip("/")

    if domain_path:
        if drive_text.strip() in {"", "FreeCloud"}:
            drive_name = domain_path.split("/")[-1]
        return domain, drive_name, domain.rstrip("/")

    return domain, drive_name, f"{domain}/{urllib.parse.quote(drive_name)}"


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    os.replace(temp_path, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_config(config: dict[str, Any]) -> None:
    save_json(LAST_CONFIG_PATH, config)
    local_root_value = config.get("local_root")
    legacy_paths = [LEGACY_LAST_CONFIG_PATH]
    if local_root_value:
        legacy_paths.append(config_path(Path(str(local_root_value)).expanduser()))
    for path in legacy_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def load_app_config() -> dict[str, Any]:
    cfg = load_json(LAST_CONFIG_PATH, {})
    if isinstance(cfg, dict) and cfg:
        return cfg
    legacy_cfg = load_json(LEGACY_LAST_CONFIG_PATH, {})
    return legacy_cfg if isinstance(legacy_cfg, dict) else {}


def load_device_config() -> dict[str, Any]:
    data = load_json(DEVICE_CONFIG_PATH, {})
    return data if isinstance(data, dict) else {}


def save_device_config(device: dict[str, Any]) -> None:
    save_json(DEVICE_CONFIG_PATH, device)


def load_node_rules() -> list[dict[str, Any]]:
    data = load_json(NODE_RULES_PATH, [])
    return [rule for rule in data if isinstance(rule, dict)] if isinstance(data, list) else []


def save_node_rules(rules: list[dict[str, Any]]) -> None:
    save_json(NODE_RULES_PATH, rules)


def normalize_rule_mode(mode: str) -> str:
    mode = mode.strip().lower().replace("-", "_")
    allowed = {"browse_only", "keep_offline", "sync", "upload_only", "download_only", "storage_only"}
    if mode not in allowed:
        raise ValueError(f"Unsupported sync mode: {mode}")
    return mode


def make_node_rule(local_path: str, remote: str, mode: str, recursive: bool = True) -> dict[str, Any]:
    local = Path(local_path).expanduser().resolve()
    if not local.exists():
        raise ValueError(f"Local path does not exist: {local}")
    return {
        "local_path": str(local),
        "remote_path": remote_path(remote),
        "mode": normalize_rule_mode(mode),
        "recursive": recursive,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def node_rule_manifest(rule: dict[str, Any], include_hashes: bool = False) -> list[dict[str, Any]]:
    local_root = Path(str(rule.get("local_path") or "")).expanduser()
    remote_root = remote_path(str(rule.get("remote_path") or ""))
    recursive = bool(rule.get("recursive", True))
    if not local_root.exists():
        return []

    entries: list[dict[str, Any]] = []

    def add_entry(path: Path) -> None:
        if path.is_symlink():
            return
        rel = "" if path == local_root else path.relative_to(local_root).as_posix()
        target = remote_path("/".join(part for part in (remote_root, rel) if part))
        if not target or is_protected_path(target):
            return
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        is_dir = path.is_dir()
        entry = {
            "path": target,
            "name": path.name if rel else (target.rsplit("/", 1)[-1] if target else path.name),
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else stat.st_size,
            "mtime": int(stat.st_mtime),
            "hash": "" if is_dir or not include_hashes else file_sha256(path),
        }
        entries.append(entry)

    if local_root.is_file():
        add_entry(local_root)
        return entries

    for current_root, dir_names, file_names in os.walk(local_root, followlinks=False):
        current = Path(current_root)
        if current == local_root:
            add_entry(current)
        elif recursive:
            add_entry(current)
        else:
            dir_names[:] = []

        kept_dirs = []
        for name in dir_names:
            path = current / name
            if path.is_symlink():
                continue
            rel = path.relative_to(local_root).as_posix()
            if is_protected_path(remote_path("/".join(part for part in (remote_root, rel) if part))):
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs if recursive else []

        for name in file_names:
            add_entry(current / name)
    return entries


def node_manifest(rules: list[dict[str, Any]], include_hashes: bool = False) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if normalize_rule_mode(str(rule.get("mode") or "browse_only")) not in {"storage_only", "sync", "upload_only"}:
            continue
        for entry in node_rule_manifest(rule, include_hashes=include_hashes):
            entries[str(entry["path"])] = entry
    return [entries[path] for path in sorted(entries)]


def missing_publish_roots(rules: list[dict[str, Any]]) -> list[str]:
    missing = []
    for rule in rules:
        try:
            mode = normalize_rule_mode(str(rule.get("mode") or "browse_only"))
        except ValueError:
            continue
        if mode not in {"storage_only", "sync", "upload_only"}:
            continue
        local_path = str(rule.get("local_path") or "")
        if local_path and not Path(local_path).expanduser().exists():
            missing.append(local_path)
    return missing


def server_rules_from_node_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    server_rules = []
    for rule in rules:
        try:
            mode = normalize_rule_mode(str(rule.get("mode") or "browse_only"))
        except ValueError:
            continue
        server_rules.append(
            {
                "path": remote_path(str(rule.get("remote_path") or "")),
                "mode": mode,
                "recursive": bool(rule.get("recursive", True)),
            }
        )
    return server_rules


def local_path_for_virtual_path(rules: list[dict[str, Any]], virtual_path: str) -> Path | None:
    """Resolve a published virtual file through the most-specific serving rule."""
    requested = remote_path(virtual_path)
    candidates: list[tuple[int, Path]] = []
    for rule in rules:
        try:
            mode = normalize_rule_mode(str(rule.get("mode") or "browse_only"))
        except ValueError:
            continue
        if mode not in {"storage_only", "sync", "upload_only"}:
            continue
        remote_root = remote_path(str(rule.get("remote_path") or ""))
        if requested != remote_root and not requested.startswith(remote_root + "/"):
            continue
        suffix = requested[len(remote_root) :].lstrip("/")
        if suffix and not bool(rule.get("recursive", True)) and "/" in suffix:
            continue
        local_root = Path(str(rule.get("local_path") or "")).expanduser().resolve()
        candidate = (local_root / suffix).resolve() if suffix else local_root
        try:
            candidate.relative_to(local_root)
        except ValueError:
            continue
        candidates.append((len(remote_root), candidate))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def process_node_transfers(client: FreeCloudClient, device_id: str, rules: list[dict[str, Any]]) -> int:
    processed = 0
    for transfer in client.poll_transfers(device_id):
        transfer_id = str(transfer.get("transfer_id") or "")
        path = str(transfer.get("path") or "")
        local_file = local_path_for_virtual_path(rules, path)
        try:
            if not transfer_id or local_file is None or not local_file.is_file():
                raise RuntimeError(f"Source file is unavailable: {path}")
            expected_size = int(transfer.get("size") or 0)
            if expected_size and local_file.stat().st_size != expected_size:
                raise RuntimeError(f"Source file size changed: {path}")
            client.upload_transfer(device_id, transfer_id, local_file)
            processed += 1
        except Exception as exc:
            if transfer_id:
                try:
                    client.fail_transfer(device_id, transfer_id, str(exc))
                except Exception:
                    pass
            raise
    return processed


def is_protected_path(path: str) -> bool:
    parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
    if not parts:
        return False
    if any(part in PROTECTED_DIR_NAMES for part in parts):
        return True
    if parts[0] in PROTECTED_NAMES:
        return True
    return (
        len(parts) >= 2
        and parts[0] == "server"
        and (parts[1] in PROTECTED_SERVER_DIR_NAMES or parts[1] in PROTECTED_SERVER_FILES)
    )


def local_manifest(local_root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for current_root, dir_names, file_names in os.walk(local_root, followlinks=False):
        current = Path(current_root)

        kept_dirs = []
        for name in dir_names:
            path = current / name
            rel = path.relative_to(local_root).as_posix()
            if is_protected_path(rel) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries[rel] = {
                "path": rel,
                "type": "dir",
                "size": 0,
                "mtime": int(stat.st_mtime),
            }
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        for name in file_names:
            path = current / name
            rel = path.relative_to(local_root).as_posix()
            if is_protected_path(rel) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries[rel] = {
                "path": rel,
                "type": "file",
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
    return entries


def remote_manifest(client: FreeCloudClient) -> dict[str, dict[str, Any]]:
    return {
        entry["path"]: entry
        for entry in client.manifest()
        if entry.get("path") and not is_protected_path(str(entry["path"]))
    }


def roughly_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("type") != b.get("type"):
        return False
    if a.get("type") == "dir":
        return True
    return int(a.get("size", -1)) == int(b.get("size", -2)) and abs(int(a.get("mtime", 0)) - int(b.get("mtime", 0))) <= 2


def sync_once(client: FreeCloudClient, local_root: Path, delete_remote: bool = False) -> dict[str, int]:
    local_root.mkdir(parents=True, exist_ok=True)
    saved_state = load_json(state_path(local_root), {})
    if not isinstance(saved_state, dict):
        saved_state = {}
    previous = {
        path: entry
        for path, entry in saved_state.items()
        if not is_protected_path(path)
    }
    local = local_manifest(local_root)
    remote = remote_manifest(client)
    all_paths = sorted(set(local) | set(remote) | set(previous))
    counts = {"uploaded": 0, "downloaded": 0, "deleted_local": 0, "deleted_remote": 0, "conflicts": 0}

    for path in all_paths:
        # Three-way sync: compare local and remote against the last manifest
        # so deletes and edits can be handled differently.
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
    password = validate_password(prompt("FreeCloud password", secret=True))
    setup_method = prompt("Setup method: manual or ftps", "manual").lower()
    base_url = f"{domain}/{urllib.parse.quote(drive_name)}"

    if setup_method in {"ftp", "ftps"}:
        host = prompt("FTPS host", urllib.parse.urlparse(domain).hostname or "")
        user = prompt("FTPS username")
        ftp_password = prompt("FTPS password", secret=True)
        public_html = prompt("Remote public_html path", "public_html")
        remote_root = posixpath.join(public_html.strip("/"), drive_name)
        print(f"Uploading web app to /{remote_root} ...")
        with ftplib.FTP_TLS(host, timeout=60) as ftp:
            ftp.login(user, ftp_password)
            ftp.prot_p()
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
    save_config(config)
    print(f"Saved config: {LAST_CONFIG_PATH}")
    return config


def load_or_setup(args: argparse.Namespace) -> dict[str, Any]:
    if args.config:
        return load_json(Path(args.config).expanduser().resolve(), {})
    if args.local:
        cfg = load_json(config_path(Path(args.local).expanduser().resolve()), {})
        if cfg:
            save_config(cfg)
            return cfg
    cfg = load_json(LAST_CONFIG_PATH, {})
    if cfg:
        return cfg
    legacy_cfg = load_json(LEGACY_LAST_CONFIG_PATH, {})
    if legacy_cfg:
        save_config(legacy_cfg)
        return legacy_cfg
    return run_setup()


def client_from_saved_config() -> FreeCloudClient:
    config = load_app_config()
    if not config:
        raise RuntimeError("No FreeCloud setup found. Run the desktop setup or `freecloud-cli node setup` first.")
    return FreeCloudClient(str(config["base_url"]), str(config.get("password", "")))


def run_node_setup(args: argparse.Namespace) -> int:
    config = load_app_config()
    domain_text = str(args.domain or config.get("domain") or config.get("base_url") or prompt("Domain", "https://www.example.com"))
    drive_text = str(args.drive or config.get("drive_name") or "FreeCloud")
    password = str(args.password or config.get("password") or prompt("FreeCloud password", secret=True))
    validate_password(password)
    domain, drive_name, base_url = build_setup_urls(domain_text, drive_text)

    client = FreeCloudClient(base_url, password)
    try:
        ping = client.ping()
    except FreeCloudApiError as exc:
        if exc.code == 409:
            ping = {"setup": False}
        else:
            explain_connection_failure(exc, drive_name, base_url)
            return 1

    if not ping.get("setup"):
        client.setup(drive_name, password)

    existing_device = load_device_config()
    device_id = str(args.device_id or existing_device.get("device_id") or new_device_id())
    name = str(args.name or existing_device.get("name") or default_device_name())
    device_type = str(args.type or existing_device.get("type") or "raspberry_pi")
    response = client.register_device(
        name=name,
        device_type=device_type,
        device_id=device_id,
        capabilities=["metadata", "storage_node", "keep_offline", "autostart"],
    )
    device = dict(response.get("device") or {})
    if not device:
        raise RuntimeError("The server did not return device metadata.")

    save_config(
        {
            **config,
            "domain": domain,
            "drive_name": drive_name,
            "base_url": client.base_url,
            "password": password,
            "interval": int(args.interval or config.get("interval") or DEFAULT_INTERVAL),
        }
    )
    save_device_config(device)
    print(f"Registered device: {device.get('name')} ({device.get('device_id')})")
    print(f"Saved device config: {DEVICE_CONFIG_PATH}")
    return 0


def require_device_id() -> str:
    device = load_device_config()
    device_id = str(device.get("device_id") or "")
    if not device_id:
        raise RuntimeError("No device is registered. Run `freecloud-cli node setup` first.")
    return device_id


def run_node_status(_args: argparse.Namespace) -> int:
    config = load_app_config()
    device = load_device_config()
    rules = load_node_rules()
    print(f"Server: {config.get('base_url', 'not configured')}")
    print(f"Device: {device.get('name', 'not registered')} ({device.get('device_id', 'no id')})")
    print(f"Rules: {len(rules)}")
    for index, rule in enumerate(rules, start=1):
        print(f"  {index}. {rule.get('local_path')} -> /{rule.get('remote_path')} [{rule.get('mode')}]")
    return 0


def run_node_rules_list(_args: argparse.Namespace) -> int:
    rules = load_node_rules()
    if not rules:
        print("No node sync rules configured.")
        return 0
    for index, rule in enumerate(rules, start=1):
        recursive = "recursive" if rule.get("recursive", True) else "single level"
        print(f"{index}. {rule.get('local_path')} -> /{rule.get('remote_path')} [{rule.get('mode')}, {recursive}]")
    return 0


def run_node_rules_add(args: argparse.Namespace) -> int:
    rules = load_node_rules()
    rule = make_node_rule(args.local_path, args.remote, args.mode, recursive=not args.no_recursive)
    rules.append(rule)
    save_node_rules(rules)
    print(f"Added rule: {rule['local_path']} -> /{rule['remote_path']} [{rule['mode']}]")
    return 0


def run_node_rules_remove(args: argparse.Namespace) -> int:
    rules = load_node_rules()
    index = args.index - 1
    if index < 0 or index >= len(rules):
        raise RuntimeError("Rule index out of range.")
    removed = rules.pop(index)
    save_node_rules(rules)
    print(f"Removed rule: {removed.get('local_path')} -> /{removed.get('remote_path')}")
    return 0


def run_node_publish(args: argparse.Namespace) -> int:
    device_id = require_device_id()
    rules = load_node_rules()
    missing = missing_publish_roots(rules)
    if missing:
        raise RuntimeError(
            "Configured storage folder is missing; refusing to publish an empty listing: "
            + ", ".join(missing)
        )
    entries = node_manifest(rules, include_hashes=args.hashes)
    client = client_from_saved_config()
    client.heartbeat(device_id)
    client.set_sync_rules(device_id, server_rules_from_node_rules(rules))
    response = client.publish_manifest(device_id, entries)
    save_json(
        NODE_STATE_PATH,
        {
            "last_published_at": int(time.time()),
            "device_id": device_id,
            "entry_count": len(entries),
        },
    )
    print(f"Published {response.get('published', len(entries))} metadata entries.")
    return 0


def run_node_daemon(args: argparse.Namespace) -> int:
    interval = max(5, int(args.interval or load_app_config().get("interval") or 60))
    print(f"FreeCloud node daemon running every {interval} seconds. Press Ctrl+C to stop.")
    while True:
        try:
            run_node_publish(argparse.Namespace(hashes=args.hashes))
            device_id = require_device_id()
            client = client_from_saved_config()
            processed = process_node_transfers(client, device_id, load_node_rules())
            if processed:
                print(f"Completed {processed} transfer(s).")
        except KeyboardInterrupt:
            print("Stopped.")
            return 0
        except Exception as exc:
            print(f"Node error: {exc}", file=sys.stderr)
            try:
                NODE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with NODE_LOG_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}\n")
            except OSError:
                pass
        time.sleep(interval)


def systemd_user_service_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "systemd" / "user" / "freecloud-node.service"


def service_exec_command() -> str:
    script = Path(__file__).resolve()
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} node daemon"


def run_node_autostart(args: argparse.Namespace) -> int:
    path = systemd_user_service_path()
    if args.autostart_action == "status":
        print(f"Service file: {path}")
        print("Enabled here." if path.is_file() else "Not enabled here.")
        return 0

    if args.autostart_action == "disable":
        try:
            path.unlink()
            print(f"Removed {path}")
        except FileNotFoundError:
            print("Autostart was not enabled.")
        print("Run `systemctl --user daemon-reload` if systemd is active.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    service = f"""[Unit]
Description=FreeCloud headless node
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={service_exec_command()}
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""
    path.write_text(service, encoding="utf-8")
    print(f"Wrote {path}")
    print("To activate now, run:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now freecloud-node.service")
    print("For a Raspberry Pi that should run before login, also run:")
    print(f"  loginctl enable-linger {getpass.getuser()}")
    return 0


def build_node_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FreeCloud headless node commands")
    sub = parser.add_subparsers(dest="node_command", required=True)

    setup = sub.add_parser("setup", help="Register this machine as a FreeCloud device")
    setup.add_argument("--domain", help="Domain or full FreeCloud URL")
    setup.add_argument("--drive", help="Drive folder name", default="")
    setup.add_argument("--password", help="FreeCloud password")
    setup.add_argument("--name", help="Device name")
    setup.add_argument("--type", default="raspberry_pi", help="Device type")
    setup.add_argument("--device-id", help="Reuse a specific device id")
    setup.add_argument("--interval", type=int, help="Default daemon interval")
    setup.set_defaults(func=run_node_setup)

    rules = sub.add_parser("rules", help="Manage node folder rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_list = rules_sub.add_parser("list", help="List configured rules")
    rules_list.set_defaults(func=run_node_rules_list)
    rules_add = rules_sub.add_parser("add", help="Add a local folder rule")
    rules_add.add_argument("local_path")
    rules_add.add_argument("--remote", required=True, help="Remote FreeCloud path")
    rules_add.add_argument("--mode", default="storage_only", help="storage_only, sync, upload_only, download_only, keep_offline, browse_only")
    rules_add.add_argument("--no-recursive", action="store_true", help="Only publish direct files")
    rules_add.set_defaults(func=run_node_rules_add)
    rules_remove = rules_sub.add_parser("remove", help="Remove a rule by number")
    rules_remove.add_argument("index", type=int)
    rules_remove.set_defaults(func=run_node_rules_remove)

    publish = sub.add_parser("publish", help="Publish metadata for configured node folders")
    publish.add_argument("--hashes", action="store_true", help="Hash files while publishing")
    publish.set_defaults(func=run_node_publish)

    daemon = sub.add_parser("daemon", help="Continuously publish node metadata")
    daemon.add_argument("--interval", type=int, help="Polling interval in seconds")
    daemon.add_argument("--hashes", action="store_true", help="Hash files while publishing")
    daemon.set_defaults(func=run_node_daemon)

    status = sub.add_parser("status", help="Show node setup and rules")
    status.set_defaults(func=run_node_status)

    autostart = sub.add_parser("autostart", help="Manage user systemd autostart")
    autostart.add_argument("autostart_action", choices=["enable", "disable", "status"])
    autostart.set_defaults(func=run_node_autostart)
    return parser


def legacy_sync_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FreeCloud Linux CLI sync tool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", help="Path to .freecloud_client.json")
    parser.add_argument("--local", help="Local sync folder")
    parser.add_argument("--once", action="store_true", help="Run one sync pass and exit")
    parser.add_argument("--interval", type=int, help="Polling interval in seconds")
    parser.add_argument("--delete-remote", action="store_true", help="Propagate local deletions to the remote server")
    args = parser.parse_args(argv)

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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "node":
        parser = build_node_parser()
        args = parser.parse_args(argv[1:])
        try:
            return int(args.func(args))
        except (RuntimeError, ValueError, FreeCloudApiError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return legacy_sync_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
