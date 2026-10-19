// Paprika admin panel: stores, devices, staff, products, printers, reports and shifts.
const TOKEN_KEY = "paprika-admin-token";
const STORE_KEY = "paprika-admin-store";

const state = {
  token: readStorage(window.sessionStorage, TOKEN_KEY),
  user: null,
  stores: [],
  queueUrl: "",
  queuePort: 8001,
  storeId: Number(readStorage(window.localStorage, STORE_KEY)) || null,
  // Category filter on the products page.
  productCategory: "",
};

const app = document.querySelector("#app");
const modalRoot = document.querySelector("#modalRoot");
const toast = document.querySelector("#toast");
let toastTimer;

// --- helpers -----------------------------------------------------------------------

function readStorage(storage, key) {
  try { return storage.getItem(key) || ""; } catch { return ""; }
}

function writeStorage(storage, key, value) {
  try {
    if (value) storage.setItem(key, value);
    else storage.removeItem(key);
  } catch { /* storage blocked */ }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function money(amount) {
  const value = Number(amount || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
  return `${value} с`;
}

function kg(grams) {
  return `${(grams / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 3 })} кг`;
}

function formatDateTime(iso) {
  return iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
}

function today() {
  return new Date().toLocaleDateString("sv-SE");
}

function lastSeen(iso) {
  if (!iso) return "ни разу";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "только что";
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч назад`;
  return formatDateTime(iso);
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3800);
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = state.token ? { Authorization: `Bearer ${state.token}` } : {};
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  });
  if (raw && response.ok) return response;
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && state.token) {
    signOut(false);
    throw new Error(data.detail || "Сессия истекла. Войдите снова.");
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? "Проверьте заполнение полей." : data.detail;
    throw new Error(detail || "Не удалось выполнить запрос.");
  }
  return data;
}

function closeModal() {
  modalRoot.innerHTML = "";
}

function openModal(title, html, { wide = false } = {}) {
  modalRoot.innerHTML = `
    <div class="modal-backdrop">
      <section class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
        <div class="modal-head"><h2>${escapeHtml(title)}</h2><button class="close-button" data-close aria-label="Закрыть">×</button></div>
        ${html}
      </section>
    </div>`;
  modalRoot.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", closeModal));
  return modalRoot.querySelector(".modal");
}

function confirmAction(title, text, label = "Да") {
  return new Promise((resolve) => {
    const modal = openModal(title, `<p class="hint">${escapeHtml(text)}</p><div class="btn-row"><button class="btn btn-primary" id="confirmYes">${escapeHtml(label)}</button><button class="btn" data-close>Отмена</button></div>`);
    modal.querySelector("#confirmYes").addEventListener("click", () => { closeModal(); resolve(true); });
    modal.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => resolve(false)));
  });
}

function setMessage(element, text, isError = false) {
  element.textContent = text;
  element.classList.toggle("is-error", isError);
}

async function downloadCsv(path, filename) {
  try {
    const response = await api(path, { raw: true });
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) { showToast(error.message, true); }
}

function storeById(id) {
  return state.stores.find((store) => store.id === Number(id));
}

function isOwner() {
  return state.user?.role === "owner";
}

// --- session -----------------------------------------------------------------------

function renderLogin(message = "") {
  app.innerHTML = `
    <div class="center-screen">
      <div class="login-card">
        <h1>Paprika · Админпанель</h1>
        <p>Управление точками продаж, сотрудниками и отчётами.</p>
        <form id="loginForm">
          <label class="field">Логин<input name="login" autocomplete="username" required></label>
          <label class="field">Пароль<input name="password" type="password" autocomplete="current-password" required></label>
          <button class="btn btn-primary" type="submit">Войти</button>
          <p class="form-error" id="loginError">${escapeHtml(message)}</p>
        </form>
      </div>
    </div>`;
  const form = app.querySelector("#loginForm");
  form.elements.login.focus();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const session = await api("/api/admin/login", {
        method: "POST",
        body: { login: form.elements.login.value.trim(), password: form.elements.password.value },
      });
      state.token = session.token;
      writeStorage(window.sessionStorage, TOKEN_KEY, session.token);
      applyMe(session);
      route();
    } catch (error) {
      app.querySelector("#loginError").textContent = error.message;
      button.disabled = false;
    }
  });
}

function applyMe(me) {
  state.user = me.user;
  state.stores = me.stores;
  state.queueUrl = me.queue_url || "";
  state.queuePort = me.queue_port || 8001;
  if (!storeById(state.storeId)) state.storeId = state.stores[0]?.id || null;
}

async function refreshMe() {
  applyMe(await api("/api/admin/me"));
}

function signOut(callServer = true) {
  if (callServer && state.token) api("/api/admin/logout", { method: "POST" }).catch(() => {});
  state.token = "";
  state.user = null;
  writeStorage(window.sessionStorage, TOKEN_KEY, "");
  closeModal();
  renderLogin();
}

// --- layout and routing -----------------------------------------------------------------

const STORE_PAGES = [
  { id: "devices", label: "Устройства" },
  { id: "cashiers", label: "Сотрудники" },
  { id: "products", label: "Товары" },
  { id: "printer", label: "Принтер" },
  { id: "orders", label: "Заказы" },
];

function currentRoute() {
  const parts = window.location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean);
  if (parts[0] === "store" && storeById(parts[1])) {
    state.storeId = Number(parts[1]);
    writeStorage(window.localStorage, STORE_KEY, String(state.storeId));
    return { page: STORE_PAGES.some((page) => page.id === parts[2]) ? parts[2] : "devices", storeId: state.storeId };
  }
  const page = ["overview", "reports", "shifts", "users"].includes(parts[0]) ? parts[0] : "overview";
  if (page === "users" && !isOwner()) return { page: "overview" };
  return { page };
}

function navigate(hash) {
  if (window.location.hash === hash) route();
  else window.location.hash = hash;
}

function navHtml(current) {
  const link = (hash, label, active) => `<a class="nav-link ${active ? "active" : ""}" href="${hash}">${escapeHtml(label)}</a>`;
  const storeOptions = state.stores.map((store) => `<option value="${store.id}" ${store.id === state.storeId ? "selected" : ""}>${escapeHtml(store.name)}${store.active ? "" : " (отключена)"}</option>`).join("");
  return `
    <div class="sidebar-brand">Paprika<small>Админпанель</small></div>
    ${link("#/overview", "Точки продаж", current.page === "overview")}
    ${link("#/reports", "Отчёты", current.page === "reports")}
    ${link("#/shifts", "Смены", current.page === "shifts")}
    ${isOwner() ? link("#/users", "Управляющие", current.page === "users") : ""}
    ${state.stores.length ? `
      <div class="nav-label">Точка</div>
      <select class="store-select" id="storeSelect" aria-label="Точка">${storeOptions}</select>
      ${STORE_PAGES.map((page) => link(`#/store/${state.storeId}/${page.id}`, page.label, current.storeId && current.page === page.id)).join("")}` : ""}
    <div class="sidebar-user"><strong>${escapeHtml(state.user.name)}</strong>${isOwner() ? "Владелец" : "Управляющий"} · ${escapeHtml(state.user.login)}<br><button id="logout">Выйти</button></div>`;
}

function mobileNavHtml(current) {
  const options = [
    ["#/overview", "Точки продаж"], ["#/reports", "Отчёты"], ["#/shifts", "Смены"],
    ...(isOwner() ? [["#/users", "Управляющие"]] : []),
    ...state.stores.flatMap((store) => STORE_PAGES.map((page) => [`#/store/${store.id}/${page.id}`, `${store.name} — ${page.label}`])),
  ];
  const active = current.storeId ? `#/store/${current.storeId}/${current.page}` : `#/${current.page}`;
  return `
    <div class="mobile-bar"><strong>Paprika · Админпанель</strong><button class="btn btn-small" id="logoutMobile">Выйти</button></div>
    <select id="mobileNav" aria-label="Раздел">${options.map(([hash, label]) => `<option value="${hash}" ${hash === active ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select>`;
}

function route() {
  if (!state.user) return;
  closeModal();
  const current = currentRoute();
  app.innerHTML = `
    <div class="shell">
      <nav class="sidebar">${navHtml(current)}</nav>
      <main class="content"><div class="mobile-nav">${mobileNavHtml(current)}</div><div id="page"></div></main>
    </div>`;
  app.querySelector("#logout").addEventListener("click", () => signOut());
  app.querySelector("#logoutMobile").addEventListener("click", () => signOut());
  app.querySelector("#mobileNav").addEventListener("change", (event) => navigate(event.target.value));
  app.querySelector("#storeSelect")?.addEventListener("change", (event) => {
    navigate(`#/store/${event.target.value}/${current.storeId ? current.page : "devices"}`);
  });
  const page = app.querySelector("#page");
  const pages = {
    overview: renderOverview,
    reports: renderReports,
    shifts: renderShifts,
    users: renderUsers,
    devices: renderDevices,
    cashiers: renderCashiers,
    products: renderProducts,
    printer: renderPrinter,
    orders: renderOrders,
  };
  page.innerHTML = `<p class="empty">Загрузка…</p>`;
  pages[current.page](page, current.storeId).catch((error) => {
    page.innerHTML = `<div class="card"><p class="message is-error">${escapeHtml(error.message)}</p></div>`;
  });
}

function pageHead(title, subtitle = "", actions = "") {
  return `<div class="page-head"><div><h1>${escapeHtml(title)}</h1>${subtitle ? `<p>${subtitle}</p>` : ""}</div>${actions}</div>`;
}

// --- overview (stores) -----------------------------------------------------------------

async function renderOverview(page) {
  const stores = await api("/api/admin/stores");
  page.innerHTML = `
    ${pageHead("Точки продаж", "Сегодняшние продажи по точкам.")}
    <div class="store-cards">${stores.map((store) => `
      <article class="store-card ${store.active ? "" : "inactive"}">
        <div class="store-card-head">
          <div><h2>${escapeHtml(store.name)}</h2><div class="address">${escapeHtml(store.address || "Адрес не указан")}</div></div>
          <span class="badge ${store.active ? "ok" : "off"}">${store.active ? "Работает" : "Отключена"}</span>
        </div>
        <div class="mini-metrics">
          <div><span>Заказов</span><strong>${store.today.orders}</strong></div>
          <div><span>Сумма</span><strong>${money(store.today.amount)}</strong></div>
          <div><span>Ждут оплаты</span><strong>${store.today.unpaid}</strong></div>
          <div><span>Открытых смен</span><strong>${store.today.open_shifts}</strong></div>
        </div>
        <div class="btn-row">
          <a class="btn btn-small" href="#/store/${store.id}/devices" style="display:inline-flex;align-items:center;text-decoration:none">Управлять</a>
          ${isOwner() ? `<button class="btn btn-small" data-edit-store="${store.id}">Изменить</button><button class="btn btn-small ${store.active ? "btn-danger" : ""}" data-toggle-store="${store.id}">${store.active ? "Отключить" : "Включить"}</button>` : ""}
        </div>
      </article>`).join("") || `<p class="empty">Нет доступных точек. Обратитесь к владельцу.</p>`}
    </div>
    ${isOwner() ? `
      <section class="card">
        <h2>Новая точка</h2>
        <p class="hint">После создания добавьте кассы, киоски и экран очереди в разделе «Устройства», а затем кассиров.</p>
        <form id="storeForm" class="form-grid">
          <label class="field">Название<input name="name" maxlength="80" required placeholder="Например: Paprika Центр"></label>
          <label class="field">Адрес<input name="address" maxlength="200" placeholder="Город, улица, дом"></label>
          <button class="btn btn-primary" type="submit">Создать точку</button>
        </form>
        <p class="message" id="storeMessage"></p>
      </section>` : ""}`;
  if (!isOwner()) return;
  page.querySelector("#storeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const store = await api("/api/admin/stores", { method: "POST", body: { name: form.elements.name.value, address: form.elements.address.value } });
      await refreshMe();
      showToast(`Точка «${store.name}» создана`);
      navigate(`#/store/${store.id}/devices`);
    } catch (error) { setMessage(page.querySelector("#storeMessage"), error.message, true); }
  });
  page.querySelectorAll("[data-edit-store]").forEach((button) => button.addEventListener("click", () => {
    const store = stores.find((entry) => entry.id === Number(button.dataset.editStore));
    const modal = openModal("Изменить точку", `
      <form id="editStoreForm" class="printer-fields">
        <label class="field">Название<input name="name" maxlength="80" required value="${escapeHtml(store.name)}"></label>
        <label class="field">Адрес<input name="address" maxlength="200" value="${escapeHtml(store.address)}"></label>
        <div class="btn-row"><button class="btn btn-primary" type="submit">Сохранить</button><button class="btn" type="button" data-close>Отмена</button></div>
        <p class="message" id="editStoreMessage"></p>
      </form>`);
    modal.querySelector("[type=button][data-close]").addEventListener("click", closeModal);
    modal.querySelector("#editStoreForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        await api(`/api/admin/stores/${store.id}`, { method: "PUT", body: { name: form.elements.name.value, address: form.elements.address.value } });
        await refreshMe();
        route();
      } catch (error) { setMessage(modal.querySelector("#editStoreMessage"), error.message, true); }
    });
  }));
  page.querySelectorAll("[data-toggle-store]").forEach((button) => button.addEventListener("click", async () => {
    const store = stores.find((entry) => entry.id === Number(button.dataset.toggleStore));
    if (store.active && !await confirmAction(`Отключить «${store.name}»?`, "Киоски, кассы и экран очереди этой точки перестанут работать. Данные сохранятся.", "Отключить")) return;
    try {
      await api(`/api/admin/stores/${store.id}`, { method: "PUT", body: { active: !store.active } });
      await refreshMe();
      route();
    } catch (error) { showToast(error.message, true); }
  }));
}

