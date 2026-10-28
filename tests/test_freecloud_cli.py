from __future__ import annotations

import json
import hashlib
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import freecloud_cli as cli


class FakeClient:
    def __init__(self, entries: list[dict[str, object]]) -> None:
        self.entries = entries

    def manifest(self) -> list[dict[str, object]]:
        return self.entries


class FakeDeviceClient:
    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []
        self.publications: list[tuple[str, list[dict[str, object]], bool]] = []

    def register_device(
        self,
        name: str,
        device_type: str,
        device_id: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, object]:
        device = {
            "device_id": device_id,
            "name": name,
            "type": device_type,
            "capabilities": capabilities or [],
        }
        self.registrations.append(device)
        return {"device": device}

    def publish_manifest(
        self, device_id: str, entries: list[dict[str, object]], *, complete: bool = False,
    ) -> dict[str, object]:
        self.publications.append((device_id, entries, complete))
        return {"ok": True, "published": len(entries)}


class FakeReconciliationClient:
    def __init__(self, content: bytes, *, expected_size: int | None = None, expected_hash: str | None = None, partial: bool = False) -> None:
        self.content = content
        self.partial = partial
        self.acks: list[tuple[str, str, str]] = []
        self.job = {
            "job_id": "a" * 32,
            "path": "Phase3A/test.txt",
            "status": "ready",
            "expected_size": len(content) if expected_size is None else expected_size,
            "expected_mtime": 1_700_000_000,
            "hash": hashlib.sha256(content).hexdigest() if expected_hash is None else expected_hash,
        }

    def poll_reconciliation(self, device_id: str) -> list[dict[str, object]]:
        return [self.job]

    def download_reconciliation(self, device_id: str, job_id: str, target: Path) -> tuple[int, str]:
        data = self.content[: max(1, len(self.content) // 2)] if self.partial else self.content
        target.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def acknowledge_reconciliation(self, device_id: str, job_id: str, status: str, error: str = "") -> dict[str, object]:
        self.acks.append((job_id, status, error))
        return {"ok": True}


class FakeSourceReconciliationClient:
    def __init__(self, content: bytes) -> None:
        self.uploaded = b""
        self.upload_hash = ""
        self.commit_values: list[bool] = []
        self.job = {
            "job_id": "b" * 32, "path": "Phase3B/source.txt", "status": "queued_source",
            "expected_size": len(content), "expected_mtime": 1_700_000_100,
        }

    def poll_reconciliation(self, device_id: str) -> list[dict[str, object]]:
        return [self.job]

    def upload_reconciliation(self, device_id: str, job_id: str, source: Path, sha256: str) -> dict[str, object]:
        self.uploaded = source.read_bytes()
        self.upload_hash = sha256
        return {"ok": True, "job": {"status": "staged"}}

    def commit_reconciliation(self, device_id: str, job_id: str, source_unchanged: bool) -> dict[str, object]:
        self.commit_values.append(source_unchanged)
        return {"ok": True, "job": {"status": "complete" if source_unchanged else "conflict"}}


class FakeRemovalClient:
    def __init__(self, path: str, facts: dict[str, dict[str, object]], recursive: bool = False) -> None:
        self.acks: list[tuple[str, str, str]] = []
        self.job = {
            "job_id": "c" * 32, "path": path, "status": "queued_remove",
            "recursive": recursive, "removal_facts": facts,
        }

    def poll_reconciliation(self, device_id: str) -> list[dict[str, object]]:
        return [self.job]

    def acknowledge_reconciliation(self, device_id: str, job_id: str, status: str, error: str = "") -> dict[str, object]:
        self.acks.append((job_id, status, error))
        return {"ok": True}


class FakeHashProofClient:
    def __init__(self, path: str, content: bytes, mtime: int) -> None:
        self.content = content
        self.reports: list[tuple[str, str, str]] = []
        self.job = {
            "job_id": "d" * 32, "path": path, "status": "queued_hash",
            "expected_size": len(content), "expected_mtime": mtime,
        }

    def poll_reconciliation(self, device_id: str) -> list[dict[str, object]]:
        return [self.job]

    def report_location_hash(self, device_id: str, job_id: str, sha256: str = "", error: str = "") -> dict[str, object]:
        self.reports.append((job_id, sha256, error))
        return {"ok": True}


class FakeGlobalDeleteClient:
    def __init__(self, path: str) -> None:
        self.job = {"job_id": "e" * 32, "path": path, "status": "queued_delete"}
        self.acks: list[tuple[str, str, str]] = []

    def poll_reconciliation(self, device_id: str) -> list[dict[str, object]]:
        return [self.job]

    def acknowledge_reconciliation(self, device_id: str, job_id: str, status: str, error: str = "") -> dict[str, object]:
        self.acks.append((job_id, status, error))
        return {"ok": True}


class FreeCloudCliTests(unittest.TestCase):
    def test_global_delete_jobs_remove_only_configured_device_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "disposable"
            folder.mkdir()
            (folder / "child.txt").write_text("fixture", encoding="utf-8")
            desktop = FakeGlobalDeleteClient("disposable")
            result = cli.process_desktop_reconciliation(desktop, "desktop-1", root)
            self.assertFalse(folder.exists())
            self.assertEqual(desktop.acks[0][1], "deleted")
            self.assertEqual(result["installed"], 1)

            node_root = root / "node"
            node_root.mkdir()
            (node_root / "item.txt").write_text("fixture", encoding="utf-8")
            node = FakeGlobalDeleteClient("item.txt")
            rules = [cli.make_node_rule(str(node_root), "", "sync")]
            cli.process_node_reconciliation(node, "pi-1", rules)
            self.assertFalse((node_root / "item.txt").exists())
            self.assertEqual(node.acks[0][1], "deleted")

    def test_global_delete_keeps_content_created_after_the_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Proj"
            (folder / "sub").mkdir(parents=True)
            (folder / "old.txt").write_text("stale", encoding="utf-8")
            (folder / "sub" / "old2.txt").write_text("stale", encoding="utf-8")
            new_file = folder / "sub" / "new-work.txt"
            new_file.write_text("new work", encoding="utf-8")
            # Files written by this test have a current ctime; place the
            # deletion after them, and the new file's edit after the deletion.
            deleted_at = int(time.time()) + 100
            os.utime(new_file, (deleted_at + 60, deleted_at + 60))
            desktop = FakeGlobalDeleteClient("Proj")
            desktop.job["deleted_at"] = deleted_at
            cli.process_desktop_reconciliation(desktop, "desktop-1", root)
            self.assertEqual(new_file.read_text(encoding="utf-8"), "new work")
            self.assertFalse((folder / "old.txt").exists())
            self.assertFalse((folder / "sub" / "old2.txt").exists())
            self.assertEqual(desktop.acks[0][1], "deleted")

            node_root = root / "node"
            (node_root / "Proj").mkdir(parents=True)
            (node_root / "Proj" / "old.txt").write_text("stale", encoding="utf-8")
            node_new = node_root / "Proj" / "fresh.txt"
            node_new.write_text("fresh", encoding="utf-8")
            os.utime(node_new, (deleted_at + 60, deleted_at + 60))
            node = FakeGlobalDeleteClient("Proj")
            node.job["deleted_at"] = deleted_at
            cli.process_node_reconciliation(node, "pi-1", [cli.make_node_rule(str(node_root), "", "sync")])
            self.assertTrue(node_new.exists())
            self.assertFalse((node_root / "Proj" / "old.txt").exists())

    def test_global_delete_removes_whole_folder_when_nothing_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Gone" / "deep").mkdir(parents=True)
            (root / "Gone" / "deep" / "a.txt").write_text("a", encoding="utf-8")
            desktop = FakeGlobalDeleteClient("Gone")
            desktop.job["deleted_at"] = int(time.time()) + 100
            cli.process_desktop_reconciliation(desktop, "desktop-1", root)
            self.assertFalse((root / "Gone").exists())

    def test_desktop_and_node_hash_only_requested_file_for_removal_proof(self) -> None:
        content = b"hash only on demand"
        expected = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "proof.txt"
            source.write_bytes(content)
            os.utime(source, (1_700_000_100, 1_700_000_100))
            desktop = FakeHashProofClient("proof.txt", content, 1_700_000_100)
            cli.process_desktop_reconciliation(desktop, "desktop-1", root)
            self.assertEqual(desktop.reports, [("d" * 32, expected, "")])

            node = FakeHashProofClient("proof.txt", content, 1_700_000_100)
            rules = [cli.make_node_rule(str(root), "", "sync")]
            cli.process_node_reconciliation(node, "pi-1", rules)
            self.assertEqual(node.reports, [("d" * 32, expected, "")])

    def test_remote_urls_require_https_except_localhost(self) -> None:
        self.assertEqual(cli.normalize_domain("example.com"), "https://example.com")
        self.assertEqual(cli.normalize_domain("http://localhost:8000"), "http://localhost:8000")
        with self.assertRaises(ValueError):
            cli.normalize_domain("http://example.com")

    def test_password_requires_eight_characters(self) -> None:
        self.assertEqual(cli.validate_password("12345678"), "12345678")
        with self.assertRaises(ValueError):
            cli.validate_password("short")

    def test_protected_paths_are_filtered_from_both_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "visible.txt").write_text("visible", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret", encoding="utf-8")
            (root / ".freecloud_client.json").write_text("secret", encoding="utf-8")

            local = cli.local_manifest(root)
            self.assertEqual(set(local), {"visible.txt"})

            remote = cli.remote_manifest(
                FakeClient(
                    [
                        {"path": "visible.txt"},
                        {"path": ".git/config"},
                        {"path": ".freecloud_client.json"},
                        {"path": "server/config.json"},
                    ]
                )
            )
            self.assertEqual(set(remote), {"visible.txt"})

    def test_symlinks_are_not_added_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            root.mkdir()
            outside = Path(temp) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            (root / "link.txt").symlink_to(outside)

            self.assertEqual(cli.local_manifest(root), {})

    def test_json_files_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "config.json"
            cli.save_json(path, {"password": "test"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"password": "test"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_save_config_removes_legacy_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = cli.config_path(root)
            destination = root / "config" / "last_config.json"
            config = {"local_root": str(root), "password": "test"}
            legacy.write_text(json.dumps(config), encoding="utf-8")

            with (
                mock.patch.object(cli, "LAST_CONFIG_PATH", destination),
                mock.patch.object(cli, "LEGACY_LAST_CONFIG_PATH", root / "missing.json"),
            ):
                cli.save_config(config)

            self.assertFalse(legacy.exists())
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), config)

    def test_desktop_identity_is_stable_and_manifest_is_stat_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "files"
            root.mkdir()
            (root / "report.txt").write_text("report", encoding="utf-8")
            device_path = Path(temp) / "config" / "device.json"
            client = FakeDeviceClient()
            with mock.patch.object(cli, "DEVICE_CONFIG_PATH", device_path):
                first_device, first_count = cli.publish_desktop_manifest(client, root)
                second_device, second_count = cli.publish_desktop_manifest(client, root)

            self.assertEqual(first_device["device_id"], second_device["device_id"])
            self.assertEqual(len(first_device["device_id"]), 32)
            self.assertEqual(first_device["type"], "desktop")
            self.assertIn("local_storage", first_device["capabilities"])
            self.assertEqual((first_count, second_count), (1, 1))
            self.assertEqual(client.publications[0][0], first_device["device_id"])
            self.assertEqual(client.publications[0][1][0]["path"], "report.txt")
            self.assertEqual(client.publications[0][1][0]["hash"], "")
            self.assertTrue(client.publications[0][2])

    def test_desktop_manifest_scan_failure_does_not_publish_partial_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "files"
            root.mkdir()
            client = FakeDeviceClient()
            device_path = Path(temp) / "config" / "device.json"
            with mock.patch.object(cli, "DEVICE_CONFIG_PATH", device_path):
                with mock.patch.object(cli.os, "walk", side_effect=PermissionError("scan interrupted")):
                    with self.assertRaises(PermissionError):
                        cli.publish_desktop_manifest(client, root)
            self.assertEqual(client.publications, [])

    def test_desktop_inventory_signature_tracks_paths_and_stat_facts_without_hashing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "files"
            root.mkdir()
            target = root / "sample.txt"
            target.write_text("one", encoding="utf-8")
            first = cli.desktop_manifest(root)
            first_signature = cli.desktop_manifest_signature(first)
            with mock.patch.object(cli, "file_sha256", side_effect=AssertionError("must not hash files")):
                self.assertEqual(first_signature, cli.desktop_manifest_signature(cli.desktop_manifest(root)))
                target.write_text("two", encoding="utf-8")
                os.utime(target, (target.stat().st_mtime + 3, target.stat().st_mtime + 3))
                changed_signature = cli.desktop_manifest_signature(cli.desktop_manifest(root))
            self.assertNotEqual(first_signature, changed_signature)

    def test_background_reconciliation_publishes_only_stable_complete_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "files"
            nested = root / "copied" / "nested"
            nested.mkdir(parents=True)
            (nested / "tiny.txt").write_bytes(b"tiny")
            client = FakeClient([])
            state: dict[str, str] = {}
            signature = cli.desktop_manifest_signature(cli.desktop_manifest(root))
            with mock.patch.object(cli, "load_device_config", return_value={"device_id": "desktop-1"}), \
                 mock.patch.object(cli, "publish_desktop_manifest_entries", return_value=(
                     {"device_id": "desktop-1"}, 3, signature,
                 )) as publish, \
                 mock.patch.object(cli, "process_desktop_reconciliation", return_value={
                     "installed": 0, "uploaded": 0, "conflicts": 0, "failed": 0, "jobs": 0,
                 }) as reconcile:
                _first_counts, first_ready, first_local = cli.desktop_background_cycle(client, root, state)
                self.assertFalse(first_ready)
                self.assertIn("copied/nested/tiny.txt", first_local)
                publish.assert_not_called()

                _second_counts, second_ready, _second_local = cli.desktop_background_cycle(client, root, state)
                self.assertTrue(second_ready)
                published_entries = publish.call_args.args[1]
                self.assertIn("copied/nested/tiny.txt", {entry["path"] for entry in published_entries})
                _third_counts, third_ready, _third_local = cli.desktop_background_cycle(client, root, state)
                self.assertTrue(third_ready)
                publish.assert_called_once()
                self.assertEqual(reconcile.call_count, 3)

    def test_missing_desktop_root_cannot_publish_empty_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = FakeDeviceClient()
            device_path = Path(temp) / "config" / "device.json"
            with mock.patch.object(cli, "DEVICE_CONFIG_PATH", device_path):
                with self.assertRaisesRegex(RuntimeError, "folder is unavailable"):
                    cli.publish_desktop_manifest(client, Path(temp) / "missing")
            self.assertEqual(client.publications, [])

    def test_desired_location_api_does_not_invoke_byte_transfer(self) -> None:
        client = cli.FreeCloudClient("https://example.com/FreeCloud", "password")
        with mock.patch.object(client, "post_json", return_value={"ok": True}) as post:
            result = client.set_desired_location("Documents/report.pdf", "phone-1", True, False)

        self.assertEqual(result, {"ok": True})
        post.assert_called_once_with(
            "set_desired_location",
            {
                "path": "Documents/report.pdf",
                "device_id": "phone-1",
                "desired": True,
                "recursive": False,
            },
        )

    def test_recreate_path_api_is_explicit_and_generation_scoped(self) -> None:
        client = cli.FreeCloudClient("https://example.com/FreeCloud", "password")
        with mock.patch.object(client, "post_json", return_value={"ok": True}) as post:
            result = client.recreate_path("A Folder", "desktop-1", "phone-1", True)
        self.assertEqual(result, {"ok": True})
        post.assert_called_once_with("recreate_path", {
            "path": "A Folder", "source_device_id": "desktop-1",
            "desired_device_id": "phone-1", "recursive": True,
        })

    def test_desktop_reconciliation_atomically_installs_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeReconciliationClient(b"phase 3a\n")
            with mock.patch("freecloud_cli.os.replace", wraps=os.replace) as replace:
                counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            target = root / "Phase3A" / "test.txt"
            self.assertEqual(target.read_bytes(), b"phase 3a\n")
            self.assertEqual(int(target.stat().st_mtime), 1_700_000_000)
            self.assertEqual(counts["installed"], 1)
            self.assertEqual(client.acks[0][1], "installed")
            self.assertTrue(replace.called)
            self.assertFalse(list(root.rglob("*.freecloud-part-*")))

    def test_desktop_reconciliation_never_overwrites_existing_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "Phase3A" / "test.txt"
            target.parent.mkdir()
            target.write_bytes(b"newer local data")
            os.utime(target, (1_800_000_000, 1_800_000_000))
            client = FakeReconciliationClient(b"server data")
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertEqual(target.read_bytes(), b"newer local data")
            self.assertEqual(counts["conflicts"], 1)
            self.assertEqual(client.acks[0][1], "conflict")

    def test_web_newer_atomically_replaces_unchanged_planned_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "Phase3A" / "test.txt"
            target.parent.mkdir()
            target.write_bytes(b"old desktop")
            os.utime(target, (1_700_000_000, 1_700_000_000))
            client = FakeReconciliationClient(b"new web version")
            client.job["destination_facts"] = {
                "size": len(b"old desktop"), "mtime": 1_700_000_000, "hash": ""
            }
            client.job["expected_mtime"] = 1_700_000_200
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertEqual(target.read_bytes(), b"new web version")
            self.assertEqual(counts["installed"], 1)

    def test_failed_or_partial_reconciliation_leaves_no_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeReconciliationClient(b"incomplete transfer", partial=True)
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertFalse((root / "Phase3A" / "test.txt").exists())
            self.assertFalse(list(root.rglob("*.freecloud-part-*")))
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(client.acks[0][1], "failed")

    def test_hash_mismatch_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeReconciliationClient(b"content", expected_hash="0" * 64)
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertFalse((root / "Phase3A" / "test.txt").exists())
            self.assertEqual(counts["failed"], 1)

    def test_desktop_source_uploads_only_requested_verified_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "Phase3B" / "source.txt"
            target.parent.mkdir()
            content = b"desktop to web\n"
            target.write_bytes(content)
            os.utime(target, (1_700_000_100, 1_700_000_100))
            (root / "unrelated.txt").write_text("must not upload", encoding="utf-8")
            client = FakeSourceReconciliationClient(content)
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertEqual(client.uploaded, content)
            self.assertEqual(client.upload_hash, hashlib.sha256(content).hexdigest())
            self.assertEqual(client.commit_values, [True])
            self.assertEqual(counts["uploaded"], 1)

    def test_desktop_removal_deletes_only_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "Disposable" / "file.txt"
            target.parent.mkdir()
            target.write_bytes(b"abc")
            os.utime(target, (100, 100))
            unrelated = root / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            client = FakeRemovalClient("Disposable", {
                "Disposable": {"type": "dir", "size": 0, "mtime": 100},
                "Disposable/file.txt": {
                    "type": "file", "size": 3, "mtime": 100, "hash": hashlib.sha256(b"abc").hexdigest(),
                },
            }, recursive=True)
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertFalse((root / "Disposable").exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
            self.assertEqual(counts["installed"], 1)
            self.assertEqual(client.acks[0][1], "removed")

    def test_desktop_removal_refuses_changed_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "changed.txt"
            target.write_bytes(b"newer")
            os.utime(target, (200, 200))
            client = FakeRemovalClient("changed.txt", {
                "changed.txt": {"type": "file", "size": 3, "mtime": 100},
            })
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertEqual(target.read_bytes(), b"newer")
            self.assertEqual(counts["conflicts"], 1)
            self.assertEqual(client.acks[0][1], "conflict")

    def test_desktop_removal_refuses_same_size_mtime_with_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "same-facts.txt"
            target.write_bytes(b"xyz")
            os.utime(target, (100, 100))
            client = FakeRemovalClient("same-facts.txt", {
                "same-facts.txt": {
                    "type": "file", "size": 3, "mtime": 100, "hash": hashlib.sha256(b"abc").hexdigest(),
                },
            })
            counts = cli.process_desktop_reconciliation(client, "desktop-1", root)
            self.assertEqual(target.read_bytes(), b"xyz")
            self.assertEqual(counts["conflicts"], 1)

    def test_legacy_sync_skips_desired_managed_path(self) -> None:
        class ManagedClient(FakeClient):
            def __init__(self) -> None:
                super().__init__([{"path": "managed/delete.txt", "type": "file", "size": 7, "mtime": 100}])
                self.uploaded: list[str] = []
                self.deleted: list[str] = []

            def placement_rules(self) -> dict[str, object]:
                return {"desired_rules": {"desktop": [{"path": "managed", "recursive": True}]}, "sync_rules": {}}

            def upload(self, local_file: Path, path: str) -> None:
                self.uploaded.append(path)

            def delete(self, path: str) -> None:
                self.deleted.append(path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "managed").mkdir()
            (root / "managed" / "upload.txt").write_text("managed", encoding="utf-8")
            cli.save_json(cli.state_path(root), {"managed/delete.txt": {"type": "file", "size": 7, "mtime": 100}})
            client = ManagedClient()
            cli.sync_once(client, root, delete_remote=True)
            self.assertEqual(client.uploaded, [])
            self.assertEqual(client.deleted, [])

    def test_legacy_sync_skips_implicit_placement_default_but_keeps_legacy_path(self) -> None:
        class OwnershipClient(FakeClient):
            def __init__(self) -> None:
                super().__init__([])
                self.uploaded: list[str] = []
                self.ownership_queries: list[list[str]] = []

            def placement_rules(self) -> dict[str, object]:
                return {"desired_rules": {}, "sync_rules": {}}

            def placement_managed_paths(self, paths: list[str]) -> set[str]:
                self.ownership_queries.append(paths)
                return {"managed.txt"}

            def upload(self, local_file: Path, path: str) -> None:
                self.uploaded.append(path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "managed.txt").write_text("placement-owned", encoding="utf-8")
            (root / "legacy.txt").write_text("legacy-owned", encoding="utf-8")
            client = OwnershipClient()

            cli.sync_once(client, root)

            self.assertEqual(client.uploaded, ["legacy.txt"])
            self.assertEqual(set(client.ownership_queries[0]), {"managed.txt", "legacy.txt"})

    def test_legacy_sync_defers_new_local_paths_to_active_gui_manifest_owner(self) -> None:
        class UploadClient(FakeClient):
            def __init__(self) -> None:
                super().__init__([])
                self.uploaded: list[str] = []

            def placement_rules(self) -> dict[str, object]:
                return {"desired_rules": {}, "sync_rules": {}}

            def upload(self, local_file: Path, path: str) -> None:
                self.uploaded.append(path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "copied-folder").mkdir()
            (root / "copied-folder" / "new.txt").write_text("new", encoding="utf-8")
            client = UploadClient()
            snapshot = cli.local_manifest(root)
            cli.save_json(cli.state_path(root), {
                "copied-folder": {"type": "dir", "size": 0, "mtime": 100},
                "copied-folder/new.txt": {"type": "file", "size": 3, "mtime": 100},
            })

            with mock.patch.object(cli, "desktop_ui_running", return_value=True), \
                 mock.patch.object(cli, "local_manifest", side_effect=AssertionError("unexpected second scan")):
                cli.sync_once(client, root, local_snapshot=snapshot)

            self.assertEqual(client.uploaded, [])
            self.assertTrue((root / "copied-folder" / "new.txt").is_file())

    def test_legacy_sync_fails_closed_if_placement_state_cannot_be_read(self) -> None:
        class UnavailablePlacementClient(FakeClient):
            def placement_rules(self) -> dict[str, object]:
                raise cli.FreeCloudApiError("offline", "https://example.test", 503)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "would-upload.txt").write_text("keep legacy from acting", encoding="utf-8")
            client = UnavailablePlacementClient([])
            with self.assertRaisesRegex(RuntimeError, "stopped safely"):
                cli.sync_once(client, root)

    def test_node_rule_manifest_maps_local_folder_to_remote_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Book One").mkdir()
            (root / "Book One" / "chapter1.mp3").write_bytes(b"audio")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret", encoding="utf-8")

            rule = cli.make_node_rule(str(root), "AudioBooks", "storage-only")
            entries = cli.node_rule_manifest(rule)
            by_path = {entry["path"]: entry for entry in entries}

            self.assertIn("AudioBooks", by_path)
            self.assertIn("AudioBooks/Book One", by_path)
            self.assertIn("AudioBooks/Book One/chapter1.mp3", by_path)
            self.assertNotIn("AudioBooks/.git/config", by_path)
            self.assertEqual(by_path["AudioBooks/Book One/chapter1.mp3"]["type"], "file")
            self.assertEqual(by_path["AudioBooks/Book One/chapter1.mp3"]["size"], 5)

    def test_node_manifest_only_publishes_storage_provider_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage_root = Path(temp) / "storage"
            browse_root = Path(temp) / "browse"
            storage_root.mkdir()
            browse_root.mkdir()
            (storage_root / "kept.txt").write_text("kept", encoding="utf-8")
            (browse_root / "ignored.txt").write_text("ignored", encoding="utf-8")

            entries = cli.node_manifest(
                [
                    cli.make_node_rule(str(storage_root), "Storage", "storage_only"),
                    cli.make_node_rule(str(browse_root), "Browse", "browse_only"),
                ]
            )

            paths = {entry["path"] for entry in entries}
            self.assertIn("Storage/kept.txt", paths)
            self.assertNotIn("Browse/ignored.txt", paths)

    def test_node_manifest_can_hash_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "file.txt").write_text("abc", encoding="utf-8")

            entries = cli.node_manifest([cli.make_node_rule(str(root), "Docs", "upload_only")], include_hashes=True)
            by_path = {entry["path"]: entry for entry in entries}

            self.assertEqual(
                by_path["Docs/file.txt"]["hash"],
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_node_rules_convert_to_server_sync_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rule = cli.make_node_rule(str(root), "./AudioBooks/../AudioBooks", "storage-only")

            self.assertEqual(
                cli.server_rules_from_node_rules([rule]),
                [{"path": "AudioBooks", "mode": "storage_only", "recursive": True}],
            )

    def test_missing_publish_roots_detects_unmounted_storage_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = str(Path(temp) / "missing-drive" / "Audiobooks")

            self.assertEqual(
                cli.missing_publish_roots(
                    [{"local_path": missing, "remote_path": "Audiobooks", "mode": "storage_only"}]
                ),
                [missing],
            )

    def test_virtual_path_resolves_through_most_specific_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            broad = root / "broad"
            specific = root / "specific"
            broad.mkdir()
            specific.mkdir()
            rules = [
                cli.make_node_rule(str(broad), "Audiobooks", "storage_only"),
                cli.make_node_rule(str(specific), "Audiobooks/Book A", "storage_only"),
            ]

            self.assertEqual(
                cli.local_path_for_virtual_path(rules, "Audiobooks/Book A/chapter.mp3"),
                specific / "chapter.mp3",
            )

    def test_virtual_path_does_not_match_a_similar_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            rule = cli.make_node_rule(temp, "Audio", "storage_only")
            self.assertIsNone(cli.local_path_for_virtual_path([rule], "Audiobooks/file.mp3"))

    def test_node_destination_requires_writable_safe_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sync"
            audio = Path(temp) / "audio"
            root.mkdir()
            audio.mkdir()
            rules = [
                cli.make_node_rule(str(root), "", "sync"),
                cli.make_node_rule(str(audio), "Audiobooks", "storage_only"),
            ]
            self.assertEqual(cli.local_path_for_virtual_path(rules, "ordinary/file.txt", writable=True), root / "ordinary/file.txt")
            self.assertIsNone(cli.local_path_for_virtual_path(rules, "Audiobooks/new.m4b", writable=True))
            self.assertIsNone(cli.local_path_for_virtual_path(rules, "../escape.txt", writable=True))

    def test_node_reconciliation_installs_atomically_and_preserves_mtime(self) -> None:
        content = b"node destination\n"
        client = FakeReconciliationClient(content)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rules = [cli.make_node_rule(str(root), "", "sync")]
            counts = cli.process_node_reconciliation(client, "pi-1", rules)
            target = root / "Phase3A" / "test.txt"
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(int(target.stat().st_mtime), 1_700_000_000)
            self.assertEqual(counts["installed"], 1)
            self.assertFalse(any(root.rglob("*.freecloud-part-*")))

    def test_node_reconciliation_uploads_only_requested_source(self) -> None:
        content = b"node source\n"
        client = FakeSourceReconciliationClient(content)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Phase3B" / "source.txt"
            source.parent.mkdir()
            source.write_bytes(content)
            os.utime(source, (1_700_000_100, 1_700_000_100))
            rules = [cli.make_node_rule(str(root), "", "sync")]
            counts = cli.process_node_reconciliation(client, "pi-1", rules)
            self.assertEqual(client.uploaded, content)
            self.assertEqual(client.upload_hash, hashlib.sha256(content).hexdigest())
            self.assertEqual(counts["uploaded"], 1)


if __name__ == "__main__":
    unittest.main()
