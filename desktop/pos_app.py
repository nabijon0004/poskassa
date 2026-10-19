"""Paprika POS desktop app: opens the cash register page (/pos) in a WebView2 window.

It also prints for its store: the server cannot reach printers in the store's local
network, so this program takes the store's print jobs and sends them to the printer.

Settings live in pos-config.json next to the program:
    {"server_url": "https://paprika.example.com", "device_key": "...", "fullscreen": false}

Keys: F11 — full screen, F5 — reload, Ctrl+Shift+S — server address and register key.

pos_cloud.py builds a second program with the cloud server address built in: it asks
only for the register key and keeps its own settings, log and browser data.
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path

import webview

APP_TITLE = "Paprika — Касса"
DEFAULT_CONFIG = {"server_url": "http://127.0.0.1:8000", "device_key": "", "fullscreen": False}
PRINT_POLL_SECONDS = 2

# Next to PaprikaPOS.exe when packaged, next to this script otherwise.
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = APP_DIR / "pos-config.json"
# Set by pos_cloud.py: the server address is fixed and not asked for.
PRESET_SERVER_URL: str | None = None

log = logging.getLogger("paprika-pos")

# Hotkeys added to every page, including the server's /pos page.
HOTKEYS_JS = """
if (!window.__paprikaHotkeys) {
  window.__paprikaHotkeys = true;
  window.addEventListener("keydown", (event) => {
    if (!window.pywebview || !window.pywebview.api) return;
    if (event.key === "F11") { event.preventDefault(); window.pywebview.api.toggle_fullscreen(); }
    if (event.key === "F5") { event.preventDefault(); window.location.reload(); }
    if (event.ctrlKey && event.shiftKey && event.code === "KeyS") { event.preventDefault(); window.pywebview.api.show_settings(); }
  });
}
"""


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        save_config(config)
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", CONFIG_PATH, exc)
    return config


def save_config(config: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save %s: %s", CONFIG_PATH, exc)


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Адрес сервера должен быть вида http://192.168.1.10:8000")
    return url


def server_is_up(server_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{server_url}/api/health", timeout=3) as response:
            return json.load(response).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log.info("Server %s is not reachable: %s", server_url, exc)
        return False


def device_kind(server_url: str, key: str) -> str | None:
    """The kind of the device the key belongs to, or None if the server rejects it."""
    request = urllib.request.Request(f"{server_url}/api/device", headers={"X-Device-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response).get("kind")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        log.info("Register key check failed: %s", exc)
        return None


def settings_page(server_url: str, message: str, device_key: str = "") -> str:
    readonly = " readonly" if PRESET_SERVER_URL else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{APP_TITLE}</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f3f1ec;
         color: #23201b; font: 16px/1.5 "Segoe UI", sans-serif; }}
  .card {{ width: min(460px, calc(100% - 32px)); padding: 32px; border-radius: 22px; background: #fffefb;
           box-shadow: 0 10px 30px rgba(35, 32, 27, .12); }}
  h1 {{ margin: 0 0 6px; font-size: 24px; }}
  p {{ margin: 0 0 18px; color: #7d786e; }}
  label {{ display: grid; gap: 6px; font-weight: 600; }}
  input {{ min-height: 52px; padding: 0 14px; border: 1px solid #e4e0d6; border-radius: 12px; font: inherit; }}
  button {{ width: 100%; min-height: 54px; margin-top: 14px; border: 0; border-radius: 14px; color: white;
            background: #df4e31; font: 700 17px "Segoe UI", sans-serif; cursor: pointer; }}
  .hint {{ margin-top: 14px; font-size: 13px; }}
  #error {{ min-height: 24px; margin: 12px 0 0; color: #c43f25; font-weight: 600; }}
</style></head>
<body><div class="card">
  <h1>Paprika · Касса</h1>
  <p>{escape(message)}</p>
  <label>Адрес сервера<input id="url" value="{escape(server_url)}"{readonly} placeholder="https://paprika.example.com"></label>
  <label style="margin-top:12px">Ключ кассы<input id="key" value="{escape(device_key)}" placeholder="Из админпанели: точка → Устройства" autocomplete="off"></label>
  <button id="connect">Подключиться</button>
  <p id="error"></p>
  <p class="hint">Ключ кассы выдаётся в админпанели (точка → Устройства → Касса). С ним программа работает кассой этой точки и печатает её чеки. F11 — полный экран, Ctrl+Shift+S — этот экран.</p>
</div>
<script>
  const connect = async () => {{
    const button = document.getElementById("connect");
    button.disabled = true;
    button.textContent = "Подключение…";
    const error = await window.pywebview.api.connect(document.getElementById("url").value, document.getElementById("key").value);
    if (error) {{
      document.getElementById("error").textContent = error;
      button.disabled = false;
      button.textContent = "Подключиться";
    }}
  }};
  document.getElementById("connect").addEventListener("click", connect);
  document.querySelectorAll("input").forEach((input) => input.addEventListener("keydown", (event) => {{ if (event.key === "Enter") connect(); }}));
</script>
</body></html>"""


