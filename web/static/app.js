// PDF AI Assistant - browser client. No framework, no build step.

const $ = (id) => document.getElementById(id);

const state = {
  documents: [],
  doc: null,        // selected document
  sessionId: null,  // conversation being continued, null for a new one
  review: null,     // review on screen
  busy: false,
};

// ---------- API ----------

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) {
    // The platform's sign-in page; it comes back here afterwards.
    location.href = "/.auth/login/aad?post_login_redirect_uri=/";
    throw new Error("Sign in required.");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try { message = (await res.json()).detail || message; } catch { /* not JSON */ }
    throw new Error(typeof message === "string" ? message : "Invalid request.");
  }
  if (res.status === 204) return null;
  return res.headers.get("content-type")?.includes("text/event-stream") ? res : res.json();
}

const postJson = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// Reads a server-sent event stream: calls onEvent(name, data) for each event.
async function readEvents(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const name = raw.match(/^event: (.*)$/m)?.[1];
      const data = raw.match(/^data: (.*)$/m)?.[1];
      if (name && data) onEvent(name, JSON.parse(data));
    }
  }
}

// ---------- rendering helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child != null) node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// A small, safe Markdown subset: everything is escaped first, then only
// headings, lists, bold, italics and inline code are turned into tags.
function markdown(text) {
  const inline = (s) => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of text.split("\n")) {
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^#{1,4}\s+(.*)/))) {
      closeList(); out.push(`<h3>${inline(m[1])}</h3>`);
    } else if (line.trim()) {
      closeList(); out.push(`<p>${inline(line)}</p>`);
    } else {
      closeList();
    }
  }
  closeList();
  return out.join("");
}

const size = (bytes) => (bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`);
const money = (v) => (v == null ? "n/a" : `$${Number(v).toFixed(4)}`);
const pages = (c) => (c.start_page === c.end_page ? `p. ${c.start_page}` : `p. ${c.start_page}–${c.end_page}`);

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 5000);
}

// Citation chips: one per page range, the quoted passages shown on click.
function sourcesNode(citations) {
  if (!citations?.length) return null;
  const groups = new Map();
  for (const c of citations) {
    const key = pages(c);
    if (!groups.has(key)) groups.set(key, { first: c.start_page, quotes: [] });
    groups.get(key).quotes.push(c.cited_text);
  }
  const sorted = [...groups.entries()].sort((a, b) => a[1].first - b[1].first);
  return el("div", { class: "sources" }, el("span", {}, "Sources"),
    sorted.map(([label, g]) => el("button", {
      class: "chip", type: "button", title: "Show the quoted passage",
      onclick: (e) => showQuote(e.currentTarget, label, g.quotes),
    }, label)));
}

function showQuote(anchor, label, quotes) {
  const box = $("quote");
  box.replaceChildren(el("strong", {}, label), ...quotes.map((q) => el("q", {}, q.trim())));
  box.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  const top = Math.min(r.bottom + 6, window.innerHeight - box.offsetHeight - 10);
  box.style.top = `${Math.max(10, top)}px`;
  box.style.left = `${Math.max(10, Math.min(r.left, window.innerWidth - box.offsetWidth - 10))}px`;
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".chip, .quote")) $("quote").classList.add("hidden");
});

function metaLine(usage) {
  if (!usage) return null;
  const cached = usage.cache_read_tokens ? ` · ${usage.cache_read_tokens.toLocaleString()} cached` : "";
  return el("div", { class: "meta" },
    `${usage.model} · ${(usage.latency_ms / 1000).toFixed(1)}s · ` +
    `${usage.output_tokens.toLocaleString()} output tokens${cached} · ${money(usage.cost_usd)}`);
}

function addMessage(role, text, extra = {}) {
  const body = el("div", { class: "body" });
  if (role === "assistant") body.innerHTML = markdown(text); else body.textContent = text;
  const node = el("div", { class: `msg ${role}` }, body,
    sourcesNode(extra.citations), extra.meta || null);
  $("messages").append(node);
  node.scrollIntoView({ block: "end" });
  return node;
}

// ---------- views ----------

function show(view) {
  for (const id of ["empty", "chat", "review", "results"]) $(id).classList.toggle("hidden", id !== view);
}

async function refreshMe() {
  const me = await api("/api/me");
  $("user-name").textContent = me.auth === "none" ? "local mode" : me.name;
  $("logout").classList.toggle("hidden", me.auth === "none");
  $("budget").textContent = `${money(me.spent_today_usd)} / $${me.daily_budget_usd.toFixed(2)} today`;
}

async function refreshDocuments() {
  state.documents = await api("/api/documents");
  const list = $("documents");
  list.replaceChildren(...state.documents.map((d) => el("li",
    { class: state.doc?.id === d.id ? "active" : "" },
    el("button", { class: "item", type: "button", onclick: () => openDocument(d.id) },
      el("span", { class: "name", title: d.filename }, d.filename),
      el("small", {}, `${d.page_count ?? "?"} pages · ${d.questions} questions`)),
    el("button", {
      class: "delete", type: "button", title: "Delete document and its history",
      "aria-label": `Delete ${d.filename}`, onclick: () => deleteDocument(d),
    }, "✕"))));
  if (!state.documents.length) list.append(el("li", { class: "muted" }, "No documents yet."));
}

async function refreshSessions() {
  const sessions = await api(`/api/documents/${state.doc.id}/sessions`);
  $("sessions-block").classList.toggle("hidden", !sessions.length);
  $("sessions").replaceChildren(...sessions.map((s) => el("li",
    { class: state.sessionId === s.id ? "active" : "" },
    el("button", { class: "item", type: "button", onclick: () => openSession(s.id) },
      el("span", { class: "name" }, s.kind === "summary" ? `Summary (${s.language})` : `Chat #${s.id}`),
      el("small", {}, `${new Date(s.updated_at).toLocaleString()} · ${s.questions} q · ${money(s.cost_usd)}`)))));
}

