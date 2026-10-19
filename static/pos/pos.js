// Paprika cash register. Uses the same catalog, orders and printer as the kiosk.
const TOKEN_KEY = "paprika-pos-token";
const KIOSK_POLL_MS = 15000;
// How often the register checks the connection and sends sales made offline.
const CONNECTION_CHECK_MS = 10000;
const OFFLINE_DATA_REFRESH_MS = 5 * 60 * 1000;

const state = {
  token: readToken(),
  cashier: null,
  shift: null,
  currency: "с",
  menu: null,
  tab: "sale",
  category: "all",
  cart: [],
  serviceType: "dine_in",
  // Customer's wishes for the kitchen; printed on the kitchen ticket.
  comment: "",
  kioskOrders: [],
  kitchenOrders: [],
  history: [],
  // No connection to the server: sales are queued locally (see offline.js).
  offline: false,
  // The cashier's PIN, kept only in memory: after a connection loss the register gets a
  // fresh server session with it (the old one may be gone if the server restarted).
  offlinePin: "",
  syncError: "",
  offlineDataAt: 0,
};

const app = document.querySelector("#app");
const modalRoot = document.querySelector("#modalRoot");
const toast = document.querySelector("#toast");
let toastTimer;
let pollTimer;
let connectionTimer;

// --- helpers -------------------------------------------------------------------

function readToken() {
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function saveToken(token) {
  state.token = token;
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* Storage can be unavailable; the session then lasts until reload. */ }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function round2(value) {
  return Math.round((Number(value) + Number.EPSILON) * 100) / 100;
}

function money(amount) {
  return `<span class="money">${round2(amount).toLocaleString("ru-RU")} ${escapeHtml(state.currency)}</span>`;
}

function moneyText(amount) {
  return `${round2(amount).toLocaleString("ru-RU")} ${state.currency}`;
}

function formatGrams(grams) {
  // Weights are shown in kilograms: 200 -> "0,2 кг", 1052 -> "1,052 кг".
  return `${(grams / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 3 })} кг`;
}

// Price of a weighted portion: at least the minimum amount (0,2 kg costs 20, not 19).
function weightPrice(grams, pricing) {
  return Math.max(round2(grams * pricing.price_per_kg / 1000), pricing.min_amount);
}

function kgText(grams) {
  return (grams / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 3, useGrouping: false });
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(iso) {
  return iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3500);
}

function newKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function api(path, { method = "GET", body, headers = {} } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", "X-POS-Token": state.token, ...paprikaDevice.headers(), ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    setOffline(true);
    const error = new Error("Нет связи с сервером. Эта операция будет доступна, когда связь восстановится.");
    error.offline = true;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/pos/login") {
    if (!state.token && state.offlinePin) {
      // Logged in offline: the server session is created on the next connection check.
      throw new Error("Подключаемся к серверу… Повторите через несколько секунд.");
    }
    signOut(false);
    throw new Error(data.detail || "Сессия завершена. Войдите снова.");
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? "Проверьте введённые данные." : data.detail;
    throw new Error(detail || "Не удалось выполнить запрос.");
  }
  return data;
}

function closeModal() {
  modalRoot.innerHTML = "";
}

