// Device activation for the kiosk, the cash register and the queue screen.
// The admin panel gives each device a link ending in #activate=<key>. The key is kept in
// this browser and sent with every request; the "#" part never reaches the server logs.
const DEVICE_KEY_STORAGE = "paprika-device-key";

const paprikaDevice = (() => {
  let key = "";
  try { key = window.localStorage.getItem(DEVICE_KEY_STORAGE) || ""; } catch { /* storage blocked */ }
  const match = window.location.hash.match(/activate=([A-Za-z0-9_-]+)/);
  if (match) {
    key = match[1];
    try { window.localStorage.setItem(DEVICE_KEY_STORAGE, key); } catch { /* storage blocked */ }
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  // An activation link opened on a page that is already open changes only the "#" part:
  // keep the new key and start the page again with it.
  window.addEventListener("hashchange", () => {
    const next = window.location.hash.match(/activate=([A-Za-z0-9_-]+)/);
    if (!next) return;
    try { window.localStorage.setItem(DEVICE_KEY_STORAGE, next[1]); } catch { /* storage blocked */ }
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.location.reload();
  });
  return {
    get key() { return key; },
    headers() { return key ? { "X-Device-Key": key } : {}; },
    forget() {
      key = "";
      try { window.localStorage.removeItem(DEVICE_KEY_STORAGE); } catch { /* storage blocked */ }
    },
  };
})();

function deviceSetupHtml(title, message) {
  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  return `
    <section class="device-setup">
      <h1>${escape(title)}</h1>
      <p>${escape(message)}</p>
      <p class="device-setup-hint">Откройте на этом устройстве ссылку активации из админпанели (раздел «Устройства» точки) или вставьте ключ устройства:</p>
      <form class="device-setup-form" id="deviceSetupForm">
        <input name="key" autocomplete="off" placeholder="Ключ устройства" required>
        <button type="submit">Активировать</button>
      </form>
    </section>`;
}

// Shows the activation screen in `container`; reloads the page with the entered key.
function showDeviceSetup(container, title, message) {
  container.innerHTML = deviceSetupHtml(title, message);
  container.querySelector("#deviceSetupForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = event.currentTarget.elements.key.value.trim();
    if (!value) return;
    window.location.hash = `activate=${encodeURIComponent(value)}`;
    window.location.reload();
  });
}