// --- devices -----------------------------------------------------------------------

const DEVICE_KINDS = {
  register: { label: "Касса", plural: "Кассы", hint: "Компьютер кассира с программой PaprikaPOS.exe. Программа кассы также печатает чеки точки." },
  kiosk: { label: "Киоск", plural: "Киоски", hint: "Сенсорный экран для заказов покупателей." },
  queue: { label: "Экран очереди", plural: "Экраны очереди", hint: "Телевизор в зале с номерами «Готовится» / «Готово»." },
};

function activationLink(kind, key) {
  const origin = window.location.origin;
  if (kind === "register") return `${origin}/pos#activate=${key}`;
  if (kind === "queue") {
    const base = state.queueUrl || `${window.location.protocol}//${window.location.hostname}:${state.queuePort}`;
    return `${base}/#activate=${key}`;
  }
  return `${origin}/#activate=${key}`;
}

function showDeviceKey(device) {
  const link = activationLink(device.kind, device.key);
  const modal = openModal(`Ключ: ${device.name}`, `
    <p class="hint">Откройте ссылку на самом устройстве — оно запомнит ключ и будет работать в этой точке.</p>
    <div class="key-box">
      <span class="field">Ссылка активации</span><code id="activationLink">${escapeHtml(link)}</code>
      <span class="field">Ключ устройства</span><code>${escapeHtml(device.key)}</code>
    </div>
    ${device.kind === "register" ? `<p class="hint">В программе кассы PaprikaPOS.exe вставьте этот ключ в поле «Ключ кассы» (Ctrl+Shift+S) — тогда программа будет и печатать чеки точки.</p>` : ""}
    <p class="warning">Ключ показывается только сейчас. Сохраните его или сразу откройте ссылку на устройстве. Если ключ потерян — выпустите новый.</p>
    <div class="btn-row" style="margin-top:14px"><button class="btn btn-primary" id="copyLink">Скопировать ссылку</button><button class="btn" data-close>Готово</button></div>`);
  modal.querySelector("#copyLink").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(link);
      showToast("Ссылка скопирована");
    } catch {
      showToast("Не удалось скопировать — выделите ссылку вручную", true);
    }
  });
}

