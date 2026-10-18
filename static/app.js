const appState = {
  menu: null,
  category: "all",
  cart: [],
  screen: "home",
  currency: "с",
  pendingSubmission: null,
  orderComment: "",
  serviceType: null,
};

const main = document.querySelector("#main");
const modalRoot = document.querySelector("#modalRoot");
const notice = document.querySelector("#notice");
const PENDING_ORDER_KEY = "paprika-kiosk-pending-order";
let noticeTimeout;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function currencyLabel() {
  return currentLanguage === "en" ? "TJS" : appState.currency;
}

function money(amount) {
  return `${Number(amount).toLocaleString(numberLocale())} ${escapeHtml(currencyLabel())}`;
}

function unitLabel(unit) {
  return hasTranslation(`unit.${unit}`) ? t(`unit.${unit}`) : unit;
}

function categoryName(name) {
  return hasTranslation(`category.${name}`) ? t(`category.${name}`) : name;
}

function totalItems() {
  return appState.cart.reduce((sum, item) => sum + item.quantity, 0);
}

function cartTotal() {
  return appState.cart.reduce((sum, item) => sum + item.unitPrice * item.quantity, 0);
}

const SERVICE_ICONS = { dine_in: "🍽️", takeaway: "🛍️" };

function cartOverLimit() {
  return cartTotal() > appState.menu.max_order_total;
}

function orderLimitWarning() {
  return `<div class="order-limit-warning" role="alert"><strong>${t("limit.title", { amount: money(appState.menu.max_order_total) })}</strong>${t("limit.text")}</div>`;
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = false;
  window.clearTimeout(noticeTimeout);
  noticeTimeout = window.setTimeout(() => { notice.hidden = true; }, 4300);
}

function newIdempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...paprikaDevice.headers(), ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Server messages are in Russian; other languages get a translated generic text.
    const error = new Error(currentLanguage === DEFAULT_LANGUAGE && data.detail ? data.detail : t(path === "/api/orders" ? "error.order" : "error.generic"));
    error.status = response.status;
    throw error;
  }
  return data;
}

function startOrder() {
  if (!appState.menu) return;
  appState.screen = "menu";
  render();
}

function clearPendingSubmission() {
  appState.pendingSubmission = null;
  window.localStorage.removeItem(PENDING_ORDER_KEY);
}

function renderHome() {
  main.innerHTML = `
    <section class="welcome">
      <div class="welcome-copy">
        <span class="eyebrow">${t("home.eyebrow")}</span>
        <h1>${t("home.title")}</h1>
        <p>${t("home.text")}</p>
        <button class="primary-button" id="startOrder">${t("home.start")} <span class="arrow">→</span></button>
        <div class="welcome-perks">
          <span><i class="perk-icon">✓</i> ${t("home.perkPay")}</span><span><i class="perk-icon">✦</i> ${t("home.perkPrint")}</span>
        </div>
      </div>
      <div class="hero-art" aria-hidden="true">
        <div class="hero-pizza"><div class="pizza-face">🍕</div></div>
        <div class="floating-note">${t("home.note")}<small>${t("home.noteSmall")}</small></div>
      </div>
    </section>`;
  document.querySelector("#startOrder").addEventListener("click", startOrder);
}

function productEmoji(product) {
  const source = `${product.name} ${product.description}`.toLocaleLowerCase("ru");
  if (source.includes("пицц")) return "🍕";
  if (source.includes("бургер") || source.includes("булоч")) return "🍔";
  if (source.includes("курин") || source.includes("стрипс") || source.includes("крыл")) return "🍗";
  if (source.includes("морож") || source.includes("коктейл")) return "🍦";
  if (source.includes("кофе") || source.includes("капучино") || source.includes("латте")) return "☕";
  if (source.includes("напит") || source.includes("cola") || source.includes("сок") || source.includes("стакан")) return "🥤";
  return "🍽️";
}

function filteredProducts() {
  return appState.menu.products.filter((product) => (
    product.available && (appState.category === "all" || product.category_id === appState.category)
  ));
}

function productBasePrice(product) {
  return product.price;
}

function formatGrams(grams) {
  // Whole hundreds of grams from 1 kg are shown in kilograms: 1500 -> "1,5 кг".
  return grams >= 1000 && grams % 100 === 0
    ? `${(grams / 1000).toLocaleString(numberLocale())} ${t("weight.kg")}`
    : `${grams} ${t("weight.g")}`;
}