class Api:
    """Functions the pages can call through window.pywebview.api."""

    def __init__(self, config: dict) -> None:
        self.config = config
        # Underscore: pywebview does not expose it to the page (it logged errors walking the window).
        self._window: webview.Window | None = None

    def connect(self, url: str, key: str = "") -> str:
        try:
            server_url = PRESET_SERVER_URL or normalize_url(url)
        except ValueError as exc:
            return str(exc)
        if not server_is_up(server_url):
            return f"Нет связи с {server_url}. Проверьте адрес сервера и интернет."
        key = key.strip()
        if not key:
            return "Укажите ключ кассы из админпанели."
        kind = device_kind(server_url, key)
        if kind != "register":
            return "Ключ не подходит: нужен ключ кассы (не киоска и не экрана очереди)."
        self.config["server_url"] = server_url
        self.config["device_key"] = key
        save_config(self.config)
        log.info("Connected to %s", server_url)
        self._window.load_url(register_url(server_url, key))
        return ""

    def show_settings(self) -> None:
        self._window.load_html(
            settings_page(self.config["server_url"], "Укажите ключ кассы." if PRESET_SERVER_URL else "Укажите адрес сервера и ключ кассы.", self.config.get("device_key", ""))
        )

    def toggle_fullscreen(self) -> None:
        self._window.toggle_fullscreen()
        self.config["fullscreen"] = not self.config.get("fullscreen", False)
        save_config(self.config)

    def print_raw(self, host: str, port: int, timeout: float, data: str) -> str:
        """Print a receipt made by the page itself (offline mode); returns an error text or ""."""
        try:
            send_to_printer(host, int(port), float(timeout or 3), base64.b64decode(data))
        except (OSError, ValueError) as exc:
            log.warning("Offline print to %s:%s failed: %s", host, port, exc)
            return f"нет связи с принтером {host}:{port}"
        log.info("Printed an offline receipt")
        return ""


def register_url(server_url: str, key: str) -> str:
    # The page keeps the key; the "#" part is not sent to the server.
    return f"{server_url}/pos#activate={urllib.parse.quote(key, safe='')}"


def send_to_printer(host: str, port: int, timeout: float, data: bytes) -> None:
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(data)


class PrintAgent(threading.Thread):
    """Takes the store's print jobs from the server and prints them on the local printer."""

    def __init__(self, config: dict) -> None:
        super().__init__(name="print-agent", daemon=True)
        self.config = config
        self.last_error = ""

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        server_url = normalize_url(self.config["server_url"])
        request = urllib.request.Request(
            f"{server_url}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"X-Device-Key": self.config.get("device_key", ""), "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def report_error(self, message: str) -> None:
        # The same problem is logged once, not every two seconds.
        if message != self.last_error:
            log.warning("Print agent: %s", message)
            self.last_error = message

    def print_job(self, printer: dict, job: dict) -> None:
        data = base64.b64decode(job["data"])
        try:
            if not printer.get("host"):
                raise OSError("принтер точки не настроен в админпанели")
            send_to_printer(printer["host"], printer["port"], printer.get("timeout", 3), data)
            result = {"status": "printed"}
            log.info("Printed %s", job["title"])
        except OSError as exc:
            result = {"status": "failed", "error": f"Нет связи с принтером {printer.get('host')}:{printer.get('port')}: {exc}"[:300]}
            self.report_error(result["error"])
        self.request("POST", f"/api/agent/print-jobs/{job['id']}", result)

    def run(self) -> None:
        while True:
            if self.config.get("device_key"):
                try:
                    batch = self.request("GET", "/api/agent/print-jobs")
                    for job in batch["jobs"]:
                        self.print_job(batch["printer"], job)
                    if self.last_error and not batch["jobs"]:
                        self.last_error = ""
                except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                    self.report_error(f"нет связи с сервером: {exc}")
            time.sleep(PRINT_POLL_SECONDS)


def main(preset_server_url: str | None = None, name: str = "pos") -> None:
    """name prefixes the settings, log and browser data files next to the program."""
    global CONFIG_PATH, PRESET_SERVER_URL
    CONFIG_PATH = APP_DIR / f"{name}-config.json"
    # A handler, not basicConfig(encoding=...): that needs Python 3.9, the Windows 7 build uses 3.8.
    logging.basicConfig(
        handlers=[logging.FileHandler(APP_DIR / f"{name}-app.log", encoding="utf-8")],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    if preset_server_url:
        PRESET_SERVER_URL = normalize_url(preset_server_url)
        if config.get("server_url") != PRESET_SERVER_URL:
            config["server_url"] = PRESET_SERVER_URL
            save_config(config)
    api = Api(config)
    try:
        server_url = normalize_url(config["server_url"])
    except (KeyError, ValueError):
        server_url = DEFAULT_CONFIG["server_url"]
    online = server_is_up(server_url)
    key = config.get("device_key", "")
    # With a register key the page opens even without internet: it is kept on this computer
    # since the last online start, and sales are queued until the connection returns.
    ready = bool(key)
    message = "Укажите ключ кассы из админпанели." if PRESET_SERVER_URL else "Укажите адрес сервера и ключ кассы из админпанели."
    window = webview.create_window(
        APP_TITLE,
        url=register_url(server_url, key) if ready else None,
        html=None if ready else settings_page(server_url, message, key),
        js_api=api,
        width=1366,
        height=820,
        min_size=(1024, 680),
        fullscreen=bool(config.get("fullscreen")),
        maximized=not config.get("fullscreen"),
        text_select=True,
    )
    api._window = window
    PrintAgent(config).start()
    window.events.loaded += lambda: window.evaluate_js(HOTKEYS_JS)
    log.info("Starting, server %s (%s)", server_url, "online" if online else "offline")
    webview.start(
        gui="edgechromium",
        private_mode=False,
        storage_path=str(APP_DIR / ("webview-data" if name == "pos" else f"webview-data-{name}")),
    )


if __name__ == "__main__":
    main()