async function renderDevices(page, storeId) {
  const devices = await api(`/api/admin/stores/${storeId}/devices`);
  const store = storeById(storeId);
  const section = (kind) => {
    const list = devices.filter((device) => device.kind === kind);
    return `
      <section class="card">
        <h2>${DEVICE_KINDS[kind].plural}</h2>
        <p class="hint">${DEVICE_KINDS[kind].hint}</p>
        ${list.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>Название</th><th>Статус</th><th>На связи</th><th></th></tr></thead><tbody>${list.map((device) => `
          <tr>
            <td><strong>${escapeHtml(device.name)}</strong></td>
            <td><span class="badge ${device.active ? "ok" : "off"}">${device.active ? "Активно" : "Отключено"}</span></td>
            <td>${escapeHtml(lastSeen(device.last_seen_at))}</td>
            <td class="actions">
              <button class="btn btn-small" data-rename="${device.id}">Переименовать</button>
              <button class="btn btn-small" data-key="${device.id}">Новый ключ</button>
              <button class="btn btn-small ${device.active ? "btn-danger" : ""}" data-toggle="${device.id}">${device.active ? "Отключить" : "Включить"}</button>
            </td>
          </tr>`).join("")}</tbody></table></div>` : `<p class="empty">Пока нет.</p>`}
      </section>`;
  };
  page.innerHTML = `
    ${pageHead(`Устройства · ${store.name}`, "Каждое устройство активируется своей ссылкой и работает только в этой точке.")}
    <section class="card">
      <h2>Добавить устройство</h2>
      <form id="deviceForm" class="form-grid">
        <label class="field">Тип<select name="kind">${Object.entries(DEVICE_KINDS).map(([kind, info]) => `<option value="${kind}">${info.label}</option>`).join("")}</select></label>
        <label class="field">Название<input name="name" maxlength="60" required placeholder="Например: Касса 2"></label>
        <button class="btn btn-primary" type="submit">Добавить</button>
      </form>
      <p class="message" id="deviceMessage"></p>
    </section>
    ${section("register")}${section("kiosk")}${section("queue")}`;
  page.querySelector("#deviceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const device = await api(`/api/admin/stores/${storeId}/devices`, { method: "POST", body: { kind: form.elements.kind.value, name: form.elements.name.value } });
      await renderDevices(page, storeId);
      showDeviceKey(device);
    } catch (error) { setMessage(page.querySelector("#deviceMessage"), error.message, true); }
  });
  const find = (id) => devices.find((device) => device.id === Number(id));
  page.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", async () => {
    const device = find(button.dataset.key);
    if (!await confirmAction(`Новый ключ для «${device.name}»?`, "Старый ключ перестанет работать: устройство нужно будет активировать заново новой ссылкой.", "Выпустить ключ")) return;
    try {
      showDeviceKey(await api(`/api/admin/stores/${storeId}/devices/${device.id}/key`, { method: "POST" }));
    } catch (error) { showToast(error.message, true); }
  }));
  page.querySelectorAll("[data-rename]").forEach((button) => button.addEventListener("click", async () => {
    const device = find(button.dataset.rename);
    const name = window.prompt("Новое название", device.name);
    if (!name || !name.trim()) return;
    try {
      await api(`/api/admin/stores/${storeId}/devices/${device.id}`, { method: "PUT", body: { name } });
      renderDevices(page, storeId);
    } catch (error) { showToast(error.message, true); }
  }));
  page.querySelectorAll("[data-toggle]").forEach((button) => button.addEventListener("click", async () => {
    const device = find(button.dataset.toggle);
    try {
      await api(`/api/admin/stores/${storeId}/devices/${device.id}`, { method: "PUT", body: { active: !device.active } });
      renderDevices(page, storeId);
    } catch (error) { showToast(error.message, true); }
  }));
}

// --- cashiers ----------------------------------------------------------------------

async function renderCashiers(page, storeId) {
  const cashiers = await api(`/api/admin/stores/${storeId}/cashiers`);
  const store = storeById(storeId);
  page.innerHTML = `
    ${pageHead(`Сотрудники · ${store.name}`, "Кассиры входят в кассу своей точки по PIN-коду из 4–6 цифр.")}
    <section class="card">
      <h2>Новый кассир</h2>
      <form id="cashierForm" class="form-grid">
        <label class="field">Имя<input name="name" maxlength="60" required placeholder="Например: Алия"></label>
        <label class="field">PIN-код<input name="pin" inputmode="numeric" pattern="[0-9]{4,6}" minlength="4" maxlength="6" required placeholder="4–6 цифр" autocomplete="off"></label>
        <label class="check"><input type="checkbox" name="can_manage_shift"> Доступ к смене</label>
        <button class="btn btn-primary" type="submit">Добавить</button>
      </form>
      <p class="message" id="cashierMessage"></p>
    </section>
    <section class="card">
      <h2>Кассиры</h2>
      <p class="hint">Открывать и закрывать смену и печатать X/Z-отчёты могут только кассиры с доступом к смене.</p>
      ${cashiers.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>Имя</th><th>Статус</th><th>Доступ к смене</th><th></th></tr></thead><tbody>${cashiers.map((cashier) => `
        <tr>
          <td><strong>${escapeHtml(cashier.name)}</strong></td>
          <td><span class="badge ${cashier.active ? "ok" : "off"}">${cashier.active ? "Активен" : "Отключён"}</span></td>
          <td><span class="badge ${cashier.can_manage_shift ? "ok" : ""}">${cashier.can_manage_shift ? "Есть" : "Нет"}</span></td>
          <td class="actions">
            <button class="btn btn-small" data-shift="${cashier.id}">${cashier.can_manage_shift ? "Забрать доступ к смене" : "Дать доступ к смене"}</button>
            <button class="btn btn-small" data-pin="${cashier.id}">Сменить PIN</button>
            <button class="btn btn-small ${cashier.active ? "btn-danger" : ""}" data-active="${cashier.id}">${cashier.active ? "Отключить" : "Включить"}</button>
          </td>
        </tr>`).join("")}</tbody></table></div>` : `<p class="empty">Кассиров пока нет — добавьте первого.</p>`}
    </section>`;
  const reload = () => renderCashiers(page, storeId);
  const update = async (cashierId, body, success) => {
    try {
      await api(`/api/admin/stores/${storeId}/cashiers/${cashierId}`, { method: "PUT", body });
      if (success) showToast(success);
      reload();
    } catch (error) { showToast(error.message, true); }
  };
  page.querySelector("#cashierForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api(`/api/admin/stores/${storeId}/cashiers`, {
        method: "POST",
        body: { name: form.elements.name.value, pin: form.elements.pin.value, can_manage_shift: form.elements.can_manage_shift.checked },
      });
      showToast("Кассир добавлен");
      reload();
    } catch (error) { setMessage(page.querySelector("#cashierMessage"), error.message, true); }
  });
  const find = (id) => cashiers.find((cashier) => cashier.id === Number(id));
  page.querySelectorAll("[data-shift]").forEach((button) => button.addEventListener("click", () => {
    const cashier = find(button.dataset.shift);
    update(cashier.id, { can_manage_shift: !cashier.can_manage_shift });
  }));
  page.querySelectorAll("[data-active]").forEach((button) => button.addEventListener("click", () => {
    const cashier = find(button.dataset.active);
    update(cashier.id, { active: !cashier.active });
  }));
  page.querySelectorAll("[data-pin]").forEach((button) => button.addEventListener("click", () => {
    const pin = window.prompt("Новый PIN-код (4–6 цифр)");
    if (pin === null) return;
    if (!/^\d{4,6}$/.test(pin)) {
      showToast("PIN-код — от 4 до 6 цифр", true);
      return;
    }
    update(Number(button.dataset.pin), { pin }, "PIN-код изменён");
  }));
}

// --- products ----------------------------------------------------------------------

function productRows(catalog, { query = "", categoryId = "" } = {}) {
  const text = query.trim().toLowerCase();
  const products = catalog.products.filter((product) => (!categoryId || product.category_id === categoryId)
    && (!text || product.name.toLowerCase().includes(text)));
  return products.map((product) => `
    <div class="product-row">
      <img src="${escapeHtml(product.image)}" alt="" loading="lazy">
      <div class="product-info">
        <strong>${escapeHtml(product.name)}</strong>
        <small>${escapeHtml(catalog.categories.find((category) => category.id === product.category_id)?.name || "Категория не найдена")} · ${money(product.price)} / ${escapeHtml(product.unit)}</small>
      </div>
      <span class="product-priority" title="Приоритет">${product.priority ? `№ ${product.priority}` : ""}</span>
      <span class="badge ${product.available ? "ok" : "off"}">${product.available ? "В продаже" : "Скрыт"}</span>
      <button class="btn btn-small" data-edit-product="${escapeHtml(product.id)}">Изменить</button>
    </div>`).join("") || `<p class="empty">Ничего не найдено.</p>`;
}

