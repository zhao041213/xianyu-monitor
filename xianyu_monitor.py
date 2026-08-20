from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import platform
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


LOGGER = logging.getLogger("xianyu-monitor")
BASE_URL = "https://www.goofish.com"
ITEM_SELECTOR = 'a[href*="/item?id="]'
NO_RESULTS_TEXT = "小闲鱼没有找到你想要的宝贝~"
CAPTCHA_URL_MARKERS = ("/punish", "/captcha", "/verify")
CAPTCHA_TEXT_MARKERS = (
    "请完成安全验证",
    "请完成验证",
    "滑动滑块完成验证",
    "拖动下方滑块",
    "滑动验证",
    "验证码错误",
)
ACCESS_LIMIT_MARKERS = (
    "访问受限",
    "访问过于频繁",
    "请求过于频繁",
    "操作过于频繁",
    "异常流量",
    "当前访问存在风险",
    "系统检测到您的访问异常",
    "为了保障您的账号安全",
)
LOGIN_ABNORMAL_MARKERS = (
    "登录已失效",
    "登录状态已失效",
    "账号登录异常",
    "请重新登录",
)
PRICE_PATTERN = re.compile(
    r"[¥￥]\s*([0-9]+(?:\s*\.\s*[0-9]+)?)\s*(万)?",
    re.IGNORECASE,
)


class MonitoringSafetyStop(RuntimeError):
    """A page state that must stop automated scanning until a user intervenes."""


@dataclass(frozen=True)
class SearchItem:
    item_id: str
    title: str
    price: float
    url: str
    image_url: str | None = None
    published_label: str | None = None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def detect_blocking_issue(
    page_url: str,
    body_text: str,
    *,
    login_visible: bool = False,
    captcha_visible: bool = False,
) -> str | None:
    parsed_url = urlparse(page_url)
    location = f"{parsed_url.netloc}{parsed_url.path}".casefold()
    normalized_body = normalize_text(body_text)

    if (
        login_visible
        or "login" in parsed_url.netloc.casefold()
        or parsed_url.path.casefold().startswith("/login")
        or any(marker in normalized_body for marker in LOGIN_ABNORMAL_MARKERS)
    ):
        return "检测到闲鱼登录异常，监控已自动停止。请重新登录后手动启动。"

    if (
        captcha_visible
        or any(marker in location for marker in CAPTCHA_URL_MARKERS)
        or any(marker in normalized_body for marker in CAPTCHA_TEXT_MARKERS)
    ):
        return "检测到验证码或安全验证，监控已自动停止。请人工完成验证后再启动。"

    if any(marker in normalized_body for marker in ACCESS_LIMIT_MARKERS):
        return "检测到访问受限或异常流量提示，监控已自动停止。请稍后再手动启动。"

    return None


def parse_price(text: str) -> float | None:
    """Read the first displayed price; the first price is the sale price on Xianyu cards."""
    compact = re.sub(r"\s+", "", text).replace(",", "")
    match = PRICE_PATTERN.search(compact)
    if not match:
        return None

    value = float(match.group(1))
    if match.group(2):
        value *= 10000
    return value


def extract_item_id(href: str) -> str | None:
    try:
        values = parse_qs(urlparse(href).query).get("id", [])
    except ValueError:
        return None
    return values[0] if values and values[0] else None


def extract_title(text: str) -> str:
    before_price = re.split(r"[¥￥]", text, maxsplit=1)[0]
    title = normalize_text(before_price)
    return title[:160]


def extract_published_label(text: str) -> str | None:
    match = re.search(
        r"(?:刚刚|\d+\s*(?:分钟|小时|天))前?发布|\d+\s*分钟前发布",
        normalize_text(text),
    )
    return match.group(0) if match else None


def extract_query_terms(keyword: str) -> list[str]:
    ascii_terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", keyword.casefold())
    chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", keyword)
    terms: list[str] = []
    for term in ascii_terms + chinese_terms:
        if term not in terms:
            terms.append(term)
    return terms


