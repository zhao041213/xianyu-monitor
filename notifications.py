from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("xianyu-notifications")
CHANNEL_NAMES = {"wecom": "企业微信", "dingtalk": "钉钉"}


class WebhookDeliveryError(RuntimeError):
    """Raised when a webhook rejects a notification or cannot be reached."""


@dataclass(frozen=True)
class ChannelSettings:
    enabled: bool = False
    webhook_url: str = ""
    secret: str = ""


@dataclass(frozen=True)
class NotificationSettings:
    wecom: ChannelSettings = ChannelSettings()
    dingtalk: ChannelSettings = ChannelSettings()


@dataclass(frozen=True)
class ListingNotification:
    title: str
    price: float
    keyword: str
    max_price: float
    url: str


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_webhook(channel: str, value: str) -> str:
    webhook = value.strip()
    if not webhook:
        return ""
    if len(webhook) > 2048:
        raise ValueError(f"{CHANNEL_NAMES[channel]} Webhook 地址过长")

    parsed = urlsplit(webhook)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise ValueError(f"{CHANNEL_NAMES[channel]} Webhook 地址无效")

    query = parse_qs(parsed.query, keep_blank_values=True)
    if channel == "wecom":
        valid = (
            parsed.hostname == "qyapi.weixin.qq.com"
            and parsed.path == "/cgi-bin/webhook/send"
            and len(query.get("key", [])) == 1
            and bool(query["key"][0])
        )
    elif channel == "dingtalk":
        valid = (
            parsed.hostname == "oapi.dingtalk.com"
            and parsed.path == "/robot/send"
            and len(query.get("access_token", [])) == 1
            and bool(query["access_token"][0])
        )
    else:
        raise ValueError("不支持的通知渠道")

    if not valid:
        raise ValueError(f"{CHANNEL_NAMES[channel]} Webhook 地址无效")
    return webhook


def _channel_from_payload(
    channel: str,
    current: ChannelSettings,
    payload: dict[str, Any],
) -> ChannelSettings:
    enabled = payload.get("enabled", current.enabled)
    if not isinstance(enabled, bool):
        raise ValueError(f"{CHANNEL_NAMES[channel]}启用状态无效")

    clear = payload.get("clear", False)
    if not isinstance(clear, bool):
        raise ValueError("清除配置标记无效")

    webhook_input = payload.get("webhook_url", "")
    if not isinstance(webhook_input, str):
        raise ValueError(f"{CHANNEL_NAMES[channel]} Webhook 地址无效")
    webhook = "" if clear else webhook_input.strip() or current.webhook_url
    webhook = _validated_webhook(channel, webhook)

    secret = ""
    if channel == "dingtalk" and not clear:
        secret_input = payload.get("secret", "")
        if not isinstance(secret_input, str):
            raise ValueError("钉钉加签密钥无效")
        if len(secret_input) > 512:
            raise ValueError("钉钉加签密钥过长")
        clear_secret = payload.get("clear_secret", False)
        if not isinstance(clear_secret, bool):
            raise ValueError("清除钉钉加签密钥标记无效")
        secret = "" if clear_secret else secret_input.strip() or current.secret

    if enabled and not webhook:
        raise ValueError(f"请先填写{CHANNEL_NAMES[channel]} Webhook 地址")
    return ChannelSettings(enabled=enabled, webhook_url=webhook, secret=secret)


class NotificationConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._settings = NotificationSettings()
        self._load()

    def snapshot(self) -> NotificationSettings:
        with self._lock:
            return self._settings

    def update(self, payload: dict[str, Any]) -> NotificationSettings:
        if not isinstance(payload, dict):
            raise ValueError("通知配置格式无效")
        with self._lock:
            current = self._settings
            wecom_payload = payload.get("wecom", {})
            dingtalk_payload = payload.get("dingtalk", {})
            if not isinstance(wecom_payload, dict) or not isinstance(dingtalk_payload, dict):
                raise ValueError("通知配置格式无效")

            updated = NotificationSettings(
                wecom=_channel_from_payload("wecom", current.wecom, wecom_payload),
                dingtalk=_channel_from_payload(
                    "dingtalk", current.dingtalk, dingtalk_payload
                ),
            )
            self._save(updated)
            self._settings = updated
            return updated

    def clear(self, channel: str) -> NotificationSettings:
        if channel not in CHANNEL_NAMES:
            raise ValueError("不支持的通知渠道")
        payload = {
            channel: {"enabled": False, "clear": True},
        }
        return self.update(payload)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("通知配置格式无效")
            self._settings = NotificationSettings(
                wecom=self._load_channel("wecom", payload.get("wecom", {})),
                dingtalk=self._load_channel("dingtalk", payload.get("dingtalk", {})),
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            LOGGER.warning("通知配置无法读取，将使用关闭状态: %s", exc)
            self._settings = NotificationSettings()

    @staticmethod
    def _load_channel(channel: str, payload: Any) -> ChannelSettings:
        if not isinstance(payload, dict):
            raise ValueError("通知配置格式无效")
        return _channel_from_payload(channel, ChannelSettings(), payload)

    def _save(self, settings: NotificationSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "wecom": {
                "enabled": settings.wecom.enabled,
                "webhook_url": settings.wecom.webhook_url,
            },
            "dingtalk": {
                "enabled": settings.dingtalk.enabled,
                "webhook_url": settings.dingtalk.webhook_url,
                "secret": settings.dingtalk.secret,
            },
            "updated_at": iso_now(),
        }
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)


def _escape_markdown(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\")
    for character in "`*_[]()#!|>":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def build_webhook_payload(
    channel: str,
    notification: ListingNotification | None = None,
) -> dict[str, Any]:
    if notification is None:
        if channel == "wecom":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "content": "## 闲鱼新货雷达\n> 企业微信通知测试成功",
                },
            }
        if channel == "dingtalk":
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": "闲鱼新货雷达",
                    "text": "### 闲鱼新货雷达\n\n钉钉通知测试成功",
                },
            }
        raise ValueError("不支持的通知渠道")

    title = _escape_markdown(notification.title)
    keyword = _escape_markdown(notification.keyword)
    price = f"¥{notification.price:.2f}"
    threshold = f"¥{notification.max_price:g}"
    link = notification.url
    if channel == "wecom":
        content = (
            "## 闲鱼新商品提醒\n"
            f"> 关键词：`{keyword}`\n"
            f"> 价格：<font color=\"warning\">{price}</font>（低于 {threshold}）\n"
            f"> 商品：{title}\n\n"
            f"[查看闲鱼商品]({link})"
        )
        return {"msgtype": "markdown", "markdown": {"content": content}}
    if channel == "dingtalk":
        text = (
            "### 闲鱼新商品提醒\n\n"
            f"- 关键词：{keyword}\n"
            f"- 价格：**{price}**（低于 {threshold}）\n"
            f"- 商品：{title}\n\n"
            f"[查看闲鱼商品]({link})"
        )
        return {
            "msgtype": "markdown",
            "markdown": {"title": "闲鱼新商品提醒", "text": text},
        }
    raise ValueError("不支持的通知渠道")


