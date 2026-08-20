import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from notifications import (
    ListingNotification,
    NotificationConfigStore,
    NotificationManager,
    WebhookDeliveryError,
    build_dingtalk_url,
    build_webhook_payload,
    build_xianyu_app_entry_url,
    normalize_listing_image_url,
)


WECOM_WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
    "key=00000000-0000-0000-0000-000000000000"
)
DINGTALK_WEBHOOK = (
    "https://oapi.dingtalk.com/robot/send?access_token=test-access-token"
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.called = threading.Event()

    def __call__(self, url: str, payload: dict) -> None:
        self.calls.append((url, payload))
        self.called.set()


class NotificationTests(unittest.TestCase):
    def test_store_persists_credentials_but_public_snapshot_hides_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "notifications.json"
            manager = NotificationManager(path, transport=RecordingTransport())

            manager.save(
                {
                    "wecom": {"enabled": True, "webhook_url": WECOM_WEBHOOK},
                    "dingtalk": {
                        "enabled": True,
                        "webhook_url": DINGTALK_WEBHOOK,
                        "secret": "SEC-test-secret",
                    },
                }
            )

            raw_file = path.read_text(encoding="utf-8")
            public = manager.public_snapshot()
            self.assertIn("00000000-0000", raw_file)
            self.assertIn("SEC-test-secret", raw_file)
            self.assertTrue(public["wecom"]["configured"])
            self.assertTrue(public["dingtalk"]["secret_configured"])
            self.assertNotIn("webhook", json.dumps(public))
            self.assertNotIn("SEC-test-secret", json.dumps(public))

    def test_only_official_webhook_endpoints_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            store = NotificationConfigStore(Path(temporary_dir) / "notifications.json")

            with self.assertRaisesRegex(ValueError, "企业微信 Webhook 地址无效"):
                store.update(
                    {
                        "wecom": {
                            "enabled": True,
                            "webhook_url": "https://work.weixin.qq.com/wework_admin/common/openBotProfile/test",
                        }
                    }
                )
            with self.assertRaisesRegex(ValueError, "钉钉 Webhook 地址无效"):
                store.update(
                    {
                        "dingtalk": {
                            "enabled": True,
                            "webhook_url": "https://example.com/robot/send?access_token=test",
                        }
                    }
                )
            with self.assertRaisesRegex(ValueError, "清除钉钉加签密钥标记无效"):
                store.update(
                    {
                        "dingtalk": {
                            "enabled": False,
                            "clear_secret": "yes",
                        }
                    }
                )

    def test_blank_fields_retain_saved_values_and_clear_removes_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            store = NotificationConfigStore(Path(temporary_dir) / "notifications.json")
            store.update(
                {
                    "wecom": {"enabled": True, "webhook_url": WECOM_WEBHOOK},
                    "dingtalk": {
                        "enabled": True,
                        "webhook_url": DINGTALK_WEBHOOK,
                        "secret": "SEC-test-secret",
                    },
                }
            )

            retained = store.update(
                {
                    "wecom": {"enabled": False, "webhook_url": ""},
                    "dingtalk": {
                        "enabled": True,
                        "webhook_url": "",
                        "secret": "",
                    },
                }
            )
            self.assertEqual(retained.wecom.webhook_url, WECOM_WEBHOOK)
            self.assertEqual(retained.dingtalk.secret, "SEC-test-secret")

            cleared = store.clear("wecom")
            self.assertFalse(cleared.wecom.enabled)
            self.assertEqual(cleared.wecom.webhook_url, "")
            self.assertEqual(cleared.dingtalk.webhook_url, DINGTALK_WEBHOOK)

    def test_malformed_local_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "notifications.json"
            path.write_text(
                json.dumps(
                    {
                        "wecom": {
                            "enabled": "true",
                            "webhook_url": WECOM_WEBHOOK,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertLogs("xianyu-notifications", level="WARNING"):
                settings = NotificationConfigStore(path).snapshot()

            self.assertFalse(settings.wecom.enabled)
            self.assertEqual(settings.wecom.webhook_url, "")
            self.assertFalse(settings.dingtalk.enabled)

    def test_dingtalk_signature_matches_expected_value(self) -> None:
        signed = build_dingtalk_url(
            DINGTALK_WEBHOOK,
            "SECtest",
            timestamp_ms=1_700_000_000_000,
        )
        query = parse_qs(urlsplit(signed).query)

        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertEqual(
            query["sign"],
            ["aZLLrriXgn05YbwaGR7knYsLeJADjr9NwLaNNKpxh4g="],
        )

    def test_listing_payload_contains_product_information(self) -> None:
        notification = ListingNotification(
            title="Mardi 短袖 [全新]",
            price=59,
            keyword="mardi短袖",
            max_price=60,
            url=(
                "https://www.goofish.com/item?"
                "id=1234567890123&categoryId=126910002"
            ),
            image_url="//img.alicdn.com/bao/uploaded/test-product.jpg",
        )

        wecom = build_webhook_payload("wecom", notification)
        dingtalk = build_webhook_payload("dingtalk", notification)

        self.assertEqual(wecom["msgtype"], "news")
        articles = wecom["news"]["articles"]
        self.assertEqual(len(articles), 2)
        self.assertIn("¥59.00", articles[0]["title"])
        self.assertEqual(
            articles[0]["url"],
            "https://h5.m.goofish.com/item?id=1234567890123",
        )
        self.assertEqual(
            articles[0]["picurl"],
            "https://img.alicdn.com/bao/uploaded/test-product.jpg",
        )
        self.assertIn("网页备用", articles[1]["title"])
        self.assertEqual(articles[1]["url"], notification.url)
        self.assertIn("Mardi", dingtalk["markdown"]["text"])
        self.assertIn(
            "![商品图片](https://img.alicdn.com/",
            dingtalk["markdown"]["text"],
        )
        self.assertIn(notification.url, dingtalk["markdown"]["text"])
        self.assertNotIn("h5.m.goofish.com", dingtalk["markdown"]["text"])

    def test_wecom_without_image_keeps_markdown_links(self) -> None:
        notification = ListingNotification(
            title="Mardi 短袖",
            price=59,
            keyword="mardi短袖",
            max_price=60,
            url="https://www.goofish.com/item?id=1234567890123",
        )

        payload = build_webhook_payload("wecom", notification)

        self.assertEqual(payload["msgtype"], "markdown")
        self.assertIn("在闲鱼 App 中打开", payload["markdown"]["content"])
        self.assertIn("网页备用", payload["markdown"]["content"])

    def test_listing_image_only_accepts_alibaba_https_cdn(self) -> None:
        self.assertEqual(
            normalize_listing_image_url("//img.alicdn.com/item.jpg"),
            "https://img.alicdn.com/item.jpg",
        )
        self.assertIsNone(normalize_listing_image_url("http://img.alicdn.com/item.jpg"))
        self.assertIsNone(normalize_listing_image_url("https://example.com/item.jpg"))
        self.assertIsNone(
            normalize_listing_image_url("https://img.alicdn.com.example.com/item.jpg")
        )

    def test_xianyu_web_url_converts_to_safe_mobile_app_entry(self) -> None:
        self.assertEqual(
            build_xianyu_app_entry_url(
                "https://www.goofish.com/item?"
                "id=1234567890123&categoryId=126910002"
            ),
            "https://h5.m.goofish.com/item?id=1234567890123",
        )
        self.assertEqual(
            build_xianyu_app_entry_url(
                "https://h5.m.goofish.com/app/idleFish-F2e/"
                "fish-mini-pha/detail.html?id=1234567890123&forceFlush=1"
            ),
            "https://h5.m.goofish.com/item?id=1234567890123",
        )

    def test_xianyu_app_entry_rejects_untrusted_or_invalid_urls(self) -> None:
        urls = [
            "https://www.goofish.com.example.test/item?id=1234567890123",
            "https://www.goofish.com/search?id=1234567890123",
            "https://www.goofish.com/item?id=not-a-number",
            "https://example.com/item?id=1234567890123",
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(build_xianyu_app_entry_url(url), url)

    def test_test_send_uses_saved_webhook_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            transport = RecordingTransport()
            manager = NotificationManager(
                Path(temporary_dir) / "notifications.json",
                transport=transport,
            )
            manager.save(
                {"wecom": {"enabled": True, "webhook_url": WECOM_WEBHOOK}}
            )

            message = manager.test("wecom", {"webhook_url": ""})

            self.assertEqual(message, "企业微信测试消息已发送")
            self.assertEqual(transport.calls[0][0], WECOM_WEBHOOK)
            self.assertEqual(
                manager.public_snapshot()["wecom"]["last_result"], "success"
            )

    def test_delivery_failure_is_recorded_without_credentials(self) -> None:
        def failing_transport(_url: str, _payload: dict) -> None:
            raise WebhookDeliveryError("无法连接通知服务，请检查网络或代理")

        with tempfile.TemporaryDirectory() as temporary_dir:
            manager = NotificationManager(
                Path(temporary_dir) / "notifications.json",
                transport=failing_transport,
            )
            with self.assertRaises(WebhookDeliveryError):
                manager.test("wecom", {"webhook_url": WECOM_WEBHOOK})

            status = manager.public_snapshot()["wecom"]
            self.assertEqual(status["last_result"], "error")
            self.assertNotIn("00000000", json.dumps(status))

    def test_enabled_channel_receives_listing_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            transport = RecordingTransport()
            manager = NotificationManager(
                Path(temporary_dir) / "notifications.json",
                transport=transport,
            )
            manager.save(
                {"wecom": {"enabled": True, "webhook_url": WECOM_WEBHOOK}}
            )

            manager.notify_listing(
                ListingNotification(
                    title="新发布短袖",
                    price=58,
                    keyword="mardi短袖",
                    max_price=60,
                    url="https://www.goofish.com/item?id=new",
                )
            )

            self.assertTrue(transport.called.wait(timeout=1))
            self.assertEqual(len(transport.calls), 1)
            self.assertIn("新发布短袖", transport.calls[0][1]["markdown"]["content"])


if __name__ == "__main__":
    unittest.main()