function productEditorHtml(product, categories) {
  const editing = Boolean(product);
  return `
    <form id="productEditorForm" class="product-editor">
      <h3>${editing ? "Изменить товар" : "Новый товар"}</h3>
      <label class="field">Название<input name="name" maxlength="120" required value="${escapeHtml(product?.name || "")}"></label>
      <div class="form-grid">
        <label class="field">Цена<input name="price" type="number" min="0" max="99999999.99" step="0.01" required value="${editing ? product.price : ""}"></label>
        <label class="field">Приоритет<input name="priority" type="number" min="1" max="999" step="1" placeholder="—" value="${product?.priority ?? ""}"><small>1 — первым в списке. Пусто — без приоритета.</small></label>
        <label class="field">Категория<select name="category_id" required>${categories.map((category) => `<option value="${escapeHtml(category.id)}" ${(product?.category_id ?? state.productCategory) === category.id ? "selected" : ""}>${escapeHtml(category.name)}</option>`).join("")}</select></label>
        ${editing ? "" : `<label class="field">Единица<select name="unit"><option value="шт.">шт.</option><option value="кг">кг</option></select></label>`}
      </div>
      <label class="field">Состав<textarea name="ingredients" maxlength="300" rows="3" placeholder="Например: томатный соус, моцарелла, пепперони">${escapeHtml(product?.ingredients || "")}</textarea><small>Показывается покупателю на карточке товара.</small></label>
      <label class="field">Фотография<input name="image" type="file" accept="image/jpeg,image/png,image/webp"><small>JPEG, PNG или WebP, не более 5 МБ</small></label>
      <label class="check"><input name="available" type="checkbox" ${product?.available === false ? "" : "checked"}> Показывать в меню этой точки / доступен для заказа</label>
      <div class="btn-row"><button class="btn btn-primary" type="submit">Сохранить</button><button class="btn" type="button" id="cancelProductEdit">Отмена</button></div>
      <p class="message" id="productEditorMessage"></p>
    </form>`;
}

async function renderProducts(page, storeId) {
  const catalog = await api(`/api/admin/stores/${storeId}/products`);
  const store = storeById(storeId);
  page.innerHTML = `
    ${pageHead(`Товары · ${store.name}`, "Общее меню из tovar.xlsx. Изменения цены, фото, состава и наличия действуют только в этой точке.", `<button class="btn btn-primary" id="addProduct">Добавить товар</button>`)}
    <div id="productEditor"></div>
    <section class="card">
      <div class="product-filters">
        <select class="input" id="productCategory" aria-label="Категория">
          <option value="">Все категории (${catalog.products.length})</option>
          ${catalog.categories.map((category) => `<option value="${escapeHtml(category.id)}" ${category.id === state.productCategory ? "selected" : ""}>${escapeHtml(category.name)} (${catalog.products.filter((product) => product.category_id === category.id).length})</option>`).join("")}
        </select>
        <input class="input" id="productSearch" type="search" placeholder="Поиск по названию">
      </div>
      <div class="product-list" id="productList"></div>
    </section>`;
  const editor = page.querySelector("#productEditor");
  const list = page.querySelector("#productList");
  const bindRows = () => list.querySelectorAll("[data-edit-product]").forEach((button) => button.addEventListener("click", () => {
    openEditor(catalog.products.find((entry) => entry.id === button.dataset.editProduct));
  }));
  const categorySelect = page.querySelector("#productCategory");
  const search = page.querySelector("#productSearch");
  // The chosen category stays after a product is saved and the list is reloaded.
  if (!catalog.categories.some((category) => category.id === state.productCategory)) state.productCategory = "";
  const showProducts = () => {
    state.productCategory = categorySelect.value;
    list.innerHTML = productRows(catalog, { query: search.value, categoryId: categorySelect.value });
    bindRows();
  };
  categorySelect.addEventListener("change", showProducts);
  search.addEventListener("input", showProducts);
  page.querySelector("#addProduct").addEventListener("click", () => openEditor(null));
  showProducts();

  function openEditor(product) {
    editor.innerHTML = productEditorHtml(product, catalog.categories);
    editor.scrollIntoView({ behavior: "smooth", block: "start" });
    const form = editor.querySelector("#productEditorForm");
    form.querySelector("#cancelProductEdit").addEventListener("click", () => { editor.innerHTML = ""; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = form.querySelector("#productEditorMessage");
      const submit = form.querySelector('[type="submit"]');
      submit.disabled = true;
      setMessage(message, "Сохраняем…");
      const data = new FormData(form);
      const payload = {
        name: data.get("name"),
        price: data.get("price"),
        category_id: data.get("category_id"),
        available: form.elements.available.checked,
        ingredients: data.get("ingredients"),
        priority: data.get("priority") ? Number(data.get("priority")) : null,
      };
      try {
        const saved = await api(product ? `/api/admin/stores/${storeId}/products/${encodeURIComponent(product.id)}` : `/api/admin/stores/${storeId}/products`, {
          method: product ? "PUT" : "POST",
          body: product ? payload : { ...payload, unit: data.get("unit") },
        });
        const photo = form.elements.image.files[0];
        if (photo) {
          const upload = new FormData();
          upload.append("image", photo);
          await api(`/api/admin/stores/${storeId}/products/${encodeURIComponent(saved.id)}/image`, { method: "POST", body: upload });
        }
        showToast("Товар сохранён");
        renderProducts(page, storeId);
      } catch (error) {
        setMessage(message, error.message, true);
        submit.disabled = false;
      }
    });
  }
}

// --- printer -----------------------------------------------------------------------

function printerFormHtml(settings) {
  const receipt = settings.receipt;
  const checkbox = (name, label) => `<label class="check"><input type="checkbox" name="${name}" ${receipt[name] ? "checked" : ""}> ${label}</label>`;
  const option = (value, current, label) => `<option value="${escapeHtml(value)}" ${String(value) === String(current) ? "selected" : ""}>${escapeHtml(label)}</option>`;
  const knownEncoding = ["cp866", "cp1251"].includes(settings.encoding);
  return `
    <form id="printerForm" class="printer-layout">
      <div class="printer-fields">
        <h3>Подключение</h3>
        <div class="form-grid">
          <label class="field">IP-адрес принтера в сети точки<input name="host" value="${escapeHtml(settings.host)}" placeholder="192.168.1.50" maxlength="253" autocomplete="off"></label>
          <label class="field">Порт<input name="port" type="number" min="1" max="65535" value="${settings.port}" required></label>
          <label class="field">Ширина строки<select name="chars_per_line">${option(48, settings.chars_per_line, "48 символов (обычно для 80 мм)")}${option(42, settings.chars_per_line, "42 символа")}</select></label>
          <label class="field">Кодировка<select name="encoding">${option("cp866", settings.encoding, "CP866 (русский)")}${option("cp1251", settings.encoding, "Windows-1251")}${knownEncoding ? "" : option(settings.encoding, settings.encoding, settings.encoding)}</select></label>
        </div>
        <details class="printer-advanced">
          <summary>Дополнительно</summary>
          <div class="form-grid">
            <label class="field">Кодовая страница ESC/POS<input name="codepage" type="number" min="0" max="255" value="${settings.codepage}" required><small>CP866 — обычно 17, Windows-1251 — обычно 46.</small></label>
            <label class="field">Ожидание ответа, сек<input name="timeout" type="number" min="0.5" max="30" step="0.5" value="${settings.timeout}" required></label>
          </div>
        </details>
        <h3>Дизайн чека для кухни</h3>
        <label class="field">Заголовок<input name="header" maxlength="48" value="${escapeHtml(receipt.header)}" placeholder="КУХНЯ"></label>
        <div class="form-grid">
          <label class="field">Размер номера заказа<select name="number_size">${[1, 2, 3, 4].map((size) => option(size, receipt.number_size, size === 1 ? "Обычный" : `Крупный ×${size}`)).join("")}</select></label>
          <label class="field">Копий<select name="copies">${[1, 2, 3].map((count) => option(count, receipt.copies, count)).join("")}</select></label>
        </div>
        <div class="printer-checks">
          ${checkbox("show_time", "Дата и время")}
          ${checkbox("show_items", "Список блюд")}
          ${checkbox("items_large", "Блюда крупным шрифтом")}
          ${checkbox("show_prices", "Цены у блюд")}
          ${checkbox("show_total", "Итоговая сумма")}
          ${checkbox("show_comment", "Комментарий клиента")}
          ${checkbox("show_service_type", "В зале / с собой")}
          ${checkbox("cut", "Отрезать бумагу")}
        </div>
        <label class="field">Нижняя строка<input name="footer" maxlength="96" value="${escapeHtml(receipt.footer)}" placeholder="Например: Приятного аппетита!"></label>
        <label class="field">Пустых строк перед отрезом<input name="feed_lines" type="number" min="0" max="10" value="${receipt.feed_lines}" required></label>
        <div class="btn-row">
          <button class="btn btn-primary" type="submit">Сохранить</button>
          <button class="btn" type="button" id="testPrint">Печать пробного чека</button>
        </div>
        <p class="message" id="printerMessage"></p>
      </div>
      <div class="receipt-preview-wrap">
        <h3>Как будет выглядеть</h3>
        <div class="receipt-paper" id="receiptPreview"></div>
        <small>Пример заказа. Вид на бумаге зависит от шрифта принтера.</small>
      </div>
    </form>`;
}

