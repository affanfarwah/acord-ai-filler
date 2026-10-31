// Form editor: each page is drawn by PDF.js (vector, sharp at any size) with live fields on top.
// Edits go to the case's shared facts. If PDF.js can't load, pages fall back to server-rendered PNGs.
const D = JSON.parse(document.getElementById("data").textContent);
const CID = window.CASE_ID, USABLE = new Set(D.usable);
const facts = D.facts, inputs = {};
const pagesEl = document.getElementById("pages");
const placed = [];  // [element, rect] so layout() can rescale on resize
let S = D.scale, pdfDoc = null;

function fitScale() { return Math.min(D.scale, Math.max(0.3, (pagesEl.clientWidth - 2) / Math.max(...D.sizes.map((s) => s[0])))); }

// ---- page drawing ----
const pdfjs = window.pdfjsLib;
if (pdfjs) pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.js";
let drawToken = 0;
const tasks = new Map();  // canvas -> its running render; a canvas can only take one render at a time

async function drawPages() {
  if (!pdfDoc) return;
  const token = ++drawToken;  // a newer resize supersedes an older redraw
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  for (const pg of pagesEl.querySelectorAll(".pg")) {
    const canvas = pg.querySelector("canvas");
    if (!canvas) continue;
    const page = await pdfDoc.getPage(+pg.dataset.p);
    if (token !== drawToken) return;
    const old = tasks.get(canvas);
    if (old) { old.cancel(); await old.promise.catch(() => {}); }
    if (token !== drawToken) return;
    const vp = page.getViewport({ scale: S * dpr });
    canvas.width = Math.floor(vp.width); canvas.height = Math.floor(vp.height);
    // form-field appearances are skipped: our own inputs sit on top
    const task = page.render({ canvasContext: canvas.getContext("2d"), viewport: vp, annotationMode: pdfjs.AnnotationMode.DISABLE });
    tasks.set(canvas, task);
    try { await task.promise; } catch (e) { if (e?.name !== "RenderingCancelledException") throw e; return; }
  }
}

function usePng(pg) {
  pg.querySelector("canvas")?.remove();
  const img = document.createElement("img");
  img.src = `/cases/${CID}/forms/${window.FORM_KEY}/page/${pg.dataset.p}.png`; img.alt = `Page ${pg.dataset.p}`;
  pg.prepend(img);
}

D.sizes.forEach((sz, i) => {
  const pg = document.createElement("div");
  pg.className = "pg"; pg.dataset.p = i + 1;
  const canvas = document.createElement("canvas");
  canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", `Page ${i + 1}`);
  pg.appendChild(canvas); pagesEl.appendChild(pg);
});

function layout() {
  S = fitScale();
  pagesEl.querySelectorAll(".pg").forEach((pg) => { const sz = D.sizes[pg.dataset.p - 1]; pg.style.width = sz[0] * S + "px"; pg.style.height = sz[1] * S + "px"; });
  placed.forEach(([el, [x0, y0, x1, y1]]) => Object.assign(el.style, { left: x0 * S + "px", top: y0 * S + "px",
    width: (x1 - x0) * S + "px", height: (y1 - y0) * S + "px", fontSize: Math.max(7, Math.min(13, (y1 - y0) * S * 0.62)) + "px" }));
  drawPages().catch(() => {});
}

(async () => {
  try {
    if (!pdfjs) throw new Error("no pdf.js");
    pdfDoc = await pdfjs.getDocument({ url: `/cases/${CID}/forms/${window.FORM_KEY}/template.pdf` }).promise;
    await drawPages();
  } catch (e) {
    pagesEl.querySelectorAll(".pg").forEach(usePng);
  }
})();

// ---- fields ----
const kind = (f) => {
  const fact = facts[f.short];
  if (fact && !USABLE.has(fact.status)) return "hl";
  if (fact) return "ok";
  return f.agent_only ? "agent" : "";
};
function paint(f) { (inputs[f.short] || []).forEach((el) => { el.classList.remove("ok", "hl", "agent"); const k = kind(f); if (k) el.classList.add(k); }); }
function counts() {
  const shorts = new Set(D.fields.map((f) => f.short));
  let ok = 0, wait = 0;
  shorts.forEach((s) => { const x = facts[s]; if (x) USABLE.has(x.status) ? ok++ : wait++; });
  document.getElementById("counts").textContent = `${ok} of ${shorts.size} fields go into the PDF` + (wait ? ` · ${wait} waiting for review` : "");
}
const msg = (t) => { const m = document.getElementById("msg"); m.textContent = t; clearTimeout(msg.t); msg.t = setTimeout(() => (m.textContent = ""), 2500); };

