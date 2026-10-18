const appState = {
  menu: null,
  category: "all",
  cart: [],
  screen: "home",
  currency: "с",
  adminToken: "",
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
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Server messages are in Russian; other languages get a translated generic text.
    throw new Error(currentLanguage === DEFAULT_LANGUAGE && data.detail ? data.detail : t(path === "/api/orders" ? "error.order" : "error.generic"));
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

function renderSuccess(order) {
  appState.lastOrder = order;
  const printMessage = order.print_status === "printed"
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

async function openAdmin() {
  if (!appState.adminToken) {
    const token = window.prompt("Введите код сотрудника");
    if (token === null || !token.trim()) return;
    appState.adminToken = token.trim();
  }
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="modal admin-panel"><div class="modal-heading"><h2>Панель сотрудника</h2><button class="close-button" id="closeAdmin">×</button></div><p class="modal-description">Настройка меню, отчёты и печать заказов.</p><div id="adminContent">Загрузка…</div></section></div>`;
  document.querySelector("#closeAdmin").addEventListener("click", closeAdmin);
  try {
    const headers = { Authorization: `Bearer ${appState.adminToken}` };
    const today = new Date().toLocaleDateString("sv-SE");
    const [printerSettings, orders, report, catalog] = await Promise.all([
      api("/api/admin/printer", { headers }),
      api("/api/admin/orders", { headers }),
      api(`/api/admin/report?start_date=${today}&end_date=${today}`, { headers }),
      api("/api/admin/products", { headers }),
    ]);
    const content = document.querySelector("#adminContent");
    if (!content) return;
    content.innerHTML = `
      ${renderPrinterSettings(printerSettings)}
      <section class="admin-products">
        <div class="admin-section-heading"><h3>Товары в меню</h3><button class="small-button" id="addProduct">Добавить товар</button></div>
        <p class="modal-description">Изменения сразу сохраняются в каталоге киоска.</p>
        <div id="productEditor"></div>
        <div id="adminProductList">${renderAdminProducts(catalog)}</div>
      </section>
      <section class="admin-report">
        <div class="admin-section-heading"><h3>Отчёт по заказам</h3><span>За день</span></div>
        <form id="reportFilters" class="report-filters">
          <label>С даты<input type="date" id="reportStart" value="${today}" required></label>
          <label>По дату<input type="date" id="reportEnd" value="${today}" required></label>
          <button class="small-button" type="submit">Показать</button>
        </form>
        <div id="reportResults">${renderReport(report)}</div>
        <button class="small-button report-export" id="exportReport">Скачать CSV</button>
      </section>
      <h3>Последние заказы</h3>
      ${orders.length ? orders.map((order) => `<div class="admin-order"><span><strong>№ ${escapeHtml(order.order_number)}</strong> <small>${escapeHtml(new Date(order.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }))} · ${escapeHtml(order.order_ref)}</small><br><small>${escapeHtml(money(order.total))}${order.service_type ? ` · ${TRANSLATIONS.ru[`service.${order.service_type}`]}` : ""} · ${escapeHtml(order.print_status)}${order.comment ? `<br>Комментарий: ${escapeHtml(order.comment)}` : ""}</small></span><button class="small-button" data-reprint="${escapeHtml(order.order_ref)}">Повторить печать</button></div>`).join("") : `<p class="modal-description">Заказов пока нет.</p>`}
      <button class="text-button" id="adminLogout">Выйти из панели</button>`;
    const loadReport = async (exportCsv = false) => {
      const startDate = document.querySelector("#reportStart").value;
      const endDate = document.querySelector("#reportEnd").value;
      if (startDate > endDate) {
        showNotice("Начальная дата не может быть позже конечной.");
        return;
      }
      const query = `start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`;
      if (exportCsv) {
        try {
          const response = await fetch(`/api/admin/report.csv?${query}`, { headers });
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || "Не удалось выгрузить отчёт.");
          }
          const blob = await response.blob();
          const downloadUrl = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = downloadUrl;
          link.download = `paprika-orders-${startDate}-${endDate}.csv`;
          link.click();
          URL.revokeObjectURL(downloadUrl);
        } catch (error) { showNotice(error.message); }
        return;
      }
      const results = document.querySelector("#reportResults");
      results.textContent = "Загружаем отчёт…";
      try {
        const selectedReport = await api(`/api/admin/report?${query}`, { headers });
        results.innerHTML = renderReport(selectedReport);
      } catch (error) {
        results.textContent = error.message;
      }
    };
    content.querySelector("#reportFilters").addEventListener("submit", (event) => {
      event.preventDefault();
      loadReport();
    });
    content.querySelector("#exportReport").addEventListener("click", () => loadReport(true));
    bindProductAdmin(content, catalog);
    bindPrinterSettings(content, headers);
    content.querySelectorAll("[data-reprint]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const printed = await api("/api/admin/print", {
            method: "POST", headers,
            body: JSON.stringify({ order_ref: button.dataset.reprint }),
          });
          showNotice(`Чек заказа № ${printed.order_number} отправлен на принтер.`);
        } catch (error) { showNotice(error.message); }
      });
    });
    content.querySelector("#adminLogout").addEventListener("click", () => {
      appState.adminToken = "";
      closeAdmin();
    });
  } catch (error) {
    appState.adminToken = "";
    const content = document.querySelector("#adminContent");
    if (content) content.innerHTML = `<p class="modal-description">${escapeHtml(error.message)}</p><button class="small-button" id="retryAdmin">Повторить</button>`;
    document.querySelector("#retryAdmin")?.addEventListener("click", openAdmin);
  }
}

function renderAdminProducts(catalog) {
  return catalog.products.map((product) => `
    <div class="admin-product-row">
      <img src="${escapeHtml(product.image)}" alt="" loading="lazy">
      <div class="admin-product-info">
        <strong>${escapeHtml(product.name)}</strong>
        <small>${escapeHtml(catalog.categories.find((category) => category.id === product.category_id)?.name || "Категория не найдена")} · ${money(product.price)} / ${escapeHtml(product.unit)}</small>
      </div>
      <span class="product-priority" title="Приоритет">${product.priority ? `№ ${product.priority}` : ""}</span>
      <span class="product-status ${product.available ? "is-available" : "is-unavailable"}">${product.available ? "В продаже" : "Скрыт"}</span>
      <button class="small-button" data-edit-product="${escapeHtml(product.id)}">Изменить</button>
    </div>`).join("");
}

function renderProductEditor(product, categories) {
  const editing = Boolean(product);
  return `
    <form id="productEditorForm" class="product-editor">
      <h4>${editing ? "Изменить товар" : "Новый товар"}</h4>
      <label>Название<input name="name" maxlength="120" required value="${escapeHtml(product?.name || "")}"></label>
      <div class="product-editor-fields">
        <label>Цена<input name="price" type="number" min="0" max="99999999.99" step="0.01" required value="${editing ? product.price : ""}"></label>
        <label>Приоритет<input name="priority" type="number" min="1" max="999" step="1" placeholder="—" value="${product?.priority ?? ""}"><small>1 — первым в списке. Пусто — без приоритета.</small></label>
      </div>
      <div class="product-editor-fields">
        <label>Категория<select name="category_id" required>${categories.map((category) => `<option value="${escapeHtml(category.id)}" ${product?.category_id === category.id ? "selected" : ""}>${escapeHtml(category.name)}</option>`).join("")}</select></label>
      </div>
      ${editing ? "" : `<label>Единица<select name="unit"><option value="шт.">шт.</option><option value="кг">кг</option></select></label>`}
      <label>Состав<textarea name="ingredients" maxlength="300" rows="3" placeholder="Например: томатный соус, моцарелла, пепперони">${escapeHtml(product?.ingredients || "")}</textarea><small>Показывается покупателю на карточке товара. Оставьте пустым, если состав не нужен.</small></label>
      <label class="product-image-picker">Фотография<input name="image" type="file" accept="image/jpeg,image/png,image/webp"><small>JPEG, PNG или WebP, не более 5 МБ</small></label>
      <label class="product-availability"><input name="available" type="checkbox" ${product?.available === false ? "" : "checked"}> Показывать товар в меню / доступен для заказа</label>
      <div class="product-editor-actions"><button class="small-button" type="button" id="cancelProductEdit">Отмена</button><button class="small-button" type="submit">Сохранить</button></div>
      <p id="productEditorMessage" class="product-editor-message" role="status"></p>
    </form>`;
}

function bindProductAdmin(content, catalog) {
  const editor = content.querySelector("#productEditor");
  const headers = { Authorization: `Bearer ${appState.adminToken}` };
  const reload = async () => {
    const updatedCatalog = await api("/api/admin/products", { headers });
    content.querySelector("#adminProductList").innerHTML = renderAdminProducts(updatedCatalog);
    bindProductAdmin(content, updatedCatalog);
    appState.menu = await api("/api/menu");
  };
  content.querySelector("#addProduct").onclick = () => {
    editor.innerHTML = renderProductEditor(null, catalog.categories);
    bindProductEditor(content, catalog, null, reload);
  };
  content.querySelectorAll("[data-edit-product]").forEach((button) => {
    button.onclick = () => {
      const product = catalog.products.find((entry) => entry.id === button.dataset.editProduct);
      if (!product) return;
      editor.innerHTML = renderProductEditor(product, catalog.categories);
      bindProductEditor(content, catalog, product, reload);
      editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
  });
}

function bindProductEditor(content, catalog, product, reload) {
  const editor = content.querySelector("#productEditor");
  const form = editor.querySelector("#productEditorForm");
  editor.querySelector("#cancelProductEdit").addEventListener("click", () => { editor.innerHTML = ""; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = form.querySelector('[type="submit"]');
    const message = form.querySelector("#productEditorMessage");
    submitButton.disabled = true;
    message.textContent = "Сохраняем…";
    const formData = new FormData(form);
    const payload = {
      name: formData.get("name"),
      price: formData.get("price"),
      category_id: formData.get("category_id"),
      available: form.querySelector('[name="available"]').checked,
      ingredients: formData.get("ingredients"),
      priority: formData.get("priority") ? Number(formData.get("priority")) : null,
    };
    try {
      const saved = await api(product ? `/api/admin/products/${encodeURIComponent(product.id)}` : "/api/admin/products", {
        method: product ? "PUT" : "POST",
        headers: { Authorization: `Bearer ${appState.adminToken}` },
        body: JSON.stringify(product ? payload : { ...payload, unit: formData.get("unit") }),
      });
      product = saved;
      const photo = form.querySelector('[name="image"]').files[0];
      if (photo) {
        const upload = new FormData();
        upload.append("image", photo);
        const response = await fetch(`/api/admin/products/${encodeURIComponent(saved.id)}/image`, {
          method: "POST",
          headers: { Authorization: `Bearer ${appState.adminToken}` },
          body: upload,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || "Не удалось загрузить фотографию.");
      }
      editor.innerHTML = "";
      await reload();
      showNotice("Товар сохранён.");
    } catch (error) {
      message.textContent = error.message;
      submitButton.disabled = false;
    }
  });
}

function renderPrinterSettings(settings) {
  const receipt = settings.receipt;
  const checkbox = (name, label) => `<label class="printer-check"><input type="checkbox" name="${name}" ${receipt[name] ? "checked" : ""}> ${label}</label>`;
  const option = (value, current, label) => `<option value="${value}" ${String(value) === String(current) ? "selected" : ""}>${label}</option>`;
  return `
    <section class="admin-products printer-settings">
      <div class="admin-section-heading"><h3>Принтер и чек для кухни</h3><span>Чековый принтер 80 мм</span></div>
      <form id="printerForm" class="printer-layout">
        <div class="printer-fields">
          <h4>Подключение</h4>
          <div class="product-editor-fields">
            <label>IP-адрес принтера<input name="host" value="${escapeHtml(settings.host)}" placeholder="192.168.1.50" maxlength="253" autocomplete="off"></label>
            <label>Порт<input name="port" type="number" min="1" max="65535" value="${settings.port}" required></label>
          </div>
          <div class="product-editor-fields">
            <label>Ширина строки<select name="chars_per_line">${option(48, settings.chars_per_line, "48 символов (обычно для 80 мм)")}${option(42, settings.chars_per_line, "42 символа")}</select></label>
            <label>Кодировка<select name="encoding">${option("cp866", settings.encoding, "CP866 (русский)")}${option("cp1251", settings.encoding, "Windows-1251")}${settings.encoding !== "cp866" && settings.encoding !== "cp1251" ? option(escapeHtml(settings.encoding), settings.encoding, escapeHtml(settings.encoding)) : ""}</select></label>
          </div>
          <details class="printer-advanced">
            <summary>Дополнительно</summary>
            <div class="product-editor-fields">
              <label>Кодовая страница ESC/POS<input name="codepage" type="number" min="0" max="255" value="${settings.codepage}" required><small>CP866 — обычно 17, Windows-1251 — обычно 46. Сверьте с инструкцией.</small></label>
              <label>Ожидание ответа, сек<input name="timeout" type="number" min="0.5" max="30" step="0.5" value="${settings.timeout}" required></label>
            </div>
          </details>
          <h4>Дизайн чека</h4>
          <label>Заголовок<input name="header" maxlength="48" value="${escapeHtml(receipt.header)}" placeholder="КУХНЯ"></label>
          <div class="product-editor-fields">
            <label>Размер номера заказа<select name="number_size">${[1, 2, 3, 4].map((size) => option(size, receipt.number_size, size === 1 ? "Обычный" : `Крупный ×${size}`)).join("")}</select></label>
            <label>Копий<select name="copies">${[1, 2, 3].map((count) => option(count, receipt.copies, count)).join("")}</select></label>
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
          <label>Нижняя строка<input name="footer" maxlength="96" value="${escapeHtml(receipt.footer)}" placeholder="Например: Приятного аппетита!"></label>
          <label>Пустых строк перед отрезом<input name="feed_lines" type="number" min="0" max="10" value="${receipt.feed_lines}" required></label>
          <p id="printerMessage" class="product-editor-message" role="status"></p>
          <div class="printer-actions">
            <button class="small-button" type="button" id="checkPrinter">Проверить связь</button>
            <button class="small-button" type="button" id="testPrint">Печать пробного чека</button>
            <button class="small-button primary-small" type="submit">Сохранить</button>
          </div>
        </div>
        <div class="receipt-preview-wrap">
          <h4>Как будет выглядеть</h4>
          <div class="receipt-paper" id="receiptPreview"></div>
          <small>Пример заказа. Вид на бумаге зависит от шрифта принтера.</small>
        </div>
      </form>
    </section>`;
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

function renderReceiptPreview(preview) {
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

function bindPrinterSettings(content, headers) {
  const form = content.querySelector("#printerForm");
  const message = form.querySelector("#printerMessage");
  const preview = form.querySelector("#receiptPreview");
  let previewTimer;
  let previewRequest = 0;
  const showMessage = (text, isError = false) => {
    message.textContent = text;
    message.classList.toggle("is-error", isError);
  };
  const refreshPreview = async () => {
    const request = ++previewRequest;
    try {
      const result = await api("/api/admin/printer/preview", {
        method: "POST", headers, body: JSON.stringify(readPrinterForm(form)),
      });
      if (request !== previewRequest) return;
      preview.style.setProperty("--receipt-columns", result.chars_per_line);
      preview.innerHTML = renderReceiptPreview(result);
    } catch (error) {
      if (request === previewRequest) showMessage(error.message, true);
    }
  };
  const schedulePreview = () => {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(refreshPreview, 250);
  };
  form.addEventListener("input", schedulePreview);
  form.addEventListener("change", schedulePreview);
  const runAction = async (button, path, successText) => {
    button.disabled = true;
    showMessage("Подождите…");
    try {
      const result = await api(path, { method: "POST", headers, body: JSON.stringify(readPrinterForm(form)) });
      const ok = result.connected !== false;
      showMessage(successText || result.message, !ok);
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      button.disabled = false;
    }
  };
  form.querySelector("#checkPrinter").addEventListener("click", (event) => runAction(event.currentTarget, "/api/admin/printer/check"));
  form.querySelector("#testPrint").addEventListener("click", (event) => runAction(event.currentTarget, "/api/admin/printer/test"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    try {
      await api("/api/admin/printer", { method: "PUT", headers, body: JSON.stringify(readPrinterForm(form)) });
      showMessage("Настройки сохранены. Новые заказы будут печататься так.");
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  refreshPreview();
}

function renderReport(report) {
  const productRows = report.products.length
    ? report.products.map((product) => `
      <div class="report-product">
        <span>${escapeHtml(product.name)}</span>
        <strong>${product.quantity} ${escapeHtml(product.unit || "шт.")}${product.weight_grams ? `<small> · ≈ ${product.weight_grams} г всего</small>` : ""}</strong>
        <span>${money(product.amount)}</span>
      </div>`).join("")
    : `<p class="report-empty">За выбранный период заказов нет.</p>`;
  return `
    <div class="report-metrics">
      <div><span>Заказы</span><strong>${report.orders_count}</strong></div>
      <div><span>Единиц товара</span><strong>${report.items_count}</strong></div>
      <div><span>Сумма</span><strong>${money(report.total_amount)}</strong></div>
      <div><span>Средний заказ</span><strong>${money(report.average_order_amount)}</strong></div>
    </div>
    <div class="report-product-list"><div class="report-product report-product-header"><span>Блюдо</span><span>Количество</span><span>Сумма</span></div>${productRows}</div>`;
}

function closeAdmin() {
  appState.adminToken = "";
  modalRoot.innerHTML = "";
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
  document.querySelector("#adminOpen").textContent = t("footer.staff");
  document.querySelector("#brandHome").setAttribute("aria-label", t("brand.home"));
}

document.querySelector("#brandHome").addEventListener("click", () => {
  if (appState.screen === "success") finishOrder();
  else { appState.screen = "home"; render(); }
});
document.querySelector("#adminOpen").addEventListener("click", openAdmin);

api("/api/menu")
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
    main.innerHTML = `<section class="load-error"><h1>${t("load.title")}</h1><p>${escapeHtml(error.message)}<br>${t("load.text")}</p><button class="primary-button" id="retryMenu">${t("load.retry")}</button></section>`;
    renderLanguageSwitch();
    document.querySelector("#retryMenu").addEventListener("click", () => window.location.reload());
  });
