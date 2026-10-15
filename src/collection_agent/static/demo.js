"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = "demo_admin_token";
  let selectedInvoice = null;
  let policyVersion = null;
  let lastSig = {};
  let invoices = [];

  // ---- token -----------------------------------------------------------------------------
  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function askToken() {
    const dlg = $("token-dialog");
    if (!dlg.open) dlg.showModal();
    $("token-input").focus();
  }
  $("token-form").addEventListener("submit", () => {
    try { sessionStorage.setItem(TOKEN_KEY, $("token-input").value); } catch (e) { /* ignore */ }
    $("token-input").value = "";
    start();
  });

  // ---- helpers ---------------------------------------------------------------------------
  const PLAIN = {
    claude_not_configured: "Claude isn't configured (ANTHROPIC_API_KEY)",
    extraction_failed: "Claude couldn't extract rules from this text",
    cognee_not_configured: "Cognee isn't configured (COGNEE_BASE_URL, COGNEE_API_KEY)",
    cognee_failed: "Cognee request failed",
    dataset_not_found: "No policies dataset in Cognee yet",
    stripe_not_configured: "Stripe isn't configured (STRIPE_SECRET_KEY)",
    email_not_configured: "Email sender identity isn't configured (EMAIL_FROM_*, REPLY_TO_DOMAIN, COMPANY_NAME)",
    postmark_not_configured: "Postmark isn't configured (POSTMARK_SERVER_TOKEN)",
    no_approved_policy: "Approve a policy first",
    unauthorized: "Wrong admin token",
    network_error: "Can't reach the server",
    stripe_error: "Stripe returned an error",
    not_pending: "That email was already handled",
  };

  function toast(message, ok) {
    const el = document.createElement("div");
    el.className = "toast" + (ok ? " ok" : " err");
    el.setAttribute("role", ok ? "status" : "alert");
    const text = document.createElement("span");
    text.textContent = ok ? message : (PLAIN[message] || message.replace(/_/g, " "));
    el.appendChild(text);
    if (!ok) {
      const code = document.createElement("code");
      code.textContent = message;
      el.appendChild(code);
    }
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), ok ? 5000 : 8000);
  }

  function errorCode(body, status) {
    if (body && typeof body === "object") {
      if (typeof body.error === "string") return body.error;
      const d = body.detail;
      if (d && typeof d.error === "string") return d.error;
      if (Array.isArray(d)) return "invalid_request";
    }
    return "http_" + status;
  }

  async function call(method, path, body, opts) {
    const headers = { Authorization: "Bearer " + getToken() };
    let payload;
    if (opts && opts.raw) {
      payload = body;
      headers["Content-Type"] = "application/pdf";
    } else if (body !== undefined && body !== null) {
      payload = JSON.stringify(body);
      headers["Content-Type"] = "application/json";
    }
    let res;
    try {
      res = await fetch(path, { method, headers, body: payload });
    } catch (e) {
      const err = new Error("network_error");
      err.status = 0;
      throw err;
    }
    if (opts && opts.text && res.ok) return res.text();
    let data = null;
    try { data = await res.json(); } catch (e) { /* not json */ }
    if (!res.ok) {
      const err = new Error(errorCode(data, res.status));
      err.status = res.status;
      if (res.status === 401) askToken();
      throw err;
    }
    return data;
  }

  // Run an action behind a button; show the error code on failure.
  async function act(button, fn) {
    if (button) button.disabled = true;
    try {
      return await fn();
    } catch (e) {
      toast(e.message || "error");
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function money(minor, currency) {
    const v = (minor / 100).toFixed(2);
    const sym = { GBP: "£", USD: "$", EUR: "€" }[(currency || "").toUpperCase()];
    return (sym || (currency || "").toUpperCase() + " ") + v.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function timeOf(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  // Only re-render a region when its data changed, so open <details> survive the 3s poll.
  function changed(key, data) {
    const sig = JSON.stringify(data);
    if (lastSig[key] === sig) return false;
    lastSig[key] = sig;
    return true;
  }

  // ---- clock ------------------------------------------------------------------------------
  async function refreshClock() {
    const c = await call("GET", "/demo/api/clock");
    $("clock-date").textContent = c.weekday.slice(0, 3) + " " + c.today;
  }

  // ---- policy -----------------------------------------------------------------------------
  function valueText(v) {
    if (v && typeof v === "object") return "day " + v.day_of_month + ", +" + v.month_offset + " month";
    return String(v);
  }

  function renderCards(cards, soft, status) {
    const box = $("cards");
    box.replaceChildren();
    for (const c of cards) {
      const card = el("div", "card");
      const top = el("div", "card-top");
      top.append(el("span", "card-label", c.label), el("span", "card-value", valueText(c.value)));
      card.appendChild(top);
      const chips = el("div", "chips");
      chips.appendChild(el("span", "chip", c.source === "document" ? "from document" : c.source === "default" ? "default" : c.source));
      if (c.changed) chips.appendChild(el("span", "chip warn", "changed"));
      card.appendChild(chips);
      if (c.quote) card.appendChild(el("blockquote", null, c.quote));
      box.appendChild(card);
    }
    const list = $("soft-list");
    list.replaceChildren();
    for (const s of soft || []) {
      const text = typeof s === "string" ? s : s.summary || s.quote || s.text || "";
      list.appendChild(el("li", null, text));
    }
    $("soft").hidden = !(soft && soft.length);
    $("approve-row").hidden = false;
    $("btn-approve").disabled = status !== "draft";
  }

  async function loadApprovedPolicy() {
    try {
      const body = await call("GET", "/policies/approved");
      policyVersion = body.policy_version;
      renderCards(body.cards, body.soft_guidance, "approved");
      $("policy-status").textContent = "Approved policy, version " + body.policy_version + ".";
    } catch (e) {
      if (e.status === 404) $("policy-status").textContent = "No approved policy yet.";
    }
  }

  $("btn-extract").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    $("policy-status").textContent = "Extracting...";
    try {
      const r = await call("POST", "/policies/documents", { title: "Credit control policy", text: $("policy-text").value });
      policyVersion = r.policy_version;
      renderCards(r.cards, r.soft_guidance, "draft");
      $("policy-status").textContent = "Draft version " + r.policy_version + ". Review, then approve.";
    } catch (e) {
      $("policy-status").textContent = "Extraction failed: " + (PLAIN[e.message] || e.message);
      throw e;
    }
  }));

  $("btn-approve").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    const r = await call("POST", "/policies/" + policyVersion + "/approve", { approved_by: "Demo reviewer" });
    renderCards(r.cards, r.soft_guidance, r.status);
    $("policy-status").textContent = "Approved policy, version " + r.policy_version + ".";
    toast("Policy approved", true);
  }));

  $("btn-graph").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    try {
      const html = await call("GET", "/policies/graph", null, { text: true });
      $("graph-frame").srcdoc = html;
      $("graph-dialog").showModal();
    } catch (e) {
      if (e.status === 503) ev.currentTarget.hidden = true;
      throw e;
    }
  }));
  $("graph-close").addEventListener("click", () => $("graph-dialog").close());

  // ---- invoices ---------------------------------------------------------------------------
  const STATE_LABEL = { active: "", disputed: "disputed", with_human: "with a person", escalated: "escalated" };

  async function refreshInvoices() {
    invoices = await call("GET", "/invoices");
    if (changed("invoices", invoices)) {
      const pick = $("invoice-pick");
      pick.replaceChildren();
      if (!invoices.length) pick.appendChild(new Option("No invoices yet", ""));
      for (const inv of invoices) {
        pick.appendChild(new Option(inv.reference + " - " + inv.customer_name, inv.id));
      }
      if (!invoices.some((i) => i.id === selectedInvoice)) selectedInvoice = invoices.length ? invoices[0].id : null;
      pick.value = selectedInvoice || "";
    }
    renderHead();
  }

  function renderHead() {
    const inv = invoices.find((i) => i.id === selectedInvoice);
    $("invoice-head").hidden = !inv;
    if (!inv) return;
    $("inv-amount").textContent = money(inv.amount_minor, inv.currency);
    const pill = $("inv-pill");
    const extra = STATE_LABEL[inv.state];
    pill.textContent = inv.status === "open" && extra ? extra : inv.status;
    pill.className = "pill" + (inv.status === "paid" ? " paid" : "");
    $("inv-due").textContent = "due " + inv.due_on;
    const pay = $("inv-pay");
    if (inv.hosted_invoice_url) {
      pay.href = inv.hosted_invoice_url;
      pay.removeAttribute("aria-disabled");
    } else {
      pay.href = "#";
      pay.setAttribute("aria-disabled", "true");
    }
  }

  $("invoice-pick").addEventListener("change", (ev) => {
    selectedInvoice = ev.target.value || null;
    lastSig.timeline = null;
    renderHead();
    refreshTimeline();
  });

  const HOT = new Set(["reply_classified", "stripe_event", "email_sent", "paused", "escalated"]);

  async function refreshTimeline() {
    const box = $("timeline");
    if (!selectedInvoice) {
      box.replaceChildren(el("li", "empty", "Pick or create an invoice."));
      return;
    }
    const events = await call("GET", "/invoices/" + selectedInvoice + "/timeline");
    if (!changed("timeline", events)) return;
    box.replaceChildren();
    for (const e of events) {
      const li = el("li", "ev" + (HOT.has(e.type) ? " hot" : ""));
      li.appendChild(el("div", "ev-title", e.title));
      if (e.detail) li.appendChild(el("div", "ev-detail", e.detail));
      if (e.tags && e.tags.length) {
        const chips = el("div", "chips");
        for (const t of e.tags) chips.appendChild(el("span", "chip rule", t));
        li.appendChild(chips);
      }
      li.appendChild(el("div", "ev-time", timeOf(e.at)));
      box.appendChild(li);
    }
  }

  $("invoice-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const form = ev.currentTarget;
    const submit = form.querySelector("button[type=submit]");
    act(submit, async () => {
      const f = new FormData(form);
      const file = f.get("contract");
      if (!file || !file.size) throw new Error("contract_required");
      const safeName = (file.name.replace(/[^A-Za-z0-9 ._()-]/g, "") || "contract.pdf").replace(/^[^A-Za-z0-9]+/, "");
      const created = await call("POST", "/invoices", {
        reference: f.get("reference"),
        customer: { name: f.get("name"), email: f.get("email") },
        currency: "gbp",
        lines: [{ description: f.get("description"), amount_minor: Math.round(parseFloat(f.get("amount")) * 100) }],
      });
      await call("PUT", "/invoices/" + created.id + "/contract?filename=" + encodeURIComponent(/\.pdf$/i.test(safeName) ? safeName : "contract.pdf"), await file.arrayBuffer(), { raw: true });
      await call("POST", "/invoices/" + created.id + "/invoice-email");
      selectedInvoice = created.id;
      lastSig = {};
      $("new-invoice").open = false;
      newReference();
      toast("Invoice created, email drafted", true);
      await refreshAll();
    });
  });

  function newReference() {
    const n = String(Date.now()).slice(-6);
    document.querySelector("#invoice-form [name=reference]").value = "DEMO-" + n;
  }

  $("btn-reply").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    if (!selectedInvoice) throw new Error("no_invoice_selected");
    const r = await call("POST", "/demo/api/simulate-reply", { invoice_id: selectedInvoice, text: $("reply-text").value });
    toast("Reply routed: " + r.route, true);
    await refreshAll();
  }));

  // ---- outbox + sent mail -------------------------------------------------------------------
  function mailCard(m, actions) {
    const li = el("li", "mail");
    const chips = el("div", "chips");
    chips.appendChild(el("span", "chip", (m.kind || "email").replace(/_/g, " ")));
    if (m.rule_label) chips.appendChild(el("span", "chip rule", m.rule_label));
    li.appendChild(chips);
    li.appendChild(el("div", "mail-subject", m.subject));
    li.appendChild(el("div", "mail-to", "to " + (m.to_address)));
    const d = el("details");
    d.appendChild(el("summary", null, "Show body"));
    d.appendChild(el("pre", null, m.body_text));
    li.appendChild(d);
    if (actions) li.appendChild(actions);
    return li;
  }

  async function refreshOutbox() {
    const rows = await call("GET", "/outbox");
    if (!changed("outbox", rows)) return;
    const box = $("outbox");
    box.replaceChildren();
    if (!rows.length) box.appendChild(el("li", "empty", "Nothing waiting for approval."));
    for (const m of rows) {
      const row = el("div", "row");
      const ok = el("button", "primary small-btn", "Approve");
      const no = el("button", "ghost small-btn", "Reject");
      ok.type = no.type = "button";
      ok.addEventListener("click", () => act(ok, async () => {
        await call("POST", "/outbox/" + m.id + "/approve", { approved_by: "Demo reviewer" });
        toast("Sent", true);
        await refreshAll();
      }));
      no.addEventListener("click", () => act(no, async () => {
        await call("POST", "/outbox/" + m.id + "/reject", { rejected_by: "Demo reviewer", reason: "Rejected in demo" });
        await refreshAll();
      }));
      row.append(ok, no);
      box.appendChild(mailCard(m, row));
    }
  }

  async function refreshSent() {
    let rows;
    try {
      rows = await call("GET", "/demo/api/sent-emails");
    } catch (e) {
      return;
    }
    $("sent-wrap").hidden = !rows.length;
    if (!changed("sent", rows)) return;
    const box = $("sent");
    box.replaceChildren();
    for (const m of rows) {
      const li = mailCard({ kind: "sent", subject: m.subject, to_address: m.to_address, body_text: m.body_text }, null);
      if (m.attachment_names.length) li.querySelector(".chips").appendChild(el("span", "chip", "attached: " + m.attachment_names.join(", ")));
      box.appendChild(li);
    }
  }

  // ---- clock + daily run ----------------------------------------------------------------------
  function describeReport(r) {
    const entries = Object.entries(r.counts || {});
    if (!entries.length) return "Run for " + r.run_date + ": nothing to do.";
    return "Run for " + r.run_date + ": " + entries.map(([k, v]) => v + " x " + k).join(", ");
  }

  $("btn-run").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    const r = await call("POST", "/demo/api/run-daily");
    $("run-note").textContent = describeReport(r);
    await refreshAll();
  }));

  $("btn-skip").addEventListener("click", (ev) => act(ev.currentTarget, async () => {
    await call("POST", "/demo/api/clock/advance", { days: 7 });
    await refreshClock();
    const r = await call("POST", "/demo/api/run-daily");
    $("run-note").textContent = describeReport(r);
    await refreshAll();
  }));

  // ---- loop -----------------------------------------------------------------------------------
  async function refreshAll() {
    await Promise.all([refreshClock(), refreshInvoices().then(refreshTimeline), refreshOutbox(), refreshSent()]);
  }

  let timer = null;
  async function poll() {
    if (!getToken() || document.hidden) return;
    try {
      await refreshInvoices();
      await Promise.all([refreshTimeline(), refreshOutbox(), refreshSent(), refreshClock()]);
    } catch (e) { /* the next tick tries again; errors from clicks are toasted */ }
  }

  async function start() {
    if (!getToken()) { askToken(); return; }
    try {
      const sample = await call("GET", "/demo/api/sample-policy");
      if (!$("policy-text").value) $("policy-text").value = sample.text;
    } catch (e) {
      if (e.status === 404 && e.message === "not_found") { toast("Demo mode is off on this server"); return; }
    }
    await loadApprovedPolicy();
    try { await refreshAll(); } catch (e) { toast(e.message); }
    if (!timer) timer = setInterval(poll, 3000);
  }

  newReference();
  start();
})();