async function refreshReviews() {
  const reviews = await api(`/api/documents/${state.doc.id}/reviews`);
  const labels = Object.fromEntries([...$("playbook").options].map((o) => [o.value, o.textContent]));
  $("reviews-block").classList.toggle("hidden", !reviews.length);
  $("reviews").replaceChildren(...reviews.map((r) => el("li",
    { class: state.review?.id === r.id && !$("review").classList.contains("hidden") ? "active" : "" },
    el("button", { class: "item", type: "button", onclick: () => openReview(r.id) },
      el("span", { class: "name" }, `${labels[r.playbook] || r.playbook} (${r.language})`,
        r.overall_risk ? el("span", { class: `badge ${r.overall_risk}` }, `${r.overall_risk} risk`) : null),
      el("small", {}, `${new Date(r.created_at).toLocaleString()} · ${money(r.cost_usd)}`)))));
}

async function openDocument(id, sessionId = null) {
  state.doc = state.documents.find((d) => d.id === id);
  if (!state.doc) return;
  state.sessionId = null;
  $("doc-title").textContent = state.doc.filename;
  $("doc-meta").textContent =
    `${state.doc.page_count ?? "?"} pages · ${size(state.doc.size_bytes)}`;
  $("messages").replaceChildren();
  show("chat");
  await Promise.all([refreshDocuments(), refreshSessions(), refreshReviews()]);
  if (sessionId) await openSession(sessionId);
  $("question").focus();
}

async function openSession(id) {
  const s = await api(`/api/sessions/${id}`);
  if (state.doc?.id !== s.document_id) return openDocument(s.document_id, id);
  show("chat");
  state.sessionId = s.kind === "chat" ? s.id : null;  // a summary can't be continued as a chat
  $("messages").replaceChildren();
  for (const m of s.messages) {
    if (m.role === "user" && s.kind === "summary") continue;  // hide the summary prompt
    addMessage(m.role, m.text, {
      citations: m.citations,
      meta: m.role === "assistant" ? el("div", { class: "meta" }, `${m.model} · ${money(m.cost_usd)}`) : null,
    });
  }
  refreshSessions();
}

