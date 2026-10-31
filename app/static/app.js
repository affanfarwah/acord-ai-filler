// Shared page behaviour: new case upload, job progress, fact review, documents, missing info.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, csrf: window.CSRF }) });
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(j.error || "Something went wrong. Reload the page and try again.");
  return j;
}

// light by default; the header button switches and remembers the choice
const themeBtn = $("#theme");
if (themeBtn) {
  const label = () => themeBtn.setAttribute("aria-label", document.documentElement.dataset.theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
  label();
  themeBtn.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    if (dark) document.documentElement.dataset.theme = "dark"; else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) {}
    label();
  });
}

// confirm before destructive forms
$$("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

// ---- form picker: coverage lines -> ACORD forms ----
const picker = $("#picker");
if (picker) {
  // Forms each coverage needs. "137@" means "137 for the chosen state".
  const LINES = { auto: ["125", "127", "137@"], gl: ["125", "126"], property: ["125", "140"], wc: ["130@"], umbrella: ["125", "131"] };
  const boxes = $$("input[name=forms]", picker);
  const byNumState = (num, st) => boxes.find((b) => b.dataset.number === num && (b.dataset.state || "") === st);
  const resolve = (code, st) => {
    if (!code.endsWith("@")) return byNumState(code, "");
    const num = code.slice(0, -1);
    return byNumState(num, st) || (num === "130" ? byNumState("130", "") : null);  // 130 has a state version only for CA
  };
  const manual = new Set();  // forms the agent added or removed by hand stay that way
  function apply() {
    const st = $("#state").value;
    const want = new Set();
    $$(".line[aria-pressed=true]", picker).forEach((b) => LINES[b.dataset.line].forEach((code) => { const box = resolve(code, st); if (box) want.add(box); }));
    boxes.forEach((b) => { if (!manual.has(b)) b.checked = want.has(b); });
    render();
  }
  function render() {
    const ul = $("#chosen"); ul.innerHTML = "";
    const on = boxes.filter((b) => b.checked);
    on.forEach((b) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="chosen-no"></span><span class="chosen-name"></span><button type="button" class="linkbtn small">Remove</button>`;
      li.querySelector(".chosen-no").textContent = b.dataset.label;
      li.querySelector(".chosen-name").textContent = b.dataset.name;
      li.querySelector("button").addEventListener("click", () => { b.checked = false; manual.add(b); render(); });
      ul.appendChild(li);
    });
    if (!on.length) ul.innerHTML = "<li class='muted'>No forms yet. Pick a coverage above, or add a form below.</li>";
    $("#chosen-count").textContent = on.length ? `· ${on.length}` : "";
  }
  $$(".line", picker).forEach((b) => b.addEventListener("click", () => { b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true"); apply(); }));
  $("#state").addEventListener("change", apply);
  boxes.forEach((b) => b.addEventListener("change", () => { manual.add(b); render(); }));
  $("#formsearch").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    $$(".formrow", picker).forEach((r) => (r.hidden = !!q && !r.dataset.search.includes(q)));
    $$("fieldset[data-cat]", picker).forEach((f) => (f.hidden = !$$(".formrow", f).some((r) => !r.hidden)));
  });
  apply();
}

// ---- new case ----
const nc = $("#newcase");
if (nc) {
  const files = $("#files"), list = $("#filelist"), drop = $("#drop");
  const show = () => { list.innerHTML = ""; [...files.files].forEach((f) => { const li = document.createElement("li"); li.textContent = f.name; list.appendChild(li); }); };
  files.addEventListener("change", show);
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, () => drop.classList.add("over")));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
  nc.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(nc); fd.append("csrf", window.CSRF);
    if (!fd.getAll("forms").length) { $("#status").textContent = "Pick at least one form."; return; }
    const btn = $("#create"); btn.disabled = true; $("#status").textContent = "Uploading…";
    try {
      const r = await fetch("/cases", { method: "POST", body: fd }); const j = await r.json();
      if (!r.ok) throw new Error(j.error);
      location.href = "/cases/" + j.id;
    } catch (err) { $("#status").textContent = err.message; btn.disabled = false; }
  });
}

// ---- job progress ----
const job = $("#job");
if (job && job.dataset.status === "running") {
  const tick = async () => {
    const j = await (await fetch(`/cases/${job.dataset.case}/job`)).json();
    if (j.status === "running") {
      if (j.total) { $("#job-n").textContent = `${j.done} of ${j.total} sections`; $("#job-bar").style.width = Math.max(3, 100 * j.done / j.total) + "%"; }
      setTimeout(tick, 1500);
    } else location.reload();
  };
  tick();
}

// ---- facts review ----
const caseId = location.pathname.match(/^\/cases\/(\d+)/)?.[1];
const LABEL = { approve: ["Approved", "ok"], reject: ["Removed by you", "off"], edit: ["Entered by you", "ok"] };
function markSaved(tr) { tr.classList.remove("just-saved"); void tr.offsetWidth; tr.classList.add("just-saved"); }
function setRow(tr, act) {
  const chip = $(".chip", tr); chip.className = "chip " + LABEL[act][1]; chip.textContent = LABEL[act][0];
  tr.dataset.status = act === "reject" ? "rejected" : act === "edit" ? "edited" : "approved";
  tr.classList.toggle("removed", act === "reject");
  const cell = $(".rowact", tr); cell.innerHTML = "";
  if (act === "reject") { const b = document.createElement("button"); b.className = "linkbtn small"; b.dataset.act = "approve"; b.textContent = "Restore"; cell.appendChild(b); }
  markSaved(tr); refreshGroup(tr.closest("[data-group]"));
}
function refreshGroup(g) {
  const left = $$("tr[data-status=review], tr[data-status=confirm]", g).length;
  const box = $(".group-actions", g);
  if (!box) return;
  if (!left) box.remove();
  else $$("button", box).forEach((b) => (b.textContent = (b.dataset.groupAction === "reject" ? "Remove all " : "Approve all ") + left));
}
async function act(keys, what) {
  await postJSON(`/cases/${caseId}/facts`, { [what]: keys });
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const tr = b.closest("tr");
  try { await act([tr.dataset.key], b.dataset.act); setRow(tr, b.dataset.act); } catch (err) { alert(err.message); }
});
$$("[data-group-action]").forEach((b) => b.addEventListener("click", async () => {
  const g = b.closest("[data-group]"), what = b.dataset.groupAction;
  const rows = $$("tr[data-status=review], tr[data-status=confirm]", g);
  if (what === "approve" && !confirm(`Approve all ${rows.length}? They'll go into the PDFs as they are.`)) return;
  try { await act(rows.map((r) => r.dataset.key), what); rows.forEach((r) => setRow(r, what)); } catch (err) { alert(err.message); }
}));
$$("table.facts tr[data-key]").forEach((tr) => {
  const el = $(".fval", tr);
  el.addEventListener("change", async () => {
    const v = el.type === "checkbox" ? el.checked : el.value;
    try {
      await postJSON(`/cases/${caseId}/facts`, { edits: { [tr.dataset.key]: v } });
      setRow(tr, v === "" || v === false ? "reject" : "edit");
      if (v !== "" && v !== false) $(".src", tr).innerHTML = '<span class="src-where src-plain">Typed by you</span>';
    } catch (err) { alert(err.message); }
  });
});

// ---- read documents again ----
const ex = $("#extract");
if (ex) ex.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(ex); fd.append("csrf", window.CSRF);
  $("#extract-status").textContent = "Starting…";
  const r = await fetch(`/cases/${ex.dataset.case}/extract`, { method: "POST", body: fd }); const j = await r.json();
  if (!r.ok) { $("#extract-status").textContent = j.error; return; }
  location.href = `/cases/${ex.dataset.case}?tab=facts`;
});

