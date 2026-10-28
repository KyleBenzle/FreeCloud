from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("php"), "PHP is required")
class FreeCloudServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "server" / "freecloud_api.php"
        shutil.copy2(source, self.root / "freecloud_api.php")
        shutil.copy2(source.with_name("freecloud_lifecycle.php"), self.root / "freecloud_lifecycle.php")
        self.request(
            "setup",
            method="POST",
            form={"name": "Test", "password": "test-password"},
            authenticated=False,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(
        self,
        action: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
        form: dict[str, str] | None = None,
        method: str = "GET",
        authenticated: bool = True,
    ) -> dict[str, object]:
        post = payload if payload is not None else form if form is not None else {}
        omit_complete = bool(post.pop("_omit_complete", False)) if isinstance(post, dict) else False
        keep_new_placement_default = bool(post.pop("_keep_new_placement_default", False)) if isinstance(post, dict) else False
        if action == "publish_manifest" and payload is not None and "complete" not in payload and not omit_complete:
            # Existing fixtures model a successfully completed provider scan.
            post = {**post, "complete": True}
        script = """
$_GET = array_merge(['action' => getenv('TEST_ACTION')], json_decode(getenv('TEST_PARAMS'), true) ?: []);
$_POST = json_decode(getenv('TEST_POST'), true) ?: [];
$_SERVER['REQUEST_METHOD'] = getenv('TEST_METHOD');
$_SERVER['CONTENT_TYPE'] = 'application/x-www-form-urlencoded';
$_SERVER['HTTP_X_FREECLOUD_PASSWORD'] = getenv('TEST_PASSWORD');
include getenv('TEST_API');
"""
        env = os.environ.copy()
        env.update(
            {
                "TEST_ACTION": action,
                "TEST_POST": json.dumps(post),
                "TEST_PARAMS": json.dumps(params or {}),
                "TEST_METHOD": method,
                "TEST_PASSWORD": "test-password" if authenticated else "",
                "TEST_API": str(self.root / "freecloud_api.php"),
            }
        )
        result = subprocess.run(
            ["php", "-r", script],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"PHP API failed: {result.stderr}\n{result.stdout}")
        response = json.loads(result.stdout)
        if action == "publish_manifest" and not keep_new_placement_default:
            # Most historical regression fixtures represent content that was
            # already in the catalog before automatic new-item defaults.
            catalog_path = self.root / "freecloud_meta" / "files.json"
            if catalog_path.is_file():
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                for entry in post.get("entries", []):
                    if not isinstance(entry, dict):
                        continue
                    path = str(entry.get("path") or "")
                    if isinstance(catalog.get(path), dict):
                        catalog[path].pop("placement_default", None)
                        catalog[path].pop("placement_source_device_id", None)
                catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        return response

    def register(self, device_id: str, name: str, device_type: str, capabilities: list[str]) -> None:
        self.request(
            "register_device",
            method="POST",
            payload={
                "device_id": device_id,
                "name": name,
                "type": device_type,
                "capabilities": capabilities,
            },
        )

    @staticmethod
    def entry(path: str) -> dict[str, object]:
        return {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "type": "file",
            "size": 3,
            "mtime": 100,
            "hash": hashlib.sha256(b"abc").hexdigest(),
        }

    def test_registered_providers_publish_and_remove_only_their_locations(self) -> None:
        pi_id = "1" * 32
        android_id = "2" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        self.register(android_id, "Android Phone", "android", ["local_storage"])
        self.register(android_id, "Pixel Phone", "android", ["local_storage"])

        devices = self.request("list_devices")["devices"]
        self.assertEqual(len(devices), 2)
        self.assertEqual(next(item for item in devices if item["device_id"] == android_id)["name"], "Pixel Phone")

        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": pi_id, "entries": [self.entry("shared.txt"), self.entry("pi-only.txt")]},
        )
        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": android_id, "entries": [self.entry("shared.txt"), self.entry("phone-only.txt")]},
        )
        (self.root / "freecloud_files" / "shared.txt").write_text("web", encoding="utf-8")

        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        shared_locations = {(item["kind"], item["device_id"]) for item in manifest["shared.txt"]["locations"]}
        self.assertEqual(
            shared_locations,
            {("local_node", pi_id), ("android", android_id), ("web_server", "web_server")},
        )
        self.assertEqual(manifest["phone-only.txt"]["locations"][0]["kind"], "android")

        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": android_id, "entries": [self.entry("phone-only.txt")]},
        )
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual(
            {item["device_id"] for item in manifest["shared.txt"]["locations"]},
            {pi_id, "web_server"},
        )
        self.assertFalse(manifest["shared.txt"]["deleted"])

        self.request("publish_manifest", method="POST", payload={"device_id": android_id, "entries": []})
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertNotIn("phone-only.txt", manifest)

        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": []})
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertNotIn("pi-only.txt", manifest)

    def test_placement_ownership_includes_defaults_and_recursive_overrides_only(self) -> None:
        desktop_id = "d" * 32
        self.register(desktop_id, "Desktop", "desktop", ["local_storage"])
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [self.entry("managed-default.txt")],
            "_keep_new_placement_default": True,
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [self.entry("legacy-only.txt")],
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [self.entry("tombstoned.txt")],
        })
        self.request("global_delete", method="POST", payload={"path": "tombstoned.txt"})
        self.request("set_desired_location", method="POST", payload={
            "path": "selected", "device_id": desktop_id, "desired": False, "recursive": True,
        })

        result = self.request("placement_ownership", method="POST", payload={
            "paths": [
                "managed-default.txt", "legacy-only.txt", "selected/child.txt", "tombstoned.txt",
            ],
        })
        self.assertEqual(
            set(result["managed_paths"]),
            {"managed-default.txt", "selected/child.txt", "tombstoned.txt"},
        )

    def test_partial_manifest_never_removes_actual_locations(self) -> None:
        desktop_id = "9" * 32
        phone_id = "8" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "complete": True,
            "entries": [self.entry("keep.txt"), self.entry("maybe-missed.txt")],
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": phone_id, "complete": True, "entries": [self.entry("maybe-missed.txt")],
        })

        for completeness in (False, None):
            payload = {"device_id": desktop_id, "entries": [self.entry("keep.txt")]}
            if completeness is not None:
                payload["complete"] = completeness
            else:
                payload["_omit_complete"] = True
            self.request("publish_manifest", method="POST", payload=payload)
            placement = self.request("get_placement", params={"path": "maybe-missed.txt"})
            self.assertEqual(
                {item["device_id"] for item in placement["actual_locations"]},
                {desktop_id, phone_id},
            )

        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "complete": True, "entries": [self.entry("keep.txt")],
        })
        placement = self.request("get_placement", params={"path": "maybe-missed.txt"})
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {phone_id})

    def test_complete_manifest_confirms_absence_and_finishes_pending_removal(self) -> None:
        desktop_id = "7" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage"])
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "complete": True, "entries": [self.entry("gone.txt")],
        })
        reconcile = self.root / "freecloud_meta" / "reconciliation"
        reconcile.mkdir(parents=True, exist_ok=True)
        pending_id = "a" * 32
        hash_id = "b" * 32
        (reconcile / f"pending-remove-{pending_id}.json").write_text(json.dumps({
            "job_id": pending_id, "kind": "pending_removal", "path": "gone.txt",
            "device_id": desktop_id, "recursive": False, "hash_job_ids": [hash_id],
            "status": "hashing",
        }), encoding="utf-8")
        (reconcile / f"{hash_id}.json").write_text(json.dumps({
            "job_id": hash_id, "kind": "hash_location", "path": "gone.txt",
            "device_id": desktop_id, "status": "queued_hash",
        }), encoding="utf-8")

        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "complete": True, "entries": [],
        })
        pending = json.loads((reconcile / f"pending-remove-{pending_id}.json").read_text(encoding="utf-8"))
        hash_job = json.loads((reconcile / f"{hash_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["status"], "complete")
        self.assertEqual(hash_job["status"], "cancelled")
        placement = self.request("get_placement", params={"path": "gone.txt"})
        self.assertEqual(placement["actual_locations"], [])

    def test_device_management_rename_and_metadata_only_removal(self) -> None:
        first_phone = "a" * 32
        stale_phone = "b" * 32
        pi_id = "c" * 32
        self.register(first_phone, "Google Pixel 7a", "android", ["local_storage"])
        self.register(stale_phone, "Google Pixel 7a", "android", ["local_storage"])
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        devices = self.request("list_devices")["devices"]
        self.assertEqual(
            {item["device_id"] for item in devices if item["name"] == "Google Pixel 7a"},
            {first_phone, stale_phone},
        )

        physical = self.root / "freecloud_files" / "keep.txt"
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical.write_text("do not delete", encoding="utf-8")
        self.request("publish_manifest", method="POST", payload={"device_id": first_phone, "entries": [self.entry("keep.txt")]})
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [self.entry("Audiobooks/book.mp3")]})
        self.request(
            "set_sync_rules", method="POST",
            payload={"device_id": pi_id, "rules": [{"path": "Audiobooks", "mode": "storage_only", "recursive": True}]},
        )
        self.request("set_desired_location", method="POST", payload={"path": "keep.txt", "device_id": first_phone, "desired": True})
        self.request("set_desired_location", method="POST", payload={"path": "keep.txt", "device_id": stale_phone, "desired": True})

        renamed = self.request(
            "rename_device", method="POST", payload={"device_id": first_phone, "name": "Kyle's Phone"}
        )["device"]
        self.assertEqual(renamed["device_id"], first_phone)
        self.assertEqual(renamed["name"], "Kyle's Phone")
        self.register(first_phone, "Google Pixel 7a", "android", ["local_storage"])
        renamed_again = next(
            item for item in self.request("list_devices")["devices"] if item["device_id"] == first_phone
        )
        self.assertEqual(renamed_again["name"], "Kyle's Phone")
        entry = next(item for item in self.request("metadata_manifest")["entries"] if item["path"] == "keep.txt")
        self.assertEqual(next(item for item in entry["locations"] if item["device_id"] == first_phone)["name"], "Kyle's Phone")

        self.request("remove_device", method="POST", payload={"device_id": first_phone})
        devices = self.request("list_devices")["devices"]
        self.assertNotIn(first_phone, {item["device_id"] for item in devices})
        self.assertIn(stale_phone, {item["device_id"] for item in devices})
        entry = next(item for item in self.request("metadata_manifest")["entries"] if item["path"] == "keep.txt")
        self.assertNotIn(first_phone, {item["device_id"] for item in entry["locations"]})
        self.assertNotIn(first_phone, {item["device_id"] for item in entry["desired_locations"]})
        self.assertIn(stale_phone, {item["device_id"] for item in entry["desired_locations"]})
        self.assertEqual(physical.read_text(encoding="utf-8"), "do not delete")
        audiobook = next(item for item in self.request("metadata_manifest")["entries"] if item["path"] == "Audiobooks/book.mp3")
        self.assertEqual({item["device_id"] for item in audiobook["locations"]}, {pi_id})
        self.assertEqual({item["device_id"] for item in audiobook["desired_locations"]}, {pi_id})

    def test_remove_device_cleans_sync_rules_without_deleting_files(self) -> None:
        device_id = "d" * 32
        self.register(device_id, "Offline Phone", "android", ["local_storage"])
        physical = self.root / "freecloud_files" / "safe.txt"
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical.write_text("safe", encoding="utf-8")
        (self.root / "freecloud_meta" / "sync_rules.json").write_text(
            json.dumps({device_id: [{"path": "", "mode": "sync", "recursive": True}]}), encoding="utf-8"
        )
        self.request("remove_device", method="POST", payload={"device_id": device_id})
        rules = json.loads((self.root / "freecloud_meta" / "sync_rules.json").read_text(encoding="utf-8"))
        self.assertNotIn(device_id, rules)
        self.assertTrue(physical.is_file())

    def test_actual_and_desired_placement_are_independent(self) -> None:
        pi_id = "1" * 32
        phone_id = "2" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": pi_id, "entries": [self.entry("report.pdf")]},
        )

        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "report.pdf", "device_id": phone_id, "desired": True},
        )
        placement = self.request("get_placement", params={"path": "report.pdf"})
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {pi_id})
        self.assertEqual({item["device_id"] for item in placement["desired_locations"]}, {phone_id})
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual({item["device_id"] for item in manifest["report.pdf"]["locations"]}, {pi_id})
        self.assertEqual({item["device_id"] for item in manifest["report.pdf"]["desired_locations"]}, {phone_id})

        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "report.pdf", "device_id": phone_id, "desired": False},
        )
        placement = self.request("get_placement", params={"path": "report.pdf"})
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {pi_id})
        self.assertEqual(placement["desired_locations"], [])

        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "report.pdf", "device_id": "web_server", "desired": True},
        )
        placement = self.request("get_placement", params={"path": "report.pdf"})
        self.assertEqual({item["device_id"] for item in placement["desired_locations"]}, {"web_server"})

    def test_folder_inheritance_and_most_specific_rule(self) -> None:
        phone_id = "2" * 32
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "Documents", "device_id": phone_id, "desired": True, "recursive": True},
        )
        inherited = self.request("get_placement", params={"path": "Documents/Public/report.pdf"})
        self.assertEqual({item["device_id"] for item in inherited["desired_locations"]}, {phone_id})

        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "Documents/Private", "device_id": phone_id, "desired": False, "recursive": True},
        )
        private = self.request("get_placement", params={"path": "Documents/Private/secret.pdf"})
        public = self.request("get_placement", params={"path": "Documents/Public/report.pdf"})
        self.assertEqual(private["desired_locations"], [])
        self.assertEqual({item["device_id"] for item in public["desired_locations"]}, {phone_id})

    def test_recursive_folder_placement_drives_descendant_reconciliation(self) -> None:
        pi_id = "1" * 32
        desktop_id = "3" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        folder = {**self.entry("Demeter"), "type": "dir", "size": 0}
        child = self.entry("Demeter/Books/one.pdf")
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [folder, child]})
        self.request("set_desired_location", method="POST", payload={
            "path": "Demeter", "device_id": desktop_id, "desired": True, "recursive": True,
        })

        placement = self.request("get_placement", params={"path": "Demeter/Books/one.pdf"})
        self.assertEqual({x["device_id"] for x in placement["desired_locations"]}, {desktop_id})
        self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        jobs = [json.loads(path.read_text(encoding="utf-8")) for path in
                (self.root / "freecloud_meta" / "reconciliation").glob("*.json")]
        child_jobs = [job for job in jobs if job.get("path") == child["path"]]
        self.assertEqual(len(child_jobs), 1)
        self.assertEqual(child_jobs[0]["destination_device_id"], desktop_id)

    def test_reconciliation_check_only_does_not_plan_or_scan_catalog(self) -> None:
        pi_id = "1" * 32
        desktop_id = "3" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        folder = {**self.entry("Disposable"), "type": "dir", "size": 0}
        child = self.entry("Disposable/check-only.txt")
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [folder, child]})
        self.request("set_desired_location", method="POST", payload={
            "path": "Disposable", "device_id": desktop_id, "desired": True, "recursive": True,
        })

        checked = self.request("poll_reconciliation", method="POST", payload={
            "device_id": desktop_id, "check_only": True,
        })
        jobs_dir = self.root / "freecloud_meta" / "reconciliation"
        self.assertEqual(checked["jobs"], [])
        self.assertFalse(any(json.loads(path.read_text()).get("path") == child["path"] for path in jobs_dir.glob("*.json")))

        self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        self.assertTrue(any(json.loads(path.read_text()).get("path") == child["path"] for path in jobs_dir.glob("*.json")))

    def test_audiobook_folder_rule_stays_pi_only_during_reconciliation(self) -> None:
        pi_id = "1" * 32
        desktop_id = "3" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.request("set_sync_rules", method="POST", payload={"device_id": pi_id, "rules": [
            {"path": "Audiobooks", "mode": "storage_only", "recursive": True},
        ]})
        folder = {**self.entry("Audiobooks"), "type": "dir", "size": 0}
        child = self.entry("Audiobooks/Series/book.m4b")
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [folder, child]})
        self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        placement = self.request("get_placement", params={"path": child["path"]})
        self.assertEqual({x["device_id"] for x in placement["desired_locations"]}, {pi_id})
        jobs = [json.loads(path.read_text(encoding="utf-8")) for path in
                (self.root / "freecloud_meta" / "reconciliation").glob("*.json")]
        self.assertFalse(any(job.get("path", "").startswith("Audiobooks/") for job in jobs))

    def test_storage_only_is_pi_only_without_per_file_rules(self) -> None:
        pi_id = "1" * 32
        phone_id = "2" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.request(
            "set_sync_rules",
            method="POST",
            payload={"device_id": pi_id, "rules": [{"path": "Audiobooks", "mode": "storage_only", "recursive": True}]},
        )
        self.request(
            "publish_manifest",
            method="POST",
            payload={
                "device_id": pi_id,
                "entries": [self.entry("Audiobooks/book.m4b")],
            },
        )
        placement = self.request("get_placement", params={"path": "Audiobooks/book.m4b"})
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {pi_id})
        self.assertEqual({item["device_id"] for item in placement["desired_locations"]}, {pi_id})
        self.assertNotIn("web_server", {item["device_id"] for item in placement["desired_locations"]})
        self.assertFalse((self.root / "freecloud_meta" / "desired_locations.json").exists())

    def test_metadata_list_enriches_only_direct_children(self) -> None:
        pi_id = "1" * 32
        phone_id = "2" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.request("set_sync_rules", method="POST", payload={"device_id": pi_id, "rules": [
            {"path": "Audiobooks", "mode": "storage_only", "recursive": True},
        ]})
        self.request("set_desired_location", method="POST", payload={
            "path": "Audiobooks", "device_id": phone_id, "desired": True, "recursive": True,
        })
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [
            {**self.entry("Audiobooks"), "type": "dir", "size": 0},
            {**self.entry("Audiobooks/Book A"), "type": "dir", "size": 0},
            self.entry("Audiobooks/Book A/chapter.mp3"),
            {**self.entry("Audiobooks/Book B"), "type": "dir", "size": 0},
        ]})

        listed = self.request("metadata_list", params={"path": "Audiobooks"})["entries"]
        self.assertEqual({entry["path"] for entry in listed}, {"Audiobooks/Book A", "Audiobooks/Book B"})
        for entry in listed:
            self.assertEqual({item["device_id"] for item in entry["locations"]}, {pi_id})
            self.assertEqual({item["device_id"] for item in entry["desired_locations"]}, {pi_id, phone_id})

    def test_metadata_list_tracks_web_file_changes_and_provider_placement(self) -> None:
        pi_id = "3" * 32
        phone_id = "4" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        folder = self.root / "freecloud_files" / "Indexed"
        folder.mkdir(parents=True)
        web_file = folder / "shared.txt"
        web_file.write_text("one", encoding="utf-8")
        deep = folder / "Deep"
        deep.mkdir()
        (deep / "hidden.txt").write_text("not a direct child", encoding="utf-8")

        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [
            self.entry("Indexed/shared.txt"),
        ]})
        listed = self.request("metadata_list", params={"path": "Indexed"})["entries"]
        self.assertEqual({entry["path"] for entry in listed}, {"Indexed/shared.txt", "Indexed/Deep"})
        shared = next(entry for entry in listed if entry["path"] == "Indexed/shared.txt")
        self.assertEqual({item["device_id"] for item in shared["locations"]}, {"web_server", pi_id})

        web_file.write_text("updated content", encoding="utf-8")
        os.utime(web_file, (500, 500))
        self.request("set_desired_location", method="POST", payload={
            "path": "Indexed/shared.txt", "device_id": phone_id, "desired": True,
        })
        shared = next(entry for entry in self.request("metadata_list", params={"path": "Indexed"})["entries"]
                      if entry["path"] == "Indexed/shared.txt")
        web_location = next(item for item in shared["locations"] if item["device_id"] == "web_server")
        self.assertEqual(web_location["size"], len("updated content"))
        self.assertEqual(web_location["mtime"], 500)
        self.assertEqual({item["device_id"] for item in shared["desired_locations"]}, {phone_id})

        web_file.unlink()
        shared = next(entry for entry in self.request("metadata_list", params={"path": "Indexed"})["entries"]
                      if entry["path"] == "Indexed/shared.txt")
        self.assertEqual({item["device_id"] for item in shared["locations"]}, {pi_id})
        self.assertEqual({item["device_id"] for item in shared["desired_locations"]}, {phone_id})

    def test_old_metadata_without_desired_fields_remains_compatible(self) -> None:
        pi_id = "1" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        old_entry = self.entry("legacy.txt")
        old_entry.update(
            {
                "version": 1,
                "deleted": False,
                "locations": [
                    {
                        "kind": "local_node",
                        "device_id": pi_id,
                        "name": "Raspberry Pi",
                        "available": True,
                        "updated_at": 100,
                    }
                ],
            }
        )
        meta = self.root / "freecloud_meta"
        meta.mkdir(exist_ok=True)
        (meta / "files.json").write_text(json.dumps({"legacy.txt": old_entry}), encoding="utf-8")

        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual({item["device_id"] for item in manifest["legacy.txt"]["locations"]}, {pi_id})
        legacy_location = manifest["legacy.txt"]["locations"][0]
        self.assertEqual((legacy_location["size"], legacy_location["mtime"]), (3, 100))
        self.assertEqual(manifest["legacy.txt"]["desired_locations"], [])

    def test_adoption_is_idempotent_and_preserves_explicit_off_and_storage_only(self) -> None:
        desktop_id = "a" * 32
        phone_id = "b" * 32
        pi_id = "c" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        folder = {**self.entry("Demeter"), "type": "dir", "size": 0}
        child = self.entry("Demeter/book.txt")
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [folder, child]})
        self.request("publish_manifest", method="POST", payload={"device_id": phone_id, "entries": [folder, child]})
        self.request("set_desired_location", method="POST", payload={
            "path": "Demeter", "device_id": phone_id, "desired": False, "recursive": True,
        })
        self.request("set_sync_rules", method="POST", payload={"device_id": pi_id, "rules": [
            {"path": "Audiobooks", "mode": "storage_only", "recursive": True},
        ]})
        self.request("publish_manifest", method="POST", payload={
            "device_id": pi_id, "entries": [{**self.entry("Audiobooks"), "type": "dir", "size": 0}, self.entry("Audiobooks/book.m4b")],
        })

        first = self.request("adopt_existing_locations", method="POST", payload={})
        second = self.request("adopt_existing_locations", method="POST", payload={})
        self.assertGreaterEqual(first["adopted_rules"], 1)
        self.assertTrue(second["already_complete"])
        rules = self.request("placement_rules")["desired_rules"]
        self.assertEqual(len([r for r in rules[desktop_id] if r["path"] == "Demeter"]), 1)
        self.assertTrue(next(r for r in rules[desktop_id] if r["path"] == "Demeter")["recursive"])
        self.assertFalse(next(r for r in rules[phone_id] if r["path"] == "Demeter")["desired"])
        demeter = self.request("get_placement", params={"path": "Demeter/book.txt"})
        self.assertEqual({x["device_id"] for x in demeter["desired_locations"]}, {desktop_id})
        audiobook = self.request("get_placement", params={"path": "Audiobooks/book.m4b"})
        self.assertEqual({x["device_id"] for x in audiobook["desired_locations"]}, {pi_id})

    def test_last_copy_off_is_saved_and_physical_removal_is_blocked(self) -> None:
        desktop_id = "d" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [self.entry("only.txt")]})
        self.request("set_desired_location", method="POST", payload={
            "path": "only.txt", "device_id": desktop_id, "desired": True, "recursive": False,
        })
        result = self.request("set_desired_location", method="POST", payload={
            "path": "only.txt", "device_id": desktop_id, "desired": False, "recursive": False,
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["removal_status"], "blocked")
        placement = self.request("get_placement", params={"path": "only.txt"})
        self.assertEqual(placement["desired_locations"], [])
        self.assertEqual({x["device_id"] for x in placement["actual_locations"]}, {desktop_id})
        manifest = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertIn(desktop_id, manifest["only.txt"]["removal_blocked_locations"])

    def test_removal_refuses_unverified_or_different_alternate_bytes(self) -> None:
        first, second = "8" * 32, "9" * 32
        self.register(first, "First", "desktop", ["local_storage"])
        self.register(second, "Second", "desktop", ["local_storage"])
        target = {**self.entry("ambiguous.txt"), "hash": ""}
        alternate = {**target, "hash": ""}
        self.request("publish_manifest", method="POST", payload={"device_id": first, "entries": [target]})
        self.request("publish_manifest", method="POST", payload={"device_id": second, "entries": [alternate]})
        pending = self.request("set_desired_location", method="POST", payload={
            "path": "ambiguous.txt", "device_id": first, "desired": False, "recursive": False,
        })
        self.assertTrue(pending["ok"])
        self.assertTrue(pending["pending"])
        first_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": first})["jobs"]
        second_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": second})["jobs"]
        self.assertTrue(any(job["status"] == "queued_hash" for job in first_jobs))
        self.assertTrue(any(job["status"] == "queued_hash" for job in second_jobs))

        alternate["hash"] = hashlib.sha256(b"xyz").hexdigest()
        self.request("publish_manifest", method="POST", payload={"device_id": second, "entries": [alternate]})
        pending = self.request("set_desired_location", method="POST", payload={
            "path": "ambiguous.txt", "device_id": first, "desired": False, "recursive": False,
        })
        self.assertTrue(pending["ok"])
        self.assertTrue(pending["pending"])

    def test_blocked_off_persists_and_worker_retries_after_conflict_clears(self) -> None:
        target, alternate = "2" * 32, "3" * 32
        self.register(target, "Target", "desktop", ["local_storage"])
        self.register(alternate, "Alternate", "android", ["local_storage"])
        entry = self.entry("blocked.txt")
        for device in (target, alternate):
            self.request("publish_manifest", method="POST", payload={"device_id": device, "entries": [entry]})
        reconcile = self.root / "freecloud_meta" / "reconciliation"
        reconcile.mkdir(parents=True, exist_ok=True)
        conflict_id = "4" * 32
        conflict_path = reconcile / f"{conflict_id}.json"
        conflict_path.write_text(json.dumps({
            "job_id": conflict_id, "kind": "reconcile", "path": "blocked.txt",
            "destination_device_id": target, "status": "conflict",
        }), encoding="utf-8")
        result = self.request("set_desired_location", method="POST", payload={
            "path": "blocked.txt", "device_id": target, "desired": False, "recursive": False,
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["removal_status"], "blocked")
        placement = self.request("get_placement", params={"path": "blocked.txt"})
        self.assertNotIn(target, {x["device_id"] for x in placement["desired_locations"]})
        self.assertEqual({x["device_id"] for x in placement["actual_locations"]}, {target, alternate})

        conflict_path.write_text(json.dumps({"job_id": conflict_id, "status": "complete"}), encoding="utf-8")
        pending_path = next(reconcile.glob("pending-remove-*.json"))
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        pending["next_retry_at"] = 0
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": target})["jobs"]
        self.assertTrue(any(job.get("kind") == "remove_copy" and job["status"] == "queued_remove" for job in jobs))

    def test_offline_hash_verification_waits_then_resumes(self) -> None:
        target, alternate = "5" * 32, "6" * 32
        self.register(target, "Target", "desktop", ["local_storage"])
        self.register(alternate, "Alternate", "android", ["local_storage"])
        entry = {**self.entry("offline.txt"), "hash": ""}
        for device in (target, alternate):
            self.request("publish_manifest", method="POST", payload={"device_id": device, "entries": [entry]})
        devices_path = self.root / "freecloud_meta" / "devices.json"
        devices = json.loads(devices_path.read_text(encoding="utf-8"))
        devices[target]["last_seen_at"] = 1
        devices_path.write_text(json.dumps(devices), encoding="utf-8")
        result = self.request("set_desired_location", method="POST", payload={
            "path": "offline.txt", "device_id": target, "desired": False, "recursive": False,
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["removal_status"], "waiting")
        placement = self.request("get_placement", params={"path": "offline.txt"})
        self.assertEqual(placement["desired_locations"], [])
        self.assertEqual({x["device_id"] for x in placement["actual_locations"]}, {target, alternate})

        self.request("heartbeat", method="POST", payload={"device_id": target})
        pending_path = next((self.root / "freecloud_meta" / "reconciliation").glob("pending-remove-*.json"))
        pending_record = json.loads(pending_path.read_text(encoding="utf-8"))
        pending_record["next_retry_at"] = 0
        pending_path.write_text(json.dumps(pending_record), encoding="utf-8")
        target_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": target})["jobs"]
        other_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": alternate})["jobs"]
        for device, jobs in ((target, target_jobs), (alternate, other_jobs)):
            job = next(item for item in jobs if item["status"] == "queued_hash")
            self.request("report_location_hash", method="POST", payload={
                "device_id": device, "job_id": job["job_id"], "hash": hashlib.sha256(b"abc").hexdigest(),
            })
        removal = self.request("poll_reconciliation", method="POST", payload={"device_id": target})["jobs"]
        self.assertTrue(any(job.get("kind") == "remove_copy" for job in removal))

    def test_on_request_restores_desired_and_creates_copy_job(self) -> None:
        target, source = "7" * 32, "8" * 32
        self.register(target, "Target", "desktop", ["local_storage"])
        self.register(source, "Source", "desktop", ["local_storage"])
        entry = self.entry("restore.txt")
        self.request("publish_manifest", method="POST", payload={"device_id": source, "entries": [entry]})
        self.request("set_desired_location", method="POST", payload={
            "path": "restore.txt", "device_id": target, "desired": False, "recursive": False,
        })
        result = self.request("set_desired_location", method="POST", payload={
            "path": "restore.txt", "device_id": target, "desired": True, "recursive": False,
        })
        self.assertTrue(result["ok"])
        placement = self.request("get_placement", params={"path": "restore.txt"})
        self.assertIn(target, {x["device_id"] for x in placement["desired_locations"]})
        self.assertNotIn(target, {x["device_id"] for x in placement["actual_locations"]})
        self.assertIsNotNone(result["reconciliation_job"])

    def test_on_demand_hash_proof_completes_safe_removal_and_persists_facts(self) -> None:
        target, alternate = "6" * 32, "7" * 32
        self.register(target, "Target", "desktop", ["local_storage"])
        self.register(alternate, "Alternate", "android", ["local_storage"])
        entry = {**self.entry("proof.txt"), "hash": ""}
        for device in (target, alternate):
            self.request("publish_manifest", method="POST", payload={"device_id": device, "entries": [entry]})
        self.request("set_desired_location", method="POST", payload={
            "path": "proof.txt", "device_id": target, "desired": True, "recursive": False,
        })
        result = self.request("set_desired_location", method="POST", payload={
            "path": "proof.txt", "device_id": target, "desired": False, "recursive": False,
        })
        self.assertTrue(result["pending"])
        for device in (target, alternate):
            jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": device})["jobs"]
            job = next(item for item in jobs if item["status"] == "queued_hash")
            self.request("report_location_hash", method="POST", payload={
                "device_id": device, "job_id": job["job_id"], "hash": hashlib.sha256(b"abc").hexdigest(),
            })
        placement = self.request("get_placement", params={"path": "proof.txt"})
        self.assertEqual(placement["desired_locations"], [])
        target_location = next(item for item in placement["actual_locations"] if item["device_id"] == target)
        self.assertEqual(target_location["hash"], hashlib.sha256(b"abc").hexdigest())
        removal_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": target})["jobs"]
        self.assertTrue(any(item.get("kind") == "remove_copy" and item["status"] == "queued_remove" for item in removal_jobs))

    def test_on_demand_hash_mismatch_keeps_bytes_but_records_desired_off(self) -> None:
        target, alternate = "4" * 32, "5" * 32
        self.register(target, "Target", "desktop", ["local_storage"])
        self.register(alternate, "Alternate", "desktop", ["local_storage"])
        entry = {**self.entry("mismatch.txt"), "hash": ""}
        for device in (target, alternate):
            self.request("publish_manifest", method="POST", payload={"device_id": device, "entries": [entry]})
        self.request("set_desired_location", method="POST", payload={
            "path": "mismatch.txt", "device_id": target, "desired": True, "recursive": False,
        })
        self.request("set_desired_location", method="POST", payload={
            "path": "mismatch.txt", "device_id": target, "desired": False, "recursive": False,
        })
        for device, contents in ((target, b"abc"), (alternate, b"xyz")):
            jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": device})["jobs"]
            job = next(item for item in jobs if item["status"] == "queued_hash")
            self.request("report_location_hash", method="POST", payload={
                "device_id": device, "job_id": job["job_id"], "hash": hashlib.sha256(contents).hexdigest(),
            })
        placement = self.request("get_placement", params={"path": "mismatch.txt"})
        self.assertNotIn(target, {item["device_id"] for item in placement["desired_locations"]})
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {target, alternate})
        manifest = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertIn(target, manifest["mismatch.txt"]["removal_blocked_locations"])
        self.assertFalse(any(item.get("kind") == "remove_copy" for item in self.request("poll_reconciliation", method="POST", payload={"device_id": target})["jobs"]))

    def test_recursive_removal_refuses_to_destroy_selected_child_override(self) -> None:
        first, second = "a" * 32, "b" * 32
        for device_id, name in ((first, "First"), (second, "Second")):
            self.register(device_id, name, "desktop", ["local_storage"])
        entries = [
            {**self.entry("Folder"), "type": "dir", "size": 0},
            self.entry("Folder/ordinary.txt"), self.entry("Folder/keep.txt"),
        ]
        self.request("publish_manifest", method="POST", payload={"device_id": first, "entries": entries})
        self.request("publish_manifest", method="POST", payload={"device_id": second, "entries": entries})
        for device_id in (first, second):
            self.request("set_desired_location", method="POST", payload={
                "path": "Folder", "device_id": device_id, "desired": True, "recursive": True,
            })
        self.request("set_desired_location", method="POST", payload={
            "path": "Folder/keep.txt", "device_id": first, "desired": True, "recursive": False,
        })
        refused = self.request("set_desired_location", method="POST", payload={
            "path": "Folder", "device_id": first, "desired": False, "recursive": True,
        })
        self.assertTrue(refused["ok"])
        self.assertEqual(refused["removal_status"], "blocked")
        self.assertIn("child is still selected", refused["message"])
        parent = self.request("get_placement", params={"path": "Folder"})
        self.assertNotIn(first, {item["device_id"] for item in parent["desired_locations"]})
        rule_data = self.request("placement_rules")
        first_rules = rule_data["desired_rules"][first]
        self.assertEqual(sum(1 for item in first_rules if item["path"] == "Folder"), 1)
        self.assertEqual(sum(1 for item in first_rules if item["path"].startswith("Folder/")), 1)
        child = self.request("get_placement", params={"path": "Folder/keep.txt"})
        self.assertIn(first, {item["device_id"] for item in child["desired_locations"]})
        self.assertEqual({item["device_id"] for item in child["actual_locations"]}, {first, second})

    def test_audiobooks_pi_only_cannot_be_removed_as_last_copy(self) -> None:
        pi_id = "c" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        self.request("set_sync_rules", method="POST", payload={"device_id": pi_id, "rules": [
            {"path": "Audiobooks", "mode": "storage_only", "recursive": True},
        ]})
        self.request("publish_manifest", method="POST", payload={"device_id": pi_id, "entries": [self.entry("Audiobooks/book.mp3")]})
        before = self.request("get_placement", params={"path": "Audiobooks/book.mp3"})
        self.assertEqual({item["device_id"] for item in before["desired_locations"]}, {pi_id})
        refused = self.request("set_desired_location", method="POST", payload={
            "path": "Audiobooks", "device_id": pi_id, "desired": False, "recursive": True,
        })
        self.assertTrue(refused["ok"])
        self.assertEqual(refused["removal_status"], "blocked")
        placement = self.request("get_placement", params={"path": "Audiobooks/book.mp3"})
        self.assertEqual(placement["desired_locations"], [])
        self.assertEqual({item["device_id"] for item in placement["actual_locations"]}, {pi_id})

    def test_device_copy_removal_is_recursive_retryable_and_not_global(self) -> None:
        first = "e" * 32
        second = "f" * 32
        for device_id, name in ((first, "First"), (second, "Second")):
            self.register(device_id, name, "desktop", ["local_storage", "desktop"])
        entries = [{**self.entry("Disposable"), "type": "dir", "size": 0}, self.entry("Disposable/file.txt")]
        self.request("publish_manifest", method="POST", payload={"device_id": first, "entries": entries})
        self.request("publish_manifest", method="POST", payload={"device_id": second, "entries": entries})
        self.request("set_desired_location", method="POST", payload={
            "path": "Disposable", "device_id": first, "desired": True, "recursive": True,
        })
        self.request("set_desired_location", method="POST", payload={
            "path": "Disposable", "device_id": second, "desired": True, "recursive": True,
        })
        result = self.request("set_desired_location", method="POST", payload={
            "path": "Disposable", "device_id": first, "desired": False, "recursive": True,
        })
        job = result["reconciliation_job"]
        self.assertEqual(job["status"], "queued_remove")
        self.assertEqual(set(job["removal_facts"]), {"Disposable", "Disposable/file.txt"})
        self.assertEqual(len(list((self.root / "freecloud_meta" / "reconciliation").glob("*.json"))), 2)
        again = self.request("set_desired_location", method="POST", payload={
            "path": "Disposable", "device_id": first, "desired": False, "recursive": True,
        })
        self.assertEqual(again["reconciliation_job"]["job_id"], job["job_id"])
        before = self.request("get_placement", params={"path": "Disposable/file.txt"})
        self.assertEqual({x["device_id"] for x in before["actual_locations"]}, {first, second})
        self.assertEqual({x["device_id"] for x in before["desired_locations"]}, {second})
        polled = self.request("poll_reconciliation", method="POST", payload={"device_id": first})["jobs"]
        self.assertEqual([x["status"] for x in polled], ["queued_remove"])
        self.request("ack_reconciliation", method="POST", payload={
            "device_id": first, "job_id": job["job_id"], "status": "removed",
        })
        self.request("publish_manifest", method="POST", payload={"device_id": first, "entries": []})
        after = self.request("get_placement", params={"path": "Disposable/file.txt"})
        self.assertEqual({x["device_id"] for x in after["actual_locations"]}, {second})
        self.assertEqual({x["device_id"] for x in after["desired_locations"]}, {second})

    def test_web_copy_removal_deletes_only_managed_web_bytes(self) -> None:
        desktop_id = "1" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        target = self.root / "freecloud_files" / "remove-web.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"abc")
        os.utime(target, (100, 100))
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [self.entry("remove-web.txt")]})
        self.request("set_desired_location", method="POST", payload={
            "path": "remove-web.txt", "device_id": "web_server", "desired": True, "recursive": False,
        })
        result = self.request("set_desired_location", method="POST", payload={
            "path": "remove-web.txt", "device_id": "web_server", "desired": False, "recursive": False,
        })
        self.assertIsNotNone(result["reconciliation_job"], result)
        self.assertEqual(result["reconciliation_job"]["status"], "complete", result)
        self.assertFalse(target.exists())
        placement = self.request("get_placement", params={"path": "remove-web.txt"})
        self.assertEqual({x["device_id"] for x in placement["actual_locations"]}, {desktop_id})
        self.assertEqual(placement["desired_locations"], [])

    def test_obsolete_copy_job_is_canceled_when_destination_is_not_desired(self) -> None:
        desktop_id = "2" * 32
        pi_id = "3" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [self.entry("video.mp4")]})
        jobs = self.root / "freecloud_meta" / "reconciliation"
        jobs.mkdir(parents=True, exist_ok=True)
        record = {
            "job_id": "9" * 32, "kind": "generic_client_transfer", "path": "video.mp4",
            "source_device_id": desktop_id, "destination_device_id": pi_id,
            "status": "queued_source", "created_at": 100, "updated_at": 100,
        }
        record_path = jobs / f"{record['job_id']}.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        polled = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        self.assertEqual(polled["jobs"], [])
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8"))["status"], "canceled")

    def test_phase3a_web_to_desktop_job_is_idempotent_and_completes(self) -> None:
        desktop_id = "3" * 32
        self.register(desktop_id, "Laptop", "desktop", ["metadata", "local_storage", "desktop"])
        source = self.root / "freecloud_files" / "phase3a-test.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"safe reconciliation\n")
        os.utime(source, (1_700_000_000, 1_700_000_000))

        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        web_location = next(item for item in manifest["phase3a-test.txt"]["locations"] if item["device_id"] == "web_server")
        self.assertEqual(web_location["size"], len(b"safe reconciliation\n"))
        self.assertEqual(web_location["mtime"], 1_700_000_000)
        devices_path = self.root / "freecloud_meta" / "devices.json"
        devices_data = json.loads(devices_path.read_text(encoding="utf-8"))
        devices_data[desktop_id]["last_seen_at"] = 0
        devices_data[desktop_id]["online"] = False
        devices_path.write_text(json.dumps(devices_data), encoding="utf-8")

        response = self.request(
            "set_desired_location", method="POST",
            payload={"path": "phase3a-test.txt", "device_id": desktop_id, "desired": True, "recursive": False},
        )
        job = response["reconciliation_job"]
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["source_device_id"], "web_server")
        self.assertEqual(job["destination_device_id"], desktop_id)
        self.assertEqual(job["expected_mtime"], 1_700_000_000)
        self.assertEqual(len(job["hash"]), 64)
        jobs_dir = self.root / "freecloud_meta" / "reconciliation"
        self.assertEqual(len(list(jobs_dir.glob("*.json"))), 1)

        first_poll = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        second_poll = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        self.assertEqual(len(first_poll["jobs"]), 1)
        self.assertEqual(len(second_poll["jobs"]), 1)
        self.assertEqual(len(list(jobs_dir.glob("*.json"))), 1)

        self.request(
            "ack_reconciliation", method="POST",
            payload={"device_id": desktop_id, "job_id": job["job_id"], "status": "installed"},
        )
        self.request(
            "publish_manifest", method="POST",
            payload={"device_id": desktop_id, "entries": [{**self.entry("phase3a-test.txt"), "size": len(b"safe reconciliation\n"), "mtime": 1_700_000_000, "hash": ""}]},
        )
        final_manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        final_entry = final_manifest["phase3a-test.txt"]
        self.assertEqual({item["device_id"] for item in final_entry["locations"]}, {"web_server", desktop_id})
        desktop_location = next(item for item in final_entry["locations"] if item["device_id"] == desktop_id)
        self.assertEqual(desktop_location["hash"], job["hash"])
        self.assertEqual({item["device_id"] for item in final_entry["desired_locations"]}, {desktop_id})
        stored_job = json.loads(next(jobs_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(stored_job["status"], "complete")
        self.assertFalse((jobs_dir / f"{job['job_id']}.bin").exists())

    def test_reconciliation_plans_explicit_android_and_pi_but_not_audiobooks(self) -> None:
        desktop_id = "3" * 32
        phone_id = "4" * 32
        pi_id = "5" * 32
        self.register(desktop_id, "Offline Laptop", "desktop", ["local_storage", "desktop"])
        self.register(phone_id, "Phone", "android", ["local_storage"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        files = self.root / "freecloud_files"
        files.mkdir(parents=True, exist_ok=True)
        (files / "web-only.txt").write_text("web", encoding="utf-8")
        self.request(
            "set_desired_location", method="POST",
            payload={"path": "web-only.txt", "device_id": phone_id, "desired": True, "recursive": False},
        )
        self.request(
            "set_desired_location", method="POST",
            payload={"path": "web-only.txt", "device_id": pi_id, "desired": True, "recursive": False},
        )
        self.request(
            "set_sync_rules", method="POST",
            payload={"device_id": pi_id, "rules": [{"path": "Audiobooks", "mode": "storage_only", "recursive": True}]},
        )
        self.request(
            "publish_manifest", method="POST",
            payload={"device_id": pi_id, "entries": [self.entry("Audiobooks/book.m4b")]},
        )
        self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        jobs = [json.loads(path.read_text(encoding="utf-8")) for path in
                (self.root / "freecloud_meta" / "reconciliation").glob("*.json")]
        web_jobs = [job for job in jobs if job["path"] == "web-only.txt"]
        self.assertEqual(len(web_jobs), 2)
        self.assertEqual({job["destination_device_id"] for job in web_jobs}, {phone_id, pi_id})
        self.assertFalse(any(job["path"].startswith("Audiobooks") for job in jobs))

    def stage_reconciliation_job(self, job: dict[str, object], content: bytes) -> None:
        jobs_dir = self.root / "freecloud_meta" / "reconciliation"
        record_path = jobs_dir / f"{job['job_id']}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.update({"status": "staged", "hash": __import__("hashlib").sha256(content).hexdigest()})
        record_path.write_text(json.dumps(record), encoding="utf-8")
        (jobs_dir / f"{job['job_id']}.bin").write_bytes(content)

    def test_phase3b_desktop_to_web_missing_and_newer_update(self) -> None:
        desktop_id = "6" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        content1 = b"desktop first\n"
        entry1 = {**self.entry("phase3b.txt"), "size": len(content1), "mtime": 1_700_000_100}
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [entry1]})
        response = self.request(
            "set_desired_location", method="POST",
            payload={"path": "phase3b.txt", "device_id": "web_server", "desired": True, "recursive": False},
        )
        job1 = response["reconciliation_job"]
        self.assertEqual(job1["status"], "queued_source")
        self.assertEqual(job1["source_device_id"], desktop_id)
        self.assertEqual(job1["destination_device_id"], "web_server")
        self.assertEqual(len(list((self.root / "freecloud_meta" / "reconciliation").glob("*.json"))), 1)
        self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})
        self.assertEqual(len(list((self.root / "freecloud_meta" / "reconciliation").glob("*.json"))), 1)

        self.stage_reconciliation_job(job1, content1)
        committed1 = self.request(
            "commit_reconciliation", method="POST",
            payload={"device_id": desktop_id, "job_id": job1["job_id"], "source_unchanged": True},
        )["job"]
        target = self.root / "freecloud_files" / "phase3b.txt"
        self.assertEqual(committed1["status"], "complete")
        self.assertEqual(target.read_bytes(), content1)
        self.assertEqual(int(target.stat().st_mtime), 1_700_000_100)

        content2 = b"desktop second version\n"
        entry2 = {**self.entry("phase3b.txt"), "size": len(content2), "mtime": 1_700_000_200}
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [entry2]})
        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})["jobs"]
        job2 = next(item for item in jobs if item["status"] == "queued_source")
        self.stage_reconciliation_job(job2, content2)
        self.request(
            "commit_reconciliation", method="POST",
            payload={"device_id": desktop_id, "job_id": job2["job_id"], "source_unchanged": True},
        )
        self.assertEqual(target.read_bytes(), content2)
        self.assertEqual(int(target.stat().st_mtime), 1_700_000_200)
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual({item["device_id"] for item in manifest["phase3b.txt"]["locations"]}, {desktop_id, "web_server"})

    def test_phase3b_web_newer_uses_existing_web_to_desktop_route(self) -> None:
        desktop_id = "7" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        target = self.root / "freecloud_files" / "web-newer.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new web")
        os.utime(target, (1_700_000_300, 1_700_000_300))
        desktop_entry = {**self.entry("web-newer.txt"), "size": 3, "mtime": 1_700_000_100}
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [desktop_entry]})
        response = self.request(
            "set_desired_location", method="POST",
            payload={"path": "web-newer.txt", "device_id": desktop_id, "desired": True, "recursive": False},
        )
        job = response["reconciliation_job"]
        self.assertEqual((job["source_device_id"], job["destination_device_id"], job["status"]), ("web_server", desktop_id, "ready"))

    def test_phase3b_destination_change_and_ambiguous_versions_never_overwrite(self) -> None:
        desktop_id = "8" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        target = self.root / "freecloud_files" / "protected.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old web")
        os.utime(target, (1_700_000_100, 1_700_000_100))
        desktop_content = b"new desktop"
        desktop_entry = {**self.entry("protected.txt"), "size": len(desktop_content), "mtime": 1_700_000_300}
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [desktop_entry]})
        response = self.request(
            "set_desired_location", method="POST",
            payload={"path": "protected.txt", "device_id": "web_server", "desired": True, "recursive": False},
        )
        job = response["reconciliation_job"]
        self.stage_reconciliation_job(job, desktop_content)
        target.write_bytes(b"changed while pending")
        os.utime(target, (1_700_000_400, 1_700_000_400))
        failed = self.request(
            "commit_reconciliation", method="POST",
            payload={"device_id": desktop_id, "job_id": job["job_id"], "source_unchanged": True},
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(target.read_bytes(), b"changed while pending")

        ambiguous = self.root / "freecloud_files" / "ambiguous.txt"
        ambiguous.write_bytes(b"web")
        os.utime(ambiguous, (1_700_000_500, 1_700_000_500))
        self.request(
            "publish_manifest", method="POST",
            payload={"device_id": desktop_id, "entries": [{**self.entry("ambiguous.txt"), "size": 9, "mtime": 1_700_000_500}]},
        )
        result = self.request(
            "set_desired_location", method="POST",
            payload={"path": "ambiguous.txt", "device_id": "web_server", "desired": True, "recursive": False},
        )
        self.assertEqual(result["reconciliation_job"]["status"], "conflict")
        self.assertEqual(ambiguous.read_bytes(), b"web")

    def test_phase3b_web_not_desired_creates_no_upload(self) -> None:
        desktop_id = "9" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.request(
            "publish_manifest", method="POST",
            payload={"device_id": desktop_id, "entries": [self.entry("local-only.txt")]},
        )
        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id})["jobs"]
        self.assertFalse(any(item.get("path") == "local-only.txt" for item in jobs))

    def test_phase3c_android_routes_are_generalized_and_idempotent(self) -> None:
        desktop_id = "a" * 32
        android_id = "b" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage"])
        self.register(android_id, "Pixel", "android", ["local_storage", "background_sync"])

        web = self.root / "freecloud_files" / "web-phone.txt"
        web.parent.mkdir(parents=True, exist_ok=True)
        web.write_bytes(b"web to phone")
        os.utime(web, (1_700_001_000, 1_700_001_000))
        result = self.request("set_desired_location", method="POST", payload={
            "path": "web-phone.txt", "device_id": android_id, "desired": True, "recursive": False,
        })
        self.assertEqual((result["reconciliation_job"]["source_device_id"], result["reconciliation_job"]["destination_device_id"]),
                         ("web_server", android_id))

        android_entry = {**self.entry("phone-web.txt"), "size": 12, "mtime": 1_700_001_100}
        self.request("publish_manifest", method="POST", payload={"device_id": android_id, "entries": [android_entry]})
        result = self.request("set_desired_location", method="POST", payload={
            "path": "phone-web.txt", "device_id": "web_server", "desired": True, "recursive": False,
        })
        self.assertEqual((result["reconciliation_job"]["source_device_id"], result["reconciliation_job"]["destination_device_id"]),
                         (android_id, "web_server"))

        desktop_entry = {**self.entry("desktop-phone.txt"), "size": 13, "mtime": 1_700_001_200}
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [desktop_entry]})
        result = self.request("set_desired_location", method="POST", payload={
            "path": "desktop-phone.txt", "device_id": android_id, "desired": True, "recursive": False,
        })
        self.assertEqual((result["reconciliation_job"]["source_device_id"], result["reconciliation_job"]["destination_device_id"]),
                         (desktop_id, android_id))

        phone_entry = {**self.entry("phone-desktop.txt"), "size": 14, "mtime": 1_700_001_300}
        self.request("publish_manifest", method="POST", payload={"device_id": android_id, "entries": [android_entry, phone_entry]})
        result = self.request("set_desired_location", method="POST", payload={
            "path": "phone-desktop.txt", "device_id": desktop_id, "desired": True, "recursive": False,
        })
        self.assertEqual((result["reconciliation_job"]["source_device_id"], result["reconciliation_job"]["destination_device_id"]),
                         (android_id, desktop_id))

        jobs_dir = self.root / "freecloud_meta" / "reconciliation"
        before = len(list(jobs_dir.glob("*.json")))
        self.request("poll_reconciliation", method="POST", payload={"device_id": android_id})
        self.request("poll_reconciliation", method="POST", payload={"device_id": android_id})
        self.assertEqual(len(list(jobs_dir.glob("*.json"))), before)

    def test_pi_routes_use_generalized_reconciliation(self) -> None:
        pi_id = "c" * 32
        desktop_id = "d" * 32
        android_id = "e" * 32
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(desktop_id, "Laptop", "desktop", ["local_storage"])
        self.register(android_id, "Pixel", "android", ["local_storage"])

        def publish(device_id: str, path: str, mtime: int) -> None:
            self.request("publish_manifest", method="POST", payload={
                "device_id": device_id,
                "entries": [{**self.entry(path), "mtime": mtime}],
            })

        routes = [
            (pi_id, "pi-web.txt", "web_server"),
            (pi_id, "pi-desktop.txt", desktop_id),
            (pi_id, "pi-android.txt", android_id),
            (desktop_id, "desktop-pi.txt", pi_id),
            (android_id, "android-pi.txt", pi_id),
        ]
        for index, (source, path, destination) in enumerate(routes):
            publish(source, path, 1_700_010_000 + index * 10)
            result = self.request("set_desired_location", method="POST", payload={
                "path": path, "device_id": destination, "desired": True, "recursive": False,
            })
            job = result["reconciliation_job"]
            self.assertEqual((job["source_device_id"], job["destination_device_id"]), (source, destination))

        first_count = len(list((self.root / "freecloud_meta" / "reconciliation").glob("*.json")))
        self.request("poll_reconciliation", method="POST", payload={"device_id": pi_id})
        self.request("poll_reconciliation", method="POST", payload={"device_id": pi_id})
        self.assertEqual(len(list((self.root / "freecloud_meta" / "reconciliation").glob("*.json"))), first_count)

    def test_desktop_publication_changes_only_its_actual_locations(self) -> None:
        pi_id = "1" * 32
        desktop_id = "3" * 32
        self.register(pi_id, "Raspberry Pi", "raspberry_pi", ["storage_node"])
        self.register(desktop_id, "Laptop", "desktop", ["metadata", "local_storage", "desktop"])
        self.request(
            "set_sync_rules",
            method="POST",
            payload={"device_id": pi_id, "rules": [{"path": "Audiobooks", "mode": "storage_only", "recursive": True}]},
        )
        self.request(
            "publish_manifest",
            method="POST",
            payload={
                "device_id": pi_id,
                "entries": [self.entry("shared.txt"), self.entry("Audiobooks/book.m4b")],
            },
        )
        self.request(
            "set_desired_location",
            method="POST",
            payload={"path": "shared.txt", "device_id": "web_server", "desired": True},
        )
        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": desktop_id, "entries": [self.entry("shared.txt"), self.entry("laptop.txt")]},
        )

        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual(
            {item["device_id"] for item in manifest["shared.txt"]["locations"]},
            {pi_id, desktop_id},
        )
        self.assertEqual(
            {item["device_id"] for item in manifest["shared.txt"]["desired_locations"]},
            {"web_server"},
        )
        self.assertEqual(
            {item["device_id"] for item in manifest["Audiobooks/book.m4b"]["locations"]},
            {pi_id},
        )
        self.assertEqual(
            {item["device_id"] for item in manifest["Audiobooks/book.m4b"]["desired_locations"]},
            {pi_id},
        )

        self.request(
            "publish_manifest",
            method="POST",
            payload={"device_id": desktop_id, "entries": [self.entry("laptop.txt")]},
        )
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        self.assertEqual({item["device_id"] for item in manifest["shared.txt"]["locations"]}, {pi_id})
        self.assertEqual(
            {item["device_id"] for item in manifest["shared.txt"]["desired_locations"]},
            {"web_server"},
        )
        self.assertEqual({item["device_id"] for item in manifest["Audiobooks/book.m4b"]["locations"]}, {pi_id})

    def test_recursive_global_delete_persists_jobs_and_prevents_manifest_resurrection(self) -> None:
        desktop_id = "f" * 32
        pixel_id = "9" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pixel_id, "Pixel", "android", ["local_storage", "background_sync"])
        folder = {"path": "disposable-delete", "name": "disposable-delete", "type": "dir", "size": 0, "mtime": 100}
        child = self.entry("disposable-delete/child.txt")
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [folder, child]})
        self.request("publish_manifest", method="POST", payload={"device_id": pixel_id, "entries": [folder, child]})

        deleted = self.request("global_delete", method="POST", payload={"path": "disposable-delete", "recursive": True})
        tombstone_id = deleted["tombstone"]["tombstone_id"]
        self.assertEqual(deleted["pending_devices"], [desktop_id, pixel_id])
        listing = self.request("metadata_list", params={"path": "/"})
        self.assertNotIn("disposable-delete", {item["path"] for item in listing["entries"]})

        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id, "check_only": True})["jobs"]
        delete_job = next(item for item in jobs if item["kind"] == "global_delete")
        self.assertEqual(delete_job["status"], "queued_delete")
        self.request("ack_reconciliation", method="POST", payload={
            "device_id": desktop_id, "job_id": delete_job["job_id"], "status": "deleted",
        })

        # A stale positive snapshot cannot make the tombstoned path visible again;
        # it causes a fresh, idempotent delete command for that provider.
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [folder, child]})
        listing = self.request("metadata_list", params={"path": "/"})
        self.assertNotIn("disposable-delete", {item["path"] for item in listing["entries"]})
        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": desktop_id, "check_only": True})["jobs"]
        self.assertTrue(any(item["kind"] == "global_delete" and item["status"] == "queued_delete" for item in jobs))
        self.assertEqual(json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text())[tombstone_id]["status"], "pending")

        # Pixel represents an offline device: its queued deletion remains
        # durable and is delivered when it next polls.
        pixel_jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": pixel_id})["jobs"]
        pixel_delete = next(item for item in pixel_jobs if item["kind"] == "global_delete")
        self.assertEqual(pixel_delete["status"], "queued_delete")
        self.request("ack_reconciliation", method="POST", payload={
            "device_id": pixel_id, "job_id": pixel_delete["job_id"], "status": "deleted",
        })

    def test_explicit_recreation_starts_new_generation_and_blocks_stale_manifest(self) -> None:
        desktop_id = "a" * 32
        pixel_id = "b" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pixel_id, "Pixel", "android", ["local_storage", "background_sync"])
        folder = {"path": "3515 Leap", "name": "3515 Leap", "type": "dir", "size": 0, "mtime": 100}
        child = self.entry("3515 Leap/track.txt")
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [folder, child], "_keep_new_placement_default": True,
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": pixel_id, "entries": [folder, child],
        })
        deleted = self.request("global_delete", method="POST", payload={
            "path": "3515 Leap", "recursive": True,
        })
        old_tombstone_id = deleted["tombstone"]["tombstone_id"]
        self.assertFalse(self.request("set_desired_location", method="POST", payload={
            "path": "3515 Leap", "device_id": pixel_id, "desired": True, "recursive": True,
        }).get("ok"))

        # A fresh complete Desktop inventory is the explicit source evidence.
        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [folder, child], "complete": True,
        })
        recreated = self.request("recreate_path", method="POST", payload={
            "path": "3515 Leap", "source_device_id": desktop_id,
            "desired_device_id": pixel_id, "recursive": True,
        })
        self.assertTrue(recreated["ok"])
        self.assertEqual(recreated["entries"], 2)
        self.assertEqual(len(recreated["generation_id"]), 32)
        visible = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertIn("3515 Leap", visible)
        self.assertIn("3515 Leap/track.txt", visible)
        self.assertEqual(visible["3515 Leap/track.txt"]["generation_id"], recreated["generation_id"])
        self.assertEqual({x["device_id"] for x in visible["3515 Leap/track.txt"]["locations"]}, {desktop_id})
        self.assertEqual(
            {x["device_id"] for x in visible["3515 Leap/track.txt"]["desired_locations"]},
            {desktop_id, pixel_id},
        )

        # A stale Pixel inventory cannot restore the old generation, and its
        # old queued delete must finish before any new copy job is delivered.
        self.request("publish_manifest", method="POST", payload={
            "device_id": pixel_id, "entries": [folder, child], "complete": True,
        })
        _tombstone_debug = json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text())[old_tombstone_id]
        after_stale = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertEqual({x["device_id"] for x in after_stale["3515 Leap/track.txt"]["locations"]}, {desktop_id})
        pixel_jobs = self.request("poll_reconciliation", method="POST", payload={
            "device_id": pixel_id, "check_only": True,
        })["jobs"]
        self.assertTrue(any(job["kind"] == "global_delete" for job in pixel_jobs), {"jobs": pixel_jobs, "tombstone": _tombstone_debug})
        delete_job = next(job for job in pixel_jobs if job["kind"] == "global_delete")
        self.assertFalse(any(job.get("kind") != "global_delete" for job in pixel_jobs))
        self.request("ack_reconciliation", method="POST", payload={
            "device_id": pixel_id, "job_id": delete_job["job_id"], "status": "deleted",
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": pixel_id, "entries": [folder, child], "complete": True,
        })
        after_reconnect = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertEqual({x["device_id"] for x in after_reconnect["3515 Leap/track.txt"]["locations"]}, {desktop_id})
        self.request("set_desired_location", method="POST", payload={
            "path": "3515 Leap", "device_id": "web_server", "desired": True, "recursive": True,
        })
        self.request("poll_reconciliation", method="POST", payload={"device_id": pixel_id})
        jobs = [json.loads(path.read_text()) for path in (self.root / "freecloud_meta" / "reconciliation").glob("*.json")]
        self.assertTrue(any(job.get("destination_device_id") == pixel_id
                            and job.get("path") == "3515 Leap/track.txt"
                            and job.get("kind") != "global_delete" for job in jobs))

        history = json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text())
        self.assertEqual(history[old_tombstone_id]["status"], "superseded")
        self.assertEqual(history[old_tombstone_id]["superseded_by_generation"], recreated["generation_id"])
        deleted_again = self.request("global_delete", method="POST", payload={
            "path": "3515 Leap", "recursive": True,
        })
        self.assertNotEqual(deleted_again["tombstone"]["tombstone_id"], old_tombstone_id)
        self.assertEqual(self.request("metadata_list", params={"path": "/"})["entries"], [])

    def test_incomplete_manifest_does_not_remove_actual_location_and_complete_does(self) -> None:
        device_id = "8" * 32
        self.register(device_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.request("publish_manifest", method="POST", payload={"device_id": device_id, "entries": [self.entry("gone.txt")]})
        self.request("publish_manifest", method="POST", payload={"device_id": device_id, "entries": [], "complete": False})
        current = {item["path"]: item for item in self.request("metadata_manifest")["entries"]}
        self.assertIn(device_id, {loc["device_id"] for loc in current["gone.txt"]["locations"]})
        self.request("publish_manifest", method="POST", payload={"device_id": device_id, "entries": [], "complete": True})
        self.assertFalse(any(item["path"] == "gone.txt" for item in self.request("metadata_manifest")["entries"]))
        catalog = json.loads((self.root / "freecloud_meta" / "files.json").read_text())
        self.assertNotIn("gone.txt", catalog)

    def test_new_normal_content_defaults_to_all_storage_providers_and_toggles_override(self) -> None:
        desktop_id = "a" * 32
        pi_id = "b" * 32
        android_id = "c" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(android_id, "Pixel", "android", ["local_storage"])
        folder = {"path": "new-normal", "name": "new-normal", "type": "dir", "size": 0, "mtime": 100}
        child = {**self.entry("new-normal/test.txt"), "size": 5, "mtime": 100}

        self.request("publish_manifest", method="POST", payload={
            "device_id": desktop_id, "entries": [folder, child],
            "_keep_new_placement_default": True,
        })
        listed = {entry["path"]: entry for entry in self.request("metadata_list", params={"path": "/"})["entries"]}
        self.assertEqual(
            {item["device_id"] for item in listed["new-normal"]["desired_locations"]},
            {desktop_id, pi_id, android_id, "web_server"},
        )
        nested = self.request("metadata_list", params={"path": "/new-normal"})["entries"]
        self.assertEqual(
            {item["device_id"] for item in nested[0]["desired_locations"]},
            {desktop_id, pi_id, android_id, "web_server"},
        )

        android_path = "android-origin.txt"
        self.request("publish_manifest", method="POST", payload={
            "device_id": android_id, "entries": [self.entry(android_path)],
            "_keep_new_placement_default": True,
        })
        android_entry = next(item for item in self.request("metadata_manifest")["entries"]
                             if item["path"] == android_path)
        self.assertEqual(
            {item["device_id"] for item in android_entry["desired_locations"]},
            {desktop_id, pi_id, android_id, "web_server"},
        )

        self.request("set_sync_rules", method="POST", payload={
            "device_id": pi_id,
            "rules": [{"path": "pi-normal", "mode": "sync", "recursive": True}],
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": pi_id, "entries": [self.entry("pi-normal/pi-origin.txt")],
            "_keep_new_placement_default": True,
        })
        pi_entry = next(item for item in self.request("metadata_manifest")["entries"]
                        if item["path"] == "pi-normal/pi-origin.txt")
        self.assertEqual(
            {item["device_id"] for item in pi_entry["desired_locations"]},
            {desktop_id, pi_id, android_id, "web_server"},
        )

        # The default causes background reconciliation work without a manual
        # placement click, while explicit OFF remains authoritative.
        self.request("poll_reconciliation", method="POST", payload={"device_id": pi_id})
        jobs = [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / "freecloud_meta" / "reconciliation").glob("*.json")]
        self.assertTrue(any(job.get("path") == "new-normal/test.txt"
                            and job.get("destination_device_id") == pi_id for job in jobs))
        off = self.request("set_desired_location", method="POST", payload={
            "path": "new-normal", "device_id": pi_id, "desired": False, "recursive": True,
        })
        self.assertTrue(off["ok"])
        self.assertNotIn(pi_id, {item["device_id"] for item in off["desired_locations"]})
        child_after_off = self.request("metadata_list", params={"path": "/new-normal"})["entries"][0]
        self.assertNotIn(pi_id, {item["device_id"] for item in child_after_off["desired_locations"]})
        on = self.request("set_desired_location", method="POST", payload={
            "path": "new-normal", "device_id": pi_id, "desired": True, "recursive": True,
        })
        self.assertIn(pi_id, {item["device_id"] for item in on["desired_locations"]})

    def test_new_storage_only_content_stays_source_only_and_old_catalog_is_not_adopted(self) -> None:
        desktop_id = "d" * 32
        pi_id = "e" * 32
        android_id = "f" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(android_id, "Pixel", "android", ["local_storage"])
        self.request("set_sync_rules", method="POST", payload={
            "device_id": pi_id,
            "rules": [{"path": "external-library", "mode": "storage_only", "recursive": True}],
        })
        self.request("publish_manifest", method="POST", payload={
            "device_id": pi_id,
            "_keep_new_placement_default": True,
            "entries": [
                {"path": "external-library", "name": "external-library", "type": "dir", "size": 0, "mtime": 100},
                self.entry("external-library/book.dat"),
            ],
        })
        manifest = {entry["path"]: entry for entry in self.request("metadata_manifest")["entries"]}
        for path in ("external-library", "external-library/book.dat"):
            self.assertEqual(
                {item["device_id"] for item in manifest[path]["desired_locations"]}, {pi_id}
            )

        # Simulate a pre-policy Catalog row: merely reading/updating it must
        # not retroactively turn all its existing copies ON.
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id,
            "entries": [self.entry("legacy-existing.txt")]})
        catalog_path = self.root / "freecloud_meta" / "files.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["legacy-existing.txt"].pop("placement_default", None)
        catalog["legacy-existing.txt"].pop("placement_source_device_id", None)
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        legacy = next(item for item in self.request("metadata_manifest")["entries"]
                      if item["path"] == "legacy-existing.txt")
        self.assertEqual(legacy["desired_locations"], [])

    def test_web_created_folder_gets_new_item_default(self) -> None:
        desktop_id = "9" * 32
        pi_id = "8" * 32
        android_id = "7" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.register(pi_id, "Pi", "raspberry_pi", ["storage_node"])
        self.register(android_id, "Pixel", "android", ["local_storage"])
        created = self.request("mkdir", method="POST", params={"path": "/web-origin"})
        self.assertTrue(created["ok"])
        listed = self.request("metadata_list", params={"path": "/"})["entries"]
        item = next(entry for entry in listed if entry["path"] == "web-origin")
        self.assertEqual(
            {value["device_id"] for value in item["desired_locations"]},
            {desktop_id, pi_id, android_id, "web_server"},
        )

    def test_global_delete_keeps_legacy_delete_endpoint_non_global(self) -> None:
        desktop_id = "7" * 32
        self.register(desktop_id, "Laptop", "desktop", ["local_storage", "desktop"])
        self.request("publish_manifest", method="POST", payload={"device_id": desktop_id, "entries": [self.entry("legacy.txt")]})
        result = self.request("delete", method="POST", payload={"path": "legacy.txt"})
        self.assertFalse(result.get("ok", False))
        self.assertEqual(json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text()), [])

    def test_global_delete_removes_web_copy_and_retains_tombstone_after_catalog_gc(self) -> None:
        phone_id = "6" * 32
        self.register(phone_id, "Pixel", "android", ["local_storage", "background_sync"])
        target = self.root / "freecloud_files" / "disposable-web-delete.txt"
        target.write_bytes(b"disposable")
        result = self.request("global_delete", method="POST", payload={
            "path": "disposable-web-delete.txt", "recursive": False,
        })
        self.assertFalse(target.exists())
        self.assertEqual(result["pending_devices"], [phone_id])
        self.assertFalse(any(item["path"] == "disposable-web-delete.txt" for item in self.request("metadata_manifest")["entries"]))
        tombstones = json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text())
        tombstone = next(iter(tombstones.values()))
        self.assertEqual(tombstone["status"], "pending")
        self.assertTrue((self.root / "freecloud_meta" / "files.json").exists())
        jobs = self.request("poll_reconciliation", method="POST", payload={"device_id": phone_id, "check_only": True})["jobs"]
        delete_job = next(job for job in jobs if job["kind"] == "global_delete")
        self.request("ack_reconciliation", method="POST", payload={
            "device_id": phone_id, "job_id": delete_job["job_id"], "status": "deleted",
        })
        tombstones = json.loads((self.root / "freecloud_meta" / "tombstones.json").read_text())
        self.assertEqual(next(iter(tombstones.values()))["status"], "complete")
        catalog = json.loads((self.root / "freecloud_meta" / "files.json").read_text())
        self.assertNotIn("disposable-web-delete.txt", catalog if isinstance(catalog, dict) else {})
