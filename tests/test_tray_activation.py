import unittest
from unittest.mock import patch

from freecloud_ui import FreeCloudUi, TrayMenuActivationTracker


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


if __name__ == "__main__":
    unittest.main()