function newChat() {
  state.sessionId = null;
  $("messages").replaceChildren();
  refreshSessions();
  $("question").focus();
}

// ---------- actions ----------

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["ask-btn", "summary-btn", "new-chat-btn", "review-btn", "rerun-btn"]) $(id).disabled = busy;
}

// Streams an answer into a new assistant bubble.
async function streamAnswer(res) {
  const node = addMessage("assistant", "");
  node.classList.add("streaming");
  const body = node.querySelector(".body");
  let text = "";
  let result = null;
  try {
    await readEvents(res, (name, data) => {
      if (name === "delta") {
        text += data.text;
        body.innerHTML = markdown(text);
        node.scrollIntoView({ block: "end" });
      } else if (name === "done") {
        result = data;
      } else if (name === "error") {
        throw new Error(data.message);
      }
    });
    if (!result) throw new Error("The answer was interrupted. Nothing was saved.");
  } catch (err) {
    body.textContent = err.message;
    node.classList.add("error");
    return null;
  } finally {
    node.classList.remove("streaming");
  }
  if (result.refused) {
    body.textContent = result.text;
    node.classList.add("error");
    return result;
  }
  const extra = [sourcesNode(result.citations), metaLine(result.usage)];
  if (result.truncated) extra.push(el("div", { class: "meta" }, "The answer was cut off at the output limit."));
  node.append(...extra.filter(Boolean));
  return result;
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  setBusy(true);
  addMessage("user", question);
  try {
    const res = await postJson("/api/chat", {
      document_id: state.doc.id, session_id: state.sessionId, question,
    });
    const result = await streamAnswer(res);
    if (result?.session_id) state.sessionId = result.session_id;
  } catch (err) {
    addMessage("assistant", err.message).classList.add("error");
  } finally {
    setBusy(false);
    refreshSessions();
    refreshDocuments();
    refreshMe();
  }
}

async function summarize() {
  if (state.busy) return;
  setBusy(true);
  state.sessionId = null;
  $("messages").replaceChildren();
  show("chat");
  const language = $("summary-lang").value;
  try {
    const res = await postJson(`/api/documents/${state.doc.id}/summary`, { language });
    if (res instanceof Response) {
      await streamAnswer(res);
    } else {
      addMessage("assistant", res.text, {
        citations: res.citations,
        meta: el("div", { class: "meta" },
          `Saved summary from ${new Date(res.created_at).toLocaleString()} · no cost`),
      });
    }
  } catch (err) {
    addMessage("assistant", err.message).classList.add("error");
  } finally {
    setBusy(false);
    refreshSessions();
    refreshMe();
  }
}

// ---------- document review ----------

const SECTION_CHECK = {
  verified: ["ok", "✓ quote found in the PDF"],
  moved: ["ok", "✓ quote found (page corrected)"],
  not_found: ["warn", "⚠ quote not found in the PDF, check this finding"],
  unchecked: ["muted", "not checked (no text layer)"],
};

function evidence(item) {
  if (!item.quote) return null;
  const [cls, label] = SECTION_CHECK[item.verified] || SECTION_CHECK.unchecked;
  return el("div", { class: "evidence" },
    el("span", { class: "page" }, `p. ${item.page}`),
    el("q", {}, item.quote),
    el("span", { class: `check ${cls}` }, label));
}

