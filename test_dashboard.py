import tempfile
import unittest
from pathlib import Path

from dashboard import (
    DASHBOARD_BUILD,
    MonitorConfig,
    MonitorController,
    is_browser_closed_error,
    monitor_error_message,
    redact_diagnostic,
)
from xianyu_monitor import SearchItem


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.enabled_changes: list[bool] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled_changes.append(enabled)


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

    def test_snapshot_exposes_live_controls_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            snapshot = controller.snapshot()

            self.assertEqual(snapshot["build"], DASHBOARD_BUILD)
            self.assertTrue(snapshot["capabilities"]["live_interval"])
            self.assertTrue(snapshot["capabilities"]["live_popup"])
            self.assertTrue(snapshot["capabilities"]["file_logging"])
            self.assertTrue(snapshot["diagnostics"]["log_file"].endswith("dashboard_debug.log"))

    def test_browser_profile_conflict_has_readable_error(self) -> None:
        error = RuntimeError(
            "BrowserType.launch_persistent_context: "
            "Target page, context or browser has been closed"
        )

        message = monitor_error_message(error)

        self.assertIn("监控专用浏览器可能已在其他窗口运行", message)

    def test_closed_monitor_browser_is_fatal_during_scan(self) -> None:
        error = RuntimeError("Page.goto: Target page, context or browser has been closed")

        self.assertTrue(is_browser_closed_error(error))
        self.assertEqual(
            monitor_error_message(error, during_scan=True),
            "监控浏览器已关闭，请在面板中重新启动监控",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            controller._handle_scan_error(error)

            self.assertTrue(controller._stop_event.is_set())
            self.assertEqual(controller.status, "error")
            self.assertEqual(controller.status_text, "监控浏览器已关闭")

    def test_diagnostic_redacts_webhook_credentials(self) -> None:
        message = redact_diagnostic(
            "https://example.test/send?key=secret-key&access_token=secret-token"
        )

        self.assertNotIn("secret-key", message)
        self.assertNotIn("secret-token", message)
        self.assertIn("key=[redacted]", message)
        self.assertIn("access_token=[redacted]", message)

    def test_refresh_interval_can_be_updated_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller.running = True

            controller.set_interval({"interval": 137})

            self.assertEqual(controller.config.interval, 137)
            self.assertTrue(controller._interval_changed_event.is_set())
            self.assertIsNotNone(controller.next_scan_at)

    def test_popup_setting_updates_config_and_active_notifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            notifier = FakeNotifier()
            controller._popup_notifier = notifier

            controller.set_popup_enabled({"enabled": False})

            self.assertFalse(controller.config.popup_enabled)
            self.assertFalse(controller.snapshot()["config"]["popup_enabled"])
            self.assertEqual(notifier.enabled_changes, [False])

    def test_popup_setting_rejects_non_boolean_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            with self.assertRaisesRegex(ValueError, "系统文字弹窗状态无效"):
                controller.set_popup_enabled({"enabled": "false"})
            with self.assertRaisesRegex(ValueError, "系统文字弹窗状态无效"):
                MonitorConfig.from_payload({"popup_enabled": "false"})

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