function weightedPortion(pricing, kind, value) {
  if (!Number.isInteger(value)) return null;
  if (kind === "weight") {
    if (value < pricing.min_grams || value > pricing.max_grams) return null;
    return {
      kind, value, weight_grams: value,
      amount_somoni: Math.round(value * pricing.price_per_kg / 10) / 100,
    };
  }
  if (value < pricing.min_amount || value > pricing.max_amount) return null;
  return {
    kind, value, amount_somoni: value,
    weight_grams: Math.round(value * 1000 / pricing.price_per_kg),
  };
}

function cartLineDetails(item) {
  const bits = [];
  if (item.portion) {
    bits.push(item.portion.kind === "weight"
      ? `${formatGrams(item.portion.weight_grams)} · ${money(item.portion.amount_somoni)}`
      : t("cart.portionFor", { amount: money(item.portion.amount_somoni), weight: formatGrams(item.portion.weight_grams) }));
  }
  if (item.variant) bits.push(item.variant.name);
  if (item.modifiers.length) bits.push(...item.modifiers.map((modifier) => modifier.name));
  return bits.join(" · ");
}

function renderCart() {
  if (!appState.cart.length) {
    return `<div class="empty-cart"><span>🛍️</span>${t("cart.empty")}</div>`;
  }
  return appState.cart.map((item) => `
    <div class="cart-line" data-key="${escapeHtml(item.key)}">
      <div>
        <div class="cart-line-title">${escapeHtml(item.name)}</div>
        <div class="cart-line-meta">${escapeHtml(cartLineDetails(item) || unitLabel(item.unit))}</div>
        <div class="cart-line-controls">
          <button class="quantity-button" data-action="decrease" aria-label="${t("cart.decrease")}">−</button>
          <span class="quantity-number">${item.quantity}</span>
          <button class="quantity-button" data-action="increase" aria-label="${t("cart.increase")}">+</button>
          <button class="remove-button" data-action="remove">${t("cart.remove")}</button>
        </div>
      </div>
      <div class="cart-line-price">${money(item.unitPrice * item.quantity)}</div>
    </div>`).join("");
}

function renderMenu() {
  const categories = [
    { id: "all", name: t("menu.all") },
    ...appState.menu.categories.map((category) => ({ ...category, name: categoryName(category.name) })),
  ];
  const products = filteredProducts();
  main.innerHTML = `
    <section class="menu-page">
      <div class="page-heading">
        <button class="back-button" id="backHome"><span>←</span> ${t("menu.back")}</button>
      </div>
      <div class="menu-layout">
        <div class="catalog">
          <nav class="category-list" aria-label="${t("menu.categories")}">
            ${categories.map((category) => `<button class="category-chip ${appState.category === category.id ? "active" : ""}" data-category="${escapeHtml(category.id)}">${escapeHtml(category.name)}</button>`).join("")}
          </nav>
          <div class="product-grid">
            ${products.map((product) => `
              <article class="product-card">
                <div class="food-art"><img src="${escapeHtml(product.image)}" alt="${escapeHtml(product.name)}" loading="lazy"><span class="food-emoji" hidden>${productEmoji(product)}</span></div>
                <div class="product-info">
                  <h3>${escapeHtml(product.name)}</h3>
                  <p class="product-description">${escapeHtml(product.ingredients || categoryName(product.description))}</p>
                  <div class="product-card-bottom">
                    <span class="price">${money(productBasePrice(product))}<small> / ${escapeHtml(unitLabel(product.unit))}</small></span>
                    <button class="add-button" data-product="${escapeHtml(product.id)}" aria-label="${escapeHtml(t("menu.add", { name: product.name }))}">+</button>
                  </div>
                </div>
              </article>`).join("") || `<p>${t("menu.empty")}</p>`}
          </div>
        </div>
        <aside class="cart-panel">
          <div class="cart-header"><h2>${t("cart.title")}</h2><span class="cart-count">${t("cart.count", { count: totalItems() })}</span></div>
          <div class="cart-content">${renderCart()}</div>
          <div class="cart-summary">
            <div class="summary-row"><span>${t("cart.total")}</span><strong>${money(cartTotal())}</strong></div>
            ${cartOverLimit() ? orderLimitWarning() : ""}
            <button class="checkout-button" id="checkout" ${appState.cart.length && !cartOverLimit() ? "" : "disabled"}>${t("cart.checkout")}</button>
            <p class="payment-hint">${t("cart.hint")}</p>
          </div>
        </aside>
      </div>
    </section>`;

  document.querySelector("#backHome").addEventListener("click", () => {
    appState.screen = "home";
    render();
  });
  document.querySelectorAll("[data-category]").forEach((button) => {
    button.addEventListener("click", () => {
      appState.category = button.dataset.category;
      renderMenu();
      // Show the new category from its first product when the list was scrolled down.
      const catalog = document.querySelector(".catalog");
      if (catalog.getBoundingClientRect().top < 0) catalog.scrollIntoView({ behavior: "smooth" });
    });
  });
  document.querySelectorAll("[data-product]").forEach((button) => {
    button.addEventListener("click", () => openProduct(button.dataset.product));
  });
  document.querySelectorAll(".food-art img").forEach((image) => {
    image.addEventListener("error", () => {
      image.hidden = true;
      image.nextElementSibling.hidden = false;
    }, { once: true });
  });
  document.querySelector("#checkout").addEventListener("click", () => {
    if (!appState.cart.length || cartOverLimit()) return;
    appState.screen = "confirm";
    render();
  });
  document.querySelectorAll(".cart-line").forEach((line) => {
    line.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => updateCart(line.dataset.key, button.dataset.action));
    });
  });
}

