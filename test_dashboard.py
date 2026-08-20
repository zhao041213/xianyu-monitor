import tempfile
import unittest
from pathlib import Path

from dashboard import MonitorConfig, MonitorController
from xianyu_monitor import SearchItem


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class FakeNotificationManager:
    def __init__(self) -> None:
        self.listings = []

    def public_snapshot(self) -> dict:
        return {
            "wecom": {"enabled": False, "configured": False},
            "dingtalk": {"enabled": False, "configured": False},
        }

    def notify_listing(self, notification) -> None:
        self.listings.append(notification)


class DashboardTests(unittest.TestCase):
    def test_default_config_matches_requested_watch(self) -> None:
        config = MonitorConfig.from_payload({})
        self.assertEqual(config.keyword, "mardi短袖")
        self.assertEqual(config.max_price, 60)
        self.assertEqual(config.interval, 60)

    def test_config_rejects_too_frequent_scanning(self) -> None:
        with self.assertRaises(ValueError):
            MonitorConfig.from_payload({"interval": 10})

    def test_keyword_and_price_limit_are_configurable(self) -> None:
        config = MonitorConfig.from_payload(
            {"keyword": "iPhone 15", "max_price": 3200, "interval": 120}
        )
        self.assertEqual(config.keyword, "iPhone 15")
        self.assertEqual(config.max_price, 3200)
        self.assertEqual(config.interval, 120)

    def test_refresh_interval_accepts_custom_seconds(self) -> None:
        self.assertEqual(
            MonitorConfig.from_payload({"interval": 450}).interval,
            450,
        )

    def test_safety_issue_stops_monitor_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(root / "state.json", root / "profile")
            notifier = FakeNotifier()

            controller._stop_for_safety("检测到访问受限", notifier)

            self.assertTrue(controller._stop_event.is_set())
            self.assertEqual(controller.status, "safety_stopped")
            self.assertEqual(controller.status_text, "已自动停止")
            self.assertEqual(controller.error, "检测到访问受限")
            self.assertEqual(len(notifier.messages), 1)

    def test_first_scan_is_silent_then_only_new_low_price_item_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            notifications = FakeNotificationManager()
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=notifications,
            )
            config = MonitorConfig()
            notifier = FakeNotifier()

            controller._process_scan(
                [SearchItem("baseline", "历史商品", 20, "https://example.com/old")],
                config,
                notifier,
            )
            self.assertEqual(controller.state_store.alert_snapshot(), [])
            self.assertEqual(notifier.messages, [])

            controller._process_scan(
                [
                    SearchItem("baseline", "历史商品", 20, "https://example.com/old"),
                    SearchItem("new-low", "新低价", 59, "https://example.com/new"),
                    SearchItem("new-equal", "刚好六十", 60, "https://example.com/equal"),
                ],
                config,
                notifier,
            )
            alerts = controller.state_store.alert_snapshot(config.keyword)
            self.assertEqual([alert["item_id"] for alert in alerts], ["new-low"])
            self.assertEqual(alerts[0]["keyword"], "mardi短袖")
            self.assertEqual(len(notifier.messages), 1)
            self.assertEqual(len(notifications.listings), 1)
            self.assertEqual(notifications.listings[0].title, "新低价")


if __name__ == "__main__":
    unittest.main()
