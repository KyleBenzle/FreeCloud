import unittest
import tempfile
import hashlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from freecloud_ui import ACTION_ICON_FILES, APP_ICON_PATH, ASSET_DIR, FILE_TABLE_COLUMNS, GLOBAL_DELETE_OVERLAY_PATH, NAVIGATION_ICON_FILES, FreeCloudUi, TrayMenuActivationTracker


class DesktopIconAssetTests(unittest.TestCase):
    def test_every_mapped_action_icon_exists_in_package_relative_asset_dir(self) -> None:
        self.assertEqual(
            set(ACTION_ICON_FILES),
            {"open_local_folder", "new_folder", "upload", "view_on_web", "refresh", "start_sync", "stop_sync", "settings"},
        )
        self.assertTrue(all((ASSET_DIR / filename).is_file() for filename in ACTION_ICON_FILES.values()))
        self.assertEqual(set(NAVIGATION_ICON_FILES), {"nav_up", "nav_back", "nav_forward"})
        self.assertTrue(all((ASSET_DIR / filename).is_file() for filename in NAVIGATION_ICON_FILES.values()))
        self.assertTrue(APP_ICON_PATH.is_file())


class TrayMenuActivationTrackerTests(unittest.TestCase):
    def test_quick_menu_open_and_close_opens_window(self) -> None:
        tracker = TrayMenuActivationTracker(double_click_seconds=0.6)

        tracker.menu_opened(10.0)

        self.assertTrue(tracker.menu_closed(10.4))

    def test_slow_menu_close_does_not_open_window(self) -> None:
        tracker = TrayMenuActivationTracker(double_click_seconds=0.6)

        tracker.menu_opened(10.0)

        self.assertFalse(tracker.menu_closed(10.8))

    def test_menu_action_does_not_trigger_second_open(self) -> None:
        tracker = TrayMenuActivationTracker(double_click_seconds=0.6)

        tracker.menu_opened(10.0)
        tracker.menu_action_started()

        self.assertFalse(tracker.menu_closed(10.2))


class FontSizeTests(unittest.TestCase):
    def test_positive_font_size_changes_one_point(self) -> None:
        self.assertEqual(FreeCloudUi.adjusted_font_size(10, 1), 11)
        self.assertEqual(FreeCloudUi.adjusted_font_size(10, -1), 9)

    def test_font_size_does_not_drop_below_minimum(self) -> None:
        self.assertEqual(FreeCloudUi.adjusted_font_size(6, -1), 6)

    def test_negative_pixel_font_size_preserves_direction(self) -> None:
        self.assertEqual(FreeCloudUi.adjusted_font_size(-10, 1), -11)
        self.assertEqual(FreeCloudUi.adjusted_font_size(-10, -1), -9)

    def test_saved_font_size_is_restored(self) -> None:
        with patch("freecloud_ui.load_json", return_value={"font_size_delta": "3"}):
            self.assertEqual(FreeCloudUi.load_font_size_delta(), 3)

    def test_invalid_saved_font_size_uses_default(self) -> None:
        with patch("freecloud_ui.load_json", return_value={"font_size_delta": "large"}):
            self.assertEqual(FreeCloudUi.load_font_size_delta(), 0)