function reviewNode(r) {
  const rep = r.report;
  const v = rep.verification;
  const total = v.verified + v.moved + v.not_found + v.unchecked;
  const found = v.verified + v.moved;
  const nodes = [];

  nodes.push(el("div", { class: `verdict ${rep.overall_risk}` },
    el("div", { class: "verdict-head" },
      el("span", { class: `badge ${rep.overall_risk}` }, `${rep.overall_risk} risk`),
      el("span", { class: "doctype" }, rep.document_type),
      el("strong", {}, rep.title)),
    el("p", {}, rep.summary),
    rep.parties.length ? el("p", { class: "muted" },
      rep.parties.map((p) => `${p.name} (${p.role})`).join(" · ")) : null));

  if (total) {
    const cls = v.not_found ? "warn" : v.text_layer ? "ok" : "muted";
    const text = !v.text_layer
      ? "This PDF has no text layer (a scan), so quotes could not be checked automatically."
      : v.not_found
        ? `${found} of ${total} quotes were found in the PDF. ${v.not_found} could not be found: check those findings by hand.`
        : `All ${total} quotes were found word for word in the PDF${v.moved ? ` (${v.moved} page numbers corrected)` : ""}.`;
    nodes.push(el("div", { class: `banner ${cls}` }, text));
  }

  if (rep.checks.length) {
    nodes.push(el("h3", {}, "Arithmetic check"),
      el("ul", { class: "checks" }, rep.checks.map((c) => el("li", { class: c.status },
        el("span", {}, c.status === "ok" ? "✓" : "✗"),
        `${c.check}: `, el("b", {}, c.status === "ok" ? "correct" :
          `expected ${c.expected.toLocaleString()}, the document says ${c.found.toLocaleString()}`)))));
  }

  nodes.push(el("h3", {}, `Risks (${rep.risks.length})`));
  if (!rep.risks.length) nodes.push(el("p", { class: "muted" }, "No risks found."));
  for (const risk of rep.risks) {
    nodes.push(el("article", { class: `risk ${risk.severity}` },
      el("div", { class: "risk-head" },
        el("span", { class: `badge ${risk.severity}` }, risk.severity), el("strong", {}, risk.title)),
      el("p", {}, risk.explanation),
      el("p", { class: "advice" }, el("b", {}, "What to do: "), risk.recommendation),
      evidence(risk)));
  }

  if (rep.deadlines.length) {
    nodes.push(el("h3", {}, "Deadlines and dates"),
      el("table", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "What"), el("th", {}, "Evidence")),
        [...rep.deadlines].sort((a, b) => (a.date || "9").localeCompare(b.date || "9")).map((d) => el("tr", {},
          el("td", { class: "nowrap" }, d.date ? new Date(`${d.date}T00:00`).toLocaleDateString() : "–"),
          el("td", {}, d.description), el("td", {}, evidence(d))))));
  }

  if (rep.key_facts.length) {
    nodes.push(el("h3", {}, "Key facts"),
      el("table", {}, el("tr", {}, el("th", {}, "Item"), el("th", {}, "Value"), el("th", {}, "Evidence")),
        rep.key_facts.map((f) => el("tr", {}, el("td", {}, f.label), el("td", {}, el("b", {}, f.value)),
          el("td", {}, evidence(f))))));
  }

  if (rep.line_items.length) {
    const n = (x) => (x == null ? "–" : x.toLocaleString(undefined, { minimumFractionDigits: 2 }));
    const t = rep.totals;
    nodes.push(el("h3", {}, `Line items${t.currency ? ` (${t.currency})` : ""}`),
      el("table", {},
        el("tr", {}, el("th", {}, "Description"), el("th", { class: "num" }, "Qty"),
          el("th", { class: "num" }, "Unit price"), el("th", { class: "num" }, "Amount")),
        rep.line_items.map((l) => el("tr", {}, el("td", {}, l.description), el("td", { class: "num" }, n(l.quantity)),
          el("td", { class: "num" }, n(l.unit_price)), el("td", { class: "num" }, n(l.amount)))),
        [["Subtotal", t.subtotal], ["Tax", t.tax], ["Total", t.total]].filter(([, x]) => x != null).map(([k, x]) =>
          el("tr", { class: "total" }, el("td", { colspan: "3" }, k), el("td", { class: "num" }, n(x))))));
  }

  if (rep.missing.length) {
    nodes.push(el("h3", {}, "Missing or unclear"),
      el("ul", { class: "missing" }, rep.missing.map((m) => el("li", {}, el("b", {}, m.item), ` – ${m.why_it_matters}`))));
  }
  return nodes;
}

const ANSWER_STATUS = {
  answered: ["ok", "Answered"],
  partial: ["medium", "Partly answered"],
  needs_input: ["high", "Needs input"],
};

