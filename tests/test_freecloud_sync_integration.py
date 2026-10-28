"""Real-HTTP synchronization and data-safety regression tests.

Every test starts a disposable ``php -S`` server on 127.0.0.1 that serves a
temporary copy of all ``server/*.php`` files with its own empty
``freecloud_files`` / ``freecloud_meta``. Devices are simulated with separate
temporary roots and profiles; the end-to-end tests drive the production
``freecloud_cli`` sync functions (see ``sync_device_driver.py``), so manifests,
reconciliation jobs, uploads, downloads and SHA-256 checks all travel over the
real JSON/HTTP API. Nothing here touches real user files or metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import freecloud_cli as cli

# FREECLOUD_IT_SERVER_SOURCE lets a developer compare against another server copy.
SERVER_SOURCE = Path(os.environ.get("FREECLOUD_IT_SERVER_SOURCE") or REPO / "server")
DRIVER = Path(__file__).with_name("sync_device_driver.py")
PASSWORD = "integration-test-password"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PhpTestServer:
    """A throwaway FreeCloud server rooted in a temporary directory."""

    def __init__(self, base: Path, workers: int = 1) -> None:
        self.root = base / "srv"
        self.root.mkdir(parents=True)
        for source in SERVER_SOURCE.glob("*.php"):
            shutil.copy2(source, self.root / source.name)
        self.workers = workers
        self.log = (base / "php-server.log").open("w+b")
        self._launch()
        status, body = self.call(
            "setup", method="POST", authenticated=False,
            data=urllib.parse.urlencode({"name": "IT", "password": PASSWORD}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            raise RuntimeError(f"setup failed: {status} {body!r}")

    @property
    def files_root(self) -> Path:
        return self.root / "freecloud_files"

    @property
    def meta_root(self) -> Path:
        return self.root / "freecloud_meta"

    def _launch(self) -> None:
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        if self.workers > 1:
            env["PHP_CLI_SERVER_WORKERS"] = str(self.workers)
        self.process = subprocess.Popen(
            ["php", "-d", "display_errors=stderr", "-d", "log_errors=0",
             "-S", f"127.0.0.1:{self.port}", "-t", str(self.root)],
            stdout=self.log, stderr=subprocess.STDOUT, env=env,
        )
        deadline = time.time() + 15
        while True:
            try:
                status, _body = self.call("ping", authenticated=False)
                if status in (200, 401, 503):
                    return
            except OSError:
                pass
            if time.time() > deadline or self.process.poll() is not None:
                self.stop()
                raise RuntimeError("php -S test server did not start")
            time.sleep(0.1)

    def _terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def restart(self) -> None:
        """Start a fresh PHP process (empty OPcache), like reloading php-fpm."""
        self._terminate()
        self._launch()

    def stop(self) -> None:
        self._terminate()
        if not self.log.closed:
            self.log.close()

    def php_errors(self) -> str:
        self.log.flush()
        text = Path(self.log.name).read_text(encoding="utf-8", errors="replace")
        return "\n".join(
            line for line in text.splitlines()
            if "PHP Fatal" in line or "PHP Warning" in line or "PHP Parse" in line
            or "Fatal error" in line or "Warning:" in line
        )

    def call(
        self,
        action: str,
        *,
        params: dict[str, str] | None = None,
        raw_query: str = "",
        data: bytes | None = None,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        authenticated: bool = True,
    ) -> tuple[int, bytes]:
        query = urllib.parse.urlencode({"action": action, **(params or {})})
        if raw_query:
            query += "&" + raw_query
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["X-FreeCloud-Password"] = PASSWORD
        if json_body is not None:
            data = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(
            f"{self.base_url}/freecloud_api.php?{query}", data=data, headers=request_headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, action: str, **kwargs: object) -> tuple[int, dict[str, object]]:
        status, body = self.call(action, **kwargs)  # type: ignore[arg-type]
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {"_raw": body.decode("utf-8", errors="replace")}
        return status, decoded if isinstance(decoded, dict) else {"_value": decoded}

    def ok(self, action: str, **kwargs: object) -> dict[str, object]:
        status, data = self.json(action, **kwargs)
        if status != 200 or data.get("ok") is False:
            raise AssertionError(f"{action} failed: HTTP {status} {data}\n{self.php_errors()}")
        return data

    def catalog(self) -> dict[str, object]:
        path = self.meta_root / "files.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def entries(self) -> dict[str, dict[str, object]]:
        return {str(item["path"]): item for item in self.ok("metadata_manifest")["entries"]}  # type: ignore[index]

    def jobs(self) -> list[dict[str, object]]:
        return [json.loads(path.read_text()) for path in (self.meta_root / "reconciliation").glob("*.json")]


@unittest.skipUnless(shutil.which("php"), "PHP is required")
class ServerTestCase(unittest.TestCase):
    workers = 1

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="freecloud-it-")
        self.base = Path(self.temp.name)
        self.server = PhpTestServer(self.base, workers=self.workers)

    def tearDown(self) -> None:
        errors = self.server.php_errors()
        self.server.stop()
        self.temp.cleanup()
        if errors and not getattr(self, "_allow_php_warnings", False):
            self.fail(f"PHP reported warnings/errors during the test:\n{errors}")

    # Manual protocol devices, for precise safety scenarios.
    def register(self, device_id: str, name: str, device_type: str = "desktop") -> None:
        self.server.ok("register_device", json_body={
            "device_id": device_id, "name": name, "type": device_type,
            "capabilities": ["metadata", "local_storage", "desktop"],
        })

    def publish(self, device_id: str, entries: list[dict[str, object]], complete: bool = True) -> dict[str, object]:
        return self.server.ok("publish_manifest", json_body={
            "device_id": device_id, "entries": entries, "complete": complete,
        })

    def poll(self, device_id: str) -> list[dict[str, object]]:
        return list(self.server.ok("poll_reconciliation", json_body={"device_id": device_id})["jobs"])  # type: ignore[arg-type]

    @staticmethod
    def file_entry(path: str, data: bytes, mtime: int, with_hash: bool = True) -> dict[str, object]:
        return {
            "path": path, "name": path.rsplit("/", 1)[-1], "type": "file", "size": len(data),
            "mtime": mtime, "hash": sha256_bytes(data) if with_hash else "",
        }

    @staticmethod
    def dir_entry(path: str, mtime: int) -> dict[str, object]:
        return {"path": path, "name": path.rsplit("/", 1)[-1], "type": "dir", "size": 0, "mtime": mtime, "hash": ""}

    def upload(self, path: str, data: bytes, mtime: int | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
        params = {"path": path}
        if mtime is not None:
            params["mtime"] = str(mtime)
        return self.server.json("upload", params=params, data=data, method="POST",
                                headers={"Content-Type": "application/octet-stream", **(headers or {})})


class DeviceSandbox:
    """One simulated desktop device with its own folder and private profile."""

    def __init__(self, base: Path, name: str, server: PhpTestServer) -> None:
        self.server = server
        self.home = base / f"device-{name}"
        self.root = self.home / "FreeCloud"
        self.root.mkdir(parents=True)
        config = self.home / "config" / "freecloud"
        config.mkdir(parents=True)
        (config / "device.json").write_text(json.dumps({"name": f"Device {name}"}), encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.device_id = ""

    def cycle(self, rounds: int = 6) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(DRIVER), self.server.base_url, PASSWORD, str(self.root), "--rounds", str(rounds)],
            env=self.env, capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(f"device cycle failed:\n{result.stderr}\n{result.stdout}")
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.device_id = str(summary["device_id"])
        return summary

    def write(self, relative: str, data: bytes, mtime: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path


def settle(*devices: DeviceSandbox, passes: int = 4) -> None:
    """Let every device run several real cycles, like periodic background polls."""
    for _ in range(passes):
        for device in devices:
            device.cycle()


# ---------------------------------------------------------------------------
# Data-safety regressions (Phase 1A)
# ---------------------------------------------------------------------------


class UploadSafetyTests(ServerTestCase):
    def test_failed_upload_verification_keeps_existing_file(self) -> None:
        original = b"original contents that must survive\n" * 50
        status, data = self.upload("docs/report.txt", original, mtime=1_700_000_000)
        self.assertEqual(status, 200, data)
        target = self.server.files_root / "docs" / "report.txt"
        self.assertEqual(target.read_bytes(), original)

        replacement = b"replacement" * 100
        status, data = self.upload("docs/report.txt", replacement, mtime=1_700_000_500,
                                   headers={"X-FreeCloud-SHA256": "0" * 64})
        self.assertEqual(status, 422, data)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(int(target.stat().st_mtime), 1_700_000_000)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["report.txt"], "temp file left behind")

    def test_upload_with_matching_hash_replaces_atomically_and_sets_mtime(self) -> None:
        self.upload("a.txt", b"one", mtime=1_700_000_000)
        replacement = b"two two two"
        status, data = self.upload("a.txt", replacement, mtime=1_700_000_900,
                                   headers={"X-FreeCloud-SHA256": sha256_bytes(replacement)})
        self.assertEqual(status, 200, data)
        target = self.server.files_root / "a.txt"
        self.assertEqual(target.read_bytes(), replacement)
        self.assertEqual(int(target.stat().st_mtime), 1_700_000_900)

    def test_interrupted_upload_connection_keeps_existing_file(self) -> None:
        original = b"keep me" * 1000
        self.upload("big.bin", original, mtime=1_700_000_000)
        target = self.server.files_root / "big.bin"
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=10) as sock:
            head = (
                f"POST /freecloud_api.php?action=upload&path=big.bin&mtime=1700009999 HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\nX-FreeCloud-Password: {PASSWORD}\r\n"
                f"Content-Type: application/octet-stream\r\nContent-Length: 1000000\r\nConnection: close\r\n\r\n"
            ).encode()
            sock.sendall(head + b"x" * 4096)
            sock.shutdown(socket.SHUT_WR)
            try:
                sock.recv(4096)
            except OSError:
                pass
        time.sleep(0.5)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual([p.name for p in self.server.files_root.iterdir() if p.name != ".htaccess"], ["big.bin"])

    def test_short_stream_never_replaces_destination(self) -> None:
        """Simulates a body that ends early (client abort, FPM timeout) inside PHP itself."""
        destination = self.base / "dest.txt"
        destination.write_bytes(b"precious")
        script = r"""