class ToolbarSyncButtonTests(unittest.TestCase):
    def test_button_tracks_existing_process_state_and_swaps_action(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        ui.process = None
        ui.background_sync_running = lambda: False
        ui.icons = SimpleNamespace(toolbar_action=lambda name, size: (name, size))
        button = SimpleNamespace()
        button.configure = lambda **options: [setattr(button, key, value) for key, value in options.items()]
        ui.sync_toolbar_button = button
        actions = []
        ui.start_sync = lambda: actions.append("start")
        ui.stop_sync = lambda: actions.append("stop")

        ui.update_toolbar_sync_button()
        self.assertEqual(ui.sync_toolbar_button.text, "Start Sync")
        self.assertEqual(ui.sync_toolbar_button.image, ("start_sync", 35))
        self.assertIs(ui.sync_toolbar_button.command, ui.start_sync)
        ui.sync_toolbar_button.command()
        self.assertEqual(actions, ["start"])

        ui.process = SimpleNamespace(poll=lambda: None)
        ui.update_toolbar_sync_button()
        self.assertEqual(ui.sync_toolbar_button.text, "Stop Sync")
        self.assertEqual(ui.sync_toolbar_button.image, ("stop_sync", 35))
        self.assertIs(ui.sync_toolbar_button.command, ui.stop_sync)
        ui.sync_toolbar_button.command()
        self.assertEqual(actions, ["start", "stop"])


class AutomaticSyncStartupTests(unittest.TestCase):
    def test_saved_setup_starts_sync_when_not_already_running(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        started = []
        ui.config_data = lambda: {"base_url": "https://example.test/FreeCloud"}
        ui.sync_is_running = lambda: False
        ui.start_sync = lambda: started.append(True)

        ui.start_sync_if_needed()

        self.assertEqual(started, [True])

    def test_existing_sync_is_not_duplicated_at_startup(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        started = []
        ui.status = SimpleNamespace(set=lambda value: setattr(ui, "shown_status", value))
        ui.config_data = lambda: {"base_url": "https://example.test/FreeCloud"}
        ui.sync_is_running = lambda: True
        ui.start_sync = lambda: started.append(True)

        ui.start_sync_if_needed()

        self.assertEqual(started, [])
        self.assertEqual(ui.shown_status, "Syncing")


class AutomaticManifestSyncStateTests(unittest.TestCase):
    def test_inventory_change_requires_two_stable_snapshots(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        ui.desktop_manifest_signature = "old"
        ui.local_manifest_candidate_signature = None

        self.assertFalse(ui.local_manifest_snapshot_is_stable("new"))
        self.assertEqual(ui.local_manifest_candidate_signature, "new")
        self.assertTrue(ui.local_manifest_snapshot_is_stable("new"))
        ui.desktop_manifest_signature = "new"
        self.assertFalse(ui.local_manifest_snapshot_is_stable("new"))
        self.assertIsNone(ui.local_manifest_candidate_signature)

    def test_sync_status_does_not_claim_synced_for_missing_or_failed_copies(self) -> None:
        missing = {
            "type": "file", "desired_locations": [{"device_id": "phone"}],
            "locations": [], "stale_locations": [],
        }
        self.assertEqual(FreeCloudUi.folder_sync_state([missing], True, {}), "Syncing…")
        present = {**missing, "locations": [{"device_id": "phone"}]}
        self.assertEqual(FreeCloudUi.folder_sync_state([present], True, {}), "Synced")
        failed = {**present, "reconciliation_failed_locations": ["phone"]}
        self.assertEqual(FreeCloudUi.folder_sync_state([failed], True, {}), "Sync error")
        self.assertEqual(FreeCloudUi.folder_sync_state([present], False, {}), "Sync error")
        empty_folder_pending = {
            "type": "dir", "desired_locations": [{"device_id": "phone"}],
            "locations": [{"device_id": "desktop"}],
        }
        self.assertEqual(FreeCloudUi.folder_sync_state([empty_folder_pending], True, {}), "Syncing…")

    def test_desired_toggle_uses_server_entry_not_actual_location(self) -> None:
        entry = {
            "path": "copied/file.txt", "type": "file",
            "locations": [{"device_id": "desktop"}],
            "desired_locations": [{"device_id": "phone"}],
        }
        phone = {"kind": "android", "device_id": "phone"}
        self.assertTrue(FreeCloudUi.entry_desired_on_provider(entry, phone))
        ui = FreeCloudUi.__new__(FreeCloudUi)
        ui.desired_placement_overrides = {}
        ui.placement_status_overrides = {}
        self.assertEqual(ui.placement_status(entry, phone), "Pending...")

    def test_local_overlay_defaults_new_normal_content_to_all_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "new-folder").mkdir()
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.config_data = lambda: {"local_root": str(root)}
            ui.current_folder_placement = None
            ui.remote_manifest_cache = []
            ui.device_columns = [
                {"kind": "desktop", "device_id": "desktop-1", "label": "This Computer"},
                {"kind": "android", "device_id": "phone-1", "label": "Phone"},
                {"kind": "web_server", "device_id": "", "label": "Web Server"},
            ]
            ui.path_is_optimistically_deleted = lambda _path: False
            with patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}):
                rows = ui.local_entries_for_folder("")
            self.assertEqual(
                {item["device_id"] for item in rows[0]["desired_locations"]},
                {"desktop-1", "phone-1", "web_server"},
            )

    def test_local_overlay_inherits_known_parent_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Audiobooks").mkdir()
            (root / "Audiobooks" / "book.mp3").write_bytes(b"disposable")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.config_data = lambda: {"local_root": str(root)}
            ui.current_folder_placement = {
                "path": "Audiobooks", "type": "dir",
                "desired_locations": [{"device_id": "pi-1", "name": "Raspberry Pi"}],
            }
            ui.remote_manifest_cache = []
            ui.device_columns = [
                {"kind": "desktop", "device_id": "desktop-1", "label": "This Computer"},
                {"kind": "android", "device_id": "phone-1", "label": "Phone"},
                {"kind": "local_node", "device_id": "pi-1", "label": "Raspberry Pi"},
            ]
            ui.path_is_optimistically_deleted = lambda _path: False
            with patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}):
                rows = ui.local_entries_for_folder("Audiobooks")
            self.assertEqual(
                {item["device_id"] for item in rows[0]["desired_locations"]},
                {"pi-1"},
            )

    def test_background_inventory_scan_publishes_stable_disposable_changes(self) -> None:
        class FakeRoot:
            def __init__(self):
                self.callbacks = []
                self.next_id = 0

            def after(self, delay, callback):
                self.next_id += 1
                callback_id = str(self.next_id)
                self.callbacks.append((callback_id, delay, callback))
                return callback_id

            def after_cancel(self, _callback_id):
                pass

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        class FakeClient:
            pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "FreeCloud"
            nested = root / "copied" / "nested"
            nested.mkdir(parents=True)
            (nested / "tiny.txt").write_text("disposable", encoding="utf-8")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.root = FakeRoot()
            ui.reconciliation_stop = threading.Event()
            ui.reconciliation_lock = threading.Lock()
            ui.local_manifest_scan_after_id = None
            ui.local_manifest_scan_in_flight = False
            ui.desktop_manifest_signature = None
            ui.local_manifest_candidate_signature = None
            ui.desktop_manifest_last_published_at = 0.0
            ui.desktop_manifest_last_scan_at = 0.0
            ui.config_data = lambda: {"local_root": str(root)}
            ui.current_client = lambda: FakeClient()
            ui.refresh_current_folder_in_background = lambda **_kwargs: None
            ui.remote_manifest_refreshing = False
            ui.last_rendered_folder_key = ""
            ui.append = lambda _text: None
            ui.status = SimpleNamespace(set=lambda _value: None)
            ui.header_status_label = SimpleNamespace(configure=lambda **_kwargs: None)

            def run_next():
                _callback_id, _delay, callback = ui.root.callbacks.pop(0)
                callback()

            with patch("freecloud_ui.threading.Thread", ImmediateThread), \
                 patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}), \
                 patch("freecloud_ui.cli.publish_desktop_manifest_entries", return_value=({"device_id": "desktop-1"}, 3, "snapshot")) as publish, \
                 patch("freecloud_ui.cli.process_desktop_reconciliation", return_value={
                     "installed": 0, "uploaded": 0, "conflicts": 0, "failed": 0, "jobs": 0,
                 }) as reconcile:
                ui.run_local_manifest_scan()
                run_next()  # debounce first snapshot
                run_next()  # stable rescan publishes
                run_next()  # finish publication on the UI thread

            self.assertEqual(publish.call_count, 1)
            published_entries = publish.call_args.args[1]
            self.assertIn("copied/nested/tiny.txt", {entry["path"] for entry in published_entries})
            reconcile.assert_called_once()

    def test_failed_reconciliation_planning_retries_without_republishing_manifest(self) -> None:
        class FakeRoot:
            def __init__(self):
                self.callbacks = []
                self.next_id = 0

            def after(self, delay, callback):
                self.next_id += 1
                callback_id = str(self.next_id)
                self.callbacks.append((callback_id, delay, callback))
                return callback_id

            def after_cancel(self, _callback_id):
                pass

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        import freecloud_cli as cli

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "FreeCloud"
            root.mkdir()
            (root / "new.txt").write_text("disposable", encoding="utf-8")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.root = FakeRoot()
            ui.reconciliation_stop = threading.Event()
            ui.reconciliation_lock = threading.Lock()
            ui.local_manifest_scan_after_id = None
            ui.local_manifest_scan_in_flight = False
            ui.desktop_manifest_signature = None
            ui.local_manifest_candidate_signature = None
            ui.desktop_reconciliation_plan_pending = False
            ui.desktop_manifest_last_published_at = 0.0
            ui.desktop_manifest_last_scan_at = 0.0
            ui.config_data = lambda: {"local_root": str(root)}
            ui.current_client = lambda: object()
            ui.refresh_current_folder_in_background = lambda **_kwargs: None
            ui.remote_manifest_refreshing = False
            ui.last_rendered_folder_key = ""
            ui.append = lambda _text: None
            ui.status = SimpleNamespace(set=lambda _value: None)
            ui.header_status_label = SimpleNamespace(configure=lambda **_kwargs: None)

            def run_next():
                _callback_id, _delay, callback = ui.root.callbacks.pop(0)
                callback()

            def publish(_client, entries):
                return ({"device_id": "desktop-1"}, len(entries), cli.desktop_manifest_signature(entries))

            successful_plan = {"installed": 0, "uploaded": 0, "conflicts": 0, "failed": 0, "jobs": 0}
            with patch("freecloud_ui.threading.Thread", ImmediateThread), \
                 patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}), \
                 patch("freecloud_ui.cli.publish_desktop_manifest_entries", side_effect=publish) as publish_manifest, \
                 patch("freecloud_ui.cli.process_desktop_reconciliation", side_effect=[RuntimeError("temporary failure"), successful_plan]) as reconcile:
                ui.run_local_manifest_scan()
                run_next()  # first inventory snapshot: wait for stability
                run_next()  # publish once; planning fails
                self.assertTrue(ui.desktop_reconciliation_plan_pending)
                run_next()  # error callback schedules the next scan
                run_next()  # unchanged inventory: retry planning only
                run_next()  # finish retry

            self.assertEqual(publish_manifest.call_count, 1)
            self.assertEqual(reconcile.call_count, 2)
            self.assertFalse(ui.desktop_reconciliation_plan_pending)