function answersNode(r) {
  const rep = r.report;
  const g = rep.grounding;
  const v = rep.verification;
  const nodes = [el("div", { class: "verdict" },
    el("div", { class: "verdict-head" }, el("span", { class: "doctype" }, "questionnaire"), el("strong", {}, rep.title)),
    el("p", {}, `${rep.questions.length} questions: ${g.answered} answered, ${g.partial} partly answered, ` +
      `${g.needs_input} need a person. Answered from ${rep.library.sent} of ${rep.library.entries} library entries.`))];

  const problems = [];
  if (g.unsupported) problems.push(`${g.unsupported} answer${g.unsupported === 1 ? "" : "s"} had no valid library source and ${g.unsupported === 1 ? "was" : "were"} not filled in.`);
  if (v.not_found) problems.push(`${v.not_found} question${v.not_found === 1 ? "" : "s"} could not be found word for word in the PDF.`);
  nodes.push(el("div", { class: `banner ${problems.length ? "warn" : v.text_layer ? "ok" : "muted"}` },
    problems.length ? problems.join(" ") : v.text_layer
      ? "Every answer cites the library, and every question was found in the PDF."
      : "Every answer cites the library. This PDF has no text layer, so questions could not be checked."));

  for (const q of rep.questions) {
    const [cls, label] = ANSWER_STATUS[q.status];
    nodes.push(el("article", { class: `risk ${cls === "ok" ? "low" : cls}` },
      el("div", { class: "risk-head" },
        el("span", { class: `badge ${cls === "ok" ? "low" : cls}` }, label),
        el("strong", {}, `${q.number ? `${q.number} ` : ""}${q.question}`)),
      q.answer ? el("p", {}, q.answer) : null,
      q.note ? el("p", { class: "advice" }, el("b", {}, "Note: "), q.note) : null,
      q.draft ? el("p", { class: "muted" }, el("b", {}, "Unverified draft: "), q.draft) : null,
      el("div", { class: "evidence" },
        el("span", { class: "page" }, `p. ${q.page}`),
        q.source_refs.map((s) => el("span", { class: "source", title: s.question }, `#${s.id} ${s.source || s.question}`)),
        q.verified === "not_found" ? el("span", { class: "check warn" }, "⚠ question not found in the PDF") : null)));
  }
  return nodes;
}

function showReview(r, cached) {
  state.review = r;
  const isQuestionnaire = r.playbook === "questionnaire";
  $("review-title").textContent = r.filename;
  const cost = cached ? "saved review, no cost" : money(r.cost_usd);
  $("review-meta").textContent = `${$("playbook").selectedOptions[0]?.textContent || r.playbook} review in ` +
    `${r.language} · ${new Date(r.created_at).toLocaleString()} · ${r.model} · ` +
    `${(r.latency_ms / 1000).toFixed(0)}s · ${cost}`;
  $("export-csv").href = `/api/reviews/${r.id}/export.csv`;
  $("export-ics").href = `/api/reviews/${r.id}/deadlines.ics`;
  $("export-xlsx").href = `/api/reviews/${r.id}/answers.xlsx`;
  $("export-xlsx").classList.toggle("hidden", !isQuestionnaire);
  $("export-csv").textContent = isQuestionnaire ? "CSV" : "Excel (CSV)";
  $("export-ics").classList.toggle("hidden", isQuestionnaire || !r.report.deadlines.some((d) => d.date));
  $("review-body").replaceChildren(...(isQuestionnaire ? answersNode(r) : reviewNode(r)));
  for (const id of ["export-csv", "rerun-btn"]) $(id).classList.remove("hidden");
  show("review");
  refreshReviews();
}

async function openReview(id) {
  try {
    const r = await api(`/api/reviews/${id}`);
    $("playbook").value = r.playbook;
    $("summary-lang").value = r.language;
    showReview(r, true);
  } catch (err) {
    toast(err.message);
  }
}

