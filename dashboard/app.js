const state = {
  initialized: false,
  formDirty: false,
  keywordUpdating: false,
  intervalUpdating: false,
  popupUpdating: false,
  clientError: null,
  connectionError: null,
  alertIds: new Set(),
  currentKeyword: null,
  lastSnapshot: null,
  toastTimer: null,
};

const elements = {
  form: document.querySelector("#controlForm"),
  keyword: document.querySelector("#keyword"),
  keywordSearchButton: document.querySelector("#keywordSearchButton"),
  browserModes: [...document.querySelectorAll('input[name="browser"]')],
  maxPrice: document.querySelector("#maxPrice"),
  interval: document.querySelector("#interval"),
  intervalModes: [...document.querySelectorAll('input[name="interval_mode"]')],
  accessModes: [...document.querySelectorAll('input[name="access_mode"]')],
  popupEnabled: document.querySelector("#popupEnabled"),
  notificationSettingsButton: document.querySelector("#notificationSettingsButton"),
  notificationDialog: document.querySelector("#notificationDialog"),
  notificationForm: document.querySelector("#notificationForm"),
  notificationCloseButton: document.querySelector("#notificationCloseButton"),
  notificationCancelButton: document.querySelector("#notificationCancelButton"),
  notificationSaveButton: document.querySelector("#notificationSaveButton"),
  wecomEnabled: document.querySelector("#wecomEnabled"),
  wecomWebhook: document.querySelector("#wecomWebhook"),
  wecomTestButton: document.querySelector("#wecomTestButton"),
  wecomClearButton: document.querySelector("#wecomClearButton"),
  wecomStatusDot: document.querySelector("#wecomStatusDot"),
  wecomStatusText: document.querySelector("#wecomStatusText"),
  wecomDialogStatus: document.querySelector("#wecomDialogStatus"),
  dingtalkEnabled: document.querySelector("#dingtalkEnabled"),
  dingtalkWebhook: document.querySelector("#dingtalkWebhook"),
  dingtalkSecret: document.querySelector("#dingtalkSecret"),
  dingtalkTestButton: document.querySelector("#dingtalkTestButton"),
  dingtalkClearButton: document.querySelector("#dingtalkClearButton"),
  dingtalkStatusDot: document.querySelector("#dingtalkStatusDot"),
  dingtalkStatusText: document.querySelector("#dingtalkStatusText"),
  dingtalkDialogStatus: document.querySelector("#dingtalkDialogStatus"),
  startButton: document.querySelector("#startButton"),
  startButtonLabel: document.querySelector("#startButtonLabel"),
  stopButton: document.querySelector("#stopButton"),
  stopButtonLabel: document.querySelector("#stopButtonLabel"),
  scanButton: document.querySelector("#scanButton"),
  clearButton: document.querySelector("#clearButton"),
  topStatus: document.querySelector("#topStatus"),
  statusDot: document.querySelector("#statusDot"),
  scanBand: document.querySelector("#scanBand"),
  lastScan: document.querySelector("#lastScan"),
  nextScan: document.querySelector("#nextScan"),
  itemCount: document.querySelector("#itemCount"),
  knownCount: document.querySelector("#knownCount"),
  sessionUptime: document.querySelector("#sessionUptime"),
  requestCount: document.querySelector("#requestCount"),
  safetyPauseCount: document.querySelector("#safetyPauseCount"),
  alertCount: document.querySelector("#alertCount"),
  priceTitle: document.querySelector("#priceTitle"),
  keywordTitle: document.querySelector("#keywordTitle"),
  feedGrid: document.querySelector("#feedGrid"),
  emptyState: document.querySelector("#emptyState"),
  emptyStatus: document.querySelector("#emptyStatus"),
  errorBanner: document.querySelector("#errorBanner"),
  activityList: document.querySelector("#activityList"),
  toast: document.querySelector("#toast"),
};

const OUTDATED_SERVICE_MESSAGE = "控制服务仍是旧版本，请关闭当前启动窗口后重新运行 start_dashboard.cmd";
const LIVE_CONTROL_PATHS = new Set(["/api/interval", "/api/popup", "/api/keyword"]);