function openProduct(productId) {
  const product = appState.menu.products.find((entry) => entry.id === productId);
  if (!product) return;
  const hasVariants = product.variants.length > 0;
  const pricing = product.weighted;
  const hasWeightedPortions = Boolean(pricing);
  let quantity = 1;
  // A preset sells an exact weight; a typed amount sells "for N somoni".
  let portionKind = "weight";
  const portionValues = hasWeightedPortions
    ? { weight: pricing.default_grams, price: NaN }
    : {};
  const variantMarkup = hasVariants ? `
    <section class="option-group">
      <h3>${t("product.size")}</h3>
      <div class="option-list">${product.variants.map((variant, index) => `
        <label class="option-row">
          <span class="option-label"><input type="radio" name="variant" value="${escapeHtml(variant.id)}" ${index === 0 ? "checked" : ""}><span>${escapeHtml(variant.name)}</span></span>
          <span class="option-price">${variant.price ? `+ ${money(variant.price)}` : t("product.included")}</span>
        </label>`).join("")}</div>
    </section>` : "";
  const modifierMarkup = product.modifiers.length ? `
    <section class="option-group">
      <h3>${t("product.extras")} <span style="color:var(--muted);font-weight:400">${t("product.optional")}</span></h3>
      <div class="option-list">${product.modifiers.map((modifier) => `
        <label class="option-row">
          <span class="option-label"><input type="checkbox" name="modifier" value="${escapeHtml(modifier.id)}"><span>${escapeHtml(modifier.name)}</span></span>
          <span class="option-price">+ ${money(modifier.price)}</span>
        </label>`).join("")}</div>
    </section>` : "";
  const portionMarkup = hasWeightedPortions ? `
    <section class="option-group">
      <h3>${t("product.howMuch")} <span style="color:var(--muted);font-weight:400">${t("product.perKg", { price: money(pricing.price_per_kg) })}</span></h3>
      <div class="portion-presets">${pricing.preset_grams.map((grams) => {
        const option = weightedPortion(pricing, "weight", grams);
        return `<button type="button" class="portion-preset" data-portion-grams="${grams}"><strong>${(grams / 1000).toLocaleString(numberLocale())} ${t("weight.kg")}</strong><span>${money(option.amount_somoni)}</span></button>`;
      }).join("")}</div>
      <div class="portion-field">${t("product.ownAmount", { currency: escapeHtml(currencyLabel()) })}
        <button type="button" class="amount-display" id="portionPrice" aria-expanded="false" aria-controls="amountKeypad"><span id="amountValue"></span></button>
      </div>
      <p class="portion-hint" id="portionHint"></p>
      <div class="amount-keypad" id="amountKeypad" hidden>
        ${["1", "2", "3", "4", "5", "6", "7", "8", "9"].map((digit) => `<button type="button" data-key="${digit}">${digit}</button>`).join("")}
        <button type="button" data-key="clear" class="keypad-secondary">${t("keypad.clear")}</button>
        <button type="button" data-key="0">0</button>
        <button type="button" data-key="back" class="keypad-secondary" aria-label="${t("keypad.back")}">⌫</button>
        <button type="button" data-key="done" class="keypad-done">${t("keypad.done")}</button>
      </div>
    </section>` : "";
  modalRoot.innerHTML = `
    <div class="modal-backdrop" id="productBackdrop">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="productModalTitle">
        <div class="modal-heading"><h2 id="productModalTitle">${escapeHtml(product.name)}</h2><button class="close-button" id="closeProduct" aria-label="${t("product.close")}">×</button></div>
        <p class="modal-description">${product.ingredients ? `<strong>${t("product.ingredients")}</strong> ${escapeHtml(product.ingredients)}` : escapeHtml(categoryName(product.description))}</p>
        ${portionMarkup}${variantMarkup}${modifierMarkup}
        <div class="option-group"><h3>${t("product.quantity", { unit: hasWeightedPortions ? t("product.portions") : escapeHtml(unitLabel(product.unit)) })}</h3>
          <div class="cart-line-controls"><button class="quantity-button" id="modalMinus">−</button><span class="quantity-number" id="modalQuantity">1</span><button class="quantity-button" id="modalPlus">+</button></div>
        </div>
        <div class="modal-footer"><span class="modal-total" id="modalTotal"></span><button class="primary-button" id="addToCart">${t("product.add")}</button></div>
      </section>
    </div>`;
  const backdrop = document.querySelector("#productBackdrop");
  const updatePrice = () => {
    const variantId = document.querySelector('input[name="variant"]:checked')?.value;
    const variant = product.variants.find((entry) => entry.id === variantId);
    const portionOption = hasWeightedPortions
      ? weightedPortion(pricing, portionKind, portionValues[portionKind])
      : null;
    const modifiers = [...document.querySelectorAll('input[name="modifier"]:checked')]
      .map((input) => product.modifiers.find((entry) => entry.id === input.value));
    const amount = (portionOption?.amount_somoni || product.price + (variant?.price || 0))
      + modifiers.reduce((sum, entry) => sum + entry.price, 0);
    const invalidPortion = hasWeightedPortions && !portionOption;
    document.querySelector("#modalTotal").textContent = invalidPortion ? "—" : money(amount * quantity);
    document.querySelector("#addToCart").disabled = invalidPortion;
    if (hasWeightedPortions) {
      const hint = document.querySelector("#portionHint");
      hint.classList.toggle("is-error", invalidPortion);
      const range = { min: pricing.min_amount, max: pricing.max_amount, currency: currencyLabel() };
      hint.textContent = invalidPortion
        ? t("portion.range", range)
        : portionKind === "price"
          ? t("portion.approx", { weight: formatGrams(portionOption.weight_grams) })
          : t("portion.canEnter", range);
      document.querySelectorAll("[data-portion-grams]").forEach((button) => {
        button.classList.toggle("active", portionKind === "weight" && Number(button.dataset.portionGrams) === portionValues.weight);
      });
    }
    return { variant, modifiers, amount, portionOption };
  };
  document.querySelectorAll(".option-row input").forEach((input) => input.addEventListener("change", updatePrice));
  if (hasWeightedPortions) {
    const amountButton = document.querySelector("#portionPrice");
    const amountValue = document.querySelector("#amountValue");
    const keypad = document.querySelector("#amountKeypad");
    let amountText = "";
    const maxDigits = String(pricing.max_amount).length;
    const showAmount = () => {
      amountValue.textContent = amountText ? `${amountText} ${currencyLabel()}` : t("product.tapToEnter");
      amountButton.classList.toggle("is-empty", !amountText);
      amountButton.classList.toggle("active", Boolean(amountText));
    };
    const setKeypadOpen = (open) => {
      keypad.hidden = !open;
      amountButton.setAttribute("aria-expanded", String(open));
      if (open) keypad.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    const applyAmount = () => {
      if (amountText === "") {
        // An empty amount returns to the default weight.
        portionKind = "weight";
        portionValues.weight = pricing.default_grams;
      } else {
        portionKind = "price";
        portionValues.price = Number(amountText);
      }
      showAmount();
      updatePrice();
    };
    document.querySelectorAll("[data-portion-grams]").forEach((button) => {
      button.addEventListener("click", () => {
        portionKind = "weight";
        portionValues.weight = Number(button.dataset.portionGrams);
        amountText = "";
        setKeypadOpen(false);
        showAmount();
        updatePrice();
      });
    });
    amountButton.addEventListener("click", () => setKeypadOpen(keypad.hidden));
    keypad.querySelectorAll("[data-key]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.key;
        if (key === "done") {
          setKeypadOpen(false);
          return;
        }
        if (key === "clear") amountText = "";
        else if (key === "back") amountText = amountText.slice(0, -1);
        else if (amountText.length < maxDigits && !(amountText === "" && key === "0")) amountText += key;
        applyAmount();
      });
    });
    showAmount();
    updatePrice();
  }
  document.querySelector("#modalMinus").addEventListener("click", () => {
    quantity = Math.max(1, quantity - 1);
    document.querySelector("#modalQuantity").textContent = quantity;
    updatePrice();
  });
  document.querySelector("#modalPlus").addEventListener("click", () => {
    quantity = Math.min(20, quantity + 1);
    document.querySelector("#modalQuantity").textContent = quantity;
    updatePrice();
  });
  document.querySelector("#closeProduct").addEventListener("click", () => { modalRoot.innerHTML = ""; });
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) modalRoot.innerHTML = ""; });
  document.querySelector("#addToCart").addEventListener("click", () => {
    const selected = updatePrice();
    if (hasWeightedPortions && !selected.portionOption) return;
    const modifierIds = selected.modifiers.map((entry) => entry.id).sort();
    const portionKey = selected.portionOption ? `${selected.portionOption.kind}:${selected.portionOption.value}` : "";
    const key = `${product.id}|${portionKey}|${selected.variant?.id || ""}|${modifierIds.join(",")}`;
    const existing = appState.cart.find((entry) => entry.key === key);
    if (existing) existing.quantity = Math.min(20, existing.quantity + quantity);
    else {
      appState.cart.push({
        key, productId: product.id, name: product.name, quantity,
        unit: selected.portionOption ? "порц." : product.unit,
        unitPrice: selected.amount,
        portion: selected.portionOption || null,
        variant: selected.variant ? { id: selected.variant.id, name: selected.variant.name } : null,
        modifiers: selected.modifiers.map((entry) => ({ id: entry.id, name: entry.name })),
      });
    }
    clearPendingSubmission();
    modalRoot.innerHTML = "";
    renderMenu();
  });
  updatePrice();
}

