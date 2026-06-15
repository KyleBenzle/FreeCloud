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


if __name__ == "__main__":
    unittest.main()