function requestError(message, path, status = null) {
  const error = new Error(message);
  error.path = path;
  error.status = status;
  return error;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch (error) {
    throw requestError(`无法连接控制服务：${error.message}`, path);
  }

  const responseText = await response.text();
  let payload = {};
  if (responseText) {
    try {
      payload = JSON.parse(responseText);
    } catch (error) {
      const message = LIVE_CONTROL_PATHS.has(path) && response.status === 404
        ? OUTDATED_SERVICE_MESSAGE
        : `控制服务返回了无法识别的内容 (${response.status})`;
      throw requestError(message, path, response.status);
    }
  }
  if (!response.ok) {
    throw requestError(payload.error || `请求失败 (${response.status})`, path, response.status);
  }
  return payload;
}

function hasLiveControlCapabilities(snapshot) {
  return Boolean(
    snapshot?.capabilities?.live_interval
    && snapshot?.capabilities?.interval_cycle
    && snapshot?.capabilities?.live_popup
    && snapshot?.capabilities?.live_keyword
    && snapshot?.capabilities?.browser_selection
    && snapshot?.capabilities?.file_logging
    && snapshot?.capabilities?.long_session_metrics,
  );
}

function updateErrorBanner(snapshot = state.lastSnapshot) {
  const compatibilityError = snapshot && !hasLiveControlCapabilities(snapshot)
    ? OUTDATED_SERVICE_MESSAGE
    : null;
  const messages = [
    state.connectionError,
    compatibilityError,
    snapshot?.error,
    state.clientError,
  ].filter(Boolean);
  const uniqueMessages = [...new Set(messages)];
  elements.errorBanner.hidden = uniqueMessages.length === 0;
  if (!uniqueMessages.length) {
    elements.errorBanner.textContent = "";
    return;
  }
  const logFile = snapshot?.diagnostics?.log_file || "dashboard_debug.log（重启后台后生成）";
  elements.errorBanner.textContent = `${uniqueMessages.join("\n")}\n详细日志：${logFile}`;
}

function sendClientDiagnostic(action, error) {
  void fetch("/api/client-log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      message: error.message || String(error),
      path: error.path || "unknown",
      status: error.status ?? "-",
    }),
    keepalive: true,
  }).catch(() => {});
}

function reportClientError(action, error) {
  state.clientError = `${action}失败：${error.message || String(error)}`;
  console.error(`[闲鱼监控] ${state.clientError}`, error);
  updateErrorBanner();
  showToast(state.clientError);
  sendClientDiagnostic(action, error);
}

function configFromForm() {
  return {
    keyword: elements.keyword.value.trim(),
    max_price: Number(elements.maxPrice.value),
    interval: Number(elements.interval.value),
    interval_cycle_enabled: elements.intervalModes.some((input) => input.checked && input.value === "cycle"),
    popup_enabled: elements.popupEnabled.checked,
    access_mode: elements.accessModes.find((input) => input.checked)?.value || "login",
    browser: elements.browserModes.find((input) => input.checked)?.value || "edge",
  };
}

function formatClock(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatInterval(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return seconds % 60 === 0 ? `${seconds / 60}分` : `${seconds}秒`;
}

function formatSessionUptime(seconds, startedAt) {
  if (!startedAt) return "--";
  const totalSeconds = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) return `${hours}时 ${minutes}分`;
  if (minutes > 0) return `${minutes}分 ${remainingSeconds}秒`;
  return `${remainingSeconds}秒`;
}