async function runReview(refresh = false) {
  if (state.busy) return;
  setBusy(true);
  const playbook = $("playbook").value;
  const language = $("summary-lang").value;
  $("review-title").textContent = state.doc.filename;
  $("review-meta").textContent = "";
  for (const id of ["export-xlsx", "export-csv", "export-ics", "rerun-btn"]) $(id).classList.add("hidden");
  const progress = el("p", { class: "progress" }, "Claude is reading the whole document…");
  $("review-body").replaceChildren(el("div", { class: "working" }, el("span", { class: "spinner" }), progress));
  show("review");
  try {
    const res = playbook === "questionnaire"
      ? await postJson(`/api/documents/${state.doc.id}/autofill`, { language, refresh })
      : await postJson(`/api/documents/${state.doc.id}/review`, { playbook, language, refresh });
    if (!(res instanceof Response)) { showReview(res, true); return; }
    let result = null;
    await readEvents(res, (name, data) => {
      if (name === "progress") {
        progress.textContent = `Claude is writing the report… ${data.characters.toLocaleString()} characters so far`;
      } else if (name === "done") {
        result = data;
      } else if (name === "error") {
        throw new Error(data.message);
      }
    });
    if (!result) throw new Error("The review was interrupted. Nothing was saved.");
    showReview(result, false);
  } catch (err) {
    $("review-body").replaceChildren(el("div", { class: "banner warn" }, err.message));
    $("rerun-btn").classList.remove("hidden");
  } finally {
    setBusy(false);
    refreshMe();
    refreshReviews();
  }
}

async function loadPlaybooks() {
  const playbooks = await api("/api/playbooks");
  $("playbook").replaceChildren(...playbooks.map((p) => el("option", { value: p.id }, p.label)));
}

async function refreshLibrary() {
  const lib = await api("/api/library");
  $("library-count").textContent = lib.count
    ? `${lib.count} approved answer${lib.count === 1 ? "" : "s"}` : "No approved answers yet.";
  return lib;
}

async function importLibrary(file) {
  if (!file) return;
  if (!confirm(`Replace the answer library with the rows of "${file.name}"?`)) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const { count } = await api("/api/library", { method: "POST", body: form });
    toast(`Imported ${count} approved answers.`);
    await refreshLibrary();
  } catch (err) {
    toast(err.message);
  }
}

async function showLibrary() {
  try {
    const lib = await refreshLibrary();
    $("results-title").textContent = `Answer library (${lib.count})`;
    $("results-body").replaceChildren(lib.count
      ? el("table", {},
        el("tr", {}, el("th", {}, "#"), el("th", {}, "Question"), el("th", {}, "Approved answer"), el("th", {}, "Source")),
        lib.entries.map((e) => el("tr", {}, el("td", {}, String(e.id)), el("td", {}, e.question),
          el("td", {}, e.answer), el("td", {}, e.source || ""))))
      : el("p", { class: "muted" }, "Import an Excel or CSV file with the columns question, answer and source."));
    show("results");
  } catch (err) {
    toast(err.message);
  }
}

async function upload(file) {
  if (!file) return;
  const status = $("upload-status");
  status.textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append("file", file);
  try {
    const doc = await api("/api/documents", { method: "POST", body: form });
    status.textContent = "";
    await refreshDocuments();
    await openDocument(doc.id);
  } catch (err) {
    status.textContent = "";
    toast(err.message);
  }
}

async function deleteDocument(doc) {
  if (!confirm(`Delete "${doc.filename}" and all its conversations?`)) return;
  try {
    await api(`/api/documents/${doc.id}`, { method: "DELETE" });
    if (state.doc?.id === doc.id) { state.doc = null; show("empty"); }
    await refreshDocuments();
  } catch (err) {
    toast(err.message);
  }
}