function updateCart(key, action) {
  const item = appState.cart.find((entry) => entry.key === key);
  if (!item) return;
  if (action === "increase") item.quantity = Math.min(20, item.quantity + 1);
  if (action === "decrease") item.quantity -= 1;
  if (action === "remove" || item.quantity <= 0) {
    appState.cart = appState.cart.filter((entry) => entry.key !== key);
  }
  clearPendingSubmission();
  renderMenu();
}

const KEYBOARD_LAYOUTS = {
  cyrillic: ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбюё"],
  latin: ["qwertyuiop", "asdfghjkl", "zxcvbnm"],
};
const TAJIK_LETTERS = "ғӣқӯҳҷ";
const COMMENT_MAX_LENGTH = 100;

function renderCommentKeyboard(layout, shift) {
  const letter = (char) => (shift ? char.toLocaleUpperCase() : char);
  const keys = (chars, className = "") => [...chars].map((char) => `<button type="button" class="kb-key ${className}" data-char="${letter(char)}">${letter(char)}</button>`).join("");
  const rows = [
    keys("1234567890", "kb-digit"),
    ...(layout === "cyrillic" && currentLanguage === "tg" ? [keys(TAJIK_LETTERS)] : []),
    ...KEYBOARD_LAYOUTS[layout].map((row) => keys(row)),
  ];
  return `${rows.map((row) => `<div class="kb-row">${row}</div>`).join("")}
    <div class="kb-row">
      <button type="button" class="kb-key kb-wide kb-action ${shift ? "active" : ""}" data-kb="shift" aria-label="${t("keyboard.shift")}" aria-pressed="${shift}">⇧</button>
      <button type="button" class="kb-key kb-wide kb-action" data-kb="layout">${layout === "cyrillic" ? "ABC" : "АБВ"}</button>
      <button type="button" class="kb-key" data-char=",">,</button>
      <button type="button" class="kb-key kb-space" data-char=" ">${t("keyboard.space")}</button>
      <button type="button" class="kb-key" data-char=".">.</button>
      <button type="button" class="kb-key kb-wide kb-action" data-kb="back" aria-label="${t("keyboard.back")}">⌫</button>
      <button type="button" class="kb-key kb-wide kb-done" data-kb="done">${t("keyboard.done")}</button>
    </div>`;
}