const timers = {};
function queueSave(f, v) {
  clearTimeout(timers[f.short]);
  timers[f.short] = setTimeout(async () => {
    const r = await fetch(`/cases/${CID}/facts`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: { [f.short]: v }, csrf: window.CSRF }) });
    if (!r.ok) return msg("Couldn't save. Reload the page.");
    if (v === "" || v === false) delete facts[f.short]; else facts[f.short] = { value: v, status: "edited", source: "Entered by agent" };
    // the same fact can appear in several places on the form; keep them in sync
    (inputs[f.short] || []).forEach((el) => { if (el.type === "checkbox") { if (f.type !== "radio") el.checked = !!v; } else if (el.value !== v) el.value = v; });
    paint(f); counts(); renderPending(); msg("Saved");
  }, 450);
}

D.fields.forEach((f) => {
  const fact = facts[f.short];
  const val = fact ? fact.value : "";
  f.rects.forEach((r) => {
    const pg = pagesEl.querySelector(`.pg[data-p="${r.page}"]`);
    const [, y0, , y1] = r.rect;
    let el;
    if (f.type === "checkbox" || f.type === "radio") {
      el = document.createElement("input"); el.type = "checkbox";
      el.checked = f.type === "radio" ? String(val) === String(r.on) : !!val;
      el.addEventListener("change", () => {
        if (f.type === "radio") (inputs[f.short] || []).forEach((o) => { if (o !== el) o.checked = false; });
        queueSave(f, f.type === "radio" ? (el.checked ? r.on : "") : el.checked);
      });
    } else if (f.type === "choice") {
      el = document.createElement("select"); el.add(new Option("", ""));
      f.options.forEach((o) => el.add(new Option(o, o))); el.value = val || "";
      el.addEventListener("change", () => queueSave(f, el.value));
    } else {
      el = document.createElement(y1 - y0 > 22 ? "textarea" : "input");
      el.value = val || ""; if (f.maxlen) el.maxLength = f.maxlen;
      el.addEventListener("input", () => queueSave(f, el.value));
    }
    el.className = "f";
    el.dataset.short = f.short;
    el.setAttribute("aria-label", `${f.label}: ${f.help}`);
    el.title = f.help + (fact && fact.source ? `\nSource: ${fact.source}` : "") + (fact ? `\n${D.labels[fact.status] || ""}` : "");
    placed.push([el, r.rect]);
    (inputs[f.short] = inputs[f.short] || []).push(el);
    pg.appendChild(el);
  });
  paint(f);
});

function renderPending() {
  const ul = document.getElementById("pending"); ul.innerHTML = "";
  const seen = new Set();
  D.fields.forEach((f) => {
    const fact = facts[f.short];
    if (!fact || USABLE.has(fact.status) || seen.has(f.short)) return;
    seen.add(f.short);
    const li = document.createElement("li");
    const jump = document.createElement("button"); jump.className = "jump"; jump.textContent = f.label;
    jump.addEventListener("click", () => { const el = inputs[f.short][0]; el.scrollIntoView({ block: "center", behavior: "smooth" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1500); el.focus({ preventScroll: true }); });
    const ok = document.createElement("button"); ok.className = "btn ghost small"; ok.textContent = "Approve";
    ok.addEventListener("click", async () => {
      const r = await fetch(`/cases/${CID}/facts`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve: [f.short], csrf: window.CSRF }) });
      if (!r.ok) return msg("Couldn't approve. Reload the page.");
      fact.status = "approved"; paint(f); counts(); renderPending(); msg("Approved");
    });
    li.append(jump, ok); ul.appendChild(li);
  });
  if (!seen.size) ul.innerHTML = "<li class='muted'>Nothing waiting on this form.</li>";
}

layout();
let rz;
window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(layout, 150); });
counts(); renderPending();

document.getElementById("verify").addEventListener("click", async () => {
  const j = await (await fetch(`/cases/${CID}/forms/${window.FORM_KEY}/verify`)).json();
  msg(j.mismatches.length ? `${j.ok} of ${j.checked} values landed. Not landed: ${j.mismatches.map((m) => m.field.split(".").pop()).slice(0, 4).join(", ")}`
                          : `All ${j.checked} values are in the PDF.`);
});