def is_relevant(title: str, keyword: str) -> bool:
    terms = extract_query_terms(keyword)
    if not terms:
        return True
    normalized_title = title.casefold()
    return any(term in normalized_title for term in terms)


def parse_item(
    href: str,
    text: str,
    keyword: str,
    relevance_filter: bool = True,
    image_url: str | None = None,
) -> SearchItem | None:
    item_id = extract_item_id(href)
    title = extract_title(text)
    price = parse_price(text)
    if not item_id or not title or price is None:
        return None
    if relevance_filter and not is_relevant(title, keyword):
        return None
    return SearchItem(
        item_id=item_id,
        title=title,
        price=price,
        url=href,
        image_url=image_url,
        published_label=extract_published_label(text),
    )


class StateStore:
    """Persist per-keyword baselines and alerts across monitor restarts."""

    def __init__(self, path: Path, max_ids: int = 5000) -> None:
        self.path = path
        self.max_ids = max_ids
        self.announced_ids: set[str] = set()
        self.known_ids_by_keyword: dict[str, set[str]] = {}
        self.alerts: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            ids = payload.get("announced_ids", [])
            if isinstance(ids, list):
                self.announced_ids = {str(item_id) for item_id in ids if item_id}
            known_payload = payload.get("known_ids_by_keyword", {})
            if isinstance(known_payload, dict):
                self.known_ids_by_keyword = {
                    str(keyword): {str(item_id) for item_id in item_ids if item_id}
                    for keyword, item_ids in known_payload.items()
                    if isinstance(item_ids, list)
                }
            alerts = payload.get("alerts", [])
            if isinstance(alerts, list):
                self.alerts = [alert for alert in alerts[-100:] if isinstance(alert, dict)]
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            LOGGER.warning("状态文件无法读取，将从空状态开始: %s", exc)

    def contains(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self.announced_ids

    def mark_announced(self, item_id: str) -> None:
        with self._lock:
            self.announced_ids.add(item_id)
            if len(self.announced_ids) > self.max_ids:
                self.announced_ids = set(sorted(self.announced_ids)[-self.max_ids :])
            self._save()

    @staticmethod
    def watch_key(keyword: str) -> str:
        return normalize_text(keyword).casefold()

    def has_baseline(self, keyword: str) -> bool:
        with self._lock:
            return self.watch_key(keyword) in self.known_ids_by_keyword

    def known_ids(self, keyword: str) -> set[str]:
        with self._lock:
            return set(self.known_ids_by_keyword.get(self.watch_key(keyword), set()))

    def establish_baseline(self, keyword: str, item_ids: set[str]) -> None:
        with self._lock:
            self.known_ids_by_keyword[self.watch_key(keyword)] = set(item_ids)
            self._save()

    def record_seen(self, keyword: str, item_ids: set[str]) -> None:
        with self._lock:
            watch_key = self.watch_key(keyword)
            known_ids = self.known_ids_by_keyword.setdefault(watch_key, set())
            known_ids.update(item_ids)
            if len(known_ids) > self.max_ids:
                self.known_ids_by_keyword[watch_key] = set(
                    sorted(known_ids)[-self.max_ids :]
                )
            self._save()

    def record_alert(self, alert: dict[str, Any]) -> None:
        with self._lock:
            item_id = str(alert.get("item_id", ""))
            keyword_key = self.watch_key(str(alert.get("keyword", "")))
            if item_id:
                self.announced_ids.add(item_id)
            if len(self.announced_ids) > self.max_ids:
                self.announced_ids = set(
                    sorted(self.announced_ids)[-self.max_ids :]
                )
            self.alerts = [
                existing
                for existing in self.alerts
                if not (
                    str(existing.get("item_id", "")) == item_id
                    and self.watch_key(str(existing.get("keyword", "")))
                    == keyword_key
                )
            ]
            self.alerts.insert(0, dict(alert))
            self.alerts = self.alerts[:100]
            self._save()

    def alert_snapshot(self, keyword: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if keyword is None:
                return [dict(alert) for alert in self.alerts]
            keyword_key = self.watch_key(keyword)
            return [
                dict(alert)
                for alert in self.alerts
                if self.watch_key(str(alert.get("keyword", ""))) == keyword_key
            ]

    def clear_alerts(self, keyword: str | None = None) -> None:
        with self._lock:
            if keyword is None:
                self.alerts.clear()
            else:
                keyword_key = self.watch_key(keyword)
                self.alerts = [
                    alert
                    for alert in self.alerts
                    if self.watch_key(str(alert.get("keyword", "")))
                    != keyword_key
                ]
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "announced_ids": sorted(self.announced_ids),
                "known_ids_by_keyword": {
                    keyword: sorted(item_ids)
                    for keyword, item_ids in self.known_ids_by_keyword.items()
                },
                "alerts": self.alerts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            temporary_path = self.path.with_name(f"{self.path.name}.tmp")
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError as exc:
            LOGGER.warning("状态文件无法保存，商品可能在重启后重复播报: %s", exc)


class PopupNotifier:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._logged_unavailable = False
        if enabled:
            threading.Thread(
                target=self._worker,
                name="xianyu-popup-notifier",
                daemon=True,
            ).start()

    def notify(self, title: str, message: str) -> None:
        if not self.enabled:
            return
        self._queue.put((title, message))

    def _worker(self) -> None:
        while True:
            title, message = self._queue.get()
            try:
                if not self._show_message_box(title, message):
                    if not self._logged_unavailable:
                        LOGGER.warning("文字弹窗不可用，将保留控制台提示。")
                        self._logged_unavailable = True
                    LOGGER.info("[弹窗] %s: %s", title, message.replace("\n", " | "))
            finally:
                self._queue.task_done()

    @staticmethod
    def _show_message_box(title: str, message: str) -> bool:
        if platform.system() != "Windows":
            return False
        try:
            result = ctypes.windll.user32.MessageBoxW(
                0,
                message,
                title,
                0x40,  # MB_ICONINFORMATION
            )
            return result != 0
        except (AttributeError, OSError):
            return False


class XianyuMonitor:
    def __init__(
        self,
        page: Any,
        keyword: str,
        relevance_filter: bool,
        wait_for_login: bool,
        login_timeout: int,
        newest_first: bool = True,
    ) -> None:
        self.page = page
        self.keyword = keyword
        self.relevance_filter = relevance_filter
        self.wait_for_login = wait_for_login
        self.login_timeout = login_timeout
        self.newest_first = newest_first
        self._login_checked = False

    @property
    def search_url(self) -> str:
        return f"{BASE_URL}/search?{urlencode({'q': self.keyword})}"

    async def scan(self) -> list[SearchItem]:
        await self.page.goto(
            self.search_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await self.page.wait_for_timeout(3000)

        login_visible = await self._login_dialog_visible()
        if not self._login_checked:
            self._login_checked = True
            if login_visible:
                if self.wait_for_login:
                    await self._wait_for_login()
                    await self.page.goto(
                        self.search_url,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    await self.page.wait_for_timeout(3000)
                else:
                    raise MonitoringSafetyStop(
                        "检测到闲鱼登录异常，监控已自动停止。请重新登录后手动启动。"
                    )
        elif login_visible:
            raise MonitoringSafetyStop(
                "检测到闲鱼登录状态失效，监控已自动停止。请重新登录后手动启动。"
            )

        body_text = await self._read_page_text_or_stop()
        if NO_RESULTS_TEXT in body_text:
            return []

        if self.newest_first:
            await self._select_newest_sort()

        body_text = await self._read_page_text_or_stop()
        if NO_RESULTS_TEXT in body_text:
            return []

        cards = self.page.locator(ITEM_SELECTOR)
        count = await cards.count()
        items: list[SearchItem] = []
        seen_ids: set[str] = set()
        for index in range(count):
            card = cards.nth(index)
            href = await card.get_attribute("href")
            text = await card.inner_text()
            if not href:
                continue
            image_url: str | None = None
            images = card.locator("img")
            if await images.count():
                image_url = await images.first.get_attribute("src")
            item = parse_item(
                href=href,
                text=text,
                keyword=self.keyword,
                relevance_filter=self.relevance_filter,
                image_url=image_url,
            )
            if item is None or item.item_id in seen_ids:
                continue
            seen_ids.add(item.item_id)
            items.append(item)
        return items

    async def _read_page_text_or_stop(self) -> str:
        body_text = await self.page.locator("body").inner_text(timeout=15000)
        issue = detect_blocking_issue(
            self.page.url,
            body_text,
            login_visible=await self._login_dialog_visible(),
            captcha_visible=await self._security_dialog_visible(),
        )
        if issue:
            raise MonitoringSafetyStop(issue)
        return body_text

    async def _select_newest_sort(self) -> None:
        title = self.page.locator('span[class*="search-select-title"]').filter(
            has_text=re.compile(r"^新发布$")
        )
        if await title.count() == 0:
            raise RuntimeError("闲鱼页面中找不到“新发布”排序器")

        container = title.first.locator("xpath=../..")
        latest = container.locator('div[class*="search-select-item"]').filter(
            has_text=re.compile(r"^最新$")
        )
        for attempt in range(2):
            await container.hover()
            await title.first.click()
            await self.page.wait_for_timeout(350)
            if await latest.first.is_visible():
                await latest.first.click(force=True)
                break
            if attempt == 1:
                raise RuntimeError("闲鱼“新发布”排序器未能展开")
        await self.page.wait_for_timeout(3000)

    async def _login_dialog_visible(self) -> bool:
        locator = self.page.locator("iframe#alibaba-login-box")
        if await locator.count() == 0:
            return False
        try:
            return await locator.first.is_visible()
        except Exception:
            return False

    async def _security_dialog_visible(self) -> bool:
        locator = self.page.locator("iframe#baxia-dialog-content")
        if await locator.count() == 0:
            return False
        try:
            return await locator.first.is_visible()
        except Exception:
            return False

    async def _wait_for_login(self) -> None:
        LOGGER.warning(
            "请在已打开的闲鱼窗口中完成登录，脚本最多等待 %d 秒。",
            self.login_timeout,
        )
        deadline = time.monotonic() + self.login_timeout
        while time.monotonic() < deadline:
            if not await self._login_dialog_visible():
                LOGGER.info("登录弹层已关闭，继续监控。")
                return
            await self.page.wait_for_timeout(1000)
        raise MonitoringSafetyStop(
            "等待闲鱼登录超时，监控已自动停止。请重新登录后手动启动。"
        )


def format_announcement(item: SearchItem, max_price: float) -> str:
    return f"发现新发布的商品：{item.title}，价格 {item.price:.2f} 元。"


def select_new_eligible_items(
    items: list[SearchItem],
    known_ids: set[str],
    max_price: float,
) -> list[SearchItem]:
    return [
        item
        for item in items
        if item.item_id not in known_ids and item.price < max_price
    ]


def item_to_alert(
    item: SearchItem,
    found_at: str | None = None,
    *,
    keyword: str | None = None,
) -> dict[str, Any]:
    alert = {
        "item_id": item.item_id,
        "title": item.title,
        "price": item.price,
        "url": item.url,
        "image_url": item.image_url,
        "published_label": item.published_label,
        "found_at": found_at or datetime.now(timezone.utc).isoformat(),
    }
    if keyword:
        alert["keyword"] = normalize_text(keyword)
    return alert


async def run_monitor(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Playwright，请先执行 python -m pip install -r requirements.txt，"
            "再执行 python -m playwright install chromium"
        ) from exc

    profile_dir = Path(args.profile_dir).resolve()
    state_store = StateStore(Path(args.state_file).resolve())
    popup_notifier = PopupNotifier(enabled=not args.no_popup)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(15000)
            monitor = XianyuMonitor(
                page=page,
                keyword=args.keyword,
                relevance_filter=not args.no_relevance_filter,
                wait_for_login=not args.skip_login_wait,
                login_timeout=args.login_timeout,
            )

            async def scan_and_announce() -> None:
                items = await monitor.scan()
                current_ids = {item.item_id for item in items}
                if not state_store.has_baseline(args.keyword):
                    state_store.establish_baseline(args.keyword, current_ids)
                    LOGGER.info(
                        "首次扫描已记录 %d 个商品作为基线，不推送历史商品。",
                        len(current_ids),
                    )
                    return

                known_ids = state_store.known_ids(args.keyword)
                new_items = [item for item in items if item.item_id not in known_ids]
                eligible = select_new_eligible_items(
                    items,
                    known_ids,
                    args.max_price,
                )
                state_store.record_seen(args.keyword, current_ids)
                LOGGER.info(
                    "本轮读取 %d 个商品，其中新出现 %d 个、低于 %.2f 元 %d 个。",
                    len(items),
                    len(new_items),
                    args.max_price,
                    len(eligible),
                )
                for item in sorted(eligible, key=lambda current: current.price):
                    message = format_announcement(item, args.max_price)
                    LOGGER.info("%s | %s", message, item.url)
                    popup_notifier.notify(
                        "闲鱼低价提醒",
                        f"{message}\n{item.url}",
                    )
                    state_store.record_alert(
                        item_to_alert(item, keyword=args.keyword)
                    )

            if args.once:
                await scan_and_announce()
                return

            while True:
                started_at = time.monotonic()
                try:
                    await scan_and_announce()
                except MonitoringSafetyStop as exc:
                    LOGGER.error("%s", exc)
                    popup_notifier.notify("闲鱼监控已自动停止", str(exc))
                    return
                except Exception:
                    LOGGER.exception("本轮监控失败，下一轮会继续尝试。")
                elapsed = time.monotonic() - started_at
                await asyncio.sleep(max(0, args.interval - elapsed))
        finally:
            await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监控闲鱼新发布的低价商品")
    parser.add_argument("--keyword", default="mardi短袖", help="闲鱼搜索关键词")
    parser.add_argument("--max-price", type=float, default=60, help="只推送严格低于此价格的商品")
    parser.add_argument("--interval", type=float, default=60, help="两轮扫描的最短间隔，默认 60 秒")
    parser.add_argument("--profile-dir", default=".browser-data", help="保存登录态的 Playwright 用户目录")
    parser.add_argument("--state-file", default="monitor_state.json", help="保存已播报商品 ID 的文件")
    parser.add_argument("--login-timeout", type=int, default=600, help="首次登录最多等待秒数")
    parser.add_argument("--headless", action="store_true", help="无界面运行；首次运行不适合用此选项登录")
    parser.add_argument("--skip-login-wait", action="store_true", help="不等待登录，直接尝试读取公开结果")
    parser.add_argument(
        "--no-popup",
        "--no-speech",
        dest="no_popup",
        action="store_true",
        help="只打印日志，不弹出文字提示（--no-speech 为兼容参数）",
    )
    parser.add_argument(
        "--no-relevance-filter",
        action="store_true",
        help="不过滤猜你喜欢等与关键词无关的卡片；默认只保留标题含搜索词的卡片",
    )
    parser.add_argument("--once", action="store_true", help="只扫描一次，便于测试")
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = build_parser().parse_args()
    if args.max_price <= 0:
        LOGGER.error("--max-price 必须大于 0。")
        return 2
    if args.interval < 1:
        LOGGER.error("--interval 必须至少为 1 秒；生产监控建议保持默认的 60 秒。")
        return 2
    if args.login_timeout < 1:
        LOGGER.error("--login-timeout 必须大于 0。")
        return 2

    try:
        asyncio.run(run_monitor(args))
    except KeyboardInterrupt:
        LOGGER.info("监控已停止。")
    except Exception as exc:
        LOGGER.error("启动失败: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