// ---- missing info ----
const mi = $("#missing");
if (mi) {
  let groups = {};
  $("#find-missing").addEventListener("click", async () => {
    const j = await (await fetch(`/cases/${mi.dataset.case}/missing`)).json(); groups = j.groups;
    const box = $("#missing-list"); box.innerHTML = "";
    if (!j.total) { box.innerHTML = "<p class='muted'>Nothing missing for the first driver, vehicle and location.</p>"; return; }
    Object.entries(groups).forEach(([g, items]) => {
      const d = document.createElement("div"); d.className = "mgroup";
      const h = document.createElement("h3"); h.textContent = `${g} (${items.length})`; d.appendChild(h);
      const ul = document.createElement("ul"); items.forEach((t) => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
      d.appendChild(ul); box.appendChild(d);
    });
    const b = document.createElement("button"); b.className = "btn"; b.textContent = "Draft email to the client";
    b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Drafting…";
      try { const r = await postJSON(`/cases/${mi.dataset.case}/missing/email`, { groups }); $("#email").value = r.email; $("#email-box").hidden = false; }
      catch (err) { alert(err.message); }
      b.disabled = false; b.textContent = "Draft email to the client";
    });
    box.appendChild(b);
  });
  $("#copy-email").addEventListener("click", async (e) => { await navigator.clipboard.writeText($("#email").value); e.target.textContent = "Copied"; setTimeout(() => (e.target.textContent = "Copy email"), 1500); });
}
