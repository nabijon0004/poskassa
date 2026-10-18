// Queue screen: shows order numbers being prepared and ready ones; refreshed every few seconds.
const POLL_MS = 3000;

const preparingBox = document.querySelector("#preparing");
const readyBox = document.querySelector("#ready");
const offlineBadge = document.querySelector("#offline");
const soundButton = document.querySelector("#sound");
const clock = document.querySelector("#clock");
const storeName = document.querySelector("#storeName");
let pollTimer;

let knownReady = null; // null until the first load, so old ready orders are not announced
let soundOn = false;
const freshUntil = new Map(); // number -> time the highlight ends

function renderNumbers(box, numbers, fresh = new Set()) {
  box.replaceChildren(...numbers.map((number) => {
    const tile = document.createElement("div");
    tile.className = fresh.has(number) ? "number fresh" : "number";
    tile.textContent = number;
    return tile;
  }));
}

function announceReady(numbers) {
  if (!soundOn) return;
  // One chime, then each number in turn.
  numbers.forEach((number, index) => {
    window.setTimeout(() => announce(`Заказ номер ${number} готов`, "ru-RU", { chime: index === 0 }), index * 4500);
  });
}

async function refresh() {
  try {
    const response = await fetch("/api/queue", { cache: "no-store", headers: paprikaDevice.headers() });
    if (response.status === 401 || response.status === 403) {
      const data = await response.json().catch(() => ({}));
      paprikaDevice.forget();
      showSetup(data.detail || "Ключ экрана очереди не подходит.");
      return;
    }
    if (!response.ok) throw new Error(String(response.status));
    const queue = await response.json();
    storeName.textContent = queue.store_name || "";
    const now = Date.now();
    const newlyReady = knownReady ? queue.ready.filter((number) => !knownReady.has(number)) : [];
    newlyReady.forEach((number) => freshUntil.set(number, now + 6000));
    for (const [number, until] of freshUntil) if (until < now || !queue.ready.includes(number)) freshUntil.delete(number);
    knownReady = new Set(queue.ready);
    renderNumbers(preparingBox, queue.preparing);
    renderNumbers(readyBox, queue.ready, new Set(freshUntil.keys()));
    if (newlyReady.length) announceReady(newlyReady);
    offlineBadge.hidden = true;
  } catch {
    offlineBadge.hidden = false; // keep showing the last known numbers
  }
}

function tick() {
  clock.textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

// Browsers allow sound only after a tap on the page.
function enableSound() {
  soundOn = true;
  soundButton.hidden = true;
  playChime();
}
soundButton.addEventListener("click", enableSound);
document.body.addEventListener("click", () => { if (!soundOn) enableSound(); });

function showSetup(message) {
  window.clearInterval(pollTimer);
  showDeviceSetup(document.querySelector(".board"), "Экран очереди не активирован", message);
  document.querySelector(".board").classList.add("is-setup");
}

tick();
window.setInterval(tick, 10000);
if (!paprikaDevice.key) {
  showSetup("Этот экран ещё не привязан к точке продаж.");
} else {
  refresh();
  pollTimer = window.setInterval(refresh, POLL_MS);
}