function bindCommentKeyboard() {
  const textarea = document.querySelector("#orderComment");
  const keyboard = document.querySelector("#commentKeyboard");
  let layout = currentLanguage === "en" ? "latin" : "cyrillic";
  let shift = false;
  const sync = () => {
    textarea.value = appState.orderComment;
    document.querySelector("#commentCount").textContent = String(appState.orderComment.length);
  };
  const draw = () => {
    keyboard.innerHTML = renderCommentKeyboard(layout, shift);
  };
  const setOpen = (open) => {
    keyboard.hidden = !open;
    textarea.classList.toggle("is-typing", open);
    if (open) keyboard.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  textarea.addEventListener("focus", () => setOpen(true));
  textarea.addEventListener("click", () => setOpen(true));
  keyboard.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const action = button.dataset.kb;
    if (action === "done") {
      setOpen(false);
      textarea.blur();
      return;
    }
    if (action === "shift") shift = !shift;
    else if (action === "layout") layout = layout === "cyrillic" ? "latin" : "cyrillic";
    else if (action === "back") appState.orderComment = appState.orderComment.slice(0, -1);
    else if (button.dataset.char !== undefined && appState.orderComment.length < COMMENT_MAX_LENGTH) {
      appState.orderComment += button.dataset.char;
      // Shift applies to one letter, like on a phone keyboard.
      if (shift && button.dataset.char.trim()) shift = false;
    }
    sync();
    draw();
  });
  draw();
}