function openModal(html, { wide = false } = {}) {
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</section></div>`;
  return modalRoot.querySelector(".modal");
}

function confirmDialog(title, text, confirmLabel = "Да", danger = true) {
  return new Promise((resolve) => {
    const modal = openModal(`
      <div class="modal-head"><h3>${escapeHtml(title)}</h3></div>
      <p>${escapeHtml(text)}</p>
      <div class="modal-actions">
        <button class="btn" data-answer="no">Нет</button>
        <button class="btn ${danger ? "btn-primary" : "btn-dark"}" data-answer="yes">${escapeHtml(confirmLabel)}</button>
      </div>`);
    modal.querySelectorAll("[data-answer]").forEach((button) => button.addEventListener("click", () => {
      closeModal();
      resolve(button.dataset.answer === "yes");
    }));
  });
}

// A numeric keypad bound to a string value. `decimal` allows one comma.
function keypadHtml({ decimal = false } = {}) {
  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", decimal ? "," : "C", "0", "⌫"];
  return `<div class="keypad">${keys.map((key) => `<button type="button" data-key="${key}" class="${["C", "⌫", ","].includes(key) ? "key-secondary" : ""}">${key}</button>`).join("")}</div>`;
}

function applyKey(value, key, { maxLength = 9, decimal = false } = {}) {
  if (key === "⌫") return value.slice(0, -1);
  if (key === "C") return "";
  if (key === ",") {
    if (!decimal || value.includes(",")) return value;
    return (value || "0") + ",";
  }
  if (value.includes(",") && value.split(",")[1].length >= 2) return value;
  if (value.length >= maxLength) return value;
  if (value === "0") return key;
  return value + key;
}

function parseAmount(text) {
  return text ? Number(text.replace(",", ".")) : 0;
}

function bindKeypad(container, onKey) {
  container.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", () => onKey(button.dataset.key)));
}

// --- connection and offline mode ----------------------------------------------------

function setOffline(value) {
  if (state.offline === value) return;
  state.offline = value;
  updateNetStatus();
}

function updateNetStatus() {
  const badge = document.querySelector("#netStatus");
  if (!badge) return;
  const pending = offline.pendingSales().length;
  let text = "";
  let kind = "";
  if (state.offline) {
    text = pending ? `Нет связи · не отправлено продаж: ${pending}` : "Нет связи · работа офлайн";
    kind = "offline";
  } else if (state.syncError) {
    text = `Не отправлено продаж: ${pending} — ${state.syncError}`;
    kind = "error";
  } else if (pending) {
    text = `Отправка офлайн-продаж: ${pending}`;
  }
  badge.textContent = text;
  badge.className = `net-status ${kind}`;
  badge.hidden = !text;
}

async function refreshOfflineData(force = false) {
  if (state.offline || (!force && Date.now() - state.offlineDataAt < OFFLINE_DATA_REFRESH_MS)) return;
  try {
    const response = await fetch("/api/pos/offline-data", { headers: paprikaDevice.headers() });
    if (!response.ok) return;
    const data = await response.json();
    offline.saveData(data);
    state.offlineDataAt = Date.now();
    cacheMenuImages(data.menu);
  } catch { setOffline(true); }
}

// The shift known without the server: one opened offline, or the last one the server reported.
function offlineShift() {
  const opened = offline.ownOperations().find((operation) => operation.type === "open_shift");
  if (opened) return { id: null, offline: true, opened_at: opened.at, opening_cash: Number(opened.opening_cash), cashier_name: opened.cashier_name };
  return offline.usableData()?.shift || null;
}

function applyOfflineSession(cashier) {
  const data = offline.usableData();
  state.cashier = { id: cashier.id, name: cashier.name, active: cashier.active, can_view_revenue: cashier.can_view_revenue };
  state.store = data.store;
  state.register = data.register;
  state.currency = data.currency || "с";
  state.menu = data.menu;
  state.shift = offlineShift();
}

async function connectionTick() {
  let online = false;
  try {
    online = (await fetch("/api/health", { cache: "no-store" })).ok;
  } catch { /* still offline */ }
  if (!online) {
    setOffline(true);
    return;
  }
  const wasOffline = state.offline;
  setOffline(false);
  if (offline.outbox.length) {
    try {
      const result = await offline.sync();
      state.syncError = result.error || "";
      if (result.synced) showToast(`Офлайн-данные отправлены на сервер: ${result.synced}`);
    } catch {
      setOffline(true);
      return;
    }
  }
  if (state.cashier && (wasOffline || !state.token)) await reconnectSession();
  refreshOfflineData();
  updateNetStatus();
}

// Back online: get a server session for the cashier and fresh data for the screens.
async function reconnectSession() {
  try {
    if (state.offlinePin) {
      const session = await api("/api/pos/login", { method: "POST", body: { pin: state.offlinePin } });
      saveToken(session.token);
      applySession(session);
    } else if (state.token) {
      applySession(await api("/api/pos/session"));
    }
  } catch { return; }
  if (offline.outbox.length) return;
  if (state.shift) {
    state.menu = await api("/api/pos/menu").catch(() => state.menu);
    refreshOrders();
    if (!modalRoot.innerHTML && document.querySelector("#posBody") && state.tab !== "sale") renderMain();
  } else if (!modalRoot.innerHTML) {
    renderOpenShift();
  }
}

function startConnectionChecks() {
  window.clearInterval(connectionTimer);
  connectionTimer = window.setInterval(connectionTick, CONNECTION_CHECK_MS);
}

// --- session -------------------------------------------------------------------

function applySession(session) {
  state.cashier = session.cashier;
  state.shift = session.shift;
  state.currency = session.currency || "с";
  state.register = session.register;
  state.store = session.store;
  offline.rememberShift(session.shift);
}

function signOut(callServer = true) {
  if (callServer && state.token) api("/api/pos/logout", { method: "POST" }).catch(() => {});
  saveToken("");
  state.cashier = null;
  state.shift = null;
  state.offlinePin = "";
  state.cart = [];
  window.clearInterval(pollTimer);
  closeModal();
  renderLogin();
}

function showRegisterSetup(message) {
  showDeviceSetup(app, "Касса не активирована", message);
}

function registerServiceWorker() {
  navigator.serviceWorker?.register("/pos-sw.js", { scope: "/pos" }).catch(() => {
    /* Without it the register still works online; it only cannot open offline. */
  });
}

function cacheMenuImages(menu) {
  const urls = [...new Set((menu?.products || []).map((product) => product.image).filter(Boolean))];
  navigator.serviceWorker?.ready.then((registration) => registration.active?.postMessage({ type: "cache-images", urls }));
}

async function start() {
  registerServiceWorker();
  // The register is a device of a store: check its key before asking for a PIN.
  if (!paprikaDevice.key) {
    showRegisterSetup("Этот компьютер ещё не привязан к кассе точки продаж.");
    return;
  }
  try {
    const device = await fetch("/api/device", { headers: paprikaDevice.headers() });
    const info = await device.json().catch(() => ({}));
    if (device.status === 401 || device.status === 403 || (device.ok && info.kind !== "register")) {
      paprikaDevice.forget();
      showRegisterSetup(device.ok ? "Этот ключ не от кассы." : info.detail || "Ключ кассы не подходит.");
      return;
    }
    state.device = info;
    refreshOfflineData(true);
  } catch {
    // No connection: work with the data saved while online.
    setOffline(true);
    const data = offline.usableData();
    if (data) state.device = { store_name: data.store.name, name: data.register.name };
  }
  startConnectionChecks();
  if (!state.token) {
    renderLogin();
    return;
  }
  try {
    applySession(await api("/api/pos/session"));
    await enterApp();
  } catch {
    renderLogin();
  }
}

async function enterApp() {
  if (!state.shift) {
    renderOpenShift();
    return;
  }
  if (!state.menu) {
    try {
      state.menu = await api("/api/pos/menu");
    } catch (exc) {
      if (!exc.offline || !offline.usableData()) throw exc;
      state.menu = offline.usableData().menu;
    }
  }
  state.tab = "sale";
  renderMain();
  refreshOrders();
  window.clearInterval(pollTimer);
  pollTimer = window.setInterval(refreshOrders, KIOSK_POLL_MS);
}

function renderLogin() {
  let pin = "";
  app.innerHTML = `
    <div class="center-screen">
      <div class="login-card">
        <h1>Paprika · Касса</h1>
        <p>${state.device ? `${escapeHtml(state.device.store_name)} · ${escapeHtml(state.device.name)}<br>` : ""}Введите PIN-код кассира</p>
        ${state.offline ? `<p class="hint error">Нет связи с сервером — вход и продажи работают офлайн.</p>` : ""}
        <div class="pin-dots" id="pinDots"></div>
        ${keypadHtml()}
        <button class="btn btn-dark btn-block" id="loginButton" style="margin-top:12px">Войти</button>
        <p class="form-error" id="loginError"></p>
      </div>
    </div>`;
  const dots = app.querySelector("#pinDots");
  const error = app.querySelector("#loginError");
  const drawDots = () => {
    dots.innerHTML = Array.from({ length: Math.max(4, pin.length) }, (_, index) => `<span class="${index < pin.length ? "filled" : ""}"></span>`).join("");
  };
  const submit = async () => {
    if (pin.length < 4) {
      error.textContent = "PIN-код — от 4 до 6 цифр.";
      return;
    }
    error.textContent = "";
    try {
      const session = await api("/api/pos/login", { method: "POST", body: { pin } });
      saveToken(session.token);
      state.offlinePin = pin;
      applySession(session);
      await enterApp();
    } catch (exc) {
      if (exc.offline) {
        await offlineLogin(pin, error);
        pin = "";
        drawDots();
        return;
      }
      pin = "";
      drawDots();
      error.textContent = exc.message;
    }
  };
  bindKeypad(app, (key) => {
    pin = applyKey(pin, key, { maxLength: 6 });
    drawDots();
    if (pin.length === 6) submit();
  });
  app.querySelector("#loginButton").addEventListener("click", submit);
  drawDots();
}

async function offlineLogin(pin, error) {
  if (!offline.usableData()) {
    error.textContent = "Нет связи с сервером, а данных для работы офлайн на этом компьютере ещё нет. Подключитесь к интернету хотя бы один раз.";
    return;
  }
  error.textContent = "Проверяем PIN без связи…";
  const cashier = await offline.findCashier(pin);
  if (!cashier) {
    error.textContent = "Неверный PIN-код.";
    return;
  }
  error.textContent = "";
  // A token from before the connection loss is not trusted; a new one is taken when online.
  saveToken("");
  applyOfflineSession(cashier);
  state.offlinePin = pin;
  await enterApp();
}

function renderOpenShift() {
  let amount = "";
  app.innerHTML = `
    <div class="center-screen">
      <div class="login-card">
        <h1>Смена закрыта</h1>
        <p>Кассир: <strong>${escapeHtml(state.cashier.name)}</strong>. Сколько наличных в кассе на начало смены?</p>
        <div class="amount-display" id="openingCash"></div>
        ${keypadHtml({ decimal: true })}
        <button class="btn btn-primary btn-block" id="openShift" style="margin-top:12px">Открыть смену</button>
        <button class="btn btn-ghost btn-block" id="logout" style="margin-top:6px">Выйти</button>
        <p class="form-error" id="shiftError"></p>
      </div>
    </div>`;
  const display = app.querySelector("#openingCash");
  const draw = () => { display.textContent = `${amount || "0"} ${state.currency}`; };
  bindKeypad(app, (key) => { amount = applyKey(amount, key, { decimal: true }); draw(); });
  app.querySelector("#logout").addEventListener("click", () => signOut());
  const openOffline = async () => {
    const operation = {
      type: "open_shift", id: newKey(), at: new Date().toISOString(),
      cashier_id: state.cashier.id, opening_cash: String(parseAmount(amount)),
    };
    offline.enqueue({ ...operation, cashier_name: state.cashier.name });
    state.shift = offlineShift();
    showToast("Смена открыта без связи — она будет отправлена на сервер автоматически");
    await enterApp();
  };
  app.querySelector("#openShift").addEventListener("click", async () => {
    try {
      if (state.offline || !state.token) {
        await openOffline();
        return;
      }
      applySession(await api("/api/pos/shifts/open", { method: "POST", body: { opening_cash: String(parseAmount(amount)) } }));
      showToast("Смена открыта");
      await enterApp();
    } catch (exc) {
      if (exc.offline) await openOffline();
      else app.querySelector("#shiftError").textContent = exc.message;
    }
  });
  draw();
}

// --- main layout ----------------------------------------------------------------

const TABS = [
  { id: "sale", label: "Продажа" },
  { id: "kiosk", label: "Заказы киоска" },
  { id: "kitchen", label: "Очередь" },
  { id: "history", label: "Чеки" },
  { id: "shift", label: "Смена" },
];

function visibleTabs() {
  return TABS.filter((tab) => !tab.access || state.cashier[tab.access]);
}

function renderMain() {
  if (!visibleTabs().some((tab) => tab.id === state.tab)) state.tab = "sale";
  app.innerHTML = `
    <div class="pos">
      <header class="pos-header">
        <div class="pos-brand">Paprika<small>${escapeHtml(state.store?.name || "")} · ${escapeHtml(state.register?.name || "Касса")}</small></div>
        <nav class="pos-tabs">${visibleTabs().map((tab) => `
          <button class="pos-tab ${state.tab === tab.id ? "active" : ""}" data-tab="${tab.id}">${tab.label}${tabBadge(tab.id)}</button>`).join("")}
        </nav>
        <span class="net-status" id="netStatus" hidden></span>
        <div class="pos-user"><span>Кассир:</span> <strong>${escapeHtml(state.cashier.name)}</strong><button class="btn btn-small" id="logout">Выйти</button></div>
      </header>
      <main class="pos-body" id="posBody"></main>
    </div>`;
  app.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
    state.tab = button.dataset.tab;
    renderMain();
  }));
  app.querySelector("#logout").addEventListener("click", logOutAndCloseShift);
  const body = app.querySelector("#posBody");
  if (state.tab === "sale") renderSale(body);
  if (state.tab === "kiosk") renderKioskOrders(body);
  if (state.tab === "kitchen") renderKitchen(body);
  if (state.tab === "history") renderHistory(body);
  if (state.tab === "shift") renderShift(body);
  updateNetStatus();
}

// Logging out closes the open shift (without counting the cash and without showing the totals).
async function logOutAndCloseShift() {
  if (!state.shift) {
    signOut();
    return;
  }
  if (state.offline || !state.token || offline.ownOperations().length) {
    const leave = await confirmDialog(
      "Смена не будет закрыта",
      "Нет связи с сервером или есть неотправленные продажи. Смену можно закрыть, когда связь восстановится и продажи будут отправлены.",
      "Всё равно выйти",
    );
    if (leave) signOut();
    return;
  }
  if (!await confirmDialog("Выйти и закрыть смену?", "Смена будет закрыта автоматически.", "Закрыть смену и выйти", false)) return;
  try {
    await api("/api/pos/shifts/close", { method: "POST", body: { print_report: false } });
  } catch (exc) {
    showToast(exc.message, true);
    return;
  }
  window.clearInterval(pollTimer);
  state.shift = null;
  offline.rememberShift(null);
  showToast("Смена закрыта");
  signOut();
}

function preparingOrders() {
  return state.kitchenOrders.filter((order) => order.kitchen_status === "preparing");
}

function tabBadge(tabId) {
  const count = tabId === "kiosk" ? state.kioskOrders.length : tabId === "kitchen" ? preparingOrders().length : 0;
  return count ? `<span class="badge">${count}</span>` : "";
}

function updateBadges() {
  TABS.forEach((tab) => {
    const button = app.querySelector(`[data-tab="${tab.id}"]`);
    if (button) button.innerHTML = `${tab.label}${tabBadge(tab.id)}`;
  });
}

function refreshOrders() {
  refreshKioskOrders();
  refreshKitchen();
}

async function refreshKitchen() {
  if (!state.shift || state.offline || !state.token) return;
  try {
    state.kitchenOrders = await api("/api/pos/orders?scope=kitchen");
    updateBadges();
    if (state.tab === "kitchen" && !modalRoot.innerHTML) renderKitchen(app.querySelector("#posBody"));
  } catch { /* A failed poll is retried on the next tick. */ }
}

async function refreshKioskOrders() {
  if (!state.shift || state.offline || !state.token) return;
  try {
    const previous = state.kioskOrders.length;
    state.kioskOrders = await api("/api/pos/orders?scope=kiosk_unpaid");
    updateBadges();
    if (state.kioskOrders.length > previous && previous !== 0) showToast("Новый заказ из киоска");
    if (state.tab === "kiosk" && !modalRoot.innerHTML) renderKioskOrders(app.querySelector("#posBody"));
  } catch { /* A failed poll is retried on the next tick. */ }
}

// --- sale -------------------------------------------------------------------------

function cartSubtotal() {
  return round2(state.cart.reduce((sum, line) => sum + line.unitPrice * line.quantity, 0));
}

function lineDetail(line) {
  if (!line.portion) return "";
  return line.portion.kind === "weight"
    ? `${formatGrams(line.portion.grams)} · ${moneyText(line.portion.amount)}`
    : `на ${moneyText(line.portion.amount)} · ≈ ${formatGrams(line.portion.grams)}`;
}

function visibleProducts() {
  return state.menu.products.filter((product) => product.available
    && (state.category === "all" || product.category_id === state.category));
}

function renderSale(body) {
  const categories = [{ id: "all", name: "Все товары" }, ...state.menu.categories];
  body.innerHTML = `
    <div class="sale">
      <nav class="categories">${categories.map((category) => `
        <button class="category-button ${state.category === category.id ? "active" : ""}" data-category="${escapeHtml(category.id)}">${escapeHtml(category.name)}</button>`).join("")}
      </nav>
      <section class="catalog">
        <div class="product-grid" id="productGrid"></div>
      </section>
      <aside class="ticket" id="ticket"></aside>
    </div>`;
  body.querySelectorAll("[data-category]").forEach((button) => button.addEventListener("click", () => {
    state.category = button.dataset.category;
    body.querySelectorAll("[data-category]").forEach((entry) => entry.classList.toggle("active", entry === button));
    renderProducts();
  }));
  renderProducts();
  renderTicket();
}

function renderProducts() {
  const grid = document.querySelector("#productGrid");
  const products = visibleProducts();
  grid.innerHTML = products.map((product) => `
    <button class="product-tile" data-product="${escapeHtml(product.id)}">
      <img src="${escapeHtml(product.image)}" alt="" loading="lazy">
      <span class="tile-body">
        <span class="tile-name">${escapeHtml(product.name)}</span>
        <span class="tile-price">${money(product.price)}<small> / ${escapeHtml(product.unit)}</small></span>
      </span>
    </button>`).join("") || `<div class="empty">Ничего не найдено</div>`;
  grid.querySelectorAll("[data-product]").forEach((button) => button.addEventListener("click", () => {
    const product = state.menu.products.find((entry) => entry.id === button.dataset.product);
    if (product.weighted) openWeightedDialog(product);
    else addToCart(product, null);
  }));
}

function addToCart(product, portion) {
  const key = `${product.id}|${portion ? `${portion.kind}:${portion.value}` : ""}`;
  const existing = state.cart.find((line) => line.key === key);
  if (existing) existing.quantity += 1;
  else {
    state.cart.push({
      key, productId: product.id, name: product.name, quantity: 1,
      unitPrice: portion ? portion.amount : product.price, portion,
    });
  }
  renderTicket();
}

function renderTicket() {
  const ticket = document.querySelector("#ticket");
  if (!ticket) return;
  const total = cartSubtotal();
  ticket.innerHTML = `
    <div class="ticket-head"><h2>Чек</h2><span class="order-meta">${state.cart.reduce((sum, line) => sum + line.quantity, 0)} шт.</span></div>
    <div class="ticket-lines">${state.cart.map((line) => `
      <div class="ticket-line" data-line="${escapeHtml(line.key)}">
        <div><div class="line-name">${escapeHtml(line.name)}</div>${lineDetail(line) ? `<div class="line-detail">${escapeHtml(lineDetail(line))}</div>` : ""}</div>
        <div class="line-total">${money(line.unitPrice * line.quantity)}</div>
        <div class="qty">
          <button data-action="minus" aria-label="Меньше">−</button><span>${line.quantity}</span><button data-action="plus" aria-label="Больше">+</button>
          <button class="remove" data-action="remove">Удалить</button>
        </div>
      </div>`).join("") || `<div class="empty">Добавьте товары из каталога</div>`}
    </div>
    <div class="ticket-foot">
      <div class="sum-row total"><span>Итого</span><span>${money(total)}</span></div>
      ${state.comment ? `<div class="order-comment ticket-comment"><span>💬 ${escapeHtml(state.comment)}</span><button id="removeComment" aria-label="Удалить комментарий">×</button></div>` : ""}
      <div class="segmented">
        <button data-service="dine_in" class="${state.serviceType === "dine_in" ? "active" : ""}">В зале</button>
        <button data-service="takeaway" class="${state.serviceType === "takeaway" ? "active" : ""}">С собой</button>
      </div>
      <div class="ticket-actions">
        <button class="btn" id="commentButton" ${state.cart.length ? "" : "disabled"}>Комментарий</button>
        <button class="btn btn-primary pay-button" id="payButton" ${state.cart.length ? "" : "disabled"}>Оплатить</button>
      </div>
    </div>`;
  ticket.querySelectorAll("[data-line]").forEach((row) => row.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
    const line = state.cart.find((entry) => entry.key === row.dataset.line);
    if (button.dataset.action === "plus") line.quantity += 1;
    if (button.dataset.action === "minus") line.quantity -= 1;
    if (button.dataset.action === "remove" || line.quantity <= 0) state.cart = state.cart.filter((entry) => entry !== line);
    renderTicket();
  })));
  ticket.querySelectorAll("[data-service]").forEach((button) => button.addEventListener("click", () => {
    state.serviceType = button.dataset.service;
    renderTicket();
  }));
  ticket.querySelector("#commentButton").addEventListener("click", openCommentDialog);
  ticket.querySelector("#removeComment")?.addEventListener("click", () => {
    state.comment = "";
    renderTicket();
  });
  ticket.querySelector("#payButton").addEventListener("click", () => openPaymentDialog({ kind: "sale", total }));
}

function resetSale() {
  state.cart = [];
  state.comment = "";
  state.serviceType = "dine_in";
  renderTicket();
}

function openWeightedDialog(product) {
  const pricing = product.weighted;
  let mode = "weight";
  // The weight is entered in kilograms (e.g. "0,35"); the server gets grams.
  let value = kgText(pricing.default_grams);
  const modal = openModal(`
    <div class="modal-head"><h3>${escapeHtml(product.name)}</h3><button class="modal-close" data-close>×</button></div>
    <div class="segmented" style="margin-bottom:12px"><button data-mode="weight">Вес, кг</button><button data-mode="price">Сумма, ${escapeHtml(state.currency)}</button></div>
    <div class="preset-grid">${pricing.preset_grams.map((grams) => `<button data-grams="${grams}">${formatGrams(grams)}<small>${moneyText(weightPrice(grams, pricing))}</small></button>`).join("")}</div>
    <div class="amount-display" id="weightedValue"></div>
    <p class="hint" id="weightedHint"></p>
    ${keypadHtml({ decimal: true })}
    <div class="modal-actions"><button class="btn" data-close>Отмена</button><button class="btn btn-primary" id="addWeighted">Добавить</button></div>`);
  const portion = () => {
    if (!value || value.endsWith(",")) return null;
    if (mode === "weight") {
      const grams = Math.round(parseAmount(value) * 1000);
      if (grams < pricing.min_grams || grams > pricing.max_grams) return null;
      return { kind: "weight", value: grams, grams, amount: weightPrice(grams, pricing) };
    }
    const number = Number(value);
    if (!Number.isInteger(number)) return null;
    if (number < pricing.min_amount || number > pricing.max_amount) return null;
    return { kind: "price", value: number, amount: number, grams: Math.round(number * 1000 / pricing.price_per_kg) };
  };
  const draw = () => {
    const selected = portion();
    modal.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
    modal.querySelectorAll("[data-grams]").forEach((button) => button.classList.toggle("active", mode === "weight" && Number(button.dataset.grams) === portion()?.grams));
    modal.querySelector("#weightedValue").textContent = `${value || "0"} ${mode === "weight" ? "кг" : state.currency}`;
    const hint = modal.querySelector("#weightedHint");
    hint.classList.toggle("error", !selected);
    hint.textContent = !selected
      ? mode === "weight" ? `Вес от ${formatGrams(pricing.min_grams)} до ${formatGrams(pricing.max_grams)}` : `Сумма от ${pricing.min_amount} до ${pricing.max_amount} ${state.currency}`
      : mode === "weight" ? `К оплате: ${moneyText(selected.amount)}` : `Примерно ${formatGrams(selected.grams)}`;
    modal.querySelector("#addWeighted").disabled = !selected;
  };
  modal.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
    mode = button.dataset.mode;
    value = "";
    draw();
  }));
  modal.querySelectorAll("[data-grams]").forEach((button) => button.addEventListener("click", () => {
    mode = "weight";
    value = kgText(Number(button.dataset.grams));
    draw();
  }));
  bindKeypad(modal, (key) => {
    // Kilograms with a comma (up to 2 decimals); the amount in whole somoni.
    value = applyKey(value, key, mode === "weight" ? { decimal: true, maxLength: 5 } : { maxLength: 3 });
    draw();
  });
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  modal.querySelector("#addWeighted").addEventListener("click", () => {
    const selected = portion();
    if (!selected) return;
    closeModal();
    addToCart(product, selected);
  });
  draw();
}

const COMMENT_MAX_LENGTH = 100;
const QUICK_COMMENTS = ["Без лука", "Без майонеза", "Острее", "Не острое", "Соус отдельно", "Без льда", "Упаковать с собой"];

function openCommentDialog() {
  const modal = openModal(`
    <div class="modal-head"><h3>Комментарий к заказу</h3><button class="modal-close" data-close>×</button></div>
    <p class="hint">Печатается на чеке для кухни.</p>
    <div class="quick-amounts">${QUICK_COMMENTS.map((text) => `<button data-quick="${escapeHtml(text)}">${escapeHtml(text)}</button>`).join("")}</div>
    <textarea class="comment-input" id="commentInput" maxlength="${COMMENT_MAX_LENGTH}" rows="3" placeholder="Например: без лука, соус отдельно">${escapeHtml(state.comment)}</textarea>
    <p class="hint" id="commentCount"></p>
    <div class="modal-actions"><button class="btn" id="clearComment">Очистить</button><button class="btn btn-primary" id="saveComment">Сохранить</button></div>`);
  const input = modal.querySelector("#commentInput");
  const count = () => { modal.querySelector("#commentCount").textContent = `${input.value.length} / ${COMMENT_MAX_LENGTH}`; };
  modal.querySelectorAll("[data-quick]").forEach((button) => button.addEventListener("click", () => {
    const addition = button.dataset.quick;
    const current = input.value.trim();
    const next = current ? `${current}, ${addition.toLowerCase()}` : addition;
    if (next.length <= COMMENT_MAX_LENGTH) input.value = next;
    count();
    input.focus();
  }));
  input.addEventListener("input", count);
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  modal.querySelector("#clearComment").addEventListener("click", () => {
    input.value = "";
    count();
    input.focus();
  });
  modal.querySelector("#saveComment").addEventListener("click", () => {
    state.comment = input.value.replace(/\s+/g, " ").trim().slice(0, COMMENT_MAX_LENGTH);
    closeModal();
    renderTicket();
  });
  count();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// --- payment ----------------------------------------------------------------------

function quickCashAmounts(total) {
  const amounts = [total];
  [10, 50, 100, 500, 1000].forEach((step) => amounts.push(Math.ceil(total / step) * step));
  [200, 500, 1000].forEach((note) => amounts.push(note));
  return [...new Set(amounts.map(round2))].filter((amount) => amount >= total).sort((a, b) => a - b).slice(0, 6);
}

// `target` is either a new sale from the cart or an unpaid kiosk order.
function openPaymentDialog(target) {
  const total = round2(target.total);
  let method = "cash";
  let received = "";
  let printReceipt = true;
  const modal = openModal(`
    <div class="modal-head"><h3>${target.kind === "sale" ? "Оплата" : `Оплата заказа № ${escapeHtml(target.order.order_number)}`}</h3><button class="modal-close" data-close>×</button></div>
    <div class="pay-layout">
      <div>
        <div class="pay-total"><span>К оплате</span><strong>${money(total)}</strong></div>
        <div class="method-buttons"><button data-method="cash">Наличные</button><button data-method="card">Карта</button></div>
        <label class="check-row"><input type="checkbox" id="printReceipt" checked> Печатать чек клиенту</label>
        <div id="changeBox"></div>
      </div>
      <div id="cashPanel">
        <div class="amount-display" id="receivedValue"></div>
        <div class="quick-amounts">${quickCashAmounts(total).map((amount, index) => `<button data-amount="${amount}">${index === 0 ? "Без сдачи" : moneyText(amount)}</button>`).join("")}</div>
        ${keypadHtml({ decimal: true })}
      </div>
    </div>
    <div class="modal-actions"><button class="btn" data-close>Отмена</button><button class="btn btn-green" id="confirmPayment" style="min-width:220px">Провести оплату</button></div>`, { wide: true });
  const draw = () => {
    const amount = parseAmount(received);
    const enough = method === "card" || amount >= total;
    modal.querySelectorAll("[data-method]").forEach((button) => button.classList.toggle("active", button.dataset.method === method));
    modal.querySelector("#cashPanel").hidden = method !== "cash";
    modal.querySelector("#receivedValue").textContent = `Получено: ${received || "0"} ${state.currency}`;
    const changeBox = modal.querySelector("#changeBox");
    changeBox.innerHTML = method === "cash" && received
      ? `<div class="change-box ${enough ? "" : "short"}">${enough ? `Сдача: ${moneyText(amount - total)}` : `Не хватает: ${moneyText(total - amount)}`}</div>`
      : "";
    modal.querySelector("#confirmPayment").disabled = !enough || (method === "cash" && !received);
  };
  modal.querySelectorAll("[data-method]").forEach((button) => button.addEventListener("click", () => { method = button.dataset.method; draw(); }));
  modal.querySelectorAll("[data-amount]").forEach((button) => button.addEventListener("click", () => {
    received = String(button.dataset.amount).replace(".", ",");
    draw();
  }));
  bindKeypad(modal.querySelector("#cashPanel"), (key) => { received = applyKey(received, key, { decimal: true, maxLength: 9 }); draw(); });
  modal.querySelector("#printReceipt").addEventListener("change", (event) => { printReceipt = event.currentTarget.checked; });
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  const confirmButton = modal.querySelector("#confirmPayment");
  const idempotencyKey = newKey();
  confirmButton.addEventListener("click", async () => {
    confirmButton.disabled = true;
    confirmButton.textContent = "Проводим…";
    const payment = {
      payment_method: method,
      amount_received: method === "cash" ? String(parseAmount(received)) : null,
      print_receipt: printReceipt,
    };
    try {
      let order;
      if (target.kind === "sale" && (state.offline || !state.token)) {
        await sellOffline(payment, idempotencyKey);
        return;
      }
      if (target.kind === "sale") {
        order = await api("/api/pos/sales", {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey },
          body: {
            ...payment,
            items: state.cart.map((line) => ({
              product_id: line.productId,
              quantity: line.quantity,
              portion_kind: line.portion?.kind ?? null,
              portion_value: line.portion?.value ?? null,
            })),
            service_type: state.serviceType,
            comment: state.comment,
          },
        });
        resetSale();
        refreshKitchen();
      } else {
        order = await api(`/api/pos/orders/${encodeURIComponent(target.order.order_ref)}/pay`, { method: "POST", body: payment });
        refreshKioskOrders();
      }
      showPaymentResult(order);
    } catch (exc) {
      // The same key is used offline: if the server did get the sale, it will not be doubled.
      if (exc.offline && target.kind === "sale") {
        await sellOffline(payment, idempotencyKey);
        return;
      }
      showToast(exc.offline && target.kind !== "sale" ? "Нет связи с сервером. Заказы киоска оплачиваются, когда связь восстановится." : exc.message, true);
      confirmButton.disabled = false;
      confirmButton.textContent = "Провести оплату";
    }
  });
  draw();
}

function cartItemsForServer() {
  return state.cart.map((line) => ({
    product_id: line.productId,
    quantity: line.quantity,
    portion_kind: line.portion?.kind ?? null,
    portion_value: line.portion?.value ?? null,
  }));
}

// Records the sale on this computer, prints it on the store's printer and queues it for the server.
async function sellOffline(payment, idempotencyKey) {
  const data = offline.usableData();
  if (!data) {
    showToast("Нет связи с сервером и нет данных для работы офлайн.", true);
    return;
  }
  const now = new Date().toISOString();
  const total = cartSubtotal();
  const received = payment.payment_method === "cash" ? Number(payment.amount_received) : total;
  const order = {
    order_number: offline.nextNumber(),
    created_at: now,
    paid_at: now,
    service_type: state.serviceType,
    comment: state.comment,
    subtotal: total,
    discount: 0,
    total,
    payment_method: payment.payment_method,
    amount_received: received,
    change_due: round2(received - total),
    items: state.cart.map((line) => {
      const product = state.menu.products.find((entry) => entry.id === line.productId);
      return {
        product_id: line.productId,
        name: line.name,
        quantity: line.quantity,
        unit_price: line.unitPrice,
        unit: line.portion ? "порц." : product?.unit || "шт.",
        portion_kind: line.portion?.kind ?? null,
        portion_amount: line.portion ? line.portion.amount : null,
        weight_grams: line.portion ? line.portion.grams : null,
        variant: null,
        modifiers: [],
      };
    }),
  };
  const operation = {
    type: "sale",
    id: idempotencyKey,
    at: now,
    cashier_id: state.cashier.id,
    offline_number: order.order_number,
    items: cartItemsForServer(),
    payment_method: payment.payment_method,
    amount_received: payment.amount_received,
    service_type: state.serviceType,
    comment: state.comment,
    print_receipt: false,
    send_to_kitchen: false,
    printed: false,
  };
  try {
    offline.enqueue(operation);
  } catch (exc) {
    showToast(exc.message, true);
    return;
  }
  resetSale();
  const notes = [];
  const settings = data.printer;
  const kitchenError = await offline.print(receipts.escpos(receipts.kitchen(order, settings, state.currency), settings));
  if (kitchenError) notes.push(`Заказ на кухню не напечатан: ${kitchenError}`);
  else offline.markPrinted(operation.id);
  if (payment.print_receipt) {
    const single = receipts.singleCopy(settings);
    const lines = receipts.customer(order, single, { currency: state.currency, title: data.receipt_title, cashierName: state.cashier.name });
    const receiptError = await offline.print(receipts.escpos(lines, single));
    if (receiptError) notes.push(`Чек клиенту не напечатан: ${receiptError}`);
  }
  updateNetStatus();
  showPaymentResult({ ...order, offline: true }, notes);
}

function showPaymentResult(order, offlineNotes = []) {
  const printNotes = [...offlineNotes];
  if (order.receipt_status === "failed") printNotes.push("Чек клиенту не напечатан — принтер точки не настроен.");
  if (order.print_status === "failed" && order.source === "pos") printNotes.push("Заказ на кухню не напечатан.");
  const modal = openModal(`
    <div style="text-align:center">
      <div class="success-mark">✓</div>
      <h3 style="margin:0 0 6px;font-size:24px">Оплачено</h3>
      <p style="margin:0 0 14px;color:var(--muted)">Заказ № <strong style="color:var(--ink);font-size:22px">${escapeHtml(order.order_number)}</strong> · ${order.payment_method === "card" ? "картой" : "наличными"}</p>
      ${order.payment_method === "cash" ? `<div class="change-box">Сдача: ${moneyText(order.change_due)}</div>` : ""}
      ${order.offline ? `<p class="hint">Продажа сохранена без связи и будет отправлена на сервер автоматически.</p>` : ""}
      ${printNotes.map((note) => `<p class="hint error">${escapeHtml(note)}</p>`).join("")}
      <button class="btn btn-dark btn-block" id="nextSale" style="margin-top:16px">Готово</button>
    </div>`);
  modal.querySelector("#nextSale").addEventListener("click", () => {
    closeModal();
    if (state.tab !== "sale") renderMain();
  });
}

// --- kiosk orders ----------------------------------------------------------------

function itemsSummary(order) {
  return order.items.map((item) => {
    const detail = item.weight_grams ? ` (${formatGrams(item.weight_grams)})` : "";
    return `<li>${item.quantity} × ${escapeHtml(item.name)}${escapeHtml(detail)}</li>`;
  }).join("");
}

const SERVICE_LABELS = { dine_in: "В зале", takeaway: "С собой" };

function renderKioskOrders(body) {
  body.innerHTML = `
    <div class="page">
      <h2>Заказы из киоска — ждут оплаты</h2>
      <div class="order-cards">${state.kioskOrders.map((order) => `
        <article class="order-card">
          <div class="order-card-head"><span class="order-number">№ ${escapeHtml(order.order_number)}</span><span class="order-meta">${formatTime(order.created_at)}${order.service_type ? ` · ${SERVICE_LABELS[order.service_type]}` : ""}</span></div>
          <ul class="order-items">${itemsSummary(order)}</ul>
          ${order.comment ? `<div class="order-comment">💬 ${escapeHtml(order.comment)}</div>` : ""}
          <div class="sum-row total"><span>Итого</span><span>${money(order.total)}</span></div>
          <div class="order-card-actions">
            <button class="btn btn-primary" data-pay="${escapeHtml(order.order_ref)}">Принять оплату</button>
            <button class="btn" data-cancel="${escapeHtml(order.order_ref)}">Отменить</button>
          </div>
        </article>`).join("") || `<div class="empty">Неоплаченных заказов из киоска нет</div>`}
      </div>
    </div>`;
  body.querySelectorAll("[data-pay]").forEach((button) => button.addEventListener("click", () => {
    const order = state.kioskOrders.find((entry) => entry.order_ref === button.dataset.pay);
    openPaymentDialog({ kind: "order", order, total: order.total });
  }));
  body.querySelectorAll("[data-cancel]").forEach((button) => button.addEventListener("click", async () => {
    const order = state.kioskOrders.find((entry) => entry.order_ref === button.dataset.cancel);
    if (!await confirmDialog(`Отменить заказ № ${order.order_number}?`, "Клиент не пришёл или отказался от заказа. Отменённый заказ не попадёт в выручку.", "Отменить заказ")) return;
    try {
      await api(`/api/pos/orders/${encodeURIComponent(order.order_ref)}/cancel`, { method: "POST" });
      showToast(`Заказ № ${order.order_number} отменён`);
      await refreshKioskOrders();
      renderKioskOrders(body);
    } catch (exc) { showToast(exc.message, true); }
  }));
}

// --- queue screen -----------------------------------------------------------------

function renderKitchen(body) {
  const preparing = preparingOrders();
  const ready = state.kitchenOrders.filter((order) => order.kitchen_status === "ready");
  const card = (order) => `
    <article class="order-card ${order.kitchen_status === "ready" ? "is-ready" : ""}">
      <div class="order-card-head"><span class="order-number">№ ${escapeHtml(order.order_number)}</span><span class="order-meta">${formatTime(order.created_at)} · ${order.source === "kiosk" ? "Киоск" : "Касса"}${order.service_type ? ` · ${SERVICE_LABELS[order.service_type]}` : ""}</span></div>
      <ul class="order-items">${itemsSummary(order)}</ul>
      ${order.comment ? `<div class="order-comment">💬 ${escapeHtml(order.comment)}</div>` : ""}
      ${order.kitchen_status === "preparing"
        ? `<button class="btn btn-green" data-kitchen="${escapeHtml(order.order_ref)}" data-status="ready">Готово</button>`
        : `<div class="ready-row"><span>Готово в ${formatTime(order.ready_at)}</span><button class="btn btn-small" data-kitchen="${escapeHtml(order.order_ref)}" data-status="preparing">Вернуть</button></div>`}
    </article>`;
  body.innerHTML = `
    <div class="page">
      <h2>Готовится</h2>
      <div class="order-cards">${preparing.map(card).join("") || `<div class="empty">Нет заказов в работе</div>`}</div>
      <h2 class="section-gap">Готово <small>· на экране очереди 5 минут</small></h2>
      <div class="order-cards">${ready.map(card).join("") || `<div class="empty">Нет готовых заказов</div>`}</div>
    </div>`;
  body.querySelectorAll("[data-kitchen]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const order = await api(`/api/pos/orders/${encodeURIComponent(button.dataset.kitchen)}/kitchen`, {
        method: "POST",
        body: { status: button.dataset.status },
      });
      showToast(order.kitchen_status === "ready" ? `Заказ № ${order.order_number} — готово` : `Заказ № ${order.order_number} снова готовится`);
      await refreshKitchen();
      renderKitchen(body);
    } catch (exc) {
      showToast(exc.message, true);
      button.disabled = false;
    }
  }));
}

// --- history ----------------------------------------------------------------------

const STATUS_LABELS = { paid: "Оплачен", unpaid: "Не оплачен", refunded: "Возврат", cancelled: "Отменён" };

function pendingSalesHtml() {
  const sales = offline.pendingSales();
  if (!sales.length) return "";
  return `
    <h2>Не отправлены на сервер <small>· уйдут автоматически, когда будет связь</small></h2>
    <table class="history" style="margin-bottom:24px">
      <thead><tr><th>№</th><th>Дата и время</th><th>Оплата</th><th>Позиций</th></tr></thead>
      <tbody>${sales.map((sale) => `
        <tr>
          <td><strong>${escapeHtml(sale.offline_number)}</strong></td>
          <td>${formatDateTime(sale.at)}</td>
          <td>${sale.payment_method === "cash" ? "Наличные" : "Карта"}</td>
          <td>${sale.items.reduce((sum, item) => sum + item.quantity, 0)}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

async function renderHistory(body) {
  body.innerHTML = `<div class="page">${pendingSalesHtml()}<h2>Последние 15 чеков</h2><div id="historyTable" class="empty">Загрузка…</div></div>`;
  try {
    state.history = await api("/api/pos/orders?scope=recent");
  } catch (exc) {
    body.querySelector("#historyTable").textContent = exc.message;
    return;
  }
  const table = body.querySelector("#historyTable");
  if (!state.history.length) {
    table.textContent = "Чеков пока нет";
    return;
  }
  table.className = "";
  table.innerHTML = `
    <table class="history">
      <thead><tr><th>№</th><th>Дата и время</th><th>Откуда</th><th>Статус</th><th style="text-align:right">Сумма</th><th></th></tr></thead>
      <tbody>${state.history.map((order) => `
        <tr>
          <td><strong>${escapeHtml(order.order_number)}</strong></td>
          <td>${formatDateTime(order.created_at)}</td>
          <td>${order.source === "pos" ? "Касса" : "Киоск"}</td>
          <td><span class="status status-${order.payment_status}">${STATUS_LABELS[order.payment_status] || order.payment_status}</span></td>
          <td style="text-align:right"><strong>${money(order.total)}</strong>${order.discount ? `<br><small class="order-meta">скидка ${moneyText(order.discount)}</small>` : ""}</td>
          <td class="actions">
            ${order.payment_status === "paid" ? `<button class="btn btn-small" data-receipt="${escapeHtml(order.order_ref)}">Чек</button><button class="btn btn-small" data-refund="${escapeHtml(order.order_ref)}">Возврат</button>` : ""}
          </td>
        </tr>`).join("")}
      </tbody>
    </table>`;
  table.querySelectorAll("[data-receipt]").forEach((button) => button.addEventListener("click", () => openReceiptDialog(button.dataset.receipt)));
  table.querySelectorAll("[data-refund]").forEach((button) => button.addEventListener("click", async () => {
    const order = state.history.find((entry) => entry.order_ref === button.dataset.refund);
    const method = order.payment_method === "cash" ? "Верните клиенту наличные" : "Сделайте возврат на карту в терминале";
    if (!await confirmDialog(`Возврат заказа № ${order.order_number}?`, `${method}: ${moneyText(order.total)}. Возврат попадёт в отчёт текущей смены.`, "Оформить возврат")) return;
    try {
      await api(`/api/pos/orders/${encodeURIComponent(order.order_ref)}/refund`, { method: "POST" });
      showToast(`Возврат по заказу № ${order.order_number} оформлен`);
      renderHistory(body);
    } catch (exc) { showToast(exc.message, true); }
  }));
}

// The receipt is shown as the printer prints it; printing is up to the cashier.
async function openReceiptDialog(orderRef) {
  const path = `/api/pos/orders/${encodeURIComponent(orderRef)}/receipt`;
  let receipt;
  try {
    receipt = await api(path);
  } catch (exc) {
    showToast(exc.message, true);
    return;
  }
  const lines = receipt.lines.map((line) => {
    const stretch = line.height / line.width;
    const text = escapeHtml(line.text) || "&nbsp;";
    return `<div class="receipt-line" style="text-align:${line.align};font-size:${line.width}em;${line.bold ? "font-weight:700;" : ""}">${stretch === 1 ? text : `<span style="display:inline-block;transform:scaleY(${stretch});margin:${(stretch - 1) / 2}em 0">${text}</span>`}</div>`;
  }).join("");
  const modal = openModal(`
    <div class="modal-head"><h3>Чек № ${escapeHtml(receipt.order_number)}</h3><button class="modal-close" data-close>×</button></div>
    <div class="receipt-preview"><div class="receipt-paper" style="width:${receipt.chars_per_line}ch">${lines}</div></div>
    <div class="modal-actions"><button class="btn" data-close>Закрыть</button><button class="btn btn-primary" id="printReceipt">Печатать</button></div>`);
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  const printButton = modal.querySelector("#printReceipt");
  printButton.addEventListener("click", async () => {
    printButton.disabled = true;
    try {
      await api(path, { method: "POST" });
      showToast("Чек отправлен на принтер");
      closeModal();
    } catch (exc) {
      showToast(exc.message, true);
      printButton.disabled = false;
    }
  });
}

// --- shift -----------------------------------------------------------------------

function reportTiles(report) {
  const tile = (label, value, highlight = false) => `<div class="report-tile ${highlight ? "highlight" : ""}"><span>${label}</span><strong>${value}</strong></div>`;
  // Without revenue access the server sends only the counts.
  if (report.revenue_hidden) {
    return `
    <div class="report-grid">
      ${tile("Продаж", report.sales_count)}
      ${tile("Возвратов", report.refunds_count)}
    </div>
    <p class="hint">Выручка и наличные видны только кассирам с доступом «Выручка».</p>`;
  }
  return `
    <div class="report-grid">
      ${tile("Продаж", report.sales_count)}
      ${tile("Продажи всего", money(report.sales_total))}
      ${tile("Наличными", money(report.sales_cash))}
      ${tile("Картой", money(report.sales_card))}
      ${tile("Скидки", money(report.discounts_total))}
      ${tile(`Возвраты (${report.refunds_count})`, money(report.refunds_total))}
      ${tile("Выручка", money(report.revenue), true)}
      ${tile("Наличные на начало", money(report.opening_cash))}
      ${tile("Наличных в кассе", money(report.expected_cash), true)}
      ${report.counted_cash !== null ? tile("Пересчитано", money(report.counted_cash)) : ""}
      ${report.difference !== null ? tile("Расхождение", money(report.difference), true) : ""}
    </div>`;
}

async function renderShift(body) {
  body.innerHTML = `<div class="page"><h2>Смена</h2><div id="shiftReport" class="empty">Загрузка…</div></div>`;
  let report;
  const pending = offline.outbox.length;
  try {
    if (pending) throw Object.assign(new Error(`Отчёт смены и закрытие будут доступны после отправки офлайн-данных (${pending}).`), { offline: true });
    report = await api("/api/pos/shifts/current");
  } catch (exc) {
    body.querySelector("#shiftReport").textContent = exc.offline
      ? `${exc.message} Смена открыта ${formatDateTime(state.shift?.opened_at)}.`
      : exc.message;
    return;
  }
  const container = body.querySelector("#shiftReport");
  container.className = "";
  container.innerHTML = `
    <p class="order-meta" style="margin:0 0 14px">Смена № ${report.shift_id} · открыта ${formatDateTime(report.opened_at)} · кассир ${escapeHtml(report.cashier_name)}</p>
    ${reportTiles(report)}
    <div class="row-actions">
      ${report.revenue_hidden ? "" : `<button class="btn" id="printX">Печать X-отчёта</button>`}
      <button class="btn btn-primary" id="closeShift">${report.revenue_hidden ? "Закрыть смену" : "Закрыть смену (Z-отчёт)"}</button>
    </div>`;
  container.querySelector("#printX")?.addEventListener("click", async () => {
    try {
      const result = await api("/api/pos/shifts/current/print", { method: "POST" });
      showToast(result.print_status === "failed" ? "Принтер не настроен" : "X-отчёт отправлен на печать", result.print_status === "failed");
    } catch (exc) { showToast(exc.message, true); }
  });
  container.querySelector("#closeShift").addEventListener("click", () => openCloseShiftDialog(report));
}

// Without revenue access the cashier counts the cash blind: no expected amount, no Z-report.
function openCloseShiftDialog(report) {
  const blind = report.revenue_hidden;
  let counted = "";
  let printReport = !blind;
  const modal = openModal(`
    <div class="modal-head"><h3>Закрытие смены</h3><button class="modal-close" data-close>×</button></div>
    <p>${blind ? "Пересчитайте наличные в кассе и введите сумму." : `Пересчитайте наличные в кассе. По расчёту должно быть <strong>${money(report.expected_cash)}</strong>.`}</p>
    ${state.kioskOrders.length ? `<p class="hint error">Неоплаченных заказов из киоска: ${state.kioskOrders.length}. Их можно оплатить или отменить и после закрытия, в новой смене.</p>` : ""}
    <div class="amount-display" id="countedValue"></div>
    <p class="hint" id="countedHint"></p>
    ${keypadHtml({ decimal: true })}
    ${blind ? "" : `<label class="check-row"><input type="checkbox" id="printZ" checked> Печатать Z-отчёт</label>`}
    <div class="modal-actions"><button class="btn" data-close>Отмена</button><button class="btn btn-primary" id="confirmClose">Закрыть смену</button></div>`);
  const draw = () => {
    modal.querySelector("#countedValue").textContent = `${counted || "0"} ${state.currency}`;
    if (blind) return;
    const difference = round2(parseAmount(counted) - report.expected_cash);
    const hint = modal.querySelector("#countedHint");
    hint.classList.toggle("error", difference !== 0);
    hint.textContent = difference === 0 ? "Совпадает с расчётом" : `Расхождение: ${moneyText(difference)}`;
  };
  bindKeypad(modal, (key) => { counted = applyKey(counted, key, { decimal: true }); draw(); });
  modal.querySelector("#printZ")?.addEventListener("change", (event) => { printReport = event.currentTarget.checked; });
  modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  modal.querySelector("#confirmClose").addEventListener("click", async () => {
    try {
      const zReport = await api("/api/pos/shifts/close", { method: "POST", body: { counted_cash: String(parseAmount(counted)), print_report: printReport } });
      window.clearInterval(pollTimer);
      state.shift = null;
      offline.rememberShift(null);
      state.kioskOrders = [];
      const result = openModal(`
        <div class="modal-head"><h3>Смена № ${zReport.shift_id} закрыта</h3></div>
        ${reportTiles(zReport)}
        ${zReport.print_status === "failed" ? `<p class="hint error">Z-отчёт не напечатан — принтер точки не настроен.</p>` : ""}
        <div class="modal-actions"><button class="btn btn-dark" id="afterClose">Готово</button></div>`, { wide: true });
      result.querySelector("#afterClose").addEventListener("click", () => {
        closeModal();
        renderOpenShift();
      });
    } catch (exc) { showToast(exc.message, true); }
  });
  draw();
}

start();