function readPrinterForm(form) {
  const value = (name) => form.elements[name].value;
  const checked = (name) => form.elements[name].checked;
  return {
    host: value("host").trim(),
    port: Number(value("port")),
    timeout: Number(value("timeout")),
    chars_per_line: Number(value("chars_per_line")),
    codepage: Number(value("codepage")),
    encoding: value("encoding"),
    receipt: {
      header: value("header"),
      number_size: Number(value("number_size")),
      show_time: checked("show_time"),
      show_items: checked("show_items"),
      items_large: checked("items_large"),
      show_prices: checked("show_prices"),
      show_total: checked("show_total"),
      show_comment: checked("show_comment"),
      show_service_type: checked("show_service_type"),
      footer: value("footer"),
      copies: Number(value("copies")),
      feed_lines: Number(value("feed_lines")),
      cut: checked("cut"),
    },
  };
}

function receiptPreviewHtml(preview) {
  return preview.lines.map((line) => {
    const style = [
      `text-align:${line.align}`,
      `font-weight:${line.bold ? 700 : 400}`,
      `font-size:${line.width}em`,
      line.height > line.width ? `transform:scaleY(${line.height / line.width});transform-origin:top;margin-bottom:${(line.height / line.width - 1) * 1.35}em` : "",
    ].join(";");
    return `<div class="receipt-row" style="${style}">${escapeHtml(line.text) || "&nbsp;"}</div>`;
  }).join("");
}

const JOB_STATUS = { queued: ["В очереди", "warn"], claimed: ["Печатается", "warn"], printed: ["Напечатан", "ok"], failed: ["Ошибка", "off"] };

