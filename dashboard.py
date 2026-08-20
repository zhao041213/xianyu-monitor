from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from xianyu_monitor import (
    PopupNotifier,
    MonitoringSafetyStop,
    SearchItem,
    StateStore,
    XianyuMonitor,
    format_announcement,
    item_to_alert,
    select_new_eligible_items,
)


LOGGER = logging.getLogger("xianyu-dashboard")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "dashboard"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


@dataclass(frozen=True)
class MonitorConfig:
    keyword: str = "mardi短袖"
    max_price: float = 60.0
    interval: int = 60
    popup_enabled: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MonitorConfig":
        keyword = str(payload.get("keyword", cls.keyword)).strip()
        if not keyword or len(keyword) > 80:
            raise ValueError("关键词长度必须为 1 到 80 个字符")

        try:
            max_price = float(payload.get("max_price", cls.max_price))
            interval = int(payload.get("interval", cls.interval))
        except (TypeError, ValueError) as exc:
            raise ValueError("价格和扫描间隔必须是数字") from exc

        if not 0 < max_price <= 1_000_000:
            raise ValueError("价格上限必须大于 0")
        if not 30 <= interval <= 3600:
            raise ValueError("扫描间隔必须在 30 到 3600 秒之间")

        return cls(
            keyword=keyword,
            max_price=max_price,
            interval=interval,
            popup_enabled=bool(payload.get("popup_enabled", True)),
        )


class MonitorController:
    def __init__(self, state_path: Path, profile_dir: Path) -> None:
        self.state_store = StateStore(state_path)
        self.profile_dir = profile_dir.resolve()
        self.config = MonitorConfig()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._scan_now_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[dict[str, str]] = deque(maxlen=120)

        self.running = False
        self.scanning = False
        self.status = "stopped"
        self.status_text = "已停止"
        self.error: str | None = None
        self.baseline_ready = self.state_store.has_baseline(self.config.keyword)
        self.last_scan_at: str | None = None
        self.next_scan_at: str | None = None
        self.scans_completed = 0
        self.items_last_scan = 0
        self.new_items_last_scan = 0
        self.matched_last_scan = 0

    def start(self, payload: dict[str, Any] | None = None) -> bool:
        config = MonitorConfig.from_payload(payload or {})
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            keyword_changed = (
                self.state_store.watch_key(self.config.keyword)
                != self.state_store.watch_key(config.keyword)
            )
            self.config = config
            self._stop_event.clear()
            self._scan_now_event.clear()
            self.running = True
            self.scanning = False
            self.status = "starting"
            self.status_text = "正在启动浏览器"
            self.error = None
            self.baseline_ready = self.state_store.has_baseline(config.keyword)
            self.next_scan_at = None
            if keyword_changed:
                self.last_scan_at = None
                self.scans_completed = 0
                self.items_last_scan = 0
                self.new_items_last_scan = 0
                self.matched_last_scan = 0
            self._thread = threading.Thread(
                target=self._thread_main,
                name="xianyu-monitor-worker",
                daemon=True,
            )
            self._thread.start()
        self._log("info", f"监控已启动：{config.keyword}，价格低于 {config.max_price:g} 元")
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self.status = "stopping"
            self.status_text = "正在停止"
            self._stop_event.set()
            self._scan_now_event.set()
        self._log("info", "正在停止监控")
        return True

    def scan_now(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self.next_scan_at = iso_now()
            self._scan_now_event.set()
        self._log("info", "已请求立即扫描")
        return True

    def clear_alerts(self) -> None:
        self.state_store.clear_alerts(self.config.keyword)
        self._log("info", f"“{self.config.keyword}”的推送记录已清空")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            config = asdict(self.config)
            known_count = len(self.state_store.known_ids(self.config.keyword))
            return {
                "running": self.running,
                "scanning": self.scanning,
                "status": self.status,
                "status_text": self.status_text,
                "error": self.error,
                "baseline_ready": self.baseline_ready,
                "last_scan_at": self.last_scan_at,
                "next_scan_at": self.next_scan_at,
                "scans_completed": self.scans_completed,
                "items_last_scan": self.items_last_scan,
                "new_items_last_scan": self.new_items_last_scan,
                "matched_last_scan": self.matched_last_scan,
                "known_count": known_count,
                "config": config,
                "alerts": self.state_store.alert_snapshot(self.config.keyword),
                "logs": list(self._logs),
            }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._monitor_loop(self.config))
        except Exception as exc:
            LOGGER.exception("监控线程异常退出")
            with self._lock:
                self.error = str(exc)
                self.status = "error"
                self.status_text = "监控启动失败"
            self._log("error", str(exc))
        finally:
            with self._lock:
                self.running = False
                self.scanning = False
                self.next_scan_at = None
                if self.status not in {"error", "safety_stopped"}:
                    self.status = "stopped"
                    self.status_text = "已停止"

    async def _monitor_loop(self, config: MonitorConfig) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，请先安装 requirements.txt") from exc

        notifier = PopupNotifier(enabled=config.popup_enabled)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                locale="zh-CN",
                viewport={"width": 1280, "height": 780},
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(15000)
                monitor = XianyuMonitor(
                    page=page,
                    keyword=config.keyword,
                    relevance_filter=True,
                    wait_for_login=True,
                    login_timeout=600,
                    newest_first=True,
                )

                while not self._stop_event.is_set():
                    self._scan_now_event.clear()
                    with self._lock:
                        self.scanning = True
                        self.status = "scanning"
                        self.status_text = "正在扫描新发布"
                        self.error = None
                        self.next_scan_at = None

                    try:
                        items = await monitor.scan()
                        self._process_scan(items, config, notifier)
                    except MonitoringSafetyStop as exc:
                        self._stop_for_safety(str(exc), notifier)
                    except Exception as exc:
                        with self._lock:
                            self.error = str(exc)
                            self.status = "error"
                            self.status_text = "本轮扫描失败"
                        self._log("error", str(exc))
                    finally:
                        with self._lock:
                            self.scanning = False
                            self.last_scan_at = iso_now()
                            if self.status == "scanning":
                                self.status = "waiting"
                                self.status_text = "等待下一轮"
                            if self._stop_event.is_set():
                                self.next_scan_at = None
                            else:
                                self.next_scan_at = (
                                    utc_now() + timedelta(seconds=config.interval)
                                ).isoformat()

                    if self._stop_event.is_set():
                        break
                    await self._wait_for_next_scan(config.interval)
            finally:
                await context.close()

    def _process_scan(
        self,
        items: list[SearchItem],
        config: MonitorConfig,
        notifier: PopupNotifier,
    ) -> None:
        current_ids = {item.item_id for item in items}
        with self._lock:
            self.scans_completed += 1
            self.items_last_scan = len(items)

        if not self.state_store.has_baseline(config.keyword):
            self.state_store.establish_baseline(config.keyword, current_ids)
            with self._lock:
                self.baseline_ready = True
                self.new_items_last_scan = 0
                self.matched_last_scan = 0
                self.status_text = "基线已建立"
            self._log("info", f"首次扫描记录 {len(items)} 个商品，未推送历史商品")
            return

        known_ids = self.state_store.known_ids(config.keyword)
        new_items = [item for item in items if item.item_id not in known_ids]
        matched_items = select_new_eligible_items(items, known_ids, config.max_price)
        self.state_store.record_seen(config.keyword, current_ids)

        with self._lock:
            self.baseline_ready = True
            self.new_items_last_scan = len(new_items)
            self.matched_last_scan = len(matched_items)

        self._log(
            "info",
            f"扫描 {len(items)} 个商品，新出现 {len(new_items)} 个，命中 {len(matched_items)} 个",
        )
        for item in sorted(matched_items, key=lambda candidate: candidate.price):
            alert = item_to_alert(item, keyword=config.keyword)
            self.state_store.record_alert(alert)
            notifier.notify(
                "闲鱼新商品提醒",
                f"{format_announcement(item, config.max_price)}\n{item.url}",
            )
            self._log("match", f"¥{item.price:g} {item.title}")

    async def _wait_for_next_scan(self, interval: int) -> None:
        deadline = time.monotonic() + interval
        while not self._stop_event.is_set():
            if self._scan_now_event.is_set() or time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.25)

    def _stop_for_safety(self, reason: str, notifier: PopupNotifier) -> None:
        self._stop_event.set()
        with self._lock:
            self.error = reason
            self.status = "safety_stopped"
            self.status_text = "已自动停止"
            self.next_scan_at = None
        self._log("error", reason)
        notifier.notify("闲鱼监控已自动停止", reason)

    def _log(self, level: str, message: str) -> None:
        entry = {"time": iso_now(), "level": level, "message": message}
        with self._lock:
            self._logs.appendleft(entry)
        getattr(LOGGER, "error" if level == "error" else "info")(message)