require getenv('LIFECYCLE');
$in = fopen('php://memory', 'w+b'); fwrite($in, str_repeat('n', 10)); rewind($in);
$result = fc_install_stream_atomically(getenv('DEST'), $in, 1000, '', 1700000000);
echo json_encode($result);
"""
        env = dict(os.environ, LIFECYCLE=str(self.server.root / "freecloud_lifecycle.php"), DEST=str(destination))
        out = subprocess.run(["php", "-r", script], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(destination.read_bytes(), b"precious")
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.name.startswith(".dest")), [])

    def test_save_action_is_atomic_and_rejects_bad_hash(self) -> None:
        self.upload("notes.txt", b"draft 1", mtime=1_700_000_000)
        status, data = self.server.json("save", params={"path": "notes.txt"}, data=b"draft 2", method="POST",
                                        headers={"X-FreeCloud-SHA256": "f" * 64})
        self.assertEqual(status, 422, data)
        self.assertEqual((self.server.files_root / "notes.txt").read_bytes(), b"draft 1")
        status, data = self.server.json("save", params={"path": "notes.txt"}, data=b"draft 2", method="POST")
        self.assertEqual(status, 200, data)
        self.assertEqual((self.server.files_root / "notes.txt").read_bytes(), b"draft 2")

    def test_in_progress_upload_temp_files_are_never_catalogued(self) -> None:
        self.upload("real.txt", b"real", mtime=1_700_000_000)
        (self.server.files_root / ".real.txt.freecloud-upload-0123456789abcdef").write_bytes(b"partial")
        (self.server.files_root / ".real.txt.freecloud-part-0123456789abcdef0123456789abcdef").write_bytes(b"p")
        self.assertEqual(sorted(self.server.entries()), ["real.txt"])


class DeploymentCompatibilityTests(ServerTestCase):
    def test_api_with_outdated_lifecycle_helper_answers_503_not_fatal(self) -> None:
        self._allow_php_warnings = True
        lifecycle = self.server.root / "freecloud_lifecycle.php"
        text = lifecycle.read_text(encoding="utf-8")
        lifecycle.write_text(text.replace("const FC_LIFECYCLE_VERSION = 2;", ""), encoding="utf-8")
        self.server.restart()  # php -S caches compiled files, like php-fpm OPcache
        status, data = self.server.json("ping")
        self.assertEqual(status, 503)
        self.assertIn("out of sync", str(data.get("error")))


class CatalogIntegrityTests(ServerTestCase):
    def test_invalid_utf8_upload_path_is_rejected_without_damaging_catalog(self) -> None:
        self.upload("keep.txt", b"keep", mtime=1_700_000_000)
        before = self.server.catalog()
        self.assertIn("keep.txt", before)
        status, data = self.server.json("upload", raw_query="path=bad%FFname.txt", data=b"x", method="POST",
                                        headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 400, data)
        self.assertEqual(self.server.catalog(), before)
        self.assertFalse(any(b"bad" in os.fsencode(p.name) for p in self.server.files_root.iterdir()))

    def test_invalid_utf8_name_on_disk_is_skipped_not_deleted_and_catalog_survives(self) -> None:
        device = "a" * 32
        self.register(device, "Laptop")
        self.publish(device, [self.file_entry("doc.txt", b"doc", 1_700_000_000)])
        bad = os.path.join(os.fsencode(self.server.files_root), b"latin1-\xe9t\xe9.txt")
        with open(bad, "wb") as handle:
            handle.write(b"latin-1 named file")
        self.poll(device)
        self.publish(device, [self.file_entry("doc.txt", b"doc", 1_700_000_000)])
        catalog = self.server.catalog()
        self.assertIn("doc.txt", catalog)
        self.assertTrue(os.path.exists(bad), "a non-UTF-8 file must never be deleted")
        self.assertIn("doc.txt", self.server.entries())

    def test_invalid_utf8_json_manifest_is_rejected_without_writing(self) -> None:
        device = "a" * 32
        self.register(device, "Laptop")
        self.publish(device, [self.file_entry("doc.txt", b"doc", 1_700_000_000)])
        before = (self.server.meta_root / "files.json").read_bytes()
        body = b'{"device_id":"' + b"a" * 32 + b'","complete":true,"entries":[{"path":"x\xff.txt","type":"file","size":1,"mtime":1}]}'
        status, _ = self.server.call("publish_manifest", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        self.assertNotEqual(status, 200)
        self.assertEqual((self.server.meta_root / "files.json").read_bytes(), before)

    def test_corrupt_catalog_fails_closed_and_is_not_overwritten(self) -> None:
        self._allow_php_warnings = True
        device = "a" * 32
        self.register(device, "Laptop")
        self.publish(device, [self.file_entry("doc.txt", b"doc", 1_700_000_000)])
        corrupt = b'{"doc.txt": {"path": "doc.txt", "locat'  # truncated write / power loss
        for name in ("files.json", "tombstones.json"):
            path = self.server.meta_root / name
            original = path.read_bytes() if path.exists() else b"{}"
            path.write_bytes(corrupt)
            status, data = self.server.json("publish_manifest", json_body={
                "device_id": device, "complete": True, "entries": [self.file_entry("other.txt", b"o", 1)],
            })
            self.assertEqual(status, 503, (name, data))
            self.assertEqual(path.read_bytes(), corrupt, f"{name} was overwritten")
            status, data = self.server.json("poll_reconciliation", json_body={"device_id": device})
            self.assertEqual(status, 503, (name, data))
            self.assertEqual(path.read_bytes(), corrupt, f"{name} was overwritten by poll")
            path.write_bytes(original)

    def test_empty_catalog_file_is_treated_as_corrupt(self) -> None:
        self._allow_php_warnings = True
        device = "a" * 32
        self.register(device, "Laptop")
        self.publish(device, [self.file_entry("doc.txt", b"doc", 1_700_000_000)])
        (self.server.meta_root / "files.json").write_bytes(b"")
        status, _ = self.server.json("publish_manifest", json_body={"device_id": device, "complete": True, "entries": []})
        self.assertEqual(status, 503)
        self.assertEqual((self.server.meta_root / "files.json").read_bytes(), b"")


class LastCopyProtectionTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.laptop = "a" * 32
        self.register(self.laptop, "Laptop")
        self.data = b"only two copies of this exist"
        self.upload("photo.jpg", self.data, mtime=1_700_000_000)
        self.publish(self.laptop, [self.file_entry("photo.jpg", self.data, 1_700_000_000)])
        self.web_copy = self.server.files_root / "photo.jpg"

    def set_off(self, device_id: str, path: str = "photo.jpg", recursive: bool = False) -> dict[str, object]:
        status, data = self.server.json("set_desired_location", json_body={
            "device_id": device_id, "path": path, "desired": False, "recursive": recursive,
        })
        self.assertEqual(status, 200, data)
        return data

    def test_sequential_off_toggles_cannot_remove_the_last_copy(self) -> None:
        self.set_off(self.laptop)
        removal_jobs = [j for j in self.server.jobs() if j.get("status") == "queued_remove"]
        self.assertEqual([j["destination_device_id"] for j in removal_jobs], [self.laptop])
        self.set_off("web_server")
        self.assertTrue(self.web_copy.exists(), "Web copy deleted while the laptop copy is also being removed")
        self.assertEqual(self.web_copy.read_bytes(), self.data)

    def test_web_off_first_then_laptop_off_is_blocked(self) -> None:
        self.set_off("web_server")
        self.assertFalse(self.web_copy.exists())
        self.set_off(self.laptop)
        self.assertEqual([j for j in self.server.jobs() if j.get("status") == "queued_remove"], [])

    def test_non_recursive_folder_off_never_deletes_children(self) -> None:
        self.upload("Album/only-on-web.jpg", b"unique", mtime=1_700_000_000)
        self.publish(self.laptop, [
            self.file_entry("photo.jpg", self.data, 1_700_000_000),
            self.dir_entry("Album", 1_700_000_000),
        ])
        self.set_off("web_server", path="Album", recursive=False)
        self.assertEqual((self.server.files_root / "Album" / "only-on-web.jpg").read_bytes(), b"unique")


class ConcurrentLastCopyTests(ServerTestCase):
    workers = 4

    def test_concurrent_off_requests_keep_at_least_one_copy(self) -> None:
        laptop = "a" * 32
        self.register(laptop, "Laptop")
        data = b"concurrency victim"
        self.upload("x.bin", data, mtime=1_700_000_000)
        self.publish(laptop, [self.file_entry("x.bin", data, 1_700_000_000)])
        results: list[tuple[int, dict[str, object]]] = []

        def off(device_id: str) -> None:
            results.append(self.server.json("set_desired_location", json_body={
                "device_id": device_id, "path": "x.bin", "desired": False, "recursive": False,
            }))

        threads = [threading.Thread(target=off, args=(d,)) for d in (laptop, "web_server")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        web_kept = (self.server.files_root / "x.bin").exists()
        laptop_queued = any(j.get("status") == "queued_remove" and j.get("destination_device_id") == laptop
                            for j in self.server.jobs())
        self.assertTrue(web_kept or not laptop_queued, f"both copies scheduled for removal: {results}")


class TombstoneRecreationTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.laptop = "a" * 32
        self.phone = "b" * 32
        self.register(self.laptop, "Laptop")
        self.register(self.phone, "Phone", "android")
        self.old = 1_700_000_000
        self.old_entries = [
            self.dir_entry("Proj", self.old),
            self.file_entry("Proj/old-a.txt", b"old a", self.old),
            self.file_entry("Proj/old-b.txt", b"old b", self.old),
        ]
        self.publish(self.laptop, self.old_entries)
        self.publish(self.phone, self.old_entries)
        self.tombstone = self.server.ok("global_delete", json_body={"path": "Proj", "recursive": True})["tombstone"]
        self.deleted_at = int(self.tombstone["deleted_at"])  # type: ignore[index]
        # The laptop processes its delete job and reports an empty folder.
        for job in self.poll(self.laptop):
            if job.get("status") == "queued_delete":
                self.server.ok("ack_reconciliation", json_body={
                    "device_id": self.laptop, "job_id": job["job_id"], "status": "deleted",
                })
        self.publish(self.laptop, [])

    def delete_jobs_for(self, device_id: str) -> list[dict[str, object]]:
        return [j for j in self.poll(device_id) if j.get("status") == "queued_delete"]

    def test_new_file_under_deleted_name_is_not_deleted(self) -> None:
        new_mtime = self.deleted_at + 60
        self.publish(self.laptop, [
            self.dir_entry("Proj", new_mtime),
            self.file_entry("Proj/new-work.txt", b"new work", new_mtime),
        ])
        self.assertEqual(self.delete_jobs_for(self.laptop), [], "new content was scheduled for deletion")
        entries = self.server.entries()
        self.assertIn("Proj/new-work.txt", entries)
        self.assertFalse(entries["Proj/new-work.txt"].get("deleted"))
        desired = {d["device_id"] for d in entries["Proj/new-work.txt"]["desired_locations"]}  # type: ignore[index]
        self.assertEqual(desired, {"web_server", self.laptop, self.phone})
        self.assertNotIn("Proj/old-a.txt", entries)

    def test_stale_device_copy_is_still_deleted_and_not_resurrected(self) -> None:
        # The phone was offline during deletion and reconnects with its old files.
        self.publish(self.phone, self.old_entries)
        jobs = self.delete_jobs_for(self.phone)
        self.assertEqual([(j["path"], j.get("kind")) for j in jobs], [("Proj", "global_delete")])
        entries = self.server.entries()
        self.assertNotIn("Proj/old-a.txt", entries)
        self.assertNotIn("Proj/old-b.txt", entries)

    def test_stale_device_holding_new_content_gets_no_blind_folder_delete(self) -> None:
        new_mtime = self.deleted_at + 60
        # The phone (offline during deletion) also created new work inside Proj.
        self.publish(self.phone, self.old_entries + [self.file_entry("Proj/phone-new.txt", b"fresh", new_mtime)])
        self.assertEqual(self.delete_jobs_for(self.phone), [],
                         "a recursive folder delete would destroy the phone's new file")
        entries = self.server.entries()
        self.assertIn("Proj/phone-new.txt", entries)
        self.assertNotIn("Proj/old-a.txt", entries, "stale copies must stay hidden")

    def test_repeat_global_delete_removes_recreated_content(self) -> None:
        new_mtime = self.deleted_at + 60
        self.publish(self.laptop, [self.dir_entry("Proj", new_mtime), self.file_entry("Proj/n.txt", b"n", new_mtime)])
        self.assertIn("Proj/n.txt", self.server.entries())
        time.sleep(1.1)
        self.server.ok("global_delete", json_body={"path": "Proj", "recursive": True})
        self.assertNotIn("Proj/n.txt", self.server.entries())
        jobs = self.delete_jobs_for(self.laptop)
        self.assertEqual([j["path"] for j in jobs], ["Proj"])


class StaleReconnectTests(ServerTestCase):
    def test_reconnecting_device_with_older_copy_never_becomes_the_source(self) -> None:
        laptop, phone = "a" * 32, "b" * 32
        self.register(laptop, "Laptop")
        self.register(phone, "Phone")
        old, new = b"old", b"newer edited version"
        # Everyone agrees on the old revision.
        self.upload("doc.txt", old, mtime=1_760_000_000)
        self.publish(phone, [self.file_entry("doc.txt", old, 1_760_000_000)])
        self.publish(laptop, [self.file_entry("doc.txt", old, 1_760_000_000)])
        self.poll(laptop)
        # The phone goes offline; the laptop edits and the edit reaches the Web
        # Server through the normal reconciliation upload + commit.
        self.publish(laptop, [self.file_entry("doc.txt", new, 1_760_001_500)])
        upload_job = next(j for j in self.poll(laptop)
                          if j.get("status") == "queued_source" and j.get("destination_device_id") == "web_server")
        status, data = self.server.json(
            "upload_reconciliation", params={"job_id": str(upload_job["job_id"]), "device_id": laptop},
            data=new, method="POST",
            headers={"X-FreeCloud-SHA256": sha256_bytes(new), "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 200, data)
        self.server.ok("commit_reconciliation", json_body={
            "job_id": upload_job["job_id"], "device_id": laptop, "source_unchanged": True,
        })
        self.assertEqual((self.server.files_root / "doc.txt").read_bytes(), new)
        accepted = json.loads((self.server.meta_root / "accepted_versions.json").read_text())["doc.txt"]
        self.assertEqual(accepted["mtime"], 1_760_001_500)
        # The phone reconnects and reports its old copy.
        self.publish(phone, [self.file_entry("doc.txt", old, 1_760_000_000)])
        self.poll(phone)
        self.poll(laptop)
        live = [j for j in self.server.jobs() if j.get("path") == "doc.txt"
                and j.get("status") in {"queued_source", "staged", "ready", "queued_hash"}]
        self.assertFalse([j for j in live if j.get("source_device_id") == phone],
                         f"stale phone copy chosen as source: {live}")
        to_phone = [j for j in live if j.get("destination_device_id") == phone]
        self.assertTrue(to_phone, "phone should receive the newer revision")
        self.assertTrue(all(int(j["expected_mtime"]) == 1_760_001_500 for j in to_phone))
        self.assertEqual((self.server.files_root / "doc.txt").read_bytes(), new)


# ---------------------------------------------------------------------------
# End-to-end synchronization with real CLI devices over HTTP
# ---------------------------------------------------------------------------


class EndToEndSyncTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = DeviceSandbox(self.base, "A", self.server)
        self.b = DeviceSandbox(self.base, "B", self.server)
        self.a.cycle()
        self.b.cycle()

    def test_new_file_reaches_server_and_other_device_with_matching_hash(self) -> None:
        data = b"hello from device A\n" * 20
        self.a.write("notes/hello.txt", data, mtime=1_760_000_000)
        settle(self.a, self.b)

        entries = self.server.entries()
        self.assertIn("notes/hello.txt", entries, "catalog publication")
        entry = entries["notes/hello.txt"]
        desired = {d["device_id"] for d in entry["desired_locations"]}  # type: ignore[index]
        self.assertEqual(desired, {"web_server", self.a.device_id, self.b.device_id}, "default desired ON")

        received = self.b.root / "notes" / "hello.txt"
        self.assertTrue(received.is_file(), "device B received the bytes")
        self.assertEqual(sha256_file(received), sha256_bytes(data))
        self.assertEqual(int(received.stat().st_mtime), 1_760_000_000)
        self.assertEqual(sha256_file(self.server.files_root / "notes" / "hello.txt"), sha256_bytes(data))
        actual = {loc["device_id"] for loc in entry["locations"]}  # type: ignore[index]
        self.assertTrue({self.a.device_id}.issubset(actual))

    def test_background_cycle_publishes_stable_new_folder_and_syncs_contents(self) -> None:
        data = b"background inventory sync\n"
        self.a.write("copied/nested/file.txt", data, mtime=1_760_000_000)
        client = cli.FreeCloudClient(self.server.base_url, PASSWORD)
        state: dict[str, str] = {}
        device_config = self.a.home / "config" / "freecloud" / "device.json"

        with mock.patch.object(cli, "DEVICE_CONFIG_PATH", device_config):
            _first_counts, first_ready, _first_local = cli.desktop_background_cycle(client, self.a.root, state)
            self.assertFalse(first_ready)
            self.assertNotIn("copied/nested/file.txt", self.server.entries())

            second_counts, second_ready, _second_local = cli.desktop_background_cycle(client, self.a.root, state)
            self.assertTrue(second_ready)
            self.assertEqual(second_counts["failed"], 0)

        entry = self.server.entries()["copied/nested/file.txt"]
        desired = {item["device_id"] for item in entry["desired_locations"]}  # type: ignore[index]
        self.assertEqual(desired, {self.a.device_id, self.b.device_id, "web_server"})

        for _ in range(4):
            cli.desktop_background_cycle(client, self.a.root, state)
            self.b.cycle()
        received = self.b.root / "copied" / "nested" / "file.txt"
        self.assertTrue(received.is_file())
        self.assertEqual(sha256_file(received), sha256_bytes(data))
        self.assertEqual(sha256_file(self.server.files_root / "copied" / "nested" / "file.txt"), sha256_bytes(data))

    def test_newer_modification_propagates(self) -> None:
        self.a.write("doc.txt", b"version 1", mtime=1_760_000_000)
        settle(self.a, self.b)
        self.assertEqual((self.b.root / "doc.txt").read_bytes(), b"version 1")
        self.a.write("doc.txt", b"version 2 is longer", mtime=1_760_000_600)
        settle(self.a, self.b)
        self.assertEqual((self.server.files_root / "doc.txt").read_bytes(), b"version 2 is longer")
        self.assertEqual((self.b.root / "doc.txt").read_bytes(), b"version 2 is longer")
        self.assertEqual(int((self.b.root / "doc.txt").stat().st_mtime), 1_760_000_600)

    def test_offline_device_catches_up_on_reconnect(self) -> None:
        self.a.write("first.txt", b"first", mtime=1_760_000_000)
        settle(self.a, self.b)
        # B goes offline: it runs no cycles while A keeps working.
        self.a.write("second.txt", b"created while B offline", mtime=1_760_001_000)
        self.a.write("first.txt", b"first, edited while B offline", mtime=1_760_001_500)
        settle(self.a)
        self.assertFalse((self.b.root / "second.txt").exists())
        # B reconnects.
        settle(self.b, self.a)
        self.assertEqual((self.b.root / "second.txt").read_bytes(), b"created while B offline")
        self.assertEqual((self.b.root / "first.txt").read_bytes(), b"first, edited while B offline")

    def test_explicit_device_off_is_respected(self) -> None:
        self.a.write("private/keep-off-b.txt", b"seed", mtime=1_760_000_000)
        self.a.cycle()
        self.server.ok("set_desired_location", json_body={
            "device_id": self.b.device_id, "path": "private", "desired": False, "recursive": True,
        })
        self.a.write("private/secret.txt", b"not for B", mtime=1_760_000_100)
        settle(self.a, self.b)
        self.assertFalse((self.b.root / "private" / "secret.txt").exists())
        self.assertEqual((self.server.files_root / "private" / "secret.txt").read_bytes(), b"not for B")
        desired = {d["device_id"] for d in self.server.entries()["private/secret.txt"]["desired_locations"]}  # type: ignore[index]
        self.assertNotIn(self.b.device_id, desired)

    def test_tombstone_prevents_stale_copy_from_reappearing(self) -> None:
        self.a.write("gone/old.txt", b"old", mtime=1_760_000_000)
        settle(self.a, self.b)
        self.assertTrue((self.b.root / "gone" / "old.txt").exists())
        self.server.ok("global_delete", json_body={"path": "gone", "recursive": True})
        settle(self.a)
        self.assertFalse((self.a.root / "gone").exists())
        # B was offline during the delete and comes back with its stale copy.
        settle(self.b, self.a)
        self.assertFalse((self.b.root / "gone").exists(), "stale copy removed on reconnect")
        self.assertFalse((self.a.root / "gone").exists(), "stale copy did not resurrect on A")
        self.assertNotIn("gone/old.txt", self.server.entries())

    def test_new_content_under_deleted_name_survives_on_every_device(self) -> None:
        self.a.write("Proj/old.txt", b"old", mtime=1_760_000_000)
        settle(self.a, self.b)
        self.server.ok("global_delete", json_body={"path": "Proj", "recursive": True})
        settle(self.a, self.b)
        self.assertFalse((self.a.root / "Proj").exists())
        new_mtime = int(time.time()) + 5
        self.a.write("Proj/new-work.txt", b"brand new work", mtime=new_mtime)
        settle(self.a, self.b)
        self.assertEqual((self.a.root / "Proj" / "new-work.txt").read_bytes(), b"brand new work")
        self.assertEqual((self.b.root / "Proj" / "new-work.txt").read_bytes(), b"brand new work")
        self.assertFalse((self.b.root / "Proj" / "old.txt").exists())

    def test_existing_file_intact_when_upload_fails_midway(self) -> None:
        self.a.write("stable.txt", b"stable original", mtime=1_760_000_000)
        settle(self.a, self.b)
        web = self.server.files_root / "stable.txt"
        self.assertEqual(web.read_bytes(), b"stable original")
        status, _ = self.upload("stable.txt", b"corrupt", mtime=1_760_009_999, headers={"X-FreeCloud-SHA256": "1" * 64})
        self.assertEqual(status, 422)
        settle(self.a, self.b)
        self.assertEqual(web.read_bytes(), b"stable original")
        self.assertEqual((self.b.root / "stable.txt").read_bytes(), b"stable original")
        self.assertEqual((self.a.root / "stable.txt").read_bytes(), b"stable original")


if __name__ == "__main__":
    unittest.main()
