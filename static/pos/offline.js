// Offline work of the cash register.
// While online the register keeps a copy of the menu, cashiers, printer settings and the
// current shift. Without internet it checks PINs locally, records shifts and sales in a
// queue, prints on the store's printer itself (through PaprikaPOS.exe) and sends the queue
// to the server when the connection returns (/api/pos/offline-sync).

const OFFLINE_DATA_KEY = "paprika-pos-offline-data";
const OUTBOX_KEY = "paprika-pos-outbox";
const OFFLINE_NUMBER_KEY = "paprika-pos-offline-number";
const OFFLINE_NUMBER_MAX = 30;

const offline = {
  // --- local storage -----------------------------------------------------------------

  read(key, fallback) {
    try {
      const value = window.localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch { return fallback; }
  },

  write(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch { return false; }
  },

  get data() { return this.read(OFFLINE_DATA_KEY, null); },

  // The copy belongs to the activated register; a new key means another register.
  saveData(data) { this.write(OFFLINE_DATA_KEY, { ...data, device_key: paprikaDevice.key }); },

  // The register's current shift as last seen online, so it is known without the server.
  rememberShift(shift) {
    const data = this.usableData();
    if (data) this.write(OFFLINE_DATA_KEY, { ...data, shift: shift && !shift.offline ? shift : null });
  },

  usableData() {
    const data = this.data;
    return data && data.device_key === paprikaDevice.key ? data : null;
  },

  get outbox() { return this.read(OUTBOX_KEY, []); },

  // Each operation remembers the register it was made on: if this computer is switched to a
  // register of another store, sales not sent yet still go to their own store.
  enqueue(operation) {
    const outbox = this.outbox;
    outbox.push({ ...operation, device_key: paprikaDevice.key });
    if (!this.write(OUTBOX_KEY, outbox)) throw new Error("Не удалось сохранить продажу на этом компьютере.");
  },

  removeSynced(ids) {
    const done = new Set(ids);
    this.write(OUTBOX_KEY, this.outbox.filter((operation) => !done.has(operation.id)));
  },

  markPrinted(id) {
    this.write(OUTBOX_KEY, this.outbox.map((operation) => (operation.id === id ? { ...operation, printed: true } : operation)));
  },

  pendingSales() { return this.outbox.filter((operation) => operation.type === "sale"); },

  // --- delivery customers (same search as /api/pos/customers) ---------------------------

  findCustomers(digits) {
    const customers = (this.usableData()?.customers || []).filter((customer) => customer.phone.includes(digits));
    return customers.sort((a, b) => (b.phone === digits) - (a.phone === digits)).slice(0, 6);
  },

  // A delivery sold offline: its address is found again before the server copy is refreshed.
  rememberCustomer(phone, address) {
    const data = this.usableData();
    if (!data) return;
    const previous = (data.customers || []).find((customer) => customer.phone === phone);
    const customer = { phone, address, orders_count: (previous?.orders_count || 0) + 1, last_order_at: new Date().toISOString() };
    this.write(OFFLINE_DATA_KEY, { ...data, customers: [customer, ...(data.customers || []).filter((entry) => entry !== previous)] });
  },

  // Offline orders are numbered О1..О30 so they never look like the server's numbers.
  nextNumber() {
    const next = (Number(this.read(OFFLINE_NUMBER_KEY, 0)) % OFFLINE_NUMBER_MAX) + 1;
    this.write(OFFLINE_NUMBER_KEY, next);
    return `О${next}`;
  },

  // --- PIN check without the server (same PBKDF2 as app/pos.py hash_pin) ----------------

  async pinMatches(pin, stored) {
    const [saltHex, digestHex] = String(stored).split("$");
    if (!saltHex || !digestHex || !window.crypto?.subtle) return false;
    const salt = new Uint8Array(saltHex.match(/../g).map((byte) => parseInt(byte, 16)));
    const key = await window.crypto.subtle.importKey("raw", new TextEncoder().encode(pin), "PBKDF2", false, ["deriveBits"]);
    const bits = await window.crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations: 120000, hash: "SHA-256" }, key, 256);
    const hex = [...new Uint8Array(bits)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    return hex === digestHex;
  },

  async findCashier(pin) {
    const data = this.usableData();
    for (const cashier of data?.cashiers || []) {
      if (await this.pinMatches(pin, cashier.pin_hash)) return cashier;
    }
    return null;
  },

  // --- local printing ---------------------------------------------------------------

  canPrint() { return Boolean(window.pywebview?.api?.print_raw); },

  async print(bytes) {
    const printer = this.usableData()?.printer;
    const system = printer?.connection_type === "system";
    if (!(system ? printer.system_name : printer?.host)) return "Принтер точки не настроен.";
    if (!this.canPrint()) return "Печать без интернета работает только в программе кассы PaprikaPOS.exe.";
    const app = window.pywebview.api;
    if (system && !app.print_to_printer) return "Обновите программу кассы: эта версия не печатает на принтер Windows.";
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    const data = window.btoa(binary);
    try {
      return await (system ? app.print_to_printer(printer.system_name, data) : app.print_raw(printer.host, printer.port, printer.timeout, data)) || "";
    } catch (error) {
      return String(error?.message || error);
    }
  },

  // --- sync -------------------------------------------------------------------------

  // Operations made on this register (older ones without a key belong to it too).
  ownOperations() {
    return this.outbox.filter((operation) => !operation.device_key || operation.device_key === paprikaDevice.key);
  },

  async sync() {
    const outbox = this.outbox;
    if (!outbox.length) return { synced: 0, error: null };
    // Send the oldest register's operations first, each with that register's key.
    const key = outbox[0].device_key || paprikaDevice.key;
    const operations = outbox.filter((operation) => (operation.device_key || paprikaDevice.key) === key);
    const response = await fetch("/api/pos/offline-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Device-Key": key },
      // cashier_name and device_key are kept only on this computer.
      body: JSON.stringify({
        operations: operations.slice(0, 200).map(({ cashier_name: _name, device_key: _key, ...operation }) => operation),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? "Сервер не принял данные продажи." : data.detail;
      return { synced: 0, error: detail || `Ошибка сервера ${response.status}` };
    }
    const done = data.results.filter((result) => result.status === "ok").map((result) => result.id);
    this.removeSynced(done);
    const failed = data.results.find((result) => result.status === "error");
    return { synced: done.length, error: failed ? failed.error : null };
  },
};

// --- receipts (same layout as app/main.py build_receipt / build_customer_receipt) ----------

const receipts = {
  CHAR_REPLACEMENTS: {
    "≈": "~", "ғ": "г", "Ғ": "Г", "ӣ": "и", "Ӣ": "И", "қ": "к", "Қ": "К",
    "ӯ": "у", "Ӯ": "У", "ҳ": "х", "Ҳ": "Х", "ҷ": "ч", "Ҷ": "Ч",
  },
  SERVICE_LABELS: { dine_in: "В зале", takeaway: "С собой", delivery: "Доставка" },

  line(text, { align = "left", bold = false, width = 1, height = 1 } = {}) {
    return { text, align, bold, width, height };
  },

  // Python's textwrap.wrap(text, width, subsequent_indent=indent, break_long_words=True).
  wrap(text, width, indent = "") {
    const words = String(text).split(/\s+/).filter(Boolean);
    const lines = [];
    while (words.length) {
      const prefix = lines.length ? indent : "";
      const room = width - prefix.length;
      let current = "";
      while (words.length) {
        const word = words[0];
        const separator = current ? 1 : 0;
        if (current.length + separator + word.length <= room) {
          current += (separator ? " " : "") + word;
          words.shift();
          continue;
        }
        // A word longer than a whole line is cut to fill the rest of this line.
        if (word.length > room) {
          const space = Math.max(room - current.length - separator, 1);
          current += (separator ? " " : "") + word.slice(0, space);
          words[0] = word.slice(space);
          if (!words[0]) words.shift();
        }
        break;
      }
      lines.push(prefix + current);
    }
    return lines;
  },

  money(amount, currency) {
    const sign = amount < 0 ? "-" : "";
    const cents = Math.round(Math.abs(Number(amount)) * 100 + 1e-9);
    const text = (cents / 100).toFixed(2).replace(/0+$/, "").replace(/\.$/, "").replace(".", ",");
    return `${sign}${text} ${currency}`;
  },

  grams(grams) {
    return `${(grams / 1000).toFixed(3).replace(/0+$/, "").replace(/\.$/, "").replace(".", ",")} кг`;
  },

  dateTime(iso) {
    const date = new Date(iso);
    const pad = (value) => String(value).padStart(2, "0");
    return `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  },

  itemLines(items, columns, { showPrices, height = 1, currency }) {
    const lines = [];
    for (const item of items) {
      const title = `${item.quantity} x ${item.name}`;
      const price = showPrices ? this.money(item.unit_price * item.quantity, currency) : "";
      const nameWidth = price ? columns - price.length - 1 : columns;
      const nameLines = this.wrap(title, nameWidth, "    ");
      if (!nameLines.length) nameLines.push(title);
      lines.push(this.line(price ? `${nameLines[0].padEnd(nameWidth)} ${price}` : nameLines[0], { bold: true, height }));
      nameLines.slice(1).forEach((extra) => lines.push(this.line(extra, { bold: true, height })));
      const details = [];
      if (item.weight_grams) {
        details.push(item.portion_kind === "price"
          ? `на ${this.money(item.portion_amount, currency)} ≈ ${this.grams(item.weight_grams)}`
          : this.grams(item.weight_grams));
      }
      if (item.variant) details.push(item.variant.name);
      (item.modifiers || []).forEach((modifier) => details.push(`+ ${modifier.name}`));
      details.forEach((detail) => this.wrap(detail, columns - 4).forEach((chunk) => lines.push(this.line(`    ${chunk}`, { height }))));
    }
    return lines;
  },

  // Same as app/main.py format_phone.
  phone(digits) {
    return digits.length === 9 ? `${digits.slice(0, 2)} ${digits.slice(2, 5)} ${digits.slice(5, 7)} ${digits.slice(7)}` : digits;
  },

  deliveryLines(order, columns, bold) {
    const lines = [];
    if (order.customer_phone) lines.push(this.line(`Тел.: ${this.phone(order.customer_phone)}`.slice(0, columns), { bold }));
    if (order.delivery_address) this.wrap(`Адрес: ${order.delivery_address}`, columns, "       ").forEach((chunk) => lines.push(this.line(chunk, { bold })));
    return lines;
  },

  amountLine(label, amount, columns, currency, bold = false) {
    const value = this.money(amount, currency);
    return this.line(`${label.padEnd(columns - value.length - 1)} ${value}`, { bold });
  },

  kitchen(order, settings, currency) {
    const receipt = settings.receipt;
    const columns = settings.chars_per_line;
    const separator = this.line("-".repeat(columns));
    const lines = [];
    const wrapped = (text, style) => (this.wrap(text, columns).length ? this.wrap(text, columns) : [""]).forEach((chunk) => lines.push(this.line(chunk, style)));
    if (receipt.header) wrapped(receipt.header, { align: "center", bold: true });
    const numberColumns = Math.floor(columns / receipt.number_size);
    lines.push(this.line(order.order_number.slice(0, numberColumns), { align: "center", bold: true, width: receipt.number_size, height: receipt.number_size }));
    const service = this.SERVICE_LABELS[order.service_type];
    if (receipt.show_service_type && service) lines.push(this.line(service.toUpperCase(), { align: "center", bold: true, width: 2, height: 2 }));
    if (receipt.show_time) lines.push(this.line(this.dateTime(order.created_at), { align: "center" }));
    lines.push(...this.deliveryLines(order, columns, true));
    if (receipt.show_items) {
      lines.push(separator);
      lines.push(...this.itemLines(order.items, columns, { showPrices: receipt.show_prices, height: receipt.items_large ? 2 : 1, currency }));
      lines.push(separator);
    }
    if (receipt.show_total) {
      const total = this.money(order.total, currency);
      lines.push(this.line(`${"ИТОГО".padEnd(columns - total.length - 1)} ${total}`, { bold: true }));
    }
    if (receipt.show_comment && order.comment) wrapped(`Комментарий: ${order.comment}`, { bold: true });
    if (receipt.footer) wrapped(receipt.footer, { align: "center" });
    return lines;
  },

  customer(order, settings, { currency, title, cashierName }) {
    const columns = settings.chars_per_line;
    const separator = this.line("-".repeat(columns));
    const lines = [
      this.line(title.slice(0, Math.floor(columns / 2)), { align: "center", bold: true, width: 2, height: 2 }),
      this.line("ТОВАРНЫЙ ЧЕК", { align: "center", bold: true }),
      this.line(`Заказ № ${order.order_number}`, { align: "center", bold: true, width: 2, height: 2 }),
      this.line(this.dateTime(order.paid_at || order.created_at), { align: "center" }),
    ];
    if (cashierName) lines.push(this.line(`Кассир: ${cashierName}`.slice(0, columns), { align: "center" }));
    const service = this.SERVICE_LABELS[order.service_type];
    if (service) lines.push(this.line(service, { align: "center" }));
    lines.push(...this.deliveryLines(order, columns, false));
    lines.push(separator);
    lines.push(...this.itemLines(order.items, columns, { showPrices: true, currency }));
    lines.push(separator);
    if (order.discount) {
      lines.push(this.amountLine("Подытог", order.subtotal || order.total, columns, currency));
      lines.push(this.amountLine("Скидка", -order.discount, columns, currency));
    }
    lines.push(this.amountLine("ИТОГО", order.total, columns, currency, true));
    if (order.payment_method === "cash") {
      lines.push(this.amountLine("Наличные", order.amount_received || order.total, columns, currency));
      lines.push(this.amountLine("Сдача", order.change_due || 0, columns, currency));
    } else if (order.payment_method === "card") {
      lines.push(this.amountLine("Оплата картой", order.total, columns, currency));
    }
    lines.push(separator);
    lines.push(this.line("Спасибо за покупку!", { align: "center", bold: true }));
    lines.push(this.line("Нефискальный документ", { align: "center" }));
    return lines;
  },

  encodeChar(char, encoding) {
    const code = char.charCodeAt(0);
    if (code < 0x80) return code;
    if (encoding === "cp866") {
      if (code >= 0x410 && code <= 0x42f) return code - 0x410 + 0x80; // А-Я
      if (code >= 0x430 && code <= 0x43f) return code - 0x430 + 0xa0; // а-п
      if (code >= 0x440 && code <= 0x44f) return code - 0x440 + 0xe0; // р-я
      return { "Ё": 0xf0, "ё": 0xf1, "№": 0xfc, "°": 0xf8, "·": 0xfa }[char] ?? 0x3f;
    }
    if (encoding === "cp1251") {
      if (code >= 0x410 && code <= 0x44f) return code - 0x410 + 0xc0; // А-я
      return { "Ё": 0xa8, "ё": 0xb8, "№": 0xb9, "—": 0x97, "–": 0x96, "«": 0xab, "»": 0xbb, "°": 0xb0, "·": 0xb7 }[char] ?? 0x3f;
    }
    return 0x3f;
  },

  // Same bytes as app/main.py render_escpos.
  escpos(lines, settings) {
    const alignments = { left: 0, center: 1, right: 2 };
    const body = [0x1b, 0x40, 0x1b, 0x74, settings.codepage];
    for (const line of lines) {
      const size = ((line.width - 1) << 4) | (line.height - 1);
      body.push(0x1b, 0x61, alignments[line.align], 0x1b, 0x45, line.bold ? 1 : 0, 0x1d, 0x21, size);
      for (const char of line.text) body.push(this.encodeChar(this.CHAR_REPLACEMENTS[char] || char, settings.encoding));
      body.push(0x0a);
    }
    body.push(0x1b, 0x45, 0x00, 0x1d, 0x21, 0x00, 0x1b, 0x61, 0x00, 0x1b, 0x64, settings.receipt.feed_lines);
    if (settings.receipt.cut) body.push(0x1d, 0x56, 0x01);
    const copies = [];
    for (let copy = 0; copy < settings.receipt.copies; copy += 1) copies.push(...body);
    return copies;
  },

  singleCopy(settings) {
    return { ...settings, receipt: { ...settings.receipt, copies: 1 } };
  },
};