function printerStatusHtml(status) {
  const online = status.agents.filter((agent) => agent.online);
  return `
    <section class="card">
      <h2>Печать в точке</h2>
      <p class="hint">Сервер ставит чеки в очередь, а программа кассы PaprikaPOS.exe в точке забирает их и печатает на принтер в локальной сети. Нужна хотя бы одна запущенная программа кассы с ключом этой точки.</p>
      <div class="agent-list">${status.agents.map((agent) => `
        <div><strong>${escapeHtml(agent.name)}</strong><span class="badge ${agent.online ? "ok" : agent.active ? "" : "off"}">${agent.online ? "Печатает" : agent.active ? "Не на связи" : "Отключена"}</span><span class="hint" style="margin:0">последний раз: ${escapeHtml(lastSeen(agent.last_seen_at))}</span></div>`).join("") || `<p class="empty">В точке нет касс — добавьте кассу в разделе «Устройства».</p>`}
      </div>
      ${!status.configured ? `<p class="warning">Укажите IP-адрес принтера ниже, иначе чеки не печатаются.</p>` : online.length ? "" : `<p class="warning">Ни одна программа кассы сейчас не на связи — чеки ждут в очереди до 15 минут.</p>`}
      ${status.jobs.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>Документ</th><th>Время</th><th>Статус</th><th>Ошибка</th></tr></thead><tbody>${status.jobs.map((job) => `
        <tr><td>${escapeHtml(job.title)}</td><td>${formatDateTime(job.created_at)}</td><td><span class="badge ${JOB_STATUS[job.status]?.[1] || ""}">${JOB_STATUS[job.status]?.[0] || escapeHtml(job.status)}</span></td><td>${escapeHtml(job.error || "")}</td></tr>`).join("")}</tbody></table></div>` : ""}
    </section>`;
}

async function renderPrinter(page, storeId) {
  const [settings, status] = await Promise.all([
    api(`/api/admin/stores/${storeId}/printer`),
    api(`/api/admin/stores/${storeId}/printer/status`),
  ]);
  const store = storeById(storeId);
  page.innerHTML = `
    ${pageHead(`Принтер · ${store.name}`, "Сетевой чековый принтер 80 мм (ESC/POS, обычно порт 9100).")}
    <div id="printerStatus">${printerStatusHtml(status)}</div>
    <section class="card">${printerFormHtml(settings)}</section>`;
  const form = page.querySelector("#printerForm");
  const message = page.querySelector("#printerMessage");
  const preview = page.querySelector("#receiptPreview");
  let previewTimer;
  let previewRequest = 0;
  const refreshPreview = async () => {
    const request = ++previewRequest;
    try {
      const result = await api(`/api/admin/stores/${storeId}/printer/preview`, { method: "POST", body: readPrinterForm(form) });
      if (request !== previewRequest) return;
      preview.style.setProperty("--receipt-columns", result.chars_per_line);
      preview.innerHTML = receiptPreviewHtml(result);
    } catch (error) {
      if (request === previewRequest) setMessage(message, error.message, true);
    }
  };
  const refreshStatus = async () => {
    try {
      page.querySelector("#printerStatus").innerHTML = printerStatusHtml(await api(`/api/admin/stores/${storeId}/printer/status`));
    } catch { /* the next refresh may work */ }
  };
  const schedulePreview = () => {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(refreshPreview, 250);
  };
  form.addEventListener("input", schedulePreview);
  form.addEventListener("change", schedulePreview);
  page.querySelector("#testPrint").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/admin/stores/${storeId}/printer/test`, { method: "POST", body: readPrinterForm(form) });
      setMessage(message, result.message);
      await refreshStatus();
      window.setTimeout(refreshStatus, 4000);
    } catch (error) {
      setMessage(message, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      await api(`/api/admin/stores/${storeId}/printer`, { method: "PUT", body: readPrinterForm(form) });
      setMessage(message, "Настройки сохранены. Новые заказы точки будут печататься так.");
      refreshStatus();
    } catch (error) {
      setMessage(message, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  refreshPreview();
}

// --- orders ------------------------------------------------------------------------

const PAYMENT_STATUS = { paid: ["Оплачен", "ok"], unpaid: ["Ждёт оплаты", "warn"], refunded: ["Возврат", "off"], cancelled: ["Отменён", "off"] };
const PRINT_STATUS = { printed: "Напечатан", queued: "В очереди печати", failed: "Не напечатан", not_attempted: "—" };
const SERVICE_LABELS = { dine_in: "В зале", takeaway: "С собой" };

async function renderOrders(page, storeId) {
  const orders = await api(`/api/admin/stores/${storeId}/orders`);
  const store = storeById(storeId);
  page.innerHTML = `
    ${pageHead(`Заказы · ${store.name}`, "Последние 50 заказов точки.")}
    <section class="card">
      ${orders.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>№</th><th>Код</th><th>Время</th><th>Откуда</th><th>Касса</th><th>Кассир</th><th>Статус</th><th>Печать</th><th class="num">Сумма</th><th></th></tr></thead><tbody>${orders.map((order) => `
        <tr>
          <td><strong>${escapeHtml(order.order_number)}</strong></td>
          <td>${escapeHtml(order.order_ref)}</td>
          <td>${formatDateTime(order.created_at)}</td>
          <td>${order.source === "pos" ? "Касса" : "Киоск"}${order.service_type ? ` · ${SERVICE_LABELS[order.service_type]}` : ""}</td>
          <td>${escapeHtml(order.register_name || "—")}</td>
          <td>${escapeHtml(order.cashier_name || "—")}</td>
          <td><span class="badge ${PAYMENT_STATUS[order.payment_status]?.[1] || ""}">${PAYMENT_STATUS[order.payment_status]?.[0] || escapeHtml(order.payment_status)}</span></td>
          <td>${PRINT_STATUS[order.print_status] || escapeHtml(order.print_status)}</td>
          <td class="num">${money(order.total)}</td>
          <td class="actions"><button class="btn btn-small" data-reprint="${escapeHtml(order.order_ref)}">Печать на кухню</button></td>
        </tr>`).join("")}</tbody></table></div>` : `<p class="empty">Заказов пока нет.</p>`}
    </section>`;
  page.querySelectorAll("[data-reprint]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const printed = await api(`/api/admin/stores/${storeId}/print`, { method: "POST", body: { order_ref: button.dataset.reprint } });
      showToast(`Чек заказа № ${printed.order_number} поставлен в очередь печати`);
    } catch (error) { showToast(error.message, true); }
  }));
}

// --- reports and shifts ----------------------------------------------------------------

function filterValues(form) {
  const params = new URLSearchParams();
  for (const [name, value] of new FormData(form)) if (value) params.set(name, value);
  return params;
}

function optionList(items, selected, label) {
  return `<option value="">${escapeHtml(label)}</option>${items.map((item) => `<option value="${item.id}" ${String(item.id) === String(selected) ? "selected" : ""}>${escapeHtml(item.name)}${item.store_name && !state.storeFilter ? ` · ${escapeHtml(item.store_name)}` : ""}</option>`).join("")}`;
}

async function filterFields(params, { withShift = false, withRegister = true } = {}) {
  const storeId = params.get("store_id") || "";
  state.storeFilter = storeId;
  const filters = await api(`/api/admin/report-filters${storeId ? `?store_id=${storeId}` : ""}`);
  let shiftOptions = "";
  if (withShift) {
    const shiftQuery = new URLSearchParams();
    for (const name of ["store_id", "register_id", "start_date", "end_date"]) if (params.get(name)) shiftQuery.set(name, params.get(name));
    const shifts = await api(`/api/admin/shifts?${shiftQuery}`);
    shiftOptions = `<label class="field">Смена<select name="shift_id">${optionList(shifts.map((shift) => ({ id: shift.shift_id, name: `№ ${shift.shift_id} · ${formatDateTime(shift.opened_at)} · ${shift.cashier_name}` })), params.get("shift_id"), "Все смены")}</select></label>`;
  }
  return `
    <label class="field">Точка<select name="store_id">${state.stores.length > 1 ? `<option value="">Все точки</option>` : ""}${state.stores.map((store) => `<option value="${store.id}" ${String(store.id) === storeId ? "selected" : ""}>${escapeHtml(store.name)}</option>`).join("")}</select></label>
    <label class="field">С даты<input type="date" name="start_date" value="${escapeHtml(params.get("start_date") || today())}"></label>
    <label class="field">По дату<input type="date" name="end_date" value="${escapeHtml(params.get("end_date") || today())}"></label>
    ${withRegister ? `<label class="field">Касса<select name="register_id">${optionList(filters.registers, params.get("register_id"), "Все кассы")}</select></label>` : ""}
    <label class="field">Кассир<select name="cashier_id">${optionList(filters.cashiers, params.get("cashier_id"), "Все кассиры")}</select></label>
    ${shiftOptions}`;
}

function groupTable(title, rows) {
  if (!rows.length) return "";
  return `
    <section class="card">
      <h2>${escapeHtml(title)}</h2>
      <div class="table-wrap"><table class="data"><thead><tr><th>Название</th><th class="num">Заказов</th><th class="num">Сумма</th></tr></thead><tbody>${rows.map((row) => `
        <tr><td>${escapeHtml(row.name)}</td><td class="num">${row.orders}</td><td class="num">${money(row.amount)}</td></tr>`).join("")}</tbody></table></div>
    </section>`;
}

function reportHtml(report) {
  const products = report.products.length
    ? `<div class="table-wrap"><table class="data"><thead><tr><th>Блюдо</th><th class="num">Количество</th><th class="num">Сумма</th></tr></thead><tbody>${report.products.map((product) => `
        <tr><td>${escapeHtml(product.name)}</td><td class="num">${product.quantity} ${escapeHtml(product.unit || "шт.")}${product.weight_grams ? ` · ≈ ${kg(product.weight_grams)}` : ""}</td><td class="num">${money(product.amount)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty">За выбранный период заказов нет.</p>`;
  return `
    <div class="metrics">
      <div><span>Заказов</span><strong>${report.orders_count}</strong></div>
      <div><span>Сумма заказов</span><strong>${money(report.total_amount)}</strong></div>
      <div><span>Оплачено</span><strong>${money(report.paid_amount)}</strong></div>
      <div><span>Наличные</span><strong>${money(report.cash_amount)}</strong></div>
      <div><span>Карта</span><strong>${money(report.card_amount)}</strong></div>
      <div><span>Не оплачено</span><strong>${money(report.unpaid_amount)}</strong></div>
      <div><span>Скидки</span><strong>${money(report.discounts_amount)}</strong></div>
      <div><span>Возвраты (${report.refunds_count})</span><strong>${money(report.refunds_amount)}</strong></div>
      <div><span>Средний заказ</span><strong>${money(report.average_order_amount)}</strong></div>
      <div><span>Единиц товара</span><strong>${report.items_count}</strong></div>
    </div>
    <div class="split">
      ${report.by_store.length > 1 ? groupTable("По точкам", report.by_store) : ""}
      ${groupTable("По кассирам", report.by_cashier)}
    </div>
    <section class="card"><h2>По блюдам</h2>${products}</section>`;
}

function routeParams() {
  const query = window.location.hash.split("?")[1] || "";
  return new URLSearchParams(query);
}

async function renderReports(page) {
  const params = routeParams();
  if (!params.has("store_id") && state.stores.length === 1) params.set("store_id", state.stores[0].id);
  page.innerHTML = `
    ${pageHead("Отчёты", "Продажи по точкам, сменам и кассирам. Неоплаченные заказы киоска входят в сумму заказов.")}
    <section class="card">
      <form id="reportForm" class="filters">${await filterFields(params, { withShift: true, withRegister: false })}
        <div class="btn-row"><button class="btn btn-primary" type="submit">Показать</button><button class="btn" type="button" id="exportCsv">CSV</button></div>
      </form>
    </section>
    <div id="reportResult"><p class="empty">Загрузка…</p></div>
    <section class="card">
      <h2>Заказы</h2>
      <p class="hint">Все заказы за период, включая отменённые и возвраты. Нажмите на заказ, чтобы открыть его.</p>
      <div id="reportOrders"><p class="empty">Загрузка…</p></div>
    </section>`;
  const form = page.querySelector("#reportForm");
  // Changing the store or period refreshes the lists of cashiers and shifts.
  form.querySelectorAll('[name="store_id"], [name="start_date"], [name="end_date"]').forEach((input) => {
    input.addEventListener("change", () => {
      const values = filterValues(form);
      if (input.name === "store_id") ["cashier_id", "shift_id"].forEach((name) => values.delete(name));
      if (input.name !== "store_id") values.delete("shift_id");
      navigate(`#/reports?${values}`);
    });
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate(`#/reports?${filterValues(form)}`);
  });
  const query = filterValues(form);
  page.querySelector("#exportCsv").addEventListener("click", () => {
    downloadCsv(`/api/admin/report.csv?${filterValues(form)}`, `paprika-report-${form.elements.start_date.value}-${form.elements.end_date.value}.csv`);
  });
  loadReportOrders(page.querySelector("#reportOrders"), query);
  try {
    page.querySelector("#reportResult").innerHTML = reportHtml(await api(`/api/admin/report?${query}`));
  } catch (error) {
    page.querySelector("#reportResult").innerHTML = `<div class="card"><p class="message is-error">${escapeHtml(error.message)}</p></div>`;
  }
}

// --- order list and order card ------------------------------------------------------------

const REPORT_ORDERS_PAGE = 50;
const PAYMENT_METHODS = { cash: "Наличные", card: "Карта" };
const KITCHEN_STATUS = { preparing: "Готовится", ready: "Готово" };

async function loadReportOrders(container, query) {
  const orders = [];
  let total = 0;
  const showMultipleStores = !query.get("store_id") && state.stores.length > 1;
  const draw = () => {
    if (!orders.length) {
      container.innerHTML = `<p class="empty">За выбранный период заказов нет.</p>`;
      return;
    }
    container.innerHTML = `
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>№</th><th>Время</th>${showMultipleStores ? "<th>Точка</th>" : ""}<th>Откуда</th><th>Кассир</th><th>Статус</th><th>Оплата</th><th class="num">Сумма</th>
      </tr></thead><tbody>${orders.map((order, index) => `
        <tr class="clickable" data-order-index="${index}">
          <td><strong>${escapeHtml(order.order_number)}</strong> <span class="hint">${escapeHtml(order.order_ref)}</span></td>
          <td>${formatDateTime(order.created_at)}</td>
          ${showMultipleStores ? `<td>${escapeHtml(order.store_name)}</td>` : ""}
          <td>${order.source === "pos" ? "Касса" : "Киоск"}${order.service_type ? ` · ${SERVICE_LABELS[order.service_type]}` : ""}</td>
          <td>${escapeHtml(order.cashier_name || "—")}</td>
          <td><span class="badge ${PAYMENT_STATUS[order.payment_status]?.[1] || ""}">${PAYMENT_STATUS[order.payment_status]?.[0] || escapeHtml(order.payment_status)}</span></td>
          <td>${PAYMENT_METHODS[order.payment_method] || "—"}</td>
          <td class="num">${money(order.total)}</td>
        </tr>`).join("")}</tbody></table></div>
      <div class="btn-row" style="margin-top:12px;align-items:center">
        <span class="hint" style="margin:0">Показано ${orders.length} из ${total}</span>
        ${orders.length < total ? `<button class="btn btn-small" id="moreOrders">Показать ещё</button>` : ""}
      </div>`;
    container.querySelectorAll("[data-order-index]").forEach((row) => row.addEventListener("click", () => {
      showOrderCard(orders[Number(row.dataset.orderIndex)]);
    }));
    container.querySelector("#moreOrders")?.addEventListener("click", loadMore);
  };
  async function loadMore() {
    const params = new URLSearchParams(query);
    params.set("limit", REPORT_ORDERS_PAGE);
    params.set("offset", orders.length);
    try {
      const result = await api(`/api/admin/report/orders?${params}`);
      total = result.total;
      orders.push(...result.orders);
      draw();
    } catch (error) {
      container.innerHTML = `<p class="message is-error">${escapeHtml(error.message)}</p>`;
    }
  }
  await loadMore();
}

function itemDetails(item) {
  const details = [];
  if (item.weight_grams) {
    details.push(item.portion_kind === "price" ? `на ${money(item.portion_amount)} ≈ ${kg(item.weight_grams)}` : kg(item.weight_grams));
  }
  if (item.variant) details.push(item.variant.name);
  for (const modifier of item.modifiers || []) details.push(`+ ${modifier.name}${modifier.price ? ` (${money(modifier.price)})` : ""}`);
  return details.map((detail) => `<br><small class="hint">${escapeHtml(detail)}</small>`).join("");
}

function showOrderCard(order) {
  const row = (label, value) => (value === null || value === undefined || value === "" ? "" : `<div><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`);
  const status = PAYMENT_STATUS[order.payment_status];
  openModal(`Заказ № ${order.order_number} · ${order.order_ref}`, `
    <p class="hint">${escapeHtml(order.store_name)} · ${order.source === "pos" ? "оформлен на кассе" : "оформлен в киоске"} · ${formatDateTime(order.created_at)}</p>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Блюдо</th><th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Сумма</th></tr></thead>
      <tbody>${order.items.map((item) => `
        <tr>
          <td>${escapeHtml(item.name)}${itemDetails(item)}</td>
          <td class="num">${item.quantity} ${escapeHtml(item.unit || "шт.")}</td>
          <td class="num">${money(item.unit_price)}</td>
          <td class="num">${money(item.unit_price * item.quantity)}</td>
        </tr>`).join("")}</tbody>
    </table></div>
    <div class="report-lines">
      ${order.discount ? row("Подытог", money(order.subtotal)) + row("Скидка", `− ${money(order.discount)}`) : ""}
      ${row("Итого", money(order.total))}
      ${row("Статус", `<span class="badge ${status?.[1] || ""}">${status?.[0] || escapeHtml(order.payment_status)}</span>`)}
      ${row("Оплата", PAYMENT_METHODS[order.payment_method] ? escapeHtml(PAYMENT_METHODS[order.payment_method]) : "")}
      ${order.payment_method === "cash" ? row("Получено", money(order.amount_received)) + row("Сдача", money(order.change_due)) : ""}
      ${row("Оплачен", order.paid_at ? formatDateTime(order.paid_at) : "")}
      ${row("Кассир", order.cashier_name ? escapeHtml(order.cashier_name) : "")}
      ${row("Смена", order.shift_id ? `№ ${order.shift_id}` : "")}
      ${row("В зале / с собой", SERVICE_LABELS[order.service_type] || "")}
      ${row("Кухня", KITCHEN_STATUS[order.kitchen_status] || "")}
      ${row("Печать на кухню", PRINT_STATUS[order.print_status] || "")}
      ${row("Отменён", order.cancelled_at ? formatDateTime(order.cancelled_at) : "")}
      ${row("Возврат", order.refunded_at ? formatDateTime(order.refunded_at) : "")}
    </div>
    ${order.comment ? `<p class="warning">Комментарий: ${escapeHtml(order.comment)}</p>` : ""}`, { wide: true });
}

function shiftReportHtml(shift) {
  const row = (label, value) => `<div><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`;
  return `
    <p class="hint">${escapeHtml(shift.store_name)} · ${escapeHtml(shift.register_name)} · открыл(а) ${escapeHtml(shift.cashier_name)}${shift.closed_by_name ? `, закрыл(а) ${escapeHtml(shift.closed_by_name)}` : ""}</p>
    <div class="report-lines">
      ${row("Открыта", formatDateTime(shift.opened_at))}
      ${row("Закрыта", shift.open ? "смена открыта" : formatDateTime(shift.closed_at))}
      ${row("Продаж", shift.sales_count)}
      ${row("Продажи всего", money(shift.sales_total))}
      ${row("  наличные", money(shift.sales_cash))}
      ${row("  карта", money(shift.sales_card))}
      ${row("Скидки", money(shift.discounts_total))}
      ${row(`Возвраты (${shift.refunds_count})`, money(shift.refunds_total))}
      ${row("Выручка", money(shift.revenue))}
      ${row("Наличные на начало", money(shift.opening_cash))}
      ${row("Наличные в кассе (ожидается)", money(shift.expected_cash))}
      ${shift.counted_cash !== null && shift.counted_cash !== undefined ? row("Пересчитано", money(shift.counted_cash)) + row("Расхождение", money(shift.difference)) : ""}
    </div>`;
}

async function renderShifts(page) {
  const params = routeParams();
  if (!params.has("store_id") && state.stores.length === 1) params.set("store_id", state.stores[0].id);
  page.innerHTML = `
    ${pageHead("Смены", "Смены касс с Z-отчётами. Открытые смены показывают текущий X-отчёт.")}
    <section class="card">
      <form id="shiftForm" class="filters">${await filterFields(params)}
        <div class="btn-row"><button class="btn btn-primary" type="submit">Показать</button></div>
      </form>
    </section>
    <section class="card" id="shiftList"><p class="empty">Загрузка…</p></section>`;
  const form = page.querySelector("#shiftForm");
  form.querySelector('[name="store_id"]').addEventListener("change", () => {
    const values = filterValues(form);
    ["register_id", "cashier_id"].forEach((name) => values.delete(name));
    navigate(`#/shifts?${values}`);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate(`#/shifts?${filterValues(form)}`);
  });
  const shifts = await api(`/api/admin/shifts?${filterValues(form)}`);
  const list = page.querySelector("#shiftList");
  list.innerHTML = shifts.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>№</th><th>Точка</th><th>Касса</th><th>Открыта</th><th>Закрыта</th><th>Кассир</th><th class="num">Продаж</th><th class="num">Выручка</th><th class="num">Расхождение</th><th></th></tr></thead><tbody>${shifts.map((shift, index) => `
    <tr class="clickable" data-shift-index="${index}">
      <td><strong>${shift.shift_id}</strong></td>
      <td>${escapeHtml(shift.store_name)}</td>
      <td>${escapeHtml(shift.register_name)}</td>
      <td>${formatDateTime(shift.opened_at)}</td>
      <td>${shift.open ? `<span class="badge warn">Открыта</span>` : formatDateTime(shift.closed_at)}</td>
      <td>${escapeHtml(shift.cashier_name)}</td>
      <td class="num">${shift.sales_count}</td>
      <td class="num">${money(shift.revenue)}</td>
      <td class="num">${shift.difference === null || shift.difference === undefined ? "—" : money(shift.difference)}</td>
      <td class="actions"><button class="btn btn-small" data-shift-report="${shift.shift_id}" data-store="${shift.store_id}">Продажи</button></td>
    </tr>`).join("")}</tbody></table></div>` : `<p class="empty">Смен за выбранный период нет.</p>`;
  list.querySelectorAll("[data-shift-index]").forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("[data-shift-report]")) return;
    const shift = shifts[Number(row.dataset.shiftIndex)];
    openModal(`Смена № ${shift.shift_id} · ${shift.open ? "X-отчёт" : "Z-отчёт"}`, shiftReportHtml(shift));
  }));
  list.querySelectorAll("[data-shift-report]").forEach((button) => button.addEventListener("click", () => {
    const shift = shifts.find((entry) => String(entry.shift_id) === button.dataset.shiftReport);
    const values = new URLSearchParams({ store_id: button.dataset.store, shift_id: button.dataset.shiftReport });
    values.set("start_date", new Date(shift.opened_at).toLocaleDateString("sv-SE"));
    values.set("end_date", new Date(shift.closed_at || Date.now()).toLocaleDateString("sv-SE"));
    navigate(`#/reports?${values}`);
  }));
}