function renderConfirmation() {
  main.innerHTML = `
    <section class="confirm-page">
      <button class="back-button" id="backMenu"><span>←</span> ${t("confirm.back")}</button>
      <h1>${t("confirm.title")}</h1>
      <p>${t("confirm.subtitle")}</p>
      <div class="review-card">${appState.cart.map((item) => `
        <div class="review-line">
          <div>
            <div class="review-name">${escapeHtml(item.name)} × ${item.quantity} ${escapeHtml(unitLabel(item.unit))}</div>
            ${cartLineDetails(item) ? `<div class="review-detail">${escapeHtml(cartLineDetails(item))}</div>` : ""}
          </div>
          <div class="review-price">${money(item.unitPrice * item.quantity)}</div>
        </div>`).join("")}</div>
      <div class="order-total"><span>${t("confirm.total")}</span><strong>${money(cartTotal())}</strong></div>
      <h2 class="service-title">${t("service.title")} <span>${t("service.required")}</span></h2>
      <div class="service-options" role="radiogroup" aria-label="${t("service.group")}">${Object.entries(SERVICE_ICONS).map(([value, icon]) => `
        <button type="button" class="service-option ${appState.serviceType === value ? "active" : ""}" role="radio" aria-checked="${appState.serviceType === value}" data-service="${value}">
          <span class="service-icon" aria-hidden="true">${icon}</span><strong>${t(`service.${value}`)}</strong><small>${t(`service.${value}.hint`)}</small>
        </button>`).join("")}</div>
      <label class="order-comment-label" for="orderComment">${t("comment.label")} <span>${t("comment.optional")}</span></label>
      <textarea id="orderComment" maxlength="${COMMENT_MAX_LENGTH}" rows="3" inputmode="none" autocomplete="off" placeholder="${t("comment.placeholder")}">${escapeHtml(appState.orderComment)}</textarea>
      <div class="comment-count"><span id="commentCount">${appState.orderComment.length}</span> / ${COMMENT_MAX_LENGTH}</div>
      <div class="comment-keyboard" id="commentKeyboard" hidden></div>
      ${cartOverLimit() ? orderLimitWarning() : ""}
      <div class="confirm-actions"><button class="text-button" id="editOrder">${t("confirm.edit")}</button><button class="primary-button" id="confirmOrder" ${cartOverLimit() || !appState.serviceType ? "disabled" : ""}>${t("confirm.submit")} <span class="arrow">→</span></button></div>
    </section>`;
  document.querySelector("#backMenu").addEventListener("click", () => { appState.screen = "menu"; render(); });
  document.querySelector("#editOrder").addEventListener("click", () => { appState.screen = "menu"; render(); });
  // A hardware keyboard still works; the on-screen keyboard is for touch screens.
  document.querySelector("#orderComment").addEventListener("input", (event) => {
    appState.orderComment = event.currentTarget.value;
    document.querySelector("#commentCount").textContent = String(appState.orderComment.length);
  });
  bindCommentKeyboard();
  document.querySelectorAll("[data-service]").forEach((button) => {
    button.addEventListener("click", () => {
      appState.serviceType = button.dataset.service;
      document.querySelectorAll("[data-service]").forEach((entry) => {
        const selected = entry === button;
        entry.classList.toggle("active", selected);
        entry.setAttribute("aria-checked", String(selected));
      });
      document.querySelector("#confirmOrder").disabled = cartOverLimit();
    });
  });
  document.querySelector("#confirmOrder").addEventListener("click", submitOrder);
}