class StartupCacheTests(unittest.TestCase):
    def test_populated_cache_renders_and_schedules_live_refresh_without_server_access(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        entry = {"path": "cached.txt", "name": "cached.txt", "type": "file"}
        callbacks = []
        callback_ready = threading.Event()
        scheduled_refreshes = []
        ui.root = SimpleNamespace(
            after=lambda delay, callback: (callbacks.append(callback), callback_ready.set(), "after-id")[2]
        )
        ui.config_data = lambda: {"base_url": "https://example.test/FreeCloud"}
        ui.cached_remote_tree_entries = lambda _url: [entry]
        ui.cached_devices = lambda _url: []
        ui.current_client = lambda: self.fail("cache rendering must not wait for the server")
        ui.current_remote_path = ""
        ui.remote_manifest_refreshing = True
        ui.device_registry = []
        ui.path_is_optimistically_deleted = lambda _path: False
        ui.configure_device_columns = lambda: None
        ui.status = SimpleNamespace(set=lambda value: setattr(ui, "shown_status", value))
        ui.header_status_label = SimpleNamespace(configure=lambda **_kwargs: None)
        ui.update_storage_display = lambda _storage: None
        ui.render_cached_files_on_startup = lambda: setattr(ui, "cache_rendered", True)
        ui.schedule_folder_refresh = lambda delay: scheduled_refreshes.append(delay)
        ui.schedule_storage_refresh = lambda: None
        ui.render_devices_sidebar = lambda: None
        ui.append = lambda _text: None
        ui.files_empty_var = SimpleNamespace(set=lambda value: setattr(ui, "empty_message", value))

        with patch("freecloud_ui.startup_log"):
            ui.load_cached_files_after_startup()
            self.assertTrue(callback_ready.wait(timeout=2))
            callbacks[0]()
            self.assertEqual(ui.shown_status, "Cached")
            self.assertEqual(ui.remote_manifest_cache, [entry])
            ui.finish_folder_refresh_error(TimeoutError("server unavailable"), False, "")

        self.assertEqual(ui.remote_manifest_cache, [entry])
        self.assertTrue(ui.cache_rendered)
        self.assertEqual(scheduled_refreshes, [5000])
        self.assertEqual(ui.shown_status, "Offline")
        self.assertEqual(ui.remote_manifest_cache, [entry])


class GlobalDeleteUiTests(unittest.TestCase):
    def test_global_delete_hides_and_persists_subtree_before_server_reply(self) -> None:
        ui = FreeCloudUi.__new__(FreeCloudUi)
        ui.optimistic_global_deletions = {}
        ui.remote_manifest_cache = [
            {"path": "folder", "type": "dir"},
            {"path": "folder/child.txt", "type": "file"},
            {"path": "keep.txt", "type": "file"},
        ]
        ui.selected_remote_entry = {"path": "folder"}
        with patch("freecloud_ui.cli.save_json") as save_json:
            ui.add_optimistic_global_deletion("folder", True)

        self.assertEqual([entry["path"] for entry in ui.remote_manifest_cache], ["keep.txt"])
        self.assertTrue(ui.path_is_optimistically_deleted("folder/child.txt"))
        self.assertIsNone(ui.selected_remote_entry)
        save_json.assert_called_once_with(GLOBAL_DELETE_OVERLAY_PATH, {"paths": {"folder": True}})

    def test_nonrecursive_optimistic_delete_hides_only_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Folder").mkdir()
            (root / "Folder" / "file.txt").write_text("disposable", encoding="utf-8")
            (root / "Folder" / "keep.txt").write_text("keep", encoding="utf-8")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.optimistic_global_deletions = {"Folder/file.txt": False}
            ui.remote_manifest_cache = []
            ui.config_data = lambda: {"local_root": str(root)}

            self.assertEqual([entry["name"] for entry in ui.local_entries_for_folder("Folder")], ["keep.txt"])

    def test_recursive_optimistic_delete_hides_cached_and_local_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Folder" / "nested").mkdir(parents=True)
            (root / "Folder" / "nested" / "local.txt").write_text("disposable", encoding="utf-8")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.optimistic_global_deletions = {"Folder": True}
            ui.remote_manifest_cache = [
                {"path": "Folder", "name": "Folder", "type": "dir"},
                {"path": "Folder/nested", "name": "nested", "type": "dir"},
                {"path": "Folder/nested/cloud.txt", "name": "cloud.txt", "type": "file"},
                {"path": "Sibling.txt", "name": "Sibling.txt", "type": "file"},
            ]
            ui.config_data = lambda: {"local_root": str(root)}

            self.assertEqual([entry["path"] for entry in ui.entries_for_folder("")], ["Sibling.txt"])
            self.assertEqual(ui.local_entries_for_folder(""), [])
            self.assertEqual(ui.local_entries_for_folder("Folder/nested"), [])

    def test_exact_file_delete_does_not_hide_sibling(self) -> None:
        self.assertTrue(FreeCloudUi.global_delete_covers("Folder/file.txt", {"Folder/file.txt": False}))
        self.assertFalse(FreeCloudUi.global_delete_covers("Folder/other.txt", {"Folder/file.txt": False}))
        self.assertTrue(FreeCloudUi.global_delete_covers("Folder/child/file.txt", {"Folder": True}))

    def test_pending_delete_overlay_persists_until_absence_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "pending.txt"
            target.write_text("disposable", encoding="utf-8")
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.optimistic_global_deletions = {"pending.txt": False}
            ui.config_data = lambda: {"local_root": str(root)}
            with patch("freecloud_ui.cli.save_json") as save_json:
                ui.confirm_global_deletions_from_refresh("", [])
                self.assertIn("pending.txt", ui.optimistic_global_deletions)
                target.unlink()
                ui.confirm_global_deletions_from_refresh("", [])
            self.assertNotIn("pending.txt", ui.optimistic_global_deletions)
            self.assertEqual(save_json.call_count, 1)


class DeviceManagerTests(unittest.TestCase):
    def test_current_desktop_and_web_server_are_protected(self) -> None:
        desktop_id = "desktop-id"
        self.assertFalse(FreeCloudUi.device_can_be_removed(desktop_id, desktop_id))
        self.assertFalse(FreeCloudUi.device_can_be_removed("web_server", desktop_id))
        self.assertTrue(FreeCloudUi.device_can_be_removed("offline-phone", desktop_id))


class AvailableFromLabelTests(unittest.TestCase):
    def test_missing_locations_defaults_to_web_server(self) -> None:
        self.assertEqual(FreeCloudUi.available_from_label({}), "Web Server")

    def test_location_label_prefers_device_name(self) -> None:
        self.assertEqual(
            FreeCloudUi.available_from_label(
                {
                    "locations": [
                        {"kind": "local_node", "device_id": "pi-1", "name": "Raspberry Pi", "available": True}
                    ]
                }
            ),
            "Raspberry Pi",
        )

    def test_unavailable_locations_are_marked_unavailable(self) -> None:
        self.assertEqual(
            FreeCloudUi.available_from_label(
                {"locations": [{"kind": "local_node", "device_id": "pi-1", "available": False}]}
            ),
            "Unavailable",
        )

    def test_missing_locations_are_treated_as_web_server_files(self) -> None:
        self.assertTrue(FreeCloudUi.entry_has_web_server_location({}))

    def test_pi_only_location_is_not_treated_as_web_server_file(self) -> None:
        self.assertFalse(
            FreeCloudUi.entry_has_web_server_location(
                {"locations": [{"kind": "local_node", "device_id": "pi-1", "name": "Raspberry Pi"}]}
            )
        )

    def test_combined_folder_entries_include_local_and_remote_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            local_root = Path(temp)
            (local_root / "Local Folder").mkdir()
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.remote_manifest_cache = [
                {"path": "Audiobooks", "name": "Audiobooks", "type": "dir", "locations": []}
            ]
            ui.config_data = lambda: {"local_root": str(local_root)}

            entries = ui.combined_entries_for_folder("")

            self.assertEqual([entry["path"] for entry in entries], ["Audiobooks", "Local Folder"])
            local_entry = next(entry for entry in entries if entry["path"] == "Local Folder")
            self.assertEqual(local_entry["locations"][0]["kind"], "desktop")


class DesiredPlacementUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ui = FreeCloudUi.__new__(FreeCloudUi)
        self.ui.config_data = lambda: {"local_root": "/path/that/does/not/exist"}
        self.desktop = {"kind": "local", "device_id": "desktop-1", "label": "This Computer"}
        self.phone = {"kind": "device", "device_id": "phone-1", "label": "Phone"}
        self.web = {"kind": "web_server", "device_id": "", "label": "Web Server"}

    def test_settings_fall_back_to_current_folder_and_selection_takes_precedence(self) -> None:
        self.ui.current_remote_path = "Idiot_City/Abuse_of_Process"
        self.ui.remote_manifest_cache = [{
            "path": "Idiot_City/Abuse_of_Process", "name": "Abuse_of_Process", "type": "dir",
            "desired_locations": [],
        }]
        self.ui.current_folder_placement = {
            "path": "Idiot_City/Abuse_of_Process", "name": "Abuse_of_Process", "type": "dir",
            "desired_locations": [{"device_id": "phone-1"}],
        }
        self.ui.selected_remote_entry = None
        folder = self.ui.settings_entry_for_display()
        self.assertEqual(folder["name"], "Abuse_of_Process")
        self.assertEqual(folder["path"], self.ui.current_remote_path)
        self.assertEqual(folder["type"], "dir")
        self.assertTrue(self.ui.entry_desired_on_provider(folder, self.phone))
        self.assertEqual(
            FreeCloudUi.desired_toggle_request(folder, self.phone),
            ("Idiot_City/Abuse_of_Process", "phone-1", False, True),
        )
        selected_child = {"path": "Idiot_City/Abuse_of_Process/report.pdf", "type": "file"}
        self.ui.selected_remote_entry = selected_child
        self.assertIs(self.ui.settings_entry_for_display(), selected_child)
        self.ui.selected_remote_entry = None
        self.ui.current_remote_path = "Demeter"
        self.ui.current_folder_placement = {"path": "Demeter", "name": "Demeter", "type": "dir"}
        self.assertIs(self.ui.settings_entry_for_display(), self.ui.current_folder_placement)

    def test_all_four_visual_states(self) -> None:
        actual = {"locations": [{"device_id": "phone-1"}]}
        desired = {"desired_locations": [{"device_id": "phone-1"}]}
        both = {**actual, **desired}
        self.assertEqual(self.ui.placement_cell(both, self.phone), "☑")
        self.assertEqual(self.ui.placement_cell(desired, self.phone), "☑…")
        self.assertEqual(self.ui.placement_cell(actual, self.phone), "●")
        self.assertEqual(self.ui.placement_cell({}, self.phone), "□")
        self.assertEqual(self.ui.placement_status(both, self.phone), "Synced")
        self.assertEqual(self.ui.placement_status(desired, self.phone), "Pending...")
        self.assertEqual(self.ui.placement_status(actual, self.phone), "Removal blocked")
        self.assertEqual(self.ui.placement_status({}, self.phone), "Not stored")
        removing = {**actual, "removing_locations": ["phone-1"]}
        self.assertEqual(self.ui.placement_status(removing, self.phone), "Removing...")
        verifying = {**actual, "removal_verifying_locations": ["phone-1"]}
        self.assertEqual(self.ui.placement_status(verifying, self.phone), "Verifying...")
        waiting = {**actual, "removal_waiting_locations": ["phone-1"]}
        self.assertEqual(self.ui.placement_status(waiting, self.phone), "Waiting for device...")
        blocked = {
            **actual,
            "removal_blocked_locations": ["phone-1"],
            "removal_blocked_errors": {"phone-1": "Cannot remove: no other verified copy could be proven by SHA-256."},
        }
        self.assertEqual(self.ui.placement_status(blocked, self.phone), "Removal blocked — last verified copy")

    def test_center_table_has_no_device_columns(self) -> None:
        self.assertEqual(FILE_TABLE_COLUMNS, ("name", "size", "type", "modified"))

    def test_desired_actual_but_stale_is_pending(self) -> None:
        entry = {
            "locations": [{"kind": "web_server", "device_id": "web_server"}],
            "desired_locations": [{"device_id": "web_server"}],
            "stale_locations": ["web_server"],
        }
        self.assertEqual(self.ui.placement_cell(entry, self.web), "☑…")

    def test_optimistic_desired_state_survives_stale_metadata_until_confirmed(self) -> None:
        self.ui.desired_placement_overrides = {("report.pdf", "desktop-1"): False}
        stale = {
            "path": "report.pdf",
            "locations": [{"device_id": "desktop-1"}],
            "desired_locations": [{"device_id": "desktop-1"}],
        }
        self.assertFalse(self.ui.effective_desired_on_provider(stale, self.desktop))
        self.assertEqual(self.ui.placement_status(stale, self.desktop), "Not stored")

        confirmed = {**stale, "desired_locations": []}
        self.assertFalse(self.ui.effective_desired_on_provider(confirmed, self.desktop))
        self.assertNotIn(("report.pdf", "desktop-1"), self.ui.desired_placement_overrides)

    def test_recurring_worker_finds_delayed_remove_and_publishes_only_after_change(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.callbacks = []
                self.next_id = 0

            def after(self, delay, callback):
                self.next_id += 1
                callback_id = str(self.next_id)
                self.callbacks.append((callback_id, delay, callback))
                return callback_id

            def after_cancel(self, callback_id):
                self.callbacks = [item for item in self.callbacks if item[0] != callback_id]

        class FakeClient:
            def __init__(self, target, content):
                self.target = target
                self.content = content
                self.polls = 0
                self.hash_reported = False
                self.acks = []
                self.metadata_reads = 0
                self.mtime = int(target.stat().st_mtime)
                self.digest = hashlib.sha256(content).hexdigest()
                self.restore_ready = False
                self.restore_returned = False

            def poll_reconciliation(self, _device_id, check_only=False):
                self.assert_check_only = check_only
                self.polls += 1
                facts = {"type": "file", "size": len(self.content), "mtime": self.mtime, "hash": self.digest}
                if self.restore_ready and not self.restore_returned:
                    self.restore_returned = True
                    return [{"job_id": "restore-job", "status": "ready", "path": "Disposable/remove.txt",
                             "expected_size": len(self.content), "expected_mtime": self.mtime, "hash": self.digest,
                             "destination_facts": None}]
                if self.polls == 1:
                    return [{"job_id": "hash-job", "status": "queued_hash", "path": "Disposable/remove.txt",
                             "expected_size": len(self.content), "expected_mtime": self.mtime}]
                if self.polls == 2 and self.hash_reported:
                    return [{"job_id": "remove-job", "status": "queued_remove", "path": "Disposable/remove.txt",
                             "removal_facts": {"Disposable/remove.txt": facts}}]
                return []

            def report_location_hash(self, _device_id, _job_id, digest, error=""):
                self.hash_reported = not error and digest == self.digest

            def acknowledge_reconciliation(self, _device_id, job_id, status, _error=""):
                self.acks.append((job_id, status))

            def metadata_list(self, _path):
                self.metadata_reads += 1
                if self.target.exists():
                    return [{"path": "Disposable/remove.txt", "type": "file",
                             "locations": [{"device_id": "desktop-1"}],
                             "desired_locations": [{"device_id": "desktop-1"}]}]
                return []

            def download_reconciliation(self, _device_id, _job_id, destination):
                destination.write_bytes(self.content)
                return len(self.content), self.digest

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with tempfile.TemporaryDirectory() as temp:
            root_path = Path(temp) / "desktop"
            other_path = Path(temp) / "other"
            target = root_path / "Disposable" / "remove.txt"
            target.parent.mkdir(parents=True)
            other_path.mkdir()
            content = b"disposable removal worker test"
            target.write_bytes(content)
            (other_path / "remove.txt").write_bytes(content)
            os.utime(target, (1_700_000_000, 1_700_000_000))
            os.utime(other_path / "remove.txt", (1_700_000_000, 1_700_000_000))
            client = FakeClient(target, content)
            ui = FreeCloudUi.__new__(FreeCloudUi)
            ui.root = FakeRoot()
            ui.reconciliation_stop = threading.Event()
            ui.reconciliation_poll_after_id = None
            ui.reconciliation_poll_in_flight = False
            ui.reconciliation_lock = threading.Lock()
            ui.desktop_manifest_dirty = False
            ui.desktop_manifest_last_published_at = 0
            ui.optimistic_global_deletions = {}
            ui.current_remote_path = ""
            ui.selected_remote_entry = None
            ui.current_folder_placement = None
            ui.config_data = lambda: {"local_root": str(root_path)}
            ui.current_client = lambda: client
            ui.replace_cached_folder_entries = lambda *_args: None
            ui.render_current_folder_from_cache = lambda: None
            ui.render_device_settings = lambda: None
            ui.save_remote_tree_cache = lambda: None
            ui.append = lambda *_args: None

            def fire_cycle():
                _callback_id, _delay, timer = ui.root.callbacks.pop(0)
                timer()
                _finish_id, delay, finish = ui.root.callbacks.pop(0)
                self.assertEqual(delay, 0)
                finish()

            with patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}), \
                 patch("freecloud_ui.cli.publish_desktop_manifest") as publish, \
                 patch("freecloud_ui.threading.Thread", ImmediateThread):
                ui.schedule_reconciliation_poll(0)
                fire_cycle()  # Reports the hash; the server creates removal work afterward.
                self.assertTrue(client.assert_check_only)
                self.assertTrue(target.exists())
                self.assertEqual(publish.call_count, 0)
                fire_cycle()  # The timer discovers and processes the delayed removal job.
                self.assertFalse(target.exists())
                self.assertEqual(client.acks, [("remove-job", "removed")])
                self.assertEqual(publish.call_count, 1)
                self.assertEqual(
                    ui.placement_status(
                        {"path": "Disposable/remove.txt", "type": "file", "locations": [], "desired_locations": []},
                        {"kind": "local", "device_id": "desktop-1"},
                    ),
                    "Not stored",
                )
                fire_cycle()  # Idle poll: no new manifest or metadata listing.
                self.assertEqual(publish.call_count, 1)
                self.assertEqual(client.metadata_reads, 2)
                client.restore_ready = True
                fire_cycle()  # The next recurring poll restores an ON destination.
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(client.acks[-1], ("restore-job", "installed"))
                self.assertEqual(publish.call_count, 2)
                self.assertEqual(
                    ui.placement_status(
                        {"path": "Disposable/remove.txt", "type": "file",
                         "locations": [{"device_id": "desktop-1"}],
                         "desired_locations": [{"device_id": "desktop-1"}]},
                        {"kind": "local", "device_id": "desktop-1"},
                    ),
                    "Synced",
                )
                ui.stop_reconciliation_worker()

    def test_reconciliation_poll_does_not_overlap_an_active_cycle(self) -> None:
        class FakeRoot:
            def __init__(self):
                self.callbacks = []

            def after(self, delay, callback):
                self.callbacks.append((delay, callback))
                return "scheduled"

            def after_cancel(self, _callback_id):
                self.callbacks.clear()

        ui = FreeCloudUi.__new__(FreeCloudUi)
        ui.root = FakeRoot()
        ui.reconciliation_stop = threading.Event()
        ui.reconciliation_poll_after_id = None
        ui.reconciliation_poll_in_flight = False
        ui.reconciliation_lock = threading.Lock()
        ui.reconciliation_lock.acquire()
        ui.config_data = lambda: {"local_root": "/tmp"}
        ui.current_client = lambda: self.fail("overlapping poll should not construct a client")
        try:
            with patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}):
                ui.run_reconciliation_poll()
            self.assertEqual(len(ui.root.callbacks), 1)
            self.assertEqual(ui.root.callbacks[0][0], FreeCloudUi.RECONCILIATION_RETRY_INTERVAL_MS)
            self.assertTrue(ui.reconciliation_poll_in_flight is False)
        finally:
            ui.reconciliation_lock.release()
            ui.stop_reconciliation_worker()

    def test_file_toggle_and_second_toggle(self) -> None:
        entry = {"path": "report.pdf", "type": "file", "desired_locations": []}
        self.assertEqual(
            FreeCloudUi.desired_toggle_request(entry, self.phone),
            ("report.pdf", "phone-1", True, False),
        )
        entry["desired_locations"] = [{"device_id": "phone-1"}]
        self.assertEqual(
            FreeCloudUi.desired_toggle_request(entry, self.phone),
            ("report.pdf", "phone-1", False, False),
        )

    def test_folder_toggle_is_recursive(self) -> None:
        entry = {"path": "Documents", "type": "dir", "desired_locations": []}
        self.assertEqual(
            FreeCloudUi.desired_toggle_request(entry, self.phone),
            ("Documents", "phone-1", True, True),
        )

    def test_desktop_web_and_android_provider_ids(self) -> None:
        entry = {"path": "report.pdf", "type": "file", "desired_locations": []}
        self.assertEqual(FreeCloudUi.desired_toggle_request(entry, self.desktop)[1], "desktop-1")
        self.assertEqual(FreeCloudUi.desired_toggle_request(entry, self.web)[1], "web_server")
        self.assertEqual(FreeCloudUi.desired_toggle_request(entry, self.phone)[1], "phone-1")

    def test_registered_desktop_maps_to_single_this_computer_provider(self) -> None:
        self.ui.device_registry = [
            {"device_id": "desktop-1", "name": "Laptop"},
            {"device_id": "phone-1", "name": "Phone"},
        ]
        with patch("freecloud_ui.cli.load_device_config", return_value={"device_id": "desktop-1"}):
            providers = self.ui.placement_providers()
        self.assertEqual([item["device_id"] for item in providers], ["desktop-1", "phone-1", ""])
        self.assertEqual(providers[0]["label"], "This Computer")

    def test_render_helpers_do_not_mutate_desired_state(self) -> None:
        desired = [{"device_id": "phone-1", "rule_path": "Documents"}]
        entry = {"path": "Documents/report.pdf", "type": "file", "desired_locations": desired.copy()}
        self.ui.placement_cell(entry, self.phone)
        FreeCloudUi.desired_toggle_request(entry, self.web)
        self.assertEqual(entry["desired_locations"], desired)

    def test_successful_empty_folder_refresh_prunes_cached_descendants(self) -> None:
        self.ui.remote_manifest_cache = [
            {"path": "Bankruptcy/Forms/OldFolder", "type": "dir"},
            {"path": "Bankruptcy/Forms/OldFolder/old.pdf", "type": "file"},
            {"path": "Bankruptcy/Other/keep.pdf", "type": "file"},
        ]
        self.ui.replace_cached_folder_entries("Bankruptcy/Forms", [])
        self.assertEqual(
            [entry["path"] for entry in self.ui.remote_manifest_cache],
            ["Bankruptcy/Other/keep.pdf"],
        )

    def test_live_folder_refresh_replaces_stale_device_placement(self) -> None:
        stale = {
            "path": "Bankruptcy", "type": "dir",
            "locations": [{"device_id": "desktop-1"}],
            "desired_locations": [{"device_id": "desktop-1"}],
        }
        current = {
            "path": "Bankruptcy", "type": "dir",
            "locations": [{"device_id": "web_server"}],
            "desired_locations": [],
        }
        self.ui.remote_manifest_cache = [stale]
        self.ui.replace_cached_folder_entries("", [current])
        refreshed = self.ui.remote_manifest_cache[0]
        self.assertEqual(self.ui.placement_cell(refreshed, self.desktop), "□")
        self.assertEqual(self.ui.placement_status(refreshed, self.desktop), "Not stored")


if __name__ == "__main__":
    unittest.main()
