import tempfile
import unittest
from pathlib import Path

from xianyu_monitor import (
    SearchItem,
    StateStore,
    detect_blocking_issue,
    extract_item_id,
    extract_published_label,
    extract_query_terms,
    is_relevant,
    item_to_alert,
    parse_item,
    parse_price,
    select_new_eligible_items,
)


class XianyuParsingTests(unittest.TestCase):
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