async function submitOrder(event) {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = t("confirm.sending");
  const payload = {
    items: appState.cart.map((item) => ({
      product_id: item.productId,
      quantity: item.quantity,
      variant_id: item.variant?.id || null,
      modifier_ids: item.modifiers.map((modifier) => modifier.id),
      portion_kind: item.portion?.kind || null,
      portion_value: item.portion?.value ?? null,
    })),
    comment: appState.orderComment,
    service_type: appState.serviceType,
  };
  const payloadSignature = JSON.stringify(payload);
  try {
    if (!appState.pendingSubmission || appState.pendingSubmission.signature !== payloadSignature) {
      appState.pendingSubmission = {
        signature: payloadSignature,
        key: newIdempotencyKey(),
        payload,
      };
    }
    window.localStorage.setItem(PENDING_ORDER_KEY, JSON.stringify(appState.pendingSubmission));
    const order = await sendPendingOrder();
    appState.screen = "success";
    renderSuccess(order);
    announceOrder(order);
  } catch (error) {
    showNotice(error.message);
    appState.screen = "confirm";
    render();
  }
}

async function sendPendingOrder() {
  const pending = appState.pendingSubmission;
  if (!pending) throw new Error(t("error.generic"));
  return api("/api/orders", {
    method: "POST",
    headers: { "Idempotency-Key": pending.key },
    body: JSON.stringify(pending.payload),
  }).then((order) => {
    clearPendingSubmission();
    return order;
  });
}

function showPendingOrderRecovery() {
  renderLanguageSwitch();
  main.innerHTML = `
    <section class="confirm-page">
      <div class="success-icon" style="margin-left:0;background:#b9833d">…</div>
      <h1>${t("recovery.title")}</h1>
      <p>${t("recovery.text")}</p>
      <div id="pendingRecoveryMessage" class="confirm-note"><span>ⓘ</span><span>${t("recovery.wait")}</span></div>
    </section>`;
  sendPendingOrder()
    .then((order) => {
      appState.screen = "success";
      renderSuccess(order);
      announceOrder(order);
    })
    .catch((error) => {
      const message = document.querySelector("#pendingRecoveryMessage");
      if (!message) return;
      message.innerHTML = `<span>ⓘ</span><span>${escapeHtml(error.message)} ${t("recovery.retryNote")}</span>`;
      const retry = document.createElement("button");
      retry.className = "primary-button";
      retry.textContent = t("recovery.retry");
      retry.addEventListener("click", showPendingOrderRecovery);
      message.after(retry);
    });
}

// Windows usually has no Tajik voice: Tajik text is then read by a Russian voice
// with the Tajik letters replaced by the closest Russian ones.
const TAJIK_TO_RUSSIAN = { "ғ": "г", "ӣ": "и", "қ": "к", "ӯ": "у", "ҳ": "х", "ҷ": "ч" };

async function announceOrder(order) {
  try {
    let text = t("voice.accepted", { number: order.order_number });
    let language = LANGUAGES[currentLanguage].speech;
    // There is no Tajik voice: the Tajik phrase is read by the Russian voice.
    if (currentLanguage === "tg") {
      language = "ru-RU";
      text = text.replace(/[ғӣқӯҳҷ]/gi, (char) => {
        const replacement = TAJIK_TO_RUSSIAN[char.toLowerCase()];
        return char === char.toLowerCase() ? replacement : replacement.toUpperCase();
      });
    }
    await announce(text, language);
  } catch {
    // Speech is a convenience; the order number is on the screen anyway.
  }
}