function formatFoundAt(value) {
  if (!value) return "刚刚发现";
  return `${formatClock(value)} 发现`;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function channelStatus(channel = {}) {
  if (channel.enabled && channel.last_result === "error") return "发送失败";
  if (channel.enabled) return "已启用";
  if (channel.configured) return "已关闭";
  return "未配置";
}

function renderNotificationChannel(name, channel = {}) {
  const status = channelStatus(channel);
  const dot = elements[`${name}StatusDot`];
  const summary = elements[`${name}StatusText`];
  const dialogStatus = elements[`${name}DialogStatus`];
  const clearButton = elements[`${name}ClearButton`];

  dot.className = `route-dot ${channel.enabled ? "active" : channel.configured ? "ready" : ""} ${channel.enabled && channel.last_result === "error" ? "error" : ""}`;
  summary.textContent = status;
  dialogStatus.textContent = channel.enabled && channel.last_error
    ? channel.last_error
    : status;
  dialogStatus.classList.toggle("error", channel.enabled && channel.last_result === "error");
  clearButton.disabled = !channel.configured;
}

function renderNotifications(notifications = {}) {
  renderNotificationChannel("wecom", notifications.wecom);
  renderNotificationChannel("dingtalk", notifications.dingtalk);
}

function openNotificationDialog() {
  const notifications = state.lastSnapshot?.notifications || {};
  const wecom = notifications.wecom || {};
  const dingtalk = notifications.dingtalk || {};

  elements.wecomEnabled.checked = Boolean(wecom.enabled);
  elements.dingtalkEnabled.checked = Boolean(dingtalk.enabled);
  elements.wecomWebhook.value = "";
  elements.dingtalkWebhook.value = "";
  elements.dingtalkSecret.value = "";
  elements.wecomWebhook.placeholder = wecom.configured ? "已配置" : "";
  elements.dingtalkWebhook.placeholder = dingtalk.configured ? "已配置" : "";
  elements.dingtalkSecret.placeholder = dingtalk.secret_configured ? "已配置" : "";
  elements.notificationDialog.showModal();
}

function notificationPayloadFromForm() {
  return {
    wecom: {
      enabled: elements.wecomEnabled.checked,
      webhook_url: elements.wecomWebhook.value.trim(),
    },
    dingtalk: {
      enabled: elements.dingtalkEnabled.checked,
      webhook_url: elements.dingtalkWebhook.value.trim(),
      secret: elements.dingtalkSecret.value.trim(),
    },
  };
}

async function testNotification(channel) {
  const button = elements[`${channel}TestButton`];
  const payload = notificationPayloadFromForm()[channel];
  button.disabled = true;
  try {
    const response = await api("/api/notifications/test", {
      method: "POST",
      body: JSON.stringify({ channel, ...payload }),
    });
    state.clientError = null;
    render(response.snapshot);
    showToast(response.message);
  } catch (error) {
    reportClientError(`${channel === "wecom" ? "企业微信" : "钉钉"}通知测试`, error);
  } finally {
    button.disabled = false;
  }
}

async function clearNotification(channel) {
  const channelName = channel === "wecom" ? "企业微信" : "钉钉";
  if (!window.confirm(`移除${channelName}通知配置？`)) return;
  try {
    const snapshot = await api("/api/notifications/clear", {
      method: "POST",
      body: JSON.stringify({ channel }),
    });
    state.clientError = null;
    render(snapshot);
    elements[`${channel}Enabled`].checked = false;
    elements[`${channel}Webhook`].value = "";
    elements[`${channel}Webhook`].placeholder = "";
    if (channel === "dingtalk") {
      elements.dingtalkSecret.value = "";
      elements.dingtalkSecret.placeholder = "";
    }
    showToast(`${channelName}通知配置已移除`);
  } catch (error) {
    reportClientError(`移除${channelName}通知配置`, error);
  }
}

function render(snapshot) {
  state.lastSnapshot = snapshot;
  state.connectionError = null;
  const running = snapshot.running;
  const scanning = snapshot.scanning;
  const verificationPaused = snapshot.status === "safety_stopped";
  const liveControlsAvailable = hasLiveControlCapabilities(snapshot);
  const keywordChanged = state.currentKeyword !== snapshot.config.keyword;

  elements.topStatus.textContent = snapshot.status_text;
  elements.statusDot.className = `status-dot ${snapshot.error ? "error" : scanning ? "scanning" : running ? "running" : ""}`;
  elements.scanBand.classList.toggle("active", scanning);
  elements.startButton.disabled = running;
  elements.startButtonLabel.textContent = verificationPaused ? "继续监控" : "启动监控";
  elements.stopButton.disabled = !running && !verificationPaused;
  elements.stopButtonLabel.textContent = verificationPaused ? "关闭浏览器" : "暂停";
  elements.scanButton.disabled = !running || scanning;
  elements.keyword.disabled = verificationPaused
    || state.keywordUpdating
    || (running && !liveControlsAvailable);
  elements.keywordSearchButton.disabled = !running
    || verificationPaused
    || state.keywordUpdating
    || !liveControlsAvailable;
  elements.keywordSearchButton.textContent = state.keywordUpdating ? "检索中" : "检索";
  elements.maxPrice.disabled = running || verificationPaused;
  elements.accessModes.forEach((input) => { input.disabled = running || verificationPaused; });
  elements.browserModes.forEach((input) => {
    input.disabled = running || verificationPaused || !snapshot?.capabilities?.browser_selection;
  });
  elements.popupEnabled.disabled = state.popupUpdating || (running && !liveControlsAvailable);

  if (!state.initialized || !state.formDirty) {
    elements.keyword.value = snapshot.config.keyword;
    elements.maxPrice.value = snapshot.config.max_price;
    elements.interval.value = String(snapshot.config.interval);
    const intervalMode = snapshot.config.interval_cycle_enabled ? "cycle" : "fixed";
    elements.intervalModes.forEach((input) => { input.checked = input.value === intervalMode; });
    const accessMode = snapshot.config.access_mode || "login";
    elements.accessModes.forEach((input) => { input.checked = input.value === accessMode; });
    const browser = snapshot.config.browser || "edge";
    elements.browserModes.forEach((input) => { input.checked = input.value === browser; });
  }
  const intervalCycleEnabled = elements.intervalModes.some(
    (input) => input.checked && input.value === "cycle",
  );
  elements.interval.disabled = state.intervalUpdating
    || intervalCycleEnabled
    || (running && !liveControlsAvailable);
  elements.intervalModes.forEach((input) => {
    input.disabled = state.intervalUpdating || (running && !liveControlsAvailable);
  });
  if (!state.popupUpdating) {
    elements.popupEnabled.checked = snapshot.config.popup_enabled;
  }

  elements.priceTitle.textContent = `¥${Number(snapshot.config.max_price).toLocaleString("zh-CN")}`;
  elements.keywordTitle.textContent = snapshot.config.keyword;
  elements.lastScan.textContent = formatClock(snapshot.last_scan_at);
  const scheduledInterval = formatInterval(snapshot.scheduled_interval_seconds);
  const nextScanTime = formatClock(snapshot.next_scan_at);
  elements.nextScan.textContent = scanning
    ? "扫描中"
    : scheduledInterval && nextScanTime !== "--"
      ? `${nextScanTime} · ${scheduledInterval}`
      : nextScanTime;
  elements.itemCount.textContent = snapshot.items_last_scan;
  elements.knownCount.textContent = snapshot.known_count;
  const session = snapshot.session || {};
  elements.sessionUptime.textContent = formatSessionUptime(
    session.uptime_seconds,
    session.started_at,
  );
  elements.requestCount.textContent = Number(
    session.page_request_count || 0,
  ).toLocaleString("zh-CN");
  elements.safetyPauseCount.textContent = `${Number(session.safety_pause_count || 0)} 次`;
  elements.alertCount.textContent = `${snapshot.alerts.length} 条`;
  renderNotifications(snapshot.notifications);

  updateErrorBanner(snapshot);

  renderAlerts(snapshot.alerts);
  renderLogs(snapshot.logs);

  elements.emptyStatus.textContent = snapshot.error
    ? snapshot.status === "safety_stopped"
      ? "处理上方提示后再手动启动"
      : "检查运行记录或闲鱼登录窗口"
    : !snapshot.baseline_ready
      ? "尚未建立基线"
      : running
        ? "监控运行中"
        : "启动监控后继续等待";

  if (state.initialized && !keywordChanged) {
    const newestAlert = snapshot.alerts.find((alert) => !state.alertIds.has(alert.item_id));
    if (newestAlert) showToast(`发现新商品：¥${newestAlert.price} ${newestAlert.title}`);
  }
  state.alertIds = new Set(snapshot.alerts.map((alert) => alert.item_id));
  state.currentKeyword = snapshot.config.keyword;
  state.initialized = true;
}

function renderAlerts(alerts) {
  elements.emptyState.hidden = alerts.length > 0;
  elements.feedGrid.innerHTML = alerts.map((alert) => {
    const imageUrl = safeUrl(alert.image_url);
    const itemUrl = safeUrl(alert.url);
    const image = imageUrl
      ? `<img class="product-image" src="${escapeHtml(imageUrl)}" alt="" loading="lazy">`
      : '<div class="product-image placeholder">暂无图片</div>';
    const link = itemUrl
      ? `<a class="product-link" href="${escapeHtml(itemUrl)}" target="_blank" rel="noreferrer">查看商品 ↗</a>`
      : "";
    return `
      <article class="product-card">
        ${image}
        <div class="product-body">
          <div class="product-topline">
            <span class="product-price"><small>¥</small>${Number(alert.price).toLocaleString("zh-CN")}</span>
            <span class="product-time">${escapeHtml(alert.published_label || "新发布")}</span>
          </div>
          <h2>${escapeHtml(alert.title)}</h2>
          <div class="product-footer">
            <span class="found-at">${formatFoundAt(alert.found_at)}</span>
            ${link}
          </div>
        </div>
      </article>`;
  }).join("");
}

function renderLogs(logs) {
  elements.activityList.innerHTML = logs.slice(0, 12).map((entry) => `
    <li class="${escapeHtml(entry.level)}">
      <time>${formatClock(entry.time)}</time>${escapeHtml(entry.message)}
    </li>`).join("");
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => elements.toast.classList.remove("show"), 3500);
}

