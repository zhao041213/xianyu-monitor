import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xianyu_monitor import (
    BROWSER_CHANNELS,
    DEFAULT_BROWSER,
    PopupNotifier,
    SearchItem,
    StateStore,
    build_parser,
    detect_blocking_issue,
    default_browser_profile_dir,
    extract_item_id,
    extract_published_label,
    extract_query_terms,
    is_relevant,
    item_to_alert,
    parse_item,
    parse_price,
    select_new_eligible_items,
    select_product_image_url,
)


class XianyuParsingTests(unittest.TestCase):
    def test_browser_options_use_supported_channels_and_isolated_defaults(self) -> None:
        parser = build_parser()
        default_args = parser.parse_args([])
        chrome_args = parser.parse_args(["--browser", "chrome"])

        self.assertEqual(BROWSER_CHANNELS, {"edge": "msedge", "chrome": "chrome"})
        self.assertEqual(default_args.browser, DEFAULT_BROWSER)
        self.assertEqual(default_args.profile_dir, "")
        self.assertEqual(chrome_args.browser, "chrome")
        self.assertEqual(default_browser_profile_dir("edge"), Path(".edge-browser-data"))
        self.assertEqual(default_browser_profile_dir("chrome"), Path(".chrome-browser-data"))

    def test_product_image_skips_placeholder_and_normalizes_cdn_url(self) -> None:
        self.assertEqual(
            select_product_image_url(
                [
                    "https://img.alicdn.com/imgextra/2-tps-2-2.png",
                    "//img.alicdn.com/bao/uploaded/product.jpg 2x",
                ]
            ),
            "https://img.alicdn.com/bao/uploaded/product.jpg",
        )
        self.assertIsNone(select_product_image_url(["https://example.com/product.jpg"]))

    def test_popup_notifier_can_be_enabled_after_startup(self) -> None:
        with patch("xianyu_monitor.threading.Thread") as thread_class:
            notifier = PopupNotifier(enabled=False)
            notifier.notify("忽略", "关闭状态")
            self.assertEqual(notifier._queue.qsize(), 0)

            notifier.set_enabled(True)
            notifier.notify("提醒", "开启状态")
            self.assertEqual(notifier._queue.qsize(), 1)
            thread_class.assert_called_once()
            thread_class.return_value.start.assert_called_once()

            notifier.set_enabled(False)
            notifier.notify("忽略", "再次关闭")
            notifier.set_enabled(True)
            self.assertEqual(notifier._queue.qsize(), 1)
            thread_class.assert_called_once()

    def test_parse_prices_shown_in_separate_dom_nodes(self) -> None:
        self.assertEqual(parse_price("商品标题\n¥\n5\n.90\n¥19.80"), 5.90)
        self.assertEqual(parse_price("商品标题 ¥ 1.2 万"), 12000)
        self.assertIsNone(parse_price("商品标题 面议"))

    def test_extract_item_id(self) -> None:
        href = "https://www.goofish.com/item?id=123456&categoryId=0"
        self.assertEqual(extract_item_id(href), "123456")
        self.assertIsNone(extract_item_id("https://www.goofish.com/search?q=mardi"))

    def test_query_terms_and_relevance(self) -> None:
        self.assertEqual(extract_query_terms("mardi短袖"), ["mardi", "短袖"])
        self.assertTrue(is_relevant("MARDI 短袖夏季T恤", "mardi短袖"))
        self.assertFalse(is_relevant("普通运动鞋", "mardi短袖"))

    def test_parse_item_uses_first_price_as_current_price(self) -> None:
        item = parse_item(
            "https://www.goofish.com/item?id=123456&categoryId=0",
            "MARDI 短袖夏季T恤\n¥\n69\n¥129",
            "mardi短袖",
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.item_id, "123456")
        self.assertEqual(item.price, 69)

    def test_extract_published_label(self) -> None:
        self.assertEqual(extract_published_label("商品 49分钟前发布 ¥59"), "49分钟前发布")
        self.assertEqual(extract_published_label("商品 刚刚发布 ¥59"), "刚刚发布")

    def test_blocking_page_detection(self) -> None:
        self.assertIn(
            "验证码",
            detect_blocking_issue(
                "https://www.goofish.com/punish", "请完成安全验证"
            ),
        )
        self.assertIn(
            "验证码",
            detect_blocking_issue(
                "https://www.goofish.com/search", "", captcha_visible=True
            ),
        )
        self.assertIn(
            "访问受限",
            detect_blocking_issue(
                "https://www.goofish.com/search", "访问过于频繁，请稍后再试"
            ),
        )
        self.assertIn(
            "登录异常",
            detect_blocking_issue(
                "https://www.goofish.com/search", "", login_visible=True
            ),
        )
        self.assertIsNone(
            detect_blocking_issue(
                "https://www.goofish.com/search?q=mardi", "正常商品列表"
            )
        )

    def test_only_new_items_strictly_below_limit_are_selected(self) -> None:
        items = [
            SearchItem("known", "旧商品", 20, "https://example.com/known"),
            SearchItem("under", "新低价", 59.99, "https://example.com/under"),
            SearchItem("equal", "刚好六十", 60, "https://example.com/equal"),
        ]
        selected = select_new_eligible_items(items, {"known"}, 60)
        self.assertEqual([item.item_id for item in selected], ["under"])

    def test_state_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "state.json"
            first = StateStore(state_path)
            first.mark_announced("123456")

            second = StateStore(state_path)
            self.assertTrue(second.contains("123456"))

    def test_state_store_persists_keyword_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "state.json"
            first = StateStore(state_path)
            self.assertFalse(first.has_baseline("mardi短袖"))
            first.establish_baseline("mardi短袖", {"old-1", "old-2"})

            second = StateStore(state_path)
            self.assertTrue(second.has_baseline("MARDI短袖"))
            self.assertEqual(second.known_ids("mardi短袖"), {"old-1", "old-2"})

    def test_alert_history_is_isolated_by_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            store = StateStore(Path(temporary_dir) / "state.json")
            store.record_alert(
                item_to_alert(
                    SearchItem("same", "短袖", 59, "https://example.com/shirt"),
                    keyword="mardi短袖",
                )
            )
            store.record_alert(
                item_to_alert(
                    SearchItem("same", "手机", 999, "https://example.com/phone"),
                    keyword="iPhone 15",
                )
            )

            self.assertEqual(len(store.alert_snapshot("mardi短袖")), 1)
            self.assertEqual(len(store.alert_snapshot("iphone 15")), 1)
            store.clear_alerts("mardi短袖")
            self.assertEqual(store.alert_snapshot("mardi短袖"), [])
            self.assertEqual(len(store.alert_snapshot("iPhone 15")), 1)


if __name__ == "__main__":
    unittest.main()