class DashboardHandler(BaseHTTPRequestHandler):
    controller: MonitorController
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    }

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/status":
            self._send_json(self.controller.snapshot())
            return
        static = self.static_files.get(self.path)
        if static:
            filename, content_type = static
            self._send_bytes((STATIC_DIR / filename).read_bytes(), content_type)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json()
            if self.path == "/api/start":
                if not self.controller.start(payload):
                    self._send_json({"error": "监控已在运行"}, HTTPStatus.CONFLICT)
                    return
                self._send_json(self.controller.snapshot(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/stop":
                self.controller.stop()
                self._send_json(self.controller.snapshot(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/scan":
                if not self.controller.scan_now():
                    self._send_json({"error": "监控未运行"}, HTTPStatus.CONFLICT)
                    return
                self._send_json(self.controller.snapshot(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/alerts/clear":
                self.controller.clear_alerts()
                self._send_json(self.controller.snapshot())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOGGER.exception("API 请求处理失败")
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("请求内容过大")
        if not length:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式无效")
        return payload

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string: str, *args: Any) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="闲鱼新商品监控面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开控制面板")
    parser.add_argument("--no-auto-start", action="store_true", help="启动面板时不自动开始监控")
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = build_parser().parse_args()
    controller = MonitorController(
        state_path=BASE_DIR / "monitor_state.json",
        profile_dir=BASE_DIR / ".browser-data",
    )
    DashboardHandler.controller = controller
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"

    if not args.no_auto_start:
        controller.start(asdict(MonitorConfig()))
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    print(f"闲鱼监控面板已启动：{url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