async function search(term) {
  try {
    const hits = await api(`/api/search?q=${encodeURIComponent(term)}`);
    $("results-title").textContent = `${hits.length} result${hits.length === 1 ? "" : "s"} for “${term}”`;
    $("results-body").replaceChildren(...hits.map((h) => {
      const snippet = h.text.length > 260 ? `${h.text.slice(0, 260)}…` : h.text;
      const text = el("div");
      text.innerHTML = escapeHtml(snippet).replace(
        new RegExp(escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
      return el("button", {
        class: "hit", type: "button",
        onclick: async () => { await openDocument(h.document_id, h.session_id); },
      }, el("small", {}, `${h.filename} · ${h.role === "user" ? "You" : "AI"} · ${new Date(h.created_at).toLocaleString()}`), text);
    }));
    show("results");
  } catch (err) {
    toast(err.message);
  }
}

async function showStats() {
  try {
    const s = await api("/api/stats");
    $("results-title").textContent = "Usage and cost";
    const prompt = s.per_document.reduce((n, r) => n + r.prompt_tokens, 0);
    const answers = s.per_document.reduce((n, r) => n + r.answers, 0);
    $("results-body").replaceChildren(
      el("div", { class: "kpis" },
        el("div", { class: "kpi" }, el("b", {}, money(s.total_cost_usd)), el("span", {}, "total estimated cost")),
        el("div", { class: "kpi" }, el("b", {}, String(answers)), el("span", {}, "answers")),
        el("div", { class: "kpi" }, el("b", {}, `${Math.round(s.cache_hit_rate * 100)}%`),
          el("span", {}, "of prompt tokens served from cache")),
        el("div", { class: "kpi" }, el("b", {}, prompt.toLocaleString()), el("span", {}, "prompt tokens"))),
      el("h3", {}, "By document"),
      el("table", {},
        el("tr", {}, el("th", {}, "Document"), el("th", { class: "num" }, "Answers"),
          el("th", { class: "num" }, "Output tokens"), el("th", { class: "num" }, "Cost")),
        s.per_document.map((r) => el("tr", {}, el("td", {}, r.filename),
          el("td", { class: "num" }, String(r.answers)),
          el("td", { class: "num" }, Number(r.output_tokens).toLocaleString()),
          el("td", { class: "num" }, money(r.cost_usd))))),
      el("h3", {}, "By model"),
      el("table", {},
        el("tr", {}, el("th", {}, "Model"), el("th", { class: "num" }, "Answers"),
          el("th", { class: "num" }, "Avg latency"), el("th", { class: "num" }, "Cost")),
        s.per_model.map((r) => el("tr", {}, el("td", {}, r.model),
          el("td", { class: "num" }, String(r.answers)),
          el("td", { class: "num" }, r.avg_latency_ms ? `${(r.avg_latency_ms / 1000).toFixed(1)}s` : "–"),
          el("td", { class: "num" }, money(r.cost_usd))))));
    show("results");
  } catch (err) {
    toast(err.message);
  }
}

// ---------- wiring ----------

$("file-input").addEventListener("change", (e) => { upload(e.target.files[0]); e.target.value = ""; });
const zone = $("dropzone");
zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
zone.addEventListener("dragleave", () => zone.classList.remove("over"));
zone.addEventListener("drop", (e) => {
  e.preventDefault();
  zone.classList.remove("over");
  upload(e.dataTransfer.files[0]);
});

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value;
  $("question").value = "";
  ask(q);
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask-form").requestSubmit(); }
});
$("summary-btn").addEventListener("click", summarize);
$("review-btn").addEventListener("click", () => runReview(false));
$("rerun-btn").addEventListener("click", () => runReview(true));
$("review-close").addEventListener("click", () => { show("chat"); refreshReviews(); });
$("new-chat-btn").addEventListener("click", newChat);
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const term = $("search-input").value.trim();
  if (term.length >= 2) search(term);
});
$("stats-btn").addEventListener("click", showStats);
$("library-input").addEventListener("change", (e) => { importLibrary(e.target.files[0]); e.target.value = ""; });
$("library-view").addEventListener("click", showLibrary);
$("results-close").addEventListener("click", () => show(state.doc ? "chat" : "empty"));

(async () => {
  try {
    await Promise.all([refreshMe(), refreshDocuments(), loadPlaybooks(), refreshLibrary()]);
  } catch (err) {
    toast(err.message);
  }
})();
