"use strict";

// All untrusted text (the field update, quotes, notes) is inserted with textContent,
// never innerHTML, so a field update cannot inject markup into the page.

const CATEGORY_LABELS = {
  stock_availability: "Stock availability",
  inventory_discrepancy: "Inventory discrepancy",
  competitor_activity: "Competitor activity",
  product_quality_safety: "Product quality & safety",
  suspicious_input: "Suspicious input",
  insufficient_information: "Insufficient information",
  other: "Other",
};

const ACTION_LABELS = {
  request_more_info: "Request more information",
  verify_physical_stock: "Verify physical stock",
  recommend_replenishment: "Recommend replenishment",
  reconcile_inventory_records: "Reconcile inventory records",
  escalate_commercial_team: "Escalate to the commercial team",
  escalate_quality_safety: "Escalate to quality & safety",
  flag_security_review: "Flag for security review",
  no_action: "No action",
};

const STATUS_LABELS = {
  verified: "Verified",
  unverified_claim: "Unverified claim",
  conflicting: "Conflicts",
};

const STATUS_PRIORITY = { conflicting: 0, verified: 1, unverified_claim: 2 };

const $ = (id) => document.getElementById(id);

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.title) node.title = options.title;
  for (const child of children) node.append(child);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

let maxChars = 2000;

function updateCounter() {
  const n = $("update").value.length;
  const counter = $("counter");
  counter.textContent = `${n.toLocaleString("en-IN")} of ${maxChars.toLocaleString("en-IN")} characters`;
  counter.classList.toggle("is-near", n > maxChars * 0.9);
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    maxChars = cfg.max_input_chars;
    $("update").maxLength = maxChars;
    updateCounter();
    const label = $("mode-label");
    if (cfg.mock_mode) {
      label.textContent = "Mock mode: rules baseline, no LLM";
      label.classList.add("is-mock");
    } else {
      label.textContent = `Model: ${cfg.model}`;
    }
    $("failure-wrap").hidden = !cfg.allow_failure_simulation;
    const tbody = $("stock-rows");
    clear(tbody);
    for (const row of cfg.verified_data) {
      tbody.append(
        el("tr", {}, [
          el("td", { text: row.row_id }),
          el("td", { text: row.outlet }),
          el("td", { text: row.sku }),
          el("td", { className: "num", text: String(row.system_stock) }),
        ]),
      );
    }
  } catch (err) {
    $("mode-label").textContent = "Server unreachable";
  }
}

async function loadSamples() {
  try {
    const res = await fetch("/api/samples");
    const samples = await res.json();
    const list = $("samples");
    clear(list);
    for (const s of samples) {
      const tag = s.group === "brief" ? "From the brief" : "Added test";
      const button = el("button", { className: "sample" }, [
        el("span", { className: "sample__title", text: s.title }),
        el("span", { className: `sample__tag${s.group === "brief" ? " is-brief" : ""}`, text: tag }),
      ]);
      button.type = "button";
      button.addEventListener("click", () => {
        $("update").value = s.text;
        updateCounter();
        const select = $("failure");
        if (!$("failure-wrap").hidden) select.value = s.simulate_failure || "";
        analyze();
      });
      list.append(el("li", {}, [button]));
    }
  } catch (err) {
    /* samples are optional */
  }
}

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = false;
}

async function analyze() {
  const text = $("update").value.trim();
  $("error").hidden = true;
  if (text.length < 3) {
    showError("Type or paste a field update first (at least 3 characters).");
    return;
  }
  const body = { text };
  const failure = $("failure-wrap").hidden ? "" : $("failure").value;
  if (failure) body.simulate_failure = failure;

  const submit = $("submit");
  submit.disabled = true;
  submit.textContent = "Analyzing…";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 429) {
        const wait = res.headers.get("Retry-After");
        showError(`${data.detail || "Too many requests."}${wait ? ` Try again in ${wait} seconds.` : ""}`);
      } else if (res.status === 422) {
        const detail = Array.isArray(data.detail) ? data.detail.map((d) => `${d.field}: ${d.message}`).join("; ") : data.detail;
        showError(`The request was rejected: ${detail || "invalid input"}.`);
      } else if (res.status === 413) {
        showError("The update is too large. Shorten it and try again.");
      } else {
        showError(`The server returned an error (${res.status})${data.request_id ? `, request ${data.request_id}` : ""}. Try again.`);
      }
      return;
    }
    render(data);
  } catch (err) {
    showError("Could not reach the server. Check your connection and try again.");
  } finally {
    submit.disabled = false;
    submit.textContent = "Analyze update";
  }
}

