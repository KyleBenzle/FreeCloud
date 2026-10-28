from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("php"), "PHP is required")
class FreeCloudWebNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "server" / "freecloud.php"
        shutil.copy2(source, self.root / "freecloud.php")
        shutil.copy2(source.with_name("freecloud_lifecycle.php"), self.root / "freecloud_lifecycle.php")
        (self.root / "freecloud_files").mkdir()
        (self.root / "freecloud_meta").mkdir()
        (self.root / "config.json").write_text(
            json.dumps({"name": "Test FreeCloud", "password_hash": ""}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_metadata(self, entries: list[dict[str, object]]) -> None:
        records = {str(entry["path"]): entry for entry in entries}
        (self.root / "freecloud_meta" / "files.json").write_text(
            json.dumps(records), encoding="utf-8"
        )

    def render(self, path: str = "") -> str:
        script = """
$_GET = ['path' => getenv('TEST_PATH')];
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['HTTPS'] = 'on';
include getenv('TEST_WEB_UI');
"""
        env = os.environ.copy()
        env.update(
            {
                "TEST_PATH": path,
                "TEST_WEB_UI": str(self.root / "freecloud.php"),
            }
        )
        result = subprocess.run(
            ["php", "-r", script],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    @staticmethod
    def node_entry(path: str, entry_type: str = "file") -> dict[str, object]:
        return {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "type": entry_type,
            "size": 0 if entry_type == "dir" else 4,
            "mtime": 100,
            "deleted": False,
            "locations": [
                {
                    "kind": "local_node",
                    "device_id": "1" * 32,
                    "name": "Raspberry Pi",
                    "available": True,
                    "updated_at": 100,
                }
            ],
        }

    @staticmethod
    def row_count(page: str, name: str) -> int:
        return len(re.findall(r'class="name"[^>]*>' + re.escape(html.escape(name)) + r"</", page))

    def test_web_server_only_entry(self) -> None:
        (self.root / "freecloud_files" / "web-only.txt").write_text("web", encoding="utf-8")
        page = self.render()
        self.assertEqual(self.row_count(page, "web-only.txt"), 1)
        self.assertIn("Available from Web Server", page)

    def test_node_only_entry(self) -> None:
        self.write_metadata([self.node_entry("node-only.txt")])
        page = self.render()
        self.assertEqual(self.row_count(page, "node-only.txt"), 1)
        self.assertIn("Available from Raspberry Pi", page)

    def test_tombstone_hides_physical_and_catalog_entry(self) -> None:
        (self.root / "freecloud_files" / "deleted.txt").write_text("stale local bytes", encoding="utf-8")
        self.write_metadata([self.node_entry("deleted.txt")])
        (self.root / "freecloud_meta" / "tombstones.json").write_text(json.dumps({
            "tombstone-1": {"tombstone_id": "tombstone-1", "path": "deleted.txt", "recursive": False, "status": "pending"}
        }), encoding="utf-8")
        page = self.render()
        self.assertEqual(self.row_count(page, "deleted.txt"), 0)

    def test_same_path_merges_web_server_and_node_locations(self) -> None:
        (self.root / "freecloud_files" / "shared.txt").write_text("web", encoding="utf-8")
        self.write_metadata([self.node_entry("shared.txt")])
        page = self.render()
        self.assertEqual(self.row_count(page, "shared.txt"), 1)
        self.assertIn("Available from Web Server, Raspberry Pi", page)

    def test_root_is_mixed_provider_union(self) -> None:
        (self.root / "freecloud_files" / "WebFolder").mkdir()
        self.write_metadata([self.node_entry("NodeFolder", "dir")])
        page = self.render()
        self.assertEqual(self.row_count(page, "WebFolder"), 1)
        self.assertEqual(self.row_count(page, "NodeFolder"), 1)

    def test_web_server_folder_navigation(self) -> None:
        folder = self.root / "freecloud_files" / "WebFolder"
        folder.mkdir()
        (folder / "child.txt").write_text("child", encoding="utf-8")
        page = self.render("WebFolder")
        self.assertEqual(self.row_count(page, "child.txt"), 1)

    def test_node_only_folder_navigation(self) -> None:
        self.write_metadata(
            [
                self.node_entry("Audiobooks", "dir"),
                self.node_entry("Audiobooks/Book One", "dir"),
                self.node_entry("Audiobooks/Book Two", "dir"),
            ]
        )
        page = self.render("Audiobooks")
        self.assertEqual(self.row_count(page, "Book One"), 1)
        self.assertEqual(self.row_count(page, "Book Two"), 1)


if __name__ == "__main__":
    unittest.main()
