from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import freecloud_cli as cli


class FakeClient:
    def __init__(self, entries: list[dict[str, object]]) -> None:
        self.entries = entries

    def manifest(self) -> list[dict[str, object]]:
        return self.entries


class FreeCloudCliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