function renderAnnotated(container, text, evidence) {
  clear(container);
  const spans = evidence
    .filter((e) => e.source === "field_update")
    .map((e) => ({ start: e.span[0], end: e.span[1], status: e.status, note: e.note }))
    .sort((a, b) => STATUS_PRIORITY[a.status] - STATUS_PRIORITY[b.status] || a.start - b.start);
  const kept = [];
  for (const s of spans) {
    if (!kept.some((k) => s.start < k.end && k.start < s.end)) kept.push(s);
  }
  kept.sort((a, b) => a.start - b.start);
  let pos = 0;
  for (const s of kept) {
    if (s.start > pos) container.append(document.createTextNode(text.slice(pos, s.start)));
    container.append(
      el("mark", { className: `ev ev--${s.status}`, text: text.slice(s.start, s.end), title: `${STATUS_LABELS[s.status]}: ${s.note}` }),
    );
    pos = s.end;
  }
  if (pos < text.length) container.append(document.createTextNode(text.slice(pos)));
}

function renderEvidence(list, evidence) {
  clear(list);
  for (const e of evidence) {
    const status = el("div", { className: `ev-status ev-status--${e.status}`, text: STATUS_LABELS[e.status] }, [
      el("span", { className: "ev-source", text: e.source === "field_update" ? "Field update" : "Verified table" }),
    ]);
    let body;
    if (e.source === "field_update") {
      body = el("div", {}, [
        el("p", { className: "ev-quote", text: `“${e.quote}”` }),
        el("p", { className: "ev-note", text: `${e.note} Characters ${e.span[0]}–${e.span[1]}.` }),
      ]);
    } else {
      body = el("div", {}, [
        el("p", { className: "ev-quote", text: `${e.row_id}: ${e.record.outlet}, ${e.record.sku}, ${e.record.system_stock} units in system` }),
        el("p", { className: "ev-note", text: e.note }),
      ]);
    }
    list.append(el("li", {}, [status, body]));
  }
}

function auditRow(dl, term, value) {
  dl.append(el("dt", { text: term }));
  const dd = el("dd");
  if (Array.isArray(value)) {
    if (value.length === 0) {
      dd.textContent = "None";
    } else {
      const ul = el("ul");
      for (const v of value) ul.append(el("li", { text: v }));
      dd.append(ul);
    }
  } else {
    dd.textContent = value;
  }
  dl.append(dd);
}

function render(data) {
  $("empty").hidden = true;
  $("output").hidden = false;

  const stamp = $("stamp");
  stamp.className = `stamp ${data.needs_human_review ? "stamp--review" : "stamp--clear"}`;
  stamp.textContent = data.needs_human_review ? "Needs human review" : "No review needed";
  void stamp.offsetWidth; // restart the landing animation
  stamp.classList.add("is-landing");

  const urgency = $("urgency");
  urgency.className = `urgency urgency--${data.urgency}`;
  urgency.textContent = data.urgency.charAt(0).toUpperCase() + data.urgency.slice(1);
  $("category").textContent = CATEGORY_LABELS[data.issue_category] || data.issue_category;

  const reason = $("reason");
  reason.hidden = !data.review_reason;
  reason.textContent = data.review_reason ? `Why: ${data.review_reason}` : "";

  const degraded = $("degraded");
  degraded.hidden = !data.meta.degraded;
  degraded.textContent = data.meta.degraded
    ? "The LLM was unavailable or its output failed validation, so this result comes from the rules-only fallback and is sent for review."
    : "";

  renderAnnotated($("annotated"), data.meta.analyzed_text, data.evidence);

  const action = data.recommended_next_action;
  $("action-type").textContent = ACTION_LABELS[action.type] || action.type;
  $("action-code").textContent = action.type;
  $("action-details").textContent = action.details;

  const missing = $("missing");
  clear(missing);
  if (data.missing_information.length === 0) {
    missing.append(el("li", { className: "none", text: "Nothing missing." }));
  } else {
    for (const item of data.missing_information) missing.append(el("li", { text: item }));
  }

  renderEvidence($("evidence"), data.evidence);

  const audit = $("audit");
  clear(audit);
  const m = data.meta;
  auditRow(audit, "Model", `${m.provider} / ${m.model}${m.degraded ? " (fallback used)" : ""}`);
  auditRow(audit, "LLM attempts", String(m.llm_attempts));
  auditRow(audit, "Rules triggered", m.rules_triggered);
  auditRow(audit, "Policy overrides", m.policy_overrides);
  auditRow(audit, "Guardrail notes", m.guardrail_notes);
  auditRow(audit, "Latency", `${m.latency_ms} ms`);
  auditRow(audit, "Request ID", m.request_id);

  $("raw").textContent = JSON.stringify(data, null, 2);

  // On narrow screens the result sits below the input column; bring it into view.
  if (window.matchMedia("(max-width: 900px)").matches) {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("result").scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("update").addEventListener("input", updateCounter);
  $("analyze-form").addEventListener("submit", (event) => {
    event.preventDefault();
    analyze();
  });
  $("update").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") analyze();
  });
  updateCounter();
  loadConfig();
  loadSamples();
});