async function refresh() {
  try {
    render(await api("/api/status"));
  } catch (error) {
    const message = `控制服务未连接：${error.message || String(error)}`;
    if (state.connectionError !== message) {
      console.error(`[闲鱼监控] ${message}`, error);
      sendClientDiagnostic("刷新状态", error);
    }
    state.connectionError = message;
    elements.topStatus.textContent = "控制服务未连接";
    elements.statusDot.className = "status-dot error";
    updateErrorBanner();
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.lastSnapshot?.running) {
    await searchKeyword();
    return;
  }
  const wasPaused = state.lastSnapshot?.status === "safety_stopped";
  try {
    const snapshot = await api("/api/start", {
      method: "POST",
      body: JSON.stringify(configFromForm()),
    });
    state.formDirty = false;
    state.clientError = null;
    render(snapshot);
    showToast(wasPaused ? "监控已恢复" : "监控已启动");
  } catch (error) {
    reportClientError("启动监控", error);
  }
});

[elements.keyword, elements.maxPrice, elements.interval, ...elements.intervalModes, ...elements.accessModes, ...elements.browserModes].forEach((input) => {
  input.addEventListener("input", () => {
    state.formDirty = true;
  });
});

async function searchKeyword() {
  const keyword = elements.keyword.value.trim();
  if (!keyword) {
    elements.keyword.focus();
    showToast("请输入搜索关键词");
    return;
  }
  const previousKeyword = state.lastSnapshot?.config.keyword || "";
  state.keywordUpdating = true;
  render(state.lastSnapshot);
  try {
    const response = await api("/api/keyword", {
      method: "POST",
      body: JSON.stringify({ keyword }),
    });
    state.formDirty = false;
    state.keywordUpdating = false;
    state.clientError = null;
    render(response.snapshot);
    showToast(response.changed
      ? `已切换至“${response.snapshot.config.keyword}”并开始检索`
      : `正在检索“${response.snapshot.config.keyword}”`);
  } catch (error) {
    state.keywordUpdating = false;
    elements.keyword.value = previousKeyword;
    state.formDirty = false;
    render(state.lastSnapshot);
    reportClientError("修改搜索关键词", error);
  }
}