function renderSuccess(order) {
  appState.lastOrder = order;
  // "queued": the ticket is in the store's print queue and the register program prints it.
  const printMessage = ["printed", "queued"].includes(order.print_status)
    ? t(order.comment ? "print.printedComment" : "print.printed")
    : order.print_status === "failed"
      ? t(order.comment ? "print.failedComment" : "print.failed")
      : t("print.none");
  main.innerHTML = `
    <section class="success-page">
      <div class="success-icon">✓</div>
      <span class="eyebrow">${t("success.eyebrow")}</span>
      <h1>${t("success.title")}</h1>
      <p>${t("success.text")}</p>
      <div class="receipt-card">
        <div class="receipt-label">${t("success.number")}</div>
        <div class="receipt-number">${escapeHtml(order.order_number)}</div>
        ${SERVICE_ICONS[order.service_type] ? `<div class="receipt-service">${SERVICE_ICONS[order.service_type]} ${t(`service.${order.service_type}`)}</div>` : ""}
        <p>${t("success.pay")}</p>
        ${order.comment ? `<p class="receipt-comment"><strong>${t("success.comment")}</strong> ${escapeHtml(order.comment)}</p>` : ""}
      </div>
      <div class="print-message">${escapeHtml(printMessage)}</div>
      <button class="primary-button" id="finishOrder">${t("success.finish")} <span class="arrow">→</span></button>
    </section>`;
  document.querySelector("#finishOrder").addEventListener("click", finishOrder);
}

function finishOrder() {
  appState.cart = [];
  appState.orderComment = "";
  appState.serviceType = null;
  appState.lastOrder = null;
  clearPendingSubmission();
  // The next customer starts in the default language.
  setLanguage(DEFAULT_LANGUAGE);
  appState.screen = "home";
  appState.category = "all";
  render();
}

function renderLanguageSwitch() {
  const container = document.querySelector("#languageSwitch");
  container.setAttribute("aria-label", t("lang.label"));
  container.innerHTML = Object.entries(LANGUAGES).map(([code, language]) => `
    <button type="button" class="language-button ${code === currentLanguage ? "active" : ""}" data-language="${code}" aria-pressed="${code === currentLanguage}" title="${language.name}">${language.short}</button>`).join("");
  container.querySelectorAll("[data-language]").forEach((button) => {
    button.addEventListener("click", () => {
      setLanguage(button.dataset.language);
      if (appState.screen === "success" && appState.lastOrder) renderSuccess(appState.lastOrder);
      render();
    });
  });
}

function render() {
  if (appState.screen === "home") renderHome();
  if (appState.screen === "menu") renderMenu();
  if (appState.screen === "confirm") renderConfirmation();
  // The header with the logo and language switch is shown on the start screen only.
  document.querySelector(".topbar").hidden = appState.screen !== "home";
  renderLanguageSwitch();
  document.querySelector("#footerMessage").textContent = t("footer.message");
  document.querySelector("#brandHome").setAttribute("aria-label", t("brand.home"));
}

document.querySelector("#brandHome").addEventListener("click", () => {
  if (appState.screen === "success") finishOrder();
  else { appState.screen = "home"; render(); }
});
function showKioskSetup(message) {
  document.querySelector(".topbar").hidden = true;
  showDeviceSetup(main, "Киоск не активирован", message);
}

if (!paprikaDevice.key) showKioskSetup("Этот киоск ещё не привязан к точке продаж.");
else api("/api/menu")
  .then((menu) => {
    appState.menu = menu;
    appState.currency = menu.currency;
    appState.pendingSubmission = JSON.parse(window.localStorage.getItem(PENDING_ORDER_KEY) || "null");
    if (appState.pendingSubmission?.payload && (!appState.pendingSubmission.payload.service_type || appState.pendingSubmission.payload.items?.some((item) => "portion_amount" in item || "portion_id" in item))) {
      clearPendingSubmission();
    }
    if (appState.pendingSubmission?.key && appState.pendingSubmission?.payload) {
      showPendingOrderRecovery();
    } else {
      render();
    }
  })
  .catch((error) => {
    if (error.status === 401) {
      paprikaDevice.forget();
      showKioskSetup(error.message);
      return;
    }
    main.innerHTML = `<section class="load-error"><h1>${t("load.title")}</h1><p>${escapeHtml(error.message)}<br>${t("load.text")}</p><button class="primary-button" id="retryMenu">${t("load.retry")}</button></section>`;
    renderLanguageSwitch();
    document.querySelector("#retryMenu").addEventListener("click", () => window.location.reload());
  });
