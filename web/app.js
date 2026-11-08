"use strict";

(() => {
  // One page owns one customer session. Credentials and retry identity stay in memory.
  const state = {
    token: "", checkoutId: null, quote: null, selectedOffer: null,
    pendingApplication: null, application: null, busy: false
  };
  const byId = (id) => document.getElementById(id);
  const isText = (value) => typeof value === "string" && value.length > 0;
  const isMoney = (value) => Number.isSafeInteger(value) && value >= 0;
  const applicationStates = new Set(["HELD", "DISPATCHING", "PENDING", "UNKNOWN", "OWED", "FULFILLED", "RELEASED"]);
  const money = (paise) => isMoney(paise)
    ? new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(paise / 100)
    : "Amount unavailable";

  function validQuote(value) {
    return value && isText(value.quote_id) && isText(value.revision)
      && isText(value.expires_at) && Number.isFinite(Date.parse(value.expires_at))
      && Array.isArray(value.decisions) && value.decisions.every((decision) => decision
        && decision.eligible === true && isText(decision.offer_id) && isText(decision.issuer)
        && isMoney(decision.nominal_benefit_paise) && decision.nominal_benefit_paise > 0
        && isMoney(decision.expected_benefit_paise)
        && decision.expected_benefit_paise <= decision.nominal_benefit_paise
        && isMoney(decision.eligible_spend_paise)
        && ["instant", "cashback"].includes(decision.payout)
        && Number.isSafeInteger(decision.rank) && decision.rank > 0);
  }

  function validApplication(value) {
    return value && isText(value.application_id) && isText(value.offer_id)
      && isText(value.payment_id) && isText(value.provider_reference)
      && isMoney(value.nominal_paise) && applicationStates.has(value.state)
      && ["NOT_SENT", "IN_FLIGHT", "PENDING", "UNKNOWN", "CAPTURED", "FAILED"].includes(value.payment_state)
      && ["NONE", "OWED", "FULFILLED"].includes(value.benefit_state)
      && ["HELD", "COMMITTED", "RELEASED"].includes(value.budget_state);
  }

  class CheckoutApiClient {
    // Transport has no access to mutable controller state or browser storage.
    constructor(fetchRequest = window.fetch.bind(window), timeoutMs = 12000) {
      this.fetchRequest = fetchRequest;
      this.timeoutMs = timeoutMs;
    }

    async request(token, path, { method = "GET", body, idempotencyKey } = {}) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
      const headers = { Authorization: `Bearer ${token}`, Accept: "application/json" };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
      try {
        const response = await this.fetchRequest(path, {
          method, headers, body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error"
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result && isText(result.error) ? result.error : `The request returned HTTP ${response.status}.`);
        return result;
      } finally {
        clearTimeout(timeout);
      }
    }

    async quote(token, checkoutId) {
      const quote = await this.request(token, "/v1/quotes", { method: "POST", body: { checkout_id: checkoutId } });
      if (!validQuote(quote)) throw new Error("The server returned an invalid quote.");
      return quote;
    }

    async apply(pendingApplication) {
      const application = await this.request(pendingApplication.token, "/v1/applications", {
        method: "POST", idempotencyKey: pendingApplication.idempotencyKey,
        body: { quote_id: pendingApplication.quoteId, offer_id: pendingApplication.offerId }
      });
      if (!validApplication(application) || application.offer_id !== pendingApplication.offerId
          || application.nominal_paise !== pendingApplication.nominalPaise) {
        throw new Error("The server returned an invalid application result.");
      }
      return application;
    }

    async application(token, applicationId) {
      const application = await this.request(token, `/v1/applications/${encodeURIComponent(applicationId)}`);
      if (!validApplication(application) || application.application_id !== applicationId) {
        throw new Error("The server returned an invalid application result. The last known state is still shown.");
      }
      return application;
    }
  }

  const api = new CheckoutApiClient();

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderMessage(text, error = false) {
    const box = byId("message");
    box.textContent = text;
    box.classList.toggle("error", error);
    box.hidden = !text;
    box.setAttribute("role", error ? "alert" : "status");
  }

  function renderControls() {
    const pending = Boolean(state.pendingApplication);
    byId("quote-button").disabled = state.busy || pending;
    byId("forget-button").disabled = state.busy;
    byId("checkout-id").disabled = state.busy || pending;
    byId("customer-token").disabled = state.busy || pending;
    byId("refresh-button").disabled = state.busy || !state.application;
    byId("apply-button").disabled = state.busy || !state.selectedOffer || Boolean(state.application);
    byId("apply-button").textContent = pending ? "Retry the same application" : "Apply selected offer";
    for (const radio of document.querySelectorAll('input[name="offer"]')) {
      radio.disabled = state.busy || pending || Boolean(state.application);
    }
  }

  function clearOwnedResources() {
    // A failed switch must never leave a previous customer's or basket's choice active.
    state.checkoutId = null;
    state.quote = null;
    state.selectedOffer = null;
    state.application = null;
    byId("offer-list").replaceChildren();
    byId("status-facts").replaceChildren();
    byId("offers-panel").hidden = true;
    byId("status-panel").hidden = true;
  }

  function renderQuoteMetadata() {
    if (!state.quote) return;
    const expiry = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "long", timeZone: "UTC" }).format(new Date(state.quote.expires_at));
    byId("quote-revision").textContent = `Rules: ${state.quote.revision}`;
    byId("quote-expiry").textContent = state.application ? "Offer applied" : `Server quote expiry: ${expiry}`;
  }

  function renderOffers() {
    const list = byId("offer-list");
    list.replaceChildren();
    state.quote.decisions.forEach((decision, index) => {
      const card = element("div", "offer-card");
      const radio = element("input");
      radio.id = `offer-option-${index}`;
      radio.type = "radio";
      radio.name = "offer";
      radio.value = decision.offer_id;
      radio.checked = index === 0;
      radio.setAttribute("aria-describedby", `offer-values-${index}`);
      const selectOffer = () => { if (!radio.disabled) { radio.checked = true; state.selectedOffer = decision; renderControls(); } };
      radio.addEventListener("change", selectOffer);
      card.addEventListener("click", selectOffer);
      card.append(radio);
      const heading = element("div", "offer-heading");
      heading.append(element("span", "offer-rank", `Rank ${decision.rank} · expected value`));
      const title = element("h3");
      const label = element("label", "offer-choice-label", decision.issuer);
      label.htmlFor = radio.id;
      title.append(label);
      heading.append(title);
      card.append(heading, element("span", "offer-timing", decision.payout === "cashback" ? "Cashback later" : "Immediate discount"));
      const amounts = element("dl", "money-grid");
      amounts.id = `offer-values-${index}`;
      for (const [labelText, amount] of [["Contract benefit", decision.nominal_benefit_paise], ["Expected value", decision.expected_benefit_paise]]) {
        const item = element("div");
        item.append(element("dt", "", labelText), element("dd", "", money(amount)));
        amounts.append(item);
      }
      card.append(amounts, element("span", "offer-detail", `Eligible spend: ${money(decision.eligible_spend_paise)}`));
      const probability = Number(decision.realization_probability);
      if (decision.payout === "cashback" && Number.isFinite(probability) && probability > 0 && probability <= 1) {
        card.append(element("span", "offer-detail", `Synthetic ranking assumption: ${Math.round(probability * 100)}% realization. This is not a bank success rate.`));
      }
      card.append(element("span", "offer-detail", `Campaign funds reserved if accepted: ${money(decision.nominal_benefit_paise)}.`));
      list.append(card);
    });
    state.selectedOffer = state.quote.decisions[0] || null;
    byId("offers-panel").hidden = false;
    byId("empty-offers").hidden = state.quote.decisions.length > 0;
    byId("application-action").hidden = state.quote.decisions.length === 0;
    renderQuoteMetadata();
  }

  const explanations = {
    HELD: "The offer is reserved. A worker has not submitted the payment yet.",
    DISPATCHING: "Submission is in progress. The reservation stays held until evidence resolves it.",
    PENDING: "We are waiting for verified payment evidence. The full reservation remains held.",
    UNKNOWN: "The result needs reconciliation. A timeout is not a failed payment. Do not start a replacement payment.",
    OWED: "Payment was captured. The benefit is owed; separate matching delivery evidence is still required.",
    FULFILLED: "Matching delivery evidence confirmed the benefit. The campaign funds remain committed.",
    RELEASED: "The held reservation was released before a benefit obligation was committed. No benefit was confirmed as delivered."
  };

  function renderApplication() {
    const application = state.application;
    byId("status-panel").hidden = false;
    byId("application-state").textContent = application.state;
    byId("application-state").dataset.state = application.state;
    byId("state-explanation").textContent = explanations[application.state];
    const facts = byId("status-facts");
    facts.replaceChildren();
    for (const [label, value, wide] of [
      ["Payment", application.payment_state], ["Benefit", application.benefit_state], ["Campaign funding", application.budget_state],
      ["Contract benefit", money(application.nominal_paise)], ["Offer", application.offer_id], ["Application ID", application.application_id, true]
    ]) {
      const item = element("div", wide ? "wide" : "");
      item.append(element("dt", "", label), element("dd", "", String(value)));
      facts.append(item);
    }
    renderQuoteMetadata();
  }

  byId("checkout-id").addEventListener("input", () => {
    if (state.busy || state.pendingApplication) return;
    clearOwnedResources();
    renderControls();
  });
  byId("customer-token").addEventListener("input", () => {
    if (state.busy || state.pendingApplication) return;
    state.token = "";
    clearOwnedResources();
    renderControls();
  });

  byId("checkout-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || state.pendingApplication) return;
    const suppliedToken = byId("customer-token").value.trim();
    if (suppliedToken) state.token = suppliedToken;
    byId("customer-token").value = "";
    byId("customer-token").placeholder = state.token ? "Credential held for this page" : "Paste your customer bearer token";
    clearOwnedResources();
    if (!state.token) { renderMessage("Enter the customer credential from the reference setup.", true); renderControls(); return; }
    const checkoutId = byId("checkout-id").value.trim();
    state.busy = true;
    renderMessage("Checking the trusted basket and current offer rules…");
    renderControls();
    try {
      state.quote = await api.quote(state.token, checkoutId);
      state.checkoutId = checkoutId;
      renderOffers();
      renderMessage("The quote is ready. Viewing these offers does not reserve campaign funding.");
    } catch (error) {
      renderMessage(error.name === "AbortError" ? "The quote request timed out. You can load the quote again." : error.message, true);
    } finally {
      state.busy = false;
      renderControls();
    }
  });

  byId("apply-button").addEventListener("click", async () => {
    if (state.busy || state.application || !state.selectedOffer || !state.quote) return;
    if (!state.pendingApplication) {
      const random = crypto.getRandomValues(new Uint8Array(16));
      const idempotencyKey = Array.from(random, (byte) => byte.toString(16).padStart(2, "0")).join("");
      state.pendingApplication = Object.freeze({
        token: state.token, checkoutId: state.checkoutId, quoteId: state.quote.quote_id,
        offerId: state.selectedOffer.offer_id, nominalPaise: state.selectedOffer.nominal_benefit_paise, idempotencyKey
      });
    }
    state.busy = true;
    renderMessage("Applying the offer with one stable retry identity…");
    renderControls();
    try {
      state.application = await api.apply(state.pendingApplication);
      state.pendingApplication = null;
      renderApplication();
      renderMessage("The application is recorded. Its payment and benefit have their own states.");
      byId("status-heading").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) {
      renderMessage(`${error.name === "AbortError" ? "The request timed out." : error.message} The outcome is not confirmed on this page. Retry the same application; its identity, credential and offer remain unchanged.`, true);
    } finally {
      state.busy = false;
      renderControls();
    }
  });

  byId("refresh-button").addEventListener("click", async () => {
    if (state.busy || !state.application) return;
    state.busy = true;
    renderControls();
    try {
      state.application = await api.application(state.token, state.application.application_id);
      renderApplication();
      renderMessage("Status refreshed from the application service.");
    } catch (error) {
      renderMessage(error.name === "AbortError" ? "Status could not be refreshed before the timeout. The last known state is still shown." : error.message, true);
    } finally {
      state.busy = false;
      renderControls();
    }
  });

  byId("forget-button").addEventListener("click", () => {
    if (state.busy) return;
    state.token = "";
    state.pendingApplication = null;
    clearOwnedResources();
    byId("customer-token").value = "";
    byId("checkout-id").value = "";
    byId("customer-token").placeholder = "Paste your customer bearer token";
    renderMessage("Credential and page state cleared. Resetting this page does not cancel a recorded application or release campaign funding.");
    renderControls();
  });

  renderControls();
})();