elements.keywordSearchButton.addEventListener("click", () => {
  void searchKeyword();
});

elements.keyword.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !state.lastSnapshot?.running) return;
  event.preventDefault();
  void searchKeyword();
});

async function saveIntervalSettings() {
  const interval = Number(elements.interval.value);
  const intervalCycleEnabled = elements.intervalModes.some(
    (input) => input.checked && input.value === "cycle",
  );
  const previousInterval = state.lastSnapshot.config.interval;
  const previousCycleEnabled = state.lastSnapshot.config.interval_cycle_enabled;
  state.intervalUpdating = true;
  elements.interval.disabled = true;
  elements.intervalModes.forEach((input) => { input.disabled = true; });
  try {
    const snapshot = await api("/api/interval", {
      method: "POST",
      body: JSON.stringify({
        interval,
        interval_cycle_enabled: intervalCycleEnabled,
      }),
    });
    state.formDirty = false;
    state.intervalUpdating = false;
    state.clientError = null;
    render(snapshot);
    showToast(snapshot.config.interval_cycle_enabled
      ? "已启用 1-5 分钟循环刷新"
      : `刷新间隔已更新为 ${snapshot.config.interval} 秒`);
  } catch (error) {
    state.formDirty = false;
    state.intervalUpdating = false;
    elements.interval.value = String(previousInterval);
    const previousMode = previousCycleEnabled ? "cycle" : "fixed";
    elements.intervalModes.forEach((input) => { input.checked = input.value === previousMode; });
    elements.interval.disabled = previousCycleEnabled;
    elements.intervalModes.forEach((input) => { input.disabled = false; });
    reportClientError("修改刷新间隔", error);
  }
}