def build_dingtalk_url(
    webhook_url: str,
    secret: str,
    timestamp_ms: int | None = None,
) -> str:
    if not secret:
        return webhook_url
    timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}\n{secret}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    parsed = urlsplit(webhook_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"timestamp", "sign"}
    ]
    query.extend((("timestamp", str(timestamp)), ("sign", signature)))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def post_json(url: str, payload: dict[str, Any], timeout: float = 12.0) -> None:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65_536)
    except HTTPError as exc:
        raise WebhookDeliveryError(f"通知服务返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise WebhookDeliveryError("无法连接通知服务，请检查网络或代理") from None

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WebhookDeliveryError("通知服务返回内容无法识别") from None
    if not isinstance(result, dict) or result.get("errcode") != 0:
        error_code = result.get("errcode", "unknown") if isinstance(result, dict) else "unknown"
        raise WebhookDeliveryError(f"通知服务拒绝发送（错误码 {error_code}）")


class NotificationManager:
    def __init__(
        self,
        config_path: Path,
        transport: Callable[[str, dict[str, Any]], None] = post_json,
        result_callback: Callable[[str, bool, str], None] | None = None,
    ) -> None:
        self.store = NotificationConfigStore(config_path)
        self.transport = transport
        self.result_callback = result_callback
        self._queue: queue.Queue[tuple[str, ChannelSettings, ListingNotification]] = (
            queue.Queue(maxsize=100)
        )
        self._status_lock = threading.RLock()
        self._delivery_status: dict[str, dict[str, str | None]] = {
            channel: {"last_result": None, "last_result_at": None, "last_error": None}
            for channel in CHANNEL_NAMES
        }
        threading.Thread(
            target=self._worker,
            name="xianyu-webhook-notifier",
            daemon=True,
        ).start()

    def public_snapshot(self) -> dict[str, Any]:
        settings = self.store.snapshot()
        with self._status_lock:
            return {
                "wecom": self._public_channel("wecom", settings.wecom),
                "dingtalk": self._public_channel("dingtalk", settings.dingtalk),
            }

    def _public_channel(
        self,
        channel: str,
        settings: ChannelSettings,
    ) -> dict[str, Any]:
        delivery = dict(self._delivery_status[channel])
        return {
            "enabled": settings.enabled,
            "configured": bool(settings.webhook_url),
            "secret_configured": bool(settings.secret) if channel == "dingtalk" else False,
            **delivery,
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.update(payload)
        return self.public_snapshot()

    def clear(self, channel: str) -> dict[str, Any]:
        self.store.clear(channel)
        with self._status_lock:
            self._delivery_status[channel] = {
                "last_result": None,
                "last_result_at": None,
                "last_error": None,
            }
        return self.public_snapshot()

    def test(self, channel: str, payload: dict[str, Any]) -> str:
        if channel not in CHANNEL_NAMES:
            raise ValueError("不支持的通知渠道")
        current = getattr(self.store.snapshot(), channel)
        test_settings = _channel_from_payload(
            channel,
            current,
            {
                "enabled": True,
                "webhook_url": payload.get("webhook_url", ""),
                "secret": payload.get("secret", ""),
                "clear_secret": payload.get("clear_secret", False),
            },
        )
        try:
            self._deliver(channel, test_settings, None)
        except WebhookDeliveryError as exc:
            self._record_result(channel, False, str(exc))
            raise
        self._record_result(channel, True, "测试消息已发送")
        return f"{CHANNEL_NAMES[channel]}测试消息已发送"

    def notify_listing(self, notification: ListingNotification) -> None:
        settings = self.store.snapshot()
        for channel in CHANNEL_NAMES:
            channel_settings = getattr(settings, channel)
            if not channel_settings.enabled or not channel_settings.webhook_url:
                continue
            try:
                self._queue.put_nowait((channel, channel_settings, notification))
            except queue.Full:
                self._record_result(channel, False, "通知队列已满，本条消息未发送")

    def _worker(self) -> None:
        while True:
            channel, settings, notification = self._queue.get()
            try:
                self._deliver(channel, settings, notification)
            except WebhookDeliveryError as exc:
                self._record_result(channel, False, str(exc))
            except Exception:
                LOGGER.exception("通知发送线程异常")
                self._record_result(channel, False, "通知发送失败")
            else:
                self._record_result(channel, True, "商品提醒已发送")
            finally:
                self._queue.task_done()

    def _deliver(
        self,
        channel: str,
        settings: ChannelSettings,
        notification: ListingNotification | None,
    ) -> None:
        url = settings.webhook_url
        if channel == "dingtalk":
            url = build_dingtalk_url(url, settings.secret)
        self.transport(url, build_webhook_payload(channel, notification))

    def _record_result(self, channel: str, success: bool, message: str) -> None:
        with self._status_lock:
            self._delivery_status[channel] = {
                "last_result": "success" if success else "error",
                "last_result_at": iso_now(),
                "last_error": None if success else message,
            }
        if self.result_callback:
            self.result_callback(channel, success, message)
