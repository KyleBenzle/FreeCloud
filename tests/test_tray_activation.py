import unittest

from freecloud_ui import TrayMenuActivationTracker


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


if __name__ == "__main__":
    unittest.main()
