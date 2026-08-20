const state = {
  initialized: false,
  alertIds: new Set(),
  currentKeyword: null,
  lastSnapshot: null,
  toastTimer: null,
};

const elements = {
  form: document.querySelector("#controlForm"),
  keyword: document.querySelector("#keyword"),
  maxPrice: document.querySelector("#maxPrice"),
  interval: document.querySelector("#interval"),
  popupEnabled: document.querySelector("#popupEnabled"),
  startButton: document.querySelector("#startButton"),
  stopButton: document.querySelector("#stopButton"),
  scanButton: document.querySelector("#scanButton"),
  clearButton: document.querySelector("#clearButton"),
  topStatus: document.querySelector("#topStatus"),
  statusDot: document.querySelector("#statusDot"),
  scanBand: document.querySelector("#scanBand"),
  lastScan: document.querySelector("#lastScan"),
  nextScan: document.querySelector("#nextScan"),
  itemCount: document.querySelector("#itemCount"),
  knownCount: document.querySelector("#knownCount"),
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

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function configFromForm() {
  return {
    keyword: elements.keyword.value.trim(),
    max_price: Number(elements.maxPrice.value),
    interval: Number(elements.interval.value),
    popup_enabled: elements.popupEnabled.checked,
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

function render(snapshot) {
  state.lastSnapshot = snapshot;
  const running = snapshot.running;
  const scanning = snapshot.scanning;
  const keywordChanged = state.currentKeyword !== snapshot.config.keyword;

  elements.topStatus.textContent = snapshot.status_text;
  elements.statusDot.className = `status-dot ${snapshot.error ? "error" : scanning ? "scanning" : running ? "running" : ""}`;
  elements.scanBand.classList.toggle("active", scanning);
  elements.startButton.disabled = running;
  elements.stopButton.disabled = !running;
  elements.scanButton.disabled = !running || scanning;
  elements.keyword.disabled = running;
  elements.maxPrice.disabled = running;
  elements.interval.disabled = running;
  elements.popupEnabled.disabled = running;

  if (!state.initialized || !running) {
    elements.keyword.value = snapshot.config.keyword;
    elements.maxPrice.value = snapshot.config.max_price;
    elements.interval.value = String(snapshot.config.interval);
    elements.popupEnabled.checked = snapshot.config.popup_enabled;
  }

  elements.priceTitle.textContent = `¥${Number(snapshot.config.max_price).toLocaleString("zh-CN")}`;
  elements.keywordTitle.textContent = snapshot.config.keyword;
  elements.lastScan.textContent = formatClock(snapshot.last_scan_at);
  elements.nextScan.textContent = scanning ? "扫描中" : formatClock(snapshot.next_scan_at);
  elements.itemCount.textContent = snapshot.items_last_scan;
  elements.knownCount.textContent = snapshot.known_count;
  elements.alertCount.textContent = `${snapshot.alerts.length} 条`;

  elements.errorBanner.hidden = !snapshot.error;
  elements.errorBanner.textContent = snapshot.error || "";

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
    elements.topStatus.textContent = "控制服务未连接";
    elements.statusDot.className = "status-dot error";
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    render(await api("/api/start", { method: "POST", body: JSON.stringify(configFromForm()) }));
    showToast("监控已启动");
  } catch (error) {
    showToast(error.message);
  }
});

elements.stopButton.addEventListener("click", async () => {
  try {
    render(await api("/api/stop", { method: "POST", body: "{}" }));
  } catch (error) {
    showToast(error.message);
  }
});

elements.scanButton.addEventListener("click", async () => {
  try {
    render(await api("/api/scan", { method: "POST", body: "{}" }));
    showToast("已安排立即扫描");
  } catch (error) {
    showToast(error.message);
  }
});

elements.clearButton.addEventListener("click", async () => {
  try {
    render(await api("/api/alerts/clear", { method: "POST", body: "{}" }));
    showToast("推送记录已清空");
  } catch (error) {
    showToast(error.message);
  }
});

refresh();
window.setInterval(refresh, 2000);