elements.interval.addEventListener("change", async () => {
  if (!state.lastSnapshot?.running) return;
  await saveIntervalSettings();
});

elements.intervalModes.forEach((input) => {
  input.addEventListener("change", async () => {
    if (!input.checked) return;
    state.formDirty = true;
    if (!state.lastSnapshot?.running) {
      elements.interval.disabled = input.value === "cycle";
      return;
    }
    await saveIntervalSettings();
  });
});

elements.popupEnabled.addEventListener("change", async () => {
  const enabled = elements.popupEnabled.checked;
  const previous = state.lastSnapshot?.config.popup_enabled ?? !enabled;
  state.popupUpdating = true;
  elements.popupEnabled.disabled = true;
  try {
    const snapshot = await api("/api/popup", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    state.popupUpdating = false;
    state.clientError = null;
    render(snapshot);
    showToast(`系统文字弹窗已${enabled ? "开启" : "关闭"}`);
  } catch (error) {
    state.popupUpdating = false;
    elements.popupEnabled.checked = previous;
    elements.popupEnabled.disabled = false;
    reportClientError("修改系统文字弹窗", error);
  }
});

elements.stopButton.addEventListener("click", async () => {
  try {
    const snapshot = await api("/api/stop", { method: "POST", body: "{}" });
    state.clientError = null;
    render(snapshot);
  } catch (error) {
    reportClientError("停止监控", error);
  }
});

elements.scanButton.addEventListener("click", async () => {
  try {
    const snapshot = await api("/api/scan", { method: "POST", body: "{}" });
    state.clientError = null;
    render(snapshot);
    showToast("已安排立即扫描");
  } catch (error) {
    reportClientError("立即扫描", error);
  }
});

elements.clearButton.addEventListener("click", async () => {
  try {
    const snapshot = await api("/api/alerts/clear", { method: "POST", body: "{}" });
    state.clientError = null;
    render(snapshot);
    showToast("推送记录已清空");
  } catch (error) {
    reportClientError("清空推送记录", error);
  }
});

elements.notificationSettingsButton.addEventListener("click", openNotificationDialog);
elements.notificationCloseButton.addEventListener("click", () => elements.notificationDialog.close());
elements.notificationCancelButton.addEventListener("click", () => elements.notificationDialog.close());
elements.notificationDialog.addEventListener("click", (event) => {
  if (event.target === elements.notificationDialog) elements.notificationDialog.close();
});
elements.notificationDialog.addEventListener("close", () => {
  elements.wecomWebhook.value = "";
  elements.dingtalkWebhook.value = "";
  elements.dingtalkSecret.value = "";
});

elements.notificationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.notificationSaveButton.disabled = true;
  try {
    const snapshot = await api("/api/notifications/save", {
      method: "POST",
      body: JSON.stringify(notificationPayloadFromForm()),
    });
    state.clientError = null;
    render(snapshot);
    elements.notificationDialog.close();
    showToast("通知设置已保存");
  } catch (error) {
    reportClientError("保存通知设置", error);
  } finally {
    elements.notificationSaveButton.disabled = false;
  }
});

elements.wecomTestButton.addEventListener("click", () => testNotification("wecom"));
elements.dingtalkTestButton.addEventListener("click", () => testNotification("dingtalk"));
elements.wecomClearButton.addEventListener("click", () => clearNotification("wecom"));
elements.dingtalkClearButton.addEventListener("click", () => clearNotification("dingtalk"));

refresh();
window.setInterval(refresh, 2000);