// --- managers (owner) -------------------------------------------------------------------

function storeChecks(selected = []) {
  return state.stores.map((store) => `<label class="check"><input type="checkbox" name="store_ids" value="${store.id}" ${selected.includes(store.id) ? "checked" : ""}> ${escapeHtml(store.name)}</label>`).join("") || `<p class="empty">Сначала создайте точки.</p>`;
}

function checkedStores(container) {
  return [...container.querySelectorAll('[name="store_ids"]:checked')].map((input) => Number(input.value));
}

async function renderUsers(page) {
  const users = await api("/api/admin/users");
  const storeNames = (ids) => ids.map((id) => storeById(id)?.name).filter(Boolean).join(", ") || "нет доступа";
  page.innerHTML = `
    ${pageHead("Управляющие", "Управляющий видит только выданные ему точки: устройства, сотрудников, товары, принтер и отчёты.")}
    <section class="card">
      <h2>Новый пользователь</h2>
      <form id="userForm" class="printer-fields">
        <div class="form-grid">
          <label class="field">Имя<input name="name" maxlength="60" required></label>
          <label class="field">Логин<input name="login" pattern="[A-Za-z0-9_.@-]{3,40}" required autocomplete="off"><small>Латиница, цифры, . _ - @</small></label>
          <label class="field">Пароль<input name="password" type="password" minlength="8" required autocomplete="new-password"><small>Не короче 8 символов</small></label>
          <label class="field">Роль<select name="role"><option value="manager">Управляющий</option><option value="owner">Владелец (все точки)</option></select></label>
        </div>
        <div class="field">Доступ к точкам</div>
        <div class="printer-checks" id="newUserStores">${storeChecks()}</div>
        <div class="btn-row"><button class="btn btn-primary" type="submit">Создать</button></div>
        <p class="message" id="userMessage"></p>
      </form>
    </section>
    <section class="card">
      <h2>Пользователи</h2>
      <div class="table-wrap"><table class="data"><thead><tr><th>Имя</th><th>Логин</th><th>Роль</th><th>Точки</th><th>Статус</th><th></th></tr></thead><tbody>${users.map((user) => `
        <tr>
          <td><strong>${escapeHtml(user.name)}</strong></td>
          <td>${escapeHtml(user.login)}</td>
          <td>${user.role === "owner" ? "Владелец" : "Управляющий"}</td>
          <td>${user.role === "owner" ? "все" : escapeHtml(storeNames(user.store_ids))}</td>
          <td><span class="badge ${user.active ? "ok" : "off"}">${user.active ? "Активен" : "Отключён"}</span></td>
          <td class="actions">
            ${user.role === "manager" ? `<button class="btn btn-small" data-stores="${user.id}">Доступ к точкам</button>` : ""}
            <button class="btn btn-small" data-password="${user.id}">Сменить пароль</button>
            ${user.id !== state.user.id ? `<button class="btn btn-small ${user.active ? "btn-danger" : ""}" data-active="${user.id}">${user.active ? "Отключить" : "Включить"}</button>` : ""}
          </td>
        </tr>`).join("")}</tbody></table></div>
    </section>`;
  const form = page.querySelector("#userForm");
  form.elements.role.addEventListener("change", () => {
    page.querySelector("#newUserStores").hidden = form.elements.role.value === "owner";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/admin/users", {
        method: "POST",
        body: {
          name: form.elements.name.value,
          login: form.elements.login.value.trim(),
          password: form.elements.password.value,
          role: form.elements.role.value,
          store_ids: checkedStores(form),
        },
      });
      showToast("Пользователь создан");
      renderUsers(page);
    } catch (error) { setMessage(page.querySelector("#userMessage"), error.message, true); }
  });
  const find = (id) => users.find((user) => user.id === Number(id));
  const update = async (id, body, success) => {
    try {
      await api(`/api/admin/users/${id}`, { method: "PUT", body });
      if (success) showToast(success);
      closeModal();
      renderUsers(page);
    } catch (error) { showToast(error.message, true); }
  };
  page.querySelectorAll("[data-stores]").forEach((button) => button.addEventListener("click", () => {
    const user = find(button.dataset.stores);
    const modal = openModal(`Доступ: ${user.name}`, `
      <p class="hint">Отметьте точки, которыми управляет этот пользователь.</p>
      <div class="printer-checks">${storeChecks(user.store_ids)}</div>
      <div class="btn-row" style="margin-top:16px"><button class="btn btn-primary" id="saveStores">Сохранить</button><button class="btn" data-close>Отмена</button></div>`);
    modal.querySelectorAll("[data-close]").forEach((close) => close.addEventListener("click", closeModal));
    modal.querySelector("#saveStores").addEventListener("click", () => update(user.id, { store_ids: checkedStores(modal) }, "Доступ сохранён"));
  }));
  page.querySelectorAll("[data-password]").forEach((button) => button.addEventListener("click", () => {
    const password = window.prompt("Новый пароль (не короче 8 символов)");
    if (password === null) return;
    if (password.length < 8) {
      showToast("Пароль должен быть не короче 8 символов", true);
      return;
    }
    update(Number(button.dataset.password), { password }, "Пароль изменён");
  }));
  page.querySelectorAll("[data-active]").forEach((button) => button.addEventListener("click", () => {
    const user = find(button.dataset.active);
    update(user.id, { active: !user.active });
  }));
}

// --- start -------------------------------------------------------------------------

window.addEventListener("hashchange", route);

async function start() {
  if (!state.token) {
    renderLogin();
    return;
  }
  try {
    await refreshMe();
    route();
  } catch {
    renderLogin();
  }
}

start();
