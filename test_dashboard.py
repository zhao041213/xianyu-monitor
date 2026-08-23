import tempfile
import unittest
from pathlib import Path

from dashboard import (
    DASHBOARD_BUILD,
    ERROR_COOLDOWN_SECONDS,
    INTERVAL_CYCLE_SECONDS,
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
        self.safety_events: list[tuple[str, str]] = []

    def public_snapshot(self) -> dict:
        return {
            "wecom": {"enabled": False, "configured": False},
            "dingtalk": {"enabled": False, "configured": False},
        }

    def notify_listing(self, notification) -> None:
        self.listings.append(notification)

    def notify_safety(self, reason: str, keyword: str) -> None:
        self.safety_events.append((reason, keyword))


class DashboardTests(unittest.TestCase):
    def test_default_config_matches_requested_watch(self) -> None:
        config = MonitorConfig.from_payload({})
        self.assertEqual(config.keyword, "mardi短袖")
        self.assertEqual(config.max_price, 60)
        self.assertEqual(config.interval, 60)
        self.assertTrue(config.interval_cycle_enabled)
        self.assertEqual(config.access_mode, "login")

    def test_config_rejects_too_frequent_scanning(self) -> None:
        with self.assertRaises(ValueError):
            MonitorConfig.from_payload({"interval": 10})

    def test_keyword_and_price_limit_are_configurable(self) -> None:
        config = MonitorConfig.from_payload(
            {
                "keyword": "iPhone 15",
                "max_price": 3200,
                "interval": 120,
                "interval_cycle_enabled": False,
                "access_mode": "guest",
            }
        )
        self.assertEqual(config.keyword, "iPhone 15")
        self.assertEqual(config.max_price, 3200)
        self.assertEqual(config.interval, 120)
        self.assertFalse(config.interval_cycle_enabled)
        self.assertEqual(config.access_mode, "guest")

    def test_config_rejects_unknown_access_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "访问方式"):
            MonitorConfig.from_payload({"access_mode": "stealth"})

    def test_refresh_interval_accepts_custom_seconds(self) -> None:
        config = MonitorConfig.from_payload(
            {"interval": 450, "interval_cycle_enabled": False}
        )
        self.assertEqual(config.interval, 450)
        self.assertFalse(config.interval_cycle_enabled)

    def test_config_rejects_invalid_interval_cycle_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "循环刷新状态"):
            MonitorConfig.from_payload({"interval_cycle_enabled": "true"})

    def test_interval_cycle_repeats_from_one_to_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            with controller._lock:
                intervals = [
                    controller._take_next_interval_locked()
                    for _ in range(len(INTERVAL_CYCLE_SECONDS) + 2)
                ]

            self.assertEqual(intervals, [60, 120, 180, 240, 300, 60, 120])

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
            self.assertTrue(snapshot["capabilities"]["interval_cycle"])
            self.assertTrue(snapshot["capabilities"]["live_popup"])
            self.assertTrue(snapshot["capabilities"]["live_keyword"])
            self.assertTrue(snapshot["capabilities"]["file_logging"])
            self.assertTrue(snapshot["capabilities"]["long_session_metrics"])
            self.assertTrue(snapshot["diagnostics"]["log_file"].endswith("dashboard_debug.log"))
            self.assertEqual(snapshot["session"]["uptime_seconds"], 0)
            self.assertEqual(snapshot["session"]["page_request_count"], 0)

    def test_browser_session_metrics_count_requests_without_storing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            controller._begin_browser_session()
            controller._count_page_request(object())
            controller._count_page_request(object())
            snapshot = controller.snapshot()

            self.assertIsNotNone(snapshot["session"]["started_at"])
            self.assertGreaterEqual(snapshot["session"]["uptime_seconds"], 0)
            self.assertEqual(snapshot["session"]["page_request_count"], 2)
            controller._finish_browser_session()

    def test_browser_profile_conflict_has_readable_error(self) -> None:
        error = RuntimeError(
            "BrowserType.launch_persistent_context: "
            "Target page, context or browser has been closed"
        )

        message = monitor_error_message(error)

        self.assertIn("监控专用浏览器可能已在其他窗口运行", message)

    def test_missing_edge_has_readable_error(self) -> None:
        error = RuntimeError("BrowserType.launch: Executable doesn't exist")

        self.assertEqual(
            monitor_error_message(error),
            "未找到 Microsoft Edge，请先安装或修复 Microsoft Edge",
        )

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

            cooldown = controller._handle_scan_error(error)

            self.assertIsNone(cooldown)
            self.assertTrue(controller._stop_event.is_set())
            self.assertEqual(controller.cooldown_seconds, 0)
            self.assertEqual(controller.status, "error")
            self.assertEqual(controller.status_text, "监控浏览器已关闭")

    def test_recoverable_errors_use_progressive_cooldown_and_reset_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            error = RuntimeError("Page.goto: timeout")

            cooldowns = [controller._handle_scan_error(error) for _ in range(4)]

            self.assertEqual(cooldowns, [900, 1800, 3600, 3600])
            self.assertEqual(ERROR_COOLDOWN_SECONDS, (900, 1800, 3600))
            self.assertEqual(controller.consecutive_scan_errors, 4)
            self.assertEqual(controller.cooldown_seconds, 3600)
            self.assertEqual(controller.status, "cooldown")
            self.assertEqual(controller.status_text, "异常冷却 60 分钟")

            controller._record_scan_success()

            self.assertEqual(controller.consecutive_scan_errors, 0)
            self.assertEqual(controller.cooldown_seconds, 0)

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

            controller.set_interval(
                {"interval": 137, "interval_cycle_enabled": False}
            )

            self.assertEqual(controller.config.interval, 137)
            self.assertFalse(controller.config.interval_cycle_enabled)
            self.assertEqual(controller.scheduled_interval_seconds, 137)
            self.assertTrue(controller._interval_changed_event.is_set())
            self.assertIsNotNone(controller.next_scan_at)

    def test_running_monitor_can_switch_keyword_and_search_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller.running = True
            controller.status = "waiting"
            controller.baseline_ready = True
            controller.last_scan_at = "2026-08-23T10:00:00+00:00"
            controller.scans_completed = 3
            controller.items_last_scan = 21
            controller.new_items_last_scan = 4
            controller.matched_last_scan = 2
            controller.state_store.establish_baseline("iPhone 15", {"known"})
            previous_revision = controller._keyword_revision

            changed, message = controller.search_keyword({"keyword": "iPhone 15"})

            self.assertTrue(changed)
            self.assertIn("iPhone 15", message)
            self.assertEqual(controller.config.keyword, "iPhone 15")
            self.assertTrue(controller.baseline_ready)
            self.assertIsNone(controller.last_scan_at)
            self.assertEqual(controller.scans_completed, 0)
            self.assertEqual(controller.items_last_scan, 0)
            self.assertEqual(controller.new_items_last_scan, 0)
            self.assertEqual(controller.matched_last_scan, 0)
            self.assertEqual(controller.status_text, "正在切换关键词")
            self.assertTrue(controller._scan_now_event.is_set())
            self.assertEqual(controller._keyword_revision, previous_revision + 1)

            processed = controller._process_scan(
                [SearchItem("old", "旧关键词商品", 20, "https://example.com/old")],
                MonitorConfig(),
                FakeNotifier(),
                keyword_revision=previous_revision,
            )
            self.assertFalse(processed)
            self.assertEqual(controller.scans_completed, 0)

    def test_running_monitor_can_search_same_keyword_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller.running = True

            changed, message = controller.search_keyword({"keyword": "mardi短袖"})

            self.assertFalse(changed)
            self.assertIn("立即检索", message)
            self.assertTrue(controller._scan_now_event.is_set())

    def test_keyword_search_rejects_stopped_or_safety_paused_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )

            with self.assertRaisesRegex(ValueError, "监控未运行"):
                controller.search_keyword({"keyword": "iPhone 15"})

            controller.status = "safety_stopped"
            with self.assertRaisesRegex(ValueError, "等待人工验证"):
                controller.search_keyword({"keyword": "iPhone 15"})

    def test_interval_change_does_not_shorten_active_error_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller.running = True
            controller.cooldown_seconds = 900

            controller.set_interval(
                {"interval": 60, "interval_cycle_enabled": True}
            )

            self.assertEqual(controller.scheduled_interval_seconds, 900)

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

    def test_safety_issue_pauses_monitor_and_keeps_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            notifications = FakeNotificationManager()
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=notifications,
            )
            notifier = FakeNotifier()
            controller.running = True

            controller._pause_for_safety("检测到验证码", notifier)

            self.assertFalse(controller._stop_event.is_set())
            self.assertFalse(controller.running)
            self.assertEqual(controller.status, "safety_stopped")
            self.assertEqual(controller.status_text, "等待人工验证")
            self.assertEqual(controller.error, "检测到验证码")
            self.assertEqual(controller.safety_pause_count, 1)
            self.assertEqual(controller.cooldown_seconds, 0)
            self.assertEqual(len(notifier.messages), 1)
            self.assertEqual(
                notifications.safety_events,
                [("检测到验证码", "mardi短袖")],
            )

    def test_safety_pause_resumes_existing_monitor_thread(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller._thread = AliveThread()
            controller.status = "safety_stopped"
            controller.running = False

            started = controller.start({"access_mode": "login"})

            self.assertTrue(started)
            self.assertTrue(controller.running)
            self.assertEqual(controller.status, "resuming")
            self.assertTrue(controller._resume_event.is_set())
            self.assertFalse(controller._stop_event.is_set())

    def test_safety_pause_can_be_stopped_and_close_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            controller = MonitorController(
                root / "state.json",
                root / "profile",
                notifications=FakeNotificationManager(),
            )
            controller.status = "safety_stopped"
            controller.running = False

            stopped = controller.stop()

            self.assertTrue(stopped)
            self.assertTrue(controller._stop_event.is_set())
            self.assertTrue(controller._resume_event.is_set())
            self.assertEqual(controller.status, "stopping")

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
