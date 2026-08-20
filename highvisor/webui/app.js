"use strict";
// highvisor web cockpit — talks to the daemon over POST /rpc and streams the
// event log over GET /events (SSE). No framework, no build step.

const $ = (id) => document.getElementById(id);
let selected = null;   // currently selected window target id

async function rpc(op, extra = {}) {
  const r = await fetch("/rpc", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op, ...extra }),
  });
  return r.json();
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---------------------------------------------------------------- windows
async function refreshWindows() {
  const res = await rpc("list_targets");
  const ul = $("windows");
  ul.innerHTML = "";
  if (!res.ok) { ul.innerHTML = `<li class="bad">${res.error || "error"}</li>`; updateUserTestStatus([]); return; }
  for (const t of res.targets) {
    const li = document.createElement("li");
    if (t.focused) li.classList.add("foc");
    if (t.id === selected) li.classList.add("sel");
    li.innerHTML = `${escapeHtml(t.title || t.class_name || t.id)}`
      + `<div class="sub">${t.id} · ${t.w}×${t.h}</div>`;
    li.onclick = () => shoot(t.id, t.title || t.id);
    ul.appendChild(li);
  }
  updateUserTestStatus(res.targets);
}

async function shoot(target, label) {
  selected = target;
  document.querySelectorAll("#windows li").forEach(li =>
    li.classList.toggle("sel", li.textContent.includes(target)));
  $("shotlabel").textContent = label || target;
  const wrap = $("shotwrap");
  wrap.innerHTML = `<span class="muted">capturing ${escapeHtml(target)}…</span>`;
  const res = await rpc("screenshot", { target });
  if (!res.ok) { wrap.innerHTML = `<span class="bad">${res.error}</span>`; return; }
  wrap.innerHTML = "";
  const img = new Image();
  img.src = "data:image/png;base64," + res.png_b64;
  wrap.appendChild(img);
}

// ------------------------------------------------ congruence (1:1 diff tool)
// Overlay a Raves capture and a Qud capture: cross-fade between them, and toggle
// a "similarity map" that hides matching pixels and paints the mismatches the
// midway colour of the two — so 1:1 drift jumps out.
let _congImgs = null;   // {qud: Image, raves: Image} of the last capture

function switchPreviewTab(tab) {
  document.querySelectorAll(".panel-hd .tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("shotwrap").hidden = tab !== "preview";
  $("congruence").hidden = tab !== "congruence";
  $("shotlabel").style.visibility = tab === "preview" ? "" : "hidden";
}

function _shotImage(target) {
  return rpc("screenshot", { target }).then(res => {
    if (!res.ok) throw new Error(res.error || "capture failed");
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("decode failed"));
      img.src = "data:image/png;base64," + res.png_b64;
    });
  });
}

async function captureCongruence() {
  const btn = $("cong-capture"), status = $("cong-status");
  const wins = await rpc("list_targets");
  if (!wins.ok) { status.className = "bad"; status.textContent = "list_targets failed"; return; }
  const { raves, qud } = classifyRavesQud(wins.targets);
  if (raves.length !== 1 || qud.length !== 1) {
    status.className = "bad";
    status.textContent = _dupMessage(raves, qud).split("\n")[0];
    return;
  }
  btn.disabled = true; status.className = "muted"; status.textContent = "capturing…";
  try {
    const [qImg, rImg] = await Promise.all([_shotImage(qud[0].id), _shotImage(raves[0].id)]);
    _congImgs = { qud: qImg, raves: rImg };
    _renderCongruence();
  } catch (e) {
    status.className = "bad"; status.textContent = "capture failed: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function _renderCongruence() {
  if (!_congImgs) return;
  const { qud, raves } = _congImgs;
  const w = qud.naturalWidth, h = qud.naturalHeight;   // compare on Qud's pixel grid
  const qc = $("cong-qud"), rc = $("cong-raves"), sc = $("cong-sim");
  for (const c of [qc, rc, sc]) { c.width = w; c.height = h; }
  qc.getContext("2d").drawImage(qud, 0, 0, w, h);
  rc.getContext("2d").drawImage(raves, 0, 0, w, h);    // scaled to match if sizes differ
  const prevIW = _congIW, prevIH = _congIH;
  _congIW = w; _congIH = h;
  $("cong-frame").hidden = false;
  document.querySelector(".cong-empty").hidden = true;
  _applyCrossfade();
  _applySourcesToggle();
  _buildSimilarity();
  _applySimToggle();
  // First capture fits to the stage; later re-captures KEEP the current zoom/pan (and marker)
  // so you can hit "Capture" again without losing your place. Other settings (fader, toggles,
  // threshold) already persist — they read live from the DOM inputs.
  if (_congHadView) {
    _congComputeFit();                                 // recompute the fit floor
    if (_congZoom < _congFit) _congZoom = _congFit;    // respect it
    if (w !== prevIW || h !== prevIH) _congClamp();    // only re-clamp if the compared grid resized
    _congApplyTransform();                             // else keep the exact pan
  } else {
    _congFitView();
    _congHadView = true;
  }
}

// ------ zoom / pan / pick-position for the congruence viewer ------
let _congIW = 1920, _congIH = 1080;   // source image dims (Qud grid)
let _congZoom = 1, _congFit = 1, _congOX = 0, _congOY = 0;
let _congMark = null;                 // {px, py} last right-clicked image point
let _congPan = null;                  // drag anchor while panning
let _congHadView = false;             // has the viewer been fit once? (re-captures keep zoom/pan)

function _congStageSize() {
  const s = $("cong-stage");
  return { w: s.clientWidth, h: s.clientHeight };
}

function _congComputeFit() {
  const { w, h } = _congStageSize();
  _congFit = Math.min(w / _congIW, h / _congIH) || 1;
}

function _congApplyTransform() {
  $("cong-frame").style.transform =
    `translate(${_congOX}px, ${_congOY}px) scale(${_congZoom})`;
  const zl = $("cong-zoom");
  zl.hidden = !_congImgs;
  zl.textContent = Math.round(100 * _congZoom / _congFit) + "%";
  _congUpdateMarker();
}

function _congClamp() {
  const { w: SW, h: SH } = _congStageSize();
  const iw = _congIW * _congZoom, ih = _congIH * _congZoom;
  _congOX = iw <= SW ? (SW - iw) / 2 : Math.min(0, Math.max(SW - iw, _congOX));
  _congOY = ih <= SH ? (SH - ih) / 2 : Math.min(0, Math.max(SH - ih, _congOY));
}

function _congFitView() {
  if (!_congImgs) return;
  _congComputeFit();
  _congZoom = _congFit;
  _congClamp();                        // centers, since fit <= stage
  _congApplyTransform();
}

function _congWheel(e) {
  if (!_congImgs) return;
  e.preventDefault();
  const rect = $("cong-stage").getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  const z2 = Math.max(_congFit, Math.min(_congFit * 40, _congZoom * factor));
  _congOX = mx - (mx - _congOX) * (z2 / _congZoom);    // zoom toward the cursor
  _congOY = my - (my - _congOY) * (z2 / _congZoom);
  _congZoom = z2;
  _congClamp();
  _congApplyTransform();
}

function _congPanStart(e) {
  if (e.button !== 0 || !_congImgs) return;
  _congPan = { x: e.clientX, y: e.clientY };
  $("cong-stage").classList.add("panning");
}
function _congPanMove(e) {
  if (!_congPan) return;
  _congOX += e.clientX - _congPan.x;
  _congOY += e.clientY - _congPan.y;
  _congPan = { x: e.clientX, y: e.clientY };
  _congClamp();
  _congApplyTransform();
}
function _congPanEnd() {
  if (!_congPan) return;
  _congPan = null;
  $("cong-stage").classList.remove("panning");
}

function _congImagePos(clientX, clientY) {
  const rect = $("cong-stage").getBoundingClientRect();
  const ix = (clientX - rect.left - _congOX) / _congZoom;
  const iy = (clientY - rect.top - _congOY) / _congZoom;
  return { px: Math.round(ix), py: Math.round(iy), u: ix / _congIW, v: iy / _congIH,
           inBounds: ix >= 0 && ix < _congIW && iy >= 0 && iy < _congIH };
}

function _congUpdateMarker() {
  const m = $("cong-marker");
  if (!_congMark || !_congImgs) { m.hidden = true; return; }
  m.hidden = false;
  m.style.left = (_congOX + _congMark.px * _congZoom) + "px";
  m.style.top = (_congOY + _congMark.py * _congZoom) + "px";
}

function _congContextMenu(e) {
  if (!_congImgs) return;
  e.preventDefault();
  const p = _congImagePos(e.clientX, e.clientY);
  if (!p.inBounds) { _congHideMenu(); return; }
  _congMark = { px: p.px, py: p.py };
  _congUpdateMarker();
  const menu = $("cong-menu");
  Object.assign(menu.dataset, { px: p.px, py: p.py, u: p.u.toFixed(4), v: p.v.toFixed(4) });
  $("cong-menu-pos").textContent = `x ${p.px} · y ${p.py}  (${p.u.toFixed(3)}, ${p.v.toFixed(3)})`;
  menu.style.left = Math.min(e.clientX, innerWidth - 180) + "px";
  menu.style.top = Math.min(e.clientY, innerHeight - 90) + "px";
  menu.hidden = false;
  $("cong-status").className = "muted";
  $("cong-status").textContent = `picked x=${p.px} y=${p.py} (${p.u.toFixed(3)}, ${p.v.toFixed(3)})`;
}

function _congHideMenu() { $("cong-menu").hidden = true; }

function _congCopy(kind) {
  const d = $("cong-menu").dataset;
  const txt = kind === "uv" ? `${d.u},${d.v}` : `${d.px},${d.py}`;
  _congHideMenu();
  if (navigator.clipboard) navigator.clipboard.writeText(txt).catch(() => {});
}

function _applyCrossfade() {
  // Qud is the base layer (full opacity); Raves fades in on top. 0 = Qud, 1 = Raves.
  $("cong-raves").style.opacity = String((+$("cong-fade").value) / 100);
}

function _applySourcesToggle() {
  // Hide the Qud/Raves capture (via visibility, so #cong-qud still sizes the frame)
  // to view the similarity map alone on the stage. Pixel data is untouched.
  const v = $("cong-sources-toggle").checked ? "" : "hidden";
  $("cong-qud").style.visibility = v;
  $("cong-raves").style.visibility = v;
}

function _buildSimilarity() {
  if (!_congImgs) return;
  const qc = $("cong-qud"), rc = $("cong-raves"), sc = $("cong-sim");
  const w = qc.width, h = qc.height;
  const qd = qc.getContext("2d").getImageData(0, 0, w, h).data;
  const rd = rc.getContext("2d").getImageData(0, 0, w, h).data;
  const ctx = sc.getContext("2d");
  const out = ctx.createImageData(w, h);   // starts fully transparent (alpha 0)
  const o = out.data;
  const tol = +$("cong-thresh").value;     // per-channel tolerance (drift-forgiving)
  let diff = 0;
  for (let i = 0; i < qd.length; i += 4) {
    const d = Math.max(Math.abs(qd[i] - rd[i]),
                       Math.abs(qd[i + 1] - rd[i + 1]),
                       Math.abs(qd[i + 2] - rd[i + 2]));
    if (d > tol) {                          // different -> opaque midway colour
      o[i]     = (qd[i]     + rd[i])     >> 1;
      o[i + 1] = (qd[i + 1] + rd[i + 1]) >> 1;
      o[i + 2] = (qd[i + 2] + rd[i + 2]) >> 1;
      o[i + 3] = 255;
      diff++;
    }                                       // same -> left transparent (alpha 0)
  }
  ctx.putImageData(out, 0, 0);
  const pct = (100 * diff / (w * h)).toFixed(1);
  $("cong-status").className = "muted";
  $("cong-status").textContent = `${pct}% of pixels differ (tolerance ${tol})`;
}

function _applySimToggle() {
  const on = $("cong-sim-toggle").checked;
  $("cong-sim").hidden = !on || !_congImgs;
  document.querySelector(".cong-thresh").hidden = !on;
}

// ------------------------------------------------------------------ peers
async function refreshPeers() {
  let res;
  try { res = await (await fetch("/bridge/peers")).json(); } catch { return; }
  if (res.self) $("self").textContent = res.self.name + "@" + res.self.host;
  const ul = $("peers");
  if (!res.peers || !res.peers.length) {
    ul.innerHTML = `<li class="muted">${res.self ? "no peers yet" : "bridge offline"}</li>`;
    return;
  }
  ul.innerHTML = "";
  for (const p of res.peers) {
    const li = document.createElement("li");
    li.innerHTML = `${escapeHtml(p.name)}<div class="sub">${p.host}:${p.port}</div>`;
    ul.appendChild(li);
  }
}

// -------------------------------------------------------------- handoff
async function sendContext() {
  const ta = $("ctx");
  const text = ta.value.trim();
  if (!text) return;
  $("send").disabled = true;
  const res = await post("/bridge/send", { text });
  $("send").disabled = false;
  if (res && res.ok) { ta.value = ""; }
  else { addRow({ kind: "error", t: Date.now() / 1000, msg: (res && res.error) || "bridge offline — is it running?" }); }
}

// ---------------------------------------------------------------- layouts
let layoutCache = {};
async function refreshLayouts() {
  const res = await rpc("layout_list");
  const sel = $("layoutsel");
  sel.innerHTML = "";
  layoutCache = {};
  if (!res.ok) return;
  for (const l of res.layouts) {
    layoutCache[l.name] = l;
    const o = document.createElement("option");
    o.value = l.name;
    o.textContent = `${l.name} (${l.placements})`;
    sel.appendChild(o);
  }
  showLayoutDesc();
}
function showLayoutDesc() {
  const l = layoutCache[$("layoutsel").value];
  $("layoutdesc").textContent = l ? l.description : "";
}
async function applyLayout() {
  const name = $("layoutsel").value;
  if (!name) return;
  $("applylayout").disabled = true;
  await rpc("layout_apply", { name });   // result streams into the log via the bus
  $("applylayout").disabled = false;
  refreshWindows();
}
async function saveLayout() {
  const name = $("savename").value.trim();
  if (!name) return;
  const res = await rpc("layout_save", { name });
  if (res.ok) { $("savename").value = ""; await refreshLayouts(); $("layoutsel").value = name; showLayoutDesc(); }
}

// Identify the Raves / Caves-of-Qud game windows by their OWNING APP (owner name
// + bundle path), NOT the window title. Title-matching mis-fires on a Finder
// window showing the "raves-of-qud" folder, a Terminal cd'd there, an editor with
// the project open, etc. — all of which carry "raves" in the title but are not
// the game. Owner/path pins it to the actual app.
function _isRaves(t) {
  const owner = (t.class_name || "").toLowerCase();
  const path = (t.path || "").toLowerCase();
  const title = (t.title || "").toLowerCase();
  // exported build: the owning app itself is RavesOfQud (by process name or bundle).
  if (/raves.?of.?qud|ravesofqud/.test(owner) || /raves.?of.?qud|ravesofqud/.test(path)) return true;
  // dev-run: a Godot process whose game window names Raves — but not the Godot
  // editor ("<project> - Godot Engine").
  if (owner === "godot" && /raves/.test(title) && !/godot engine/.test(title)) return true;
  // Windows dev-run/export: Godot windows carry the Win32 class "Engine"; the
  // editor is excluded by its "<project> - Godot Engine" title.
  if (owner === "engine" && /raves/.test(title) && !/godot engine/.test(title)) return true;
  return false;
}
function _isQud(t) {
  const owner = (t.class_name || "").toLowerCase();
  const path = (t.path || "").toLowerCase();
  return /caves ?of ?qud|cavesofqud/.test(owner) || /caves of qud|coq\.app/.test(path);
}
// Split the open windows into the Raves side and the Qud side (arrays, so callers
// can spot duplicates).
function classifyRavesQud(targets) {
  const list = targets || [];
  // The daemon classifies (highvisor.apps.classify_target — ONE implementation,
  // both OSes) and stamps `role` on every list_targets row. Trust it when
  // present; the local regex rules remain only as the older-daemon fallback.
  if (list.some(t => "role" in t)) {
    return { raves: list.filter(t => t.role === "raves"),
             qud: list.filter(t => t.role === "qud") };
  }
  return { raves: list.filter(_isRaves), qud: list.filter(_isQud) };
}

function _label(t) { return t.title || t.class_name || t.id; }

// Duplicate checker: reflect whether exactly one Raves and one Qud are open,
// and flag duplicates (which would make the arrange button ambiguous).
function updateUserTestStatus(targets) {
  const el = $("uttest-status");
  if (!el) return;
  const { raves, qud } = classifyRavesQud(targets);
  const dup = raves.length > 1 || qud.length > 1;
  const missing = raves.length === 0 || qud.length === 0;
  if (dup) {
    const parts = [];
    if (raves.length > 1) parts.push(`${raves.length}× Raves`);
    if (qud.length > 1) parts.push(`${qud.length}× Qud`);
    el.className = "uttest-status warn";
    el.textContent = `⚠ duplicate windows (${parts.join(", ")}) — close extras`;
  } else if (missing) {
    el.className = "uttest-status muted";
    el.textContent = `need both: Raves ${raves.length ? "✓" : "✗"} · Qud ${qud.length ? "✓" : "✗"}`;
  } else {
    el.className = "uttest-status okline";
    el.textContent = "Raves ×1 · Qud ×1 ✓";
  }
}
async function refreshUserTestStatus() {
  const r = await rpc("list_targets");
  if (r.ok) updateUserTestStatus(r.targets);
}

function _dupMessage(raves, qud) {
  const lines = [];
  if (raves.length === 0) lines.push("• No Raves window found.");
  else if (raves.length > 1)
    lines.push(`• ${raves.length} Raves windows:\n    - ` + raves.map(_label).join("\n    - "));
  if (qud.length === 0) lines.push("• No Caves of Qud window found.");
  else if (qud.length > 1)
    lines.push(`• ${qud.length} Caves of Qud windows:\n    - ` + qud.map(_label).join("\n    - "));
  return "Can't arrange — need exactly one Raves and one Caves of Qud window.\n\n"
    + lines.join("\n") + "\n\nClose the extra window(s) and try again.";
}

// Toggle Raves between user mode and 1:1 (parity) mode by injecting its Ctrl+M hotkey into the
// Raves window. Raves-only (Qud not required); refuses on zero/duplicate Raves windows.
async function toggleRaves1to1() {
  const wins = await rpc("list_targets");
  if (!wins.ok) { alert("list_targets failed: " + (wins.error || "?")); return; }
  const { raves } = classifyRavesQud(wins.targets);
  if (raves.length === 0) { alert("No Raves window found."); return; }
  if (raves.length > 1) {
    alert(`${raves.length} Raves windows open:\n  - ` + raves.map(_label).join("\n  - ")
      + "\n\nClose the extras so the toggle isn't ambiguous.");
    return;
  }
  const btn = $("raves-1to1");
  btn.disabled = true;
  try {
    await rpc("key", { target: raves[0].id, keys: "ctrl+m" });   // Raves' user⇄1:1 hotkey
  } catch (e) {
    alert("toggle failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// Toggle Qud godmode: runs the "godmode" wish through the Raves mod bridge (the daemon's
// qudwish op speaks Qud's 48710 socket directly — no window focus or key injection needed).
// Godmode is a toggle in Qud with no cheap state readback, so the button is stateless; the
// wish result ("Godmode now True/False") lands in the game's own message log.
async function qudGodmode() {
  const btn = $("qud-godmode");
  btn.disabled = true;
  try {
    const r = await rpc("qudwish", { wish: "godmode" });
    if (!r.ok) alert("godmode failed: " + (r.error || "?"));
  } catch (e) {
    alert("godmode failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// First-party Escape for Qud's modern menus (the mod's uiback bridge command) —
// closes screens that ignore synthesized keys, including a STUCK status screen
// whose nav context died (seen after a mutation-buy popup).
async function qudBack() {
  const btn = $("qud-back");
  btn.disabled = true;
  try {
    const r = await rpc("qudback", {});
    if (!r.ok) alert("uiback failed: " + (r.error || "?"));
  } catch (e) {
    alert("uiback failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// Grant XP through the 'xp:<n>' wish (same bridge path as godmode). Levels the
// player up so point-spend workflows (mutations/attributes) are testable on a
// fresh save. The wish result lands in the game's own message log.
async function qudXp() {
  const btn = $("qud-xp");
  const amt = parseInt($("xp-amount").value || "0", 10);
  if (!(amt > 0)) { alert("enter an XP amount"); return; }
  btn.disabled = true;
  try {
    const r = await rpc("qudwish", { wish: "xp:" + amt });
    if (!r.ok) alert("xp wish failed: " + (r.error || "?"));
  } catch (e) {
    alert("xp wish failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// Cybernetics test fixture. TWO buttons because credits and licence tiers are two
// different kinds of thing in Qud, and conflating them is the trap: the terminal
// recounts CREDITS from inventory every time it opens (Part.Credits * GO.Count over
// CyberneticsCreditWedge), so credits can only come from items — hence a chest — while
// the licence TIER is a plain int property on the player (`CyberneticsLicenses`), which
// Qud's own upgrade flow bumps with ModIntProperty(...,1) after destroying the wedges it
// charged. So one button spawns items and the other sets a number, and neither can do
// the other's job.
//
// Both go through the mod's turn-thread path, which a parked screen blocks — CLOSE the
// terminal first. The mod refuses them out loud rather than firing late (that guard is
// why this reports the refusal instead of looking like it worked).
async function qudCyberChest() {
  const btn = $("qud-cyberchest");
  const wedges = parseInt($("cyber-wedges").value || "0", 10);
  if (!(wedges > 0)) { alert("enter a wedge count"); return; }
  btn.disabled = true;
  try {
    const r = await rpc("qudbridge", { name: "cyberchest", args: { wedges: String(wedges) } });
    if (!r.ok) alert("cyberchest failed: " + (r.error || "?"));
  } catch (e) {
    alert("cyberchest failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// A CHEST OF EVERY WEAPON AND EVERY AMMO, carryable. `zoo weapons` scatters one of each
// across the zone, which is right for looking at art and useless for equipping — you cannot
// pick a zone up. This packs them into one Chest at your feet with enough spheres of negative
// weight to cancel the load; the mod computes that count off the Suspensor part rather than
// guessing. Same turn-thread path as the cybernetics chest, so the same rule: no parked screen.
async function qudLoadout() {
  const btn = $("qud-loadout");
  btn.disabled = true;
  try {
    const r = await rpc("qudbridge", { name: "loadout", args: {} });
    if (!r.ok) alert("loadout failed: " + (r.error || "?"));
  } catch (e) {
    alert("loadout failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function qudCyberLicense() {
  const btn = $("qud-cyberlicense");
  const n = parseInt($("cyber-licenses").value || "0", 10);
  if (!(n > 0)) { alert("enter a number of licence tiers"); return; }
  btn.disabled = true;
  try {
    const r = await rpc("qudbridge", { name: "cyberlicense", args: { n: String(n) } });
    if (!r.ok) alert("cyberlicense failed: " + (r.error || "?"));
  } catch (e) {
    alert("cyberlicense failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// Start the latest Raves build and arrange it. Launches via the `raves` launcher
// (which itself spawns Caves of Qud borderless — see launch.json / QudLauncher),
// waits for BOTH windows to appear (Qud's takes ~20s), then tiles at 1920×1080.
// Idempotent-ish: if a Raves is already open it skips the launch and just arranges,
// so a second click won't spawn a duplicate.
async function startRavesLatest() {
  const btn = $("start-raves");
  const status = $("uttest-status");
  const setStatus = (cls, txt) => {
    if (status) { status.className = "uttest-status " + cls; status.textContent = txt; }
  };
  btn.disabled = true;
  try {
    const first = await rpc("list_targets");
    const cur = classifyRavesQud(first.ok ? first.targets : []);
    if (cur.raves.length > 1) { alert(_dupMessage(cur.raves, cur.qud)); return; }
    if (cur.raves.length === 0) {
      setStatus("muted", "launching latest Raves… (it starts Caves of Qud)");
      const r = await rpc("launch", { name: "raves" });
      if (!r.ok) { alert("launch failed: " + (r.error || "?")); return; }
    }
    // Wait for exactly one Raves and one Qud window (Qud boots ~20s after spawn).
    const deadline = Date.now() + 90000;
    for (;;) {
      const t = await rpc("list_targets");
      const c = classifyRavesQud(t.ok ? t.targets : []);
      await refreshWindows();
      if (c.raves.length > 1 || c.qud.length > 1) { alert(_dupMessage(c.raves, c.qud)); return; }
      if (c.raves.length === 1 && c.qud.length === 1) break;
      if (Date.now() > deadline) {
        setStatus("warn", "timed out waiting for Raves + Qud to open");
        return;
      }
      const need = [c.raves.length ? null : "Raves", c.qud.length ? null : "Qud"].filter(Boolean);
      setStatus("muted", "waiting for " + need.join(" + ") + "…");
      await new Promise(res => setTimeout(res, 1500));
    }
    // Machine override first (a user layout named "pair"), else the built-in slots.
    if (!(await applyPairLayoutOverride())) await userTestLayout(1920, 1080);
  } catch (e) {
    alert("start Raves failed: " + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// Machine pair-stage override: if the user's layouts.json defines a layout named
// "pair", it IS this machine's pair stage — apply it instead of the built-in
// Mac-style slot math (standardSlots / userTestLayout). Lets a box with a different
// monitor topology (e.g. Lumpy's two 4Ks: Raves on primary, Qud on secondary)
// restage the pair without forking the cockpit. No "pair" layout -> false, callers
// fall back to the built-ins; a solo window still lands in its pair position because
// layout placements simply MISS absent windows.
async function applyPairLayoutOverride() {
  try {
    const r = await rpc("layout_apply", { name: "pair" });
    return !!(r && r.ok && r.applied > 0);
  } catch (e) {
    return false;
  }
}

// The machine pair stage for W×H windows — non-null only when this machine
// defines a "pair" layout. Stacks the column on the MAIN display when two rows
// fit (Raves above Qud, centered, small margins — the portrait-monitor stage);
// else one app per display; null → callers use the built-in Mac slot math.
async function machinePairStage(W, H) {
  try {
    const l = await rpc("layouts");
    const names = (l.ok && l.layouts ? l.layouts : []).map(x => x.name);
    if (!names.includes("pair")) return null;
    const d = await rpc("displays");
    if (!d.ok || !d.displays || !d.displays.length) return null;
    const main = d.displays.find(m => m.main) || d.displays[0];
    const GAP = 8, TOP = 40;
    if (main.h >= TOP + 2 * H + GAP && main.w >= W) {
      const x = main.x + Math.floor((main.w - W) / 2);
      return { ravesRect: { x, y: main.y + TOP, w: W, h: H },
               qudRect:   { x, y: main.y + TOP + H + GAP, w: W, h: H } };
    }
    const other = d.displays.find(m => m !== main) || main;
    return { ravesRect: { x: main.x, y: main.y, w: W, h: H },
             qudRect:   { x: other.x, y: other.y, w: W, h: H } };
  } catch (e) {
    return null;
  }
}

// The standard pair slots at W×H on the roomiest display (shared by the pair launch, the
// resolution buttons, and the solo launches — a solo window lands EXACTLY where the pair
// layout would put it, so adding the second app later never moves the first).
async function standardSlots(W, H) {
  const disps = await rpc("displays");
  const displays = (disps.ok && disps.displays && disps.displays.length)
    ? disps.displays : [{ x: 0, y: 0, w: 2 * W, h: 2 * H }];
  const byArea = displays.slice().sort((a, b) => b.w * b.h - a.w * a.h);
  const d = byArea.find(x => x.w >= W && x.h >= 2 * H) || byArea[0];
  const MARGIN_X = 50, MARGIN_Y = 4;   // overscan nudge — the monitor clips edge pixels
  const x = d.x + Math.max(0, d.w - W) - MARGIN_X;
  return { ravesRect: { x, y: d.y - MARGIN_Y, w: W, h: H },
           qudRect:   { x, y: d.y + Math.floor(d.h / 2) - MARGIN_Y, w: W, h: H } };
}

// Launch ONE app of the pair and place it in its standard slot. Qud solo uses the
// `qud_solo` launcher (the CoQ binary with the same borderless args Raves would pass);
// Raves solo launches the .app with NO args, so it does NOT spawn Qud.
async function startSolo(which) {
  const btn = $(which === "qud" ? "start-qud-solo" : "start-raves-solo");
  const status = $("uttest-status");
  const setStatus = (cls, txt) => {
    if (status) { status.className = "uttest-status " + cls; status.textContent = txt; }
  };
  btn.disabled = true;
  try {
    const first = await rpc("list_targets");
    const cur = classifyRavesQud(first.ok ? first.targets : []);
    const have = which === "qud" ? cur.qud : cur.raves;
    if (have.length > 1) { alert(_dupMessage(cur.raves, cur.qud)); return; }
    if (have.length === 0) {
      setStatus("muted", `launching ${which} (solo)…`);
      let launchName = which === "qud" ? "qud_solo" : "raves_solo";
      let r = await rpc("launch", { name: launchName });
      if (!r.ok && which === "qud") r = await rpc("launch", { name: "qud" });  // fallback: steam launcher
      if (!r.ok) { alert("launch failed: " + (r.error || "?")); return; }
    }
    const deadline = Date.now() + 90000;
    for (;;) {
      const t = await rpc("list_targets");
      const c = classifyRavesQud(t.ok ? t.targets : []);
      await refreshWindows();
      const now = which === "qud" ? c.qud : c.raves;
      if (now.length > 1) { alert(_dupMessage(c.raves, c.qud)); return; }
      if (now.length === 1) {
        // Machine "pair" layout override places the solo window in its pair slot too
        // (placements just MISS the absent app); else the built-in standard slot.
        if (!(await applyPairLayoutOverride())) {
          const slots = await standardSlots(1920, 1080);
          const rect = which === "qud" ? slots.qudRect : slots.ravesRect;
          await rpc("move", { target: now[0].id, ...rect, topmost: false });
        }
        setStatus("ok", `${which} up · standard slot`);
        return;
      }
      if (Date.now() > deadline) { setStatus("warn", `timed out waiting for ${which}`); return; }
      setStatus("muted", `waiting for ${which}…`);
      await new Promise(res => setTimeout(res, 1500));
    }
  } catch (e) {
    alert(`start ${which} failed: ` + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

// One-click user-testing setup at a chosen resolution: Raves in the UPPER-RIGHT
// quadrant, Caves of Qud in the LOWER-RIGHT, W×H each (right-edge aligned in the
// right half — the same placement the responsive parity test uses). Refuses to
// run on duplicates.
async function userTestLayout(W, H) {
  const wins = await rpc("list_targets");
  if (!wins.ok) { alert("list_targets failed: " + (wins.error || "?")); return; }
  const { raves, qud } = classifyRavesQud(wins.targets);
  if (raves.length !== 1 || qud.length !== 1) {
    updateUserTestStatus(wins.targets);
    alert(_dupMessage(raves, qud));
    return;
  }
  const btns = document.querySelectorAll(".ut");
  btns.forEach(b => (b.disabled = true));
  try {
    // Machine pair-stage (a "pair" layout): the W×H column on the main display
    // (or one app per monitor when the column doesn't fit) instead of Mac slots.
    const pm = await machinePairStage(W, H);
    if (pm) {
      await rpc("move", { target: raves[0].id, ...pm.ravesRect, topmost: false });
      await rpc("move", { target: qud[0].id, ...pm.qudRect, topmost: false });
    } else {
      // Same slot math as the solo launches (standardSlots) — one source for the placement.
      const slots = await standardSlots(W, H);
      await rpc("move", { target: raves[0].id, ...slots.ravesRect, topmost: false });
      await rpc("move", { target: qud[0].id, ...slots.qudRect, topmost: false });
    }
    await refreshWindows();
  } catch (e) {
    alert("user-test layout failed: " + (e.message || e));
  } finally {
    btns.forEach(b => (b.disabled = false));
  }
}

// ------------------------------------------------------ pending (agent loop)
async function refreshPending() {
  let res;
  try { res = await (await fetch("/orch/pending")).json(); } catch { return; }
  $("lanes").textContent = (res.lanes && res.lanes.length) ? "auto: " + res.lanes.join(" · ") : "";
  const el = $("pending");
  if (!res.pending || !res.pending.length) { el.innerHTML = `<div class="muted">nothing pending</div>`; return; }
  el.innerHTML = "";
  for (const p of res.pending) {
    const d = document.createElement("div");
    d.className = "pend";
    const tgt = escapeHtml((p.target || "").split("/").pop() || "target");
    d.innerHTML =
      `<div class="pend-hd"><b>${escapeHtml(p.verb)}</b> → ${escapeHtml(p.target)} `
      + `<span class="muted">from ${escapeHtml(p.src || "")}</span></div>`
      + `<div class="pend-body">${escapeHtml(p.body || "(no body)")}</div>`
      + optsHtml(p)
      // approve/deny act on the ASK itself — they forward it to its TARGET (or drop it).
      // Labeled with the destination so it's not confused with the option buttons (which answer the ASKER).
      + `<div class="pend-btns">`
      + `<button class="ok" data-fp="${p.fp}" data-a="approve" title="deliver this ask to ${tgt}">send to ${tgt}</button>`
      + `<button data-fp="${p.fp}" data-a="approve-all" title="auto-deliver this lane to ${tgt} from now on">auto-send to ${tgt}</button>`
      + `<button class="no" data-fp="${p.fp}" data-a="deny" title="drop this ask without sending it anywhere">dismiss</button></div>`;
    el.appendChild(d);
  }
  el.querySelectorAll(".pend-btns button").forEach(b =>
    b.onclick = () => actOpcode(b.dataset.fp, b.dataset.a));
  el.querySelectorAll(".pend-opt").forEach(b =>
    b.onclick = () => pickOption(b));
}
async function actOpcode(fp, action) {
  await post("/orch/act", { fp, action });
  refreshPending();
}

// Turn an ask body into clickable choice buttons: lines like "(a) label" become a button,
// grouped under a preceding "Qn." header. This is the "dynamic buttons from an ask" feature —
// author options as `(x) …` lines and the cockpit renders them.
function parseAsk(body) {
  const groups = [];
  let cur = null;
  for (const line of (body || "").split("\n")) {
    const qm = line.match(/^\s*(Q\d+)\b/);
    if (qm) { cur = { qn: qm[1], opts: [] }; groups.push(cur); continue; }
    const om = line.match(/^\s*\(([A-Za-z0-9])\)\s*(.+)/);
    if (om) {
      if (!cur) { cur = { qn: "", opts: [] }; groups.push(cur); }
      cur.opts.push({ letter: om[1], label: om[2].trim() });
    }
  }
  return groups.filter(g => g.opts.length);
}
function optsHtml(p) {
  const groups = parseAsk(p.body);
  if (!groups.length) return "";
  const src = escapeHtml((p.src || "asker").split("/").pop() || "asker");
  let h = `<div class="pend-opts"><div class="pend-answerhint">↳ click a choice to answer → ${src} (not the send button)</div>`;
  for (const g of groups) {
    h += `<div class="pend-q">` + (g.qn ? `<div class="qn">${escapeHtml(g.qn)}</div>` : "");
    for (const o of g.opts)
      h += `<button class="pend-opt" data-fp="${p.fp}" data-src="${escapeHtml(p.src || "")}" `
        + `data-q="${escapeHtml(g.qn)}" data-opt="${escapeHtml(o.letter)}" data-label="${escapeHtml(o.label)}">`
        + `<b>(${escapeHtml(o.letter)})</b> ${escapeHtml(o.label)}</button>`;
    h += `</div>`;
  }
  return h + `</div>`;
}
const pickState = {};   // fp -> { qn -> {opt, label} } — accumulates so the composer shows all picks
let pickSubmitTimer = null;
async function pickOption(btn) {
  const { fp, src, q, opt, label } = btn.dataset;
  (pickState[fp] || (pickState[fp] = {}))[q || "?"] = { opt, label };
  const summary = "Cockpit picks — " + Object.entries(pickState[fp])
    .map(([qn, v]) => `${qn}=(${v.opt}) ${v.label}`).join("; ");
  // highlight only within THIS question (leave other questions' picks intact)
  btn.parentElement.querySelectorAll(".pend-opt").forEach(x => x.classList.remove("picked"));
  btn.classList.add("picked");
  await post("/pick", { fp, src, q, opt, label, summary });
  // debounced auto-submit: ~1.8s after the LAST pick, send the answer AND clear this pending opcode
  // (it was answered directly, so it should not linger or be forwardable to its target). Multi-question
  // asks keep resetting the timer, so all answers accumulate before the one send+clear.
  clearTimeout(pickSubmitTimer);
  pickSubmitTimer = setTimeout(() => { post("/pick_submit", { src, fp }); refreshPending(); }, 1800);
}

// Draggable column splitters: dragging a gutter sets --cw-left / --cw-mid (px); right col is 1fr.
// Draggable splitters: vertical .gutter set column widths (--cw-left/--cw-mid);
// the horizontal .hgutter sets the pending panel height (--ph) vs the preview.
function initGutters() {
  const root = document.documentElement;
  document.querySelectorAll(".gutter, .hgutter").forEach(g => {
    g.addEventListener("pointerdown", e => {
      e.preventDefault();
      g.classList.add("dragging");
      const which = g.dataset.resize;                    // left | mid | pending
      const vertical = g.classList.contains("hgutter");  // hgutter = drag height
      let el, varName, min;
      if (which === "left") { el = document.querySelector(".col.left"); varName = "--cw-left"; min = 140; }
      else if (which === "mid") { el = document.querySelector(".col.mid"); varName = "--cw-mid"; min = 140; }
      else if (which === "gametree") { el = document.querySelector(".col.right .gt-resizable"); varName = "--gth"; min = 110; }
      else { el = document.querySelector(".panel.resizable"); varName = "--ph"; min = 90; }
      const startPos = vertical ? e.clientY : e.clientX;
      const startSize = el.getBoundingClientRect()[vertical ? "height" : "width"];
      const move = ev => {
        const d = (vertical ? ev.clientY : ev.clientX) - startPos;
        root.style.setProperty(varName, Math.max(min, startSize + d) + "px");
      };
      const up = () => {
        g.classList.remove("dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  });
}

// ----------------------------------------------------------------- log
const logEl = () => $("log");
function fmtTime(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0, 8);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function addRow(ev) {
  const el = logEl();
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  const row = document.createElement("div");
  row.className = "row";
  let msg = renderEvent(ev);
  row.innerHTML = `<span class="ts">${fmtTime(ev.t)}</span>`
    + `<span class="k k-${ev.kind}">${ev.kind}</span>`
    + `<span class="msg">${msg}</span>`;
  el.appendChild(row);
  while (el.childElementCount > 1000) el.removeChild(el.firstChild);
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function renderEvent(ev) {
  if (ev.kind === "op") {
    const okc = ev.ok ? "ok" : "bad";
    let s = `<b>${escapeHtml(ev.op)}</b> <span class="${okc}">${ev.ok ? "ok" : "fail"}</span>`;
    if (ev.tier != null) s += ` <span class="muted">tier${ev.tier}</span>`;
    if (ev.target) s += ` ${escapeHtml(ev.target)}`;
    if (ev.detail) s += ` <span class="muted">${escapeHtml(ev.detail)}</span>`;
    if (ev.error) s += ` <span class="bad">${escapeHtml(ev.error)}</span>`;
    return s;
  }
  if (ev.kind === "context") {
    const from = ev.from ? `<span class="k-peer">${escapeHtml(ev.from)}</span> ` : "";
    const body = escapeHtml(ev.text || "");
    return `${from}${body} <span class="copy" onclick="copyText(this)" data-t="${encodeURIComponent(ev.text || "")}">copy</span>`;
  }
  if (ev.kind === "opcode") {
    const st = ev.status || "";
    const cls = st === "denied" ? "bad" : "k-peer";
    return `<span class="${cls}">${escapeHtml(st)}</span> <b>${escapeHtml(ev.verb || "")}</b> → `
      + `${escapeHtml(ev.target || "")} <span class="muted">${escapeHtml((ev.body || "").slice(0, 60))}</span>`;
  }
  if (ev.kind === "gamego")
    return `<b>goto</b> ${escapeHtml(ev.app || "")} → ${escapeHtml(ev.node || "")} `
      + `<span class="muted">${escapeHtml(JSON.stringify(ev.step || {}))}</span>`;
  if (ev.kind === "peer") return `${escapeHtml(ev.event || "")} <b>${escapeHtml(ev.name || "")}</b> <span class="muted">${escapeHtml(ev.host || "")}</span>`;
  if (ev.kind === "pick") return `<span class="k-peer">picked</span> <b>${escapeHtml(ev.q || "")}</b> = (${escapeHtml(ev.opt || "")}) <span class="muted">${escapeHtml((ev.label || "").slice(0, 70))}</span>`;
  if (ev.msg) return escapeHtml(ev.msg);
  return escapeHtml(JSON.stringify(ev));
}
function copyText(el) {
  navigator.clipboard.writeText(decodeURIComponent(el.dataset.t || ""));
  el.textContent = "copied";
  setTimeout(() => (el.textContent = "copy"), 1200);
}
window.copyText = copyText;

// -------------------------------------------------------------- wire-up
function connectEvents() {
  const es = new EventSource("/events");
  es.onopen = () => $("conn").classList.add("on");
  es.onerror = () => $("conn").classList.remove("on");
  es.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data);
      addRow(ev);
      if (ev.kind === "opcode" || ev.kind === "orch") refreshPending();
    } catch {}
  };
}

// "restart needed" indicator: the daemon reports whether its .py sources are newer on disk than
// when it booted. Newer → it's running stale code → tint the running light amber + show the label.
async function checkStale() {
  let stale = true, why = "daemon predates the restart indicator — restart it to enable this";
  try {
    const r = await fetch("/status");
    if (r.ok) {
      const j = await r.json();
      stale = !!j.stale;
      why = stale ? "daemon is running OLDER code than what's on disk — restart it to pick up changes"
                  : "daemon is up to date";
    }
  } catch { return; }   // daemon down is shown by the connection dot itself
  const dot = $("conn");
  if (dot) { dot.classList.toggle("stale", stale); dot.title = "event stream — " + why; }
  const msg = $("stalemsg");
  if (msg) msg.hidden = !stale;
}

// ------------------------------------------------------- title bg nudge
// Live pan/zoom of Raves' title background. Writes ~/Library/.../RavesOfQud/title_bg.json,
// which MainMenu polls (~3x/s) and applies with no rebuild. Bake the readout into
// title_bg.seed.json once dialed in.
const bgNudge = { dx: 0, dy: 0, sx: 1.0, sy: 1.0 };
const BGN_PATH = "~/Library/Application Support/RavesOfQud/title_bg.json";
let bgnWriteTimer = null;

function bgnRender() {
  $("bgn-dx").textContent = bgNudge.dx;
  $("bgn-dy").textContent = bgNudge.dy;
  $("bgn-sx").textContent = bgNudge.sx.toFixed(3);
  $("bgn-sy").textContent = bgNudge.sy.toFixed(3);
  $("bgn-readout").textContent = JSON.stringify(bgNudge);
}
function bgnWrite() {
  clearTimeout(bgnWriteTimer);
  bgnWriteTimer = setTimeout(() => rpc("write_text", { path: BGN_PATH, content: JSON.stringify(bgNudge) }), 60);
}
function bgnStep(k, d) {
  bgNudge[k] = Math.round((bgNudge[k] + d) * 1000) / 1000;
  if (k === "sx" || k === "sy") bgNudge[k] = Math.max(0.1, bgNudge[k]);   // below-cover shows a border
  bgnRender();
  bgnWrite();
}

// ------------------------------------------------------- game state tree
// One canonical tree (from the `gametree` op) rendered as an aligned master column
// (the node labels) + a Raves column + a Qud column, each showing that app's 0–1
// completion bar and a live "you are here" highlight. Live state comes from the
// `gamestate` op: a cheap window+port poll (fast) merged with a slower OCR poll that
// refines menu-side screens (title / load / chargen). The OCR result wins while fresh
// so the highlight doesn't flicker back to the coarse guess between OCR reads.
let gtTree = null;
const gtCoarse = {};   // app -> last cheap poll result
const gtFine = {};     // app -> last OCR poll result (+ .ts)
// app -> {node: cost} from the planner. THE GATE for click-to-drive: a cell is clickable when
// a route exists from where the app is NOW, which is a different (and more useful) question
// than the one this used to ask.
const gtCosts = {};
let gtBusy = "";       // a drive or a check in flight; "" when idle

function gtCurrent(app) {
  const f = gtFine[app];
  if (f && f.running && (Date.now() - f.ts) < 12000) return f;
  return gtCoarse[app] || null;
}

async function loadGameTree() {
  const r = await rpc("gametree");
  if (r.ok && r.tree) { gtTree = r.tree; renderGameTree(); }
}

async function pollGameState(ocr) {
  const r = await rpc("gamestate", ocr ? { ocr: true } : {});
  if (!r.ok || !r.states) return;
  const now = Date.now();
  for (const [app, st] of Object.entries(r.states)) {
    st.ts = now;
    if (ocr) gtFine[app] = st; else gtCoarse[app] = st;
  }
  await gtLoadCosts();
  renderGameTree();
}

// One `plan_route` per app with NO node returns the cost to EVERY reachable state — so the
// whole tree's clickability and every hover price come from two calls, not one per node per app.
//
// Refetched only when an app has actually CHANGED NODE. The costs are pure computation on the
// daemon, but they are only wrong when the start state moves, and refetching on every poll made
// the cockpit the chattiest thing on the socket — four extra ops per cycle, drowning the log
// that exists to show what the harness is doing.
const gtCostFrom = {};   // app -> the node the current cost map was computed from
async function gtLoadCosts(force) {
  for (const app of Object.keys((gtTree && gtTree.apps) || {})) {
    const st = gtCurrent(app);
    const at = st ? (st.off ? "off" : (st.node || "unknown")) : null;
    if (!force && gtCostFrom[app] === at) continue;
    const r = await rpc("plan_route", { app });
    if (r.ok && r.costs) { gtCosts[app] = r.costs; gtCostFrom[app] = at; }
  }
}

// Collapsed node ids persist across reloads; a node collapses to its row (children hidden).
const gtCollapsed = new Set(JSON.parse(localStorage.getItem("hv-gt-collapsed") || "[]"));
function gtSaveCollapsed() {
  localStorage.setItem("hv-gt-collapsed", JSON.stringify([...gtCollapsed]));
}
function gtToggle(id) {
  if (gtCollapsed.has(id)) gtCollapsed.delete(id); else gtCollapsed.add(id);
  gtSaveCollapsed(); renderGameTree();
}
function gtSetAll(collapse) {
  gtCollapsed.clear();
  if (collapse && gtTree) {
    const walk = (n) => { if ((n.children || []).length && n.id !== "root") gtCollapsed.add(n.id); (n.children || []).forEach(walk); };
    walk(gtTree.root);
  }
  gtSaveCollapsed(); renderGameTree();
}
window.gtToggle = gtToggle;

// Click an app's cell on a row whose node carries a goto recipe for that app → drive the app
// there (the engine's gamego op runs the recipe; progress streams into the log).
async function gtGo(app, nodeId) {
  if (gtBusy) { gtSay(`already running ${gtBusy} — wait for it to finish`); return; }
  const cost = (gtCosts[app] || {})[nodeId];
  // The one thing worth knowing BEFORE the click: cost >= 100 means the only route runs through
  // the "*" -> title RESTART edge, i.e. minutes and the app relaunched.
  if (cost >= 100 && !confirm(`The only route for ${app} → ${nodeId} costs ${cost} — it goes via a RESTART (minutes, and the app is relaunched).\n\nGo ahead?`)) return;
  gtBusy = `${app} → ${nodeId}`;
  gtSay(`driving ${gtBusy} …`);
  const r = await rpc("gamego", { app, node: nodeId });
  gtBusy = "";
  if (r.ok) {
    gtSay(`${app} → ${nodeId}: OK  (${r.route || ""})`);
  } else {
    // Name the step that broke, not just ok/fail — the trace is the useful part.
    const bad = (r.steps || []).filter(s => !s.ok)[0];
    gtSay(`${app} → ${nodeId}: FAILED — ${(bad && bad.error) || r.error || "?"}`, true);
  }
  await gtLoadCosts(true);
  renderGameTree();
}
window.gtGo = gtGo;

// Run a check REGISTERED IN THE TREE. `nodeId` is "" for the harness-wide ones. The command
// text lives in gametree.json — this names WHICH check, never what to execute.
async function gtTest(nodeId, testId) {
  if (gtBusy) { gtSay(`already running ${gtBusy} — wait for it to finish`); return; }
  gtBusy = `check ${testId}`;
  gtSay(`running ${testId} …`);
  const r = await rpc("run_test", { node: nodeId, test: testId });
  gtBusy = "";
  if (r.exit === undefined) { gtSay(`check ${testId}: ${r.error || "did not run"}`, true); return; }
  // TAIL, not head: a check that fails says so at the end.
  const tail = (r.tail || []).slice(-3).join(" / ");
  gtSay(`check ${testId}: ${r.detail}${tail ? "  —  " + tail : ""}`, !r.ok);
}
window.gtTest = gtTest;

// The status line under the tree header. Kept separate from the SSE log: this is the answer to
// something you just clicked, and it should not scroll away under the daemon's own chatter.
function gtSay(text, bad) {
  const el = $("gt-status");
  if (!el) return;
  el.textContent = text;
  el.className = "gt-status" + (bad ? " bad" : "");
}

function gtRow(node, depth, appIds, cur) {
  const pad = 6 + depth * 14;
  const kids = (node.children || []).length > 0;
  const closed = gtCollapsed.has(node.id);
  const twist = kids
    ? `<span class="gt-twist" onclick="gtToggle('${escapeHtml(node.id)}')" title="${closed ? "expand" : "collapse"}">${closed ? "▸" : "▾"}</span>`
    : `<span class="gt-twist gt-twist-leaf"></span>`;
  let cells = "";
  for (const app of appIds) {
    const st = cur[app];
    const isCur = !!(st && st.node === node.id);
    const onPath = !!(st && Array.isArray(st.path) && st.path.includes(node.id) && !isCur);
    const done = (node.done && typeof node.done[app] === "number") ? node.done[app] : null;
    const bar = done == null
      ? `<span class="gt-bar gt-bar-na"></span><span class="gt-num"></span>`
      : `<span class="gt-bar"><span class="gt-bar-fill" style="width:${Math.round(done * 100)}%"></span></span><span class="gt-num">${done.toFixed(1)}</span>`;
    // REACHABILITY, not a stored recipe. This used to read `node.goto[app]`, and when the 20
    // legacy recipes were deleted for the transition graph every cell silently stopped being
    // clickable — the cockpit looked fine and did nothing.
    const cost = (gtCosts[app] || {})[node.id];
    const canGo = cost !== undefined;
    const cls = `gt-cell${isCur ? " cur" : ""}${onPath ? " onpath" : ""}${canGo ? " gt-go" : ""}`;
    const label = escapeHtml(gtTree.apps[app].label || app);
    const attrs = canGo
      ? ` onclick="gtGo('${app}','${escapeHtml(node.id)}')" title="drive ${label} to ${escapeHtml(node.label || node.id)} — cost ${cost}${cost >= 100 ? " (via RESTART)" : ""}"`
      : ` title="${label} has no route to ${escapeHtml(node.label || node.id)} from here"`;
    cells += `<span class="${cls}"${attrs}>${bar}${isCur ? '<span class="gt-here">●</span>' : ""}</span>`;
  }
  const anyCur = appIds.some(a => cur[a] && cur[a].node === node.id);
  const hiddenCur = closed && appIds.some(a => cur[a] && Array.isArray(cur[a].path) && cur[a].path.includes(node.id) && cur[a].node !== node.id);
  const marks = (node.tests || []).map(t =>
    `<span class="gt-test" onclick="gtTest('${escapeHtml(node.id)}','${escapeHtml(t.id)}')" title="run ${escapeHtml(t.tier || "")} check ${escapeHtml(t.id)}: ${escapeHtml(t.cmd || "")}">[T]</span>`).join("");
  return `<div class="gt-row${anyCur ? " rowcur" : ""}">${twist}<span class="gt-label" style="padding-left:${pad}px" title="${escapeHtml(node.id)}">${escapeHtml(node.label || node.id)}${hiddenCur ? ' <span class="gt-here">●</span>' : ""}${marks}</span>${cells}</div>`;
}

function renderGameTree() {
  const host = $("gametree");
  if (!host) return;
  if (!gtTree) { host.innerHTML = '<div class="muted" style="padding:8px">loading…</div>'; return; }
  const appIds = Object.keys(gtTree.apps || {});
  const cur = {};
  const legend = [];
  for (const app of appIds) {
    const st = gtCurrent(app);
    cur[app] = st;
    const lbl = (gtTree.apps[app].label || app);
    const extra = st && st.extra ? Object.entries(st.extra).filter(([k]) => k === "popup" || k === "mode")
      .map(([k, v]) => `${k}=${v}`).join(" ") : "";
    legend.push(`${lbl}: ${st ? (st.off ? "off" : (st.label || "…")) : "—"}${extra ? " · " + extra : ""}`);
  }
  const leg = $("gt-legend"); if (leg) leg.textContent = legend.join("   ·   ");
  // Harness-wide checks test the SUPERVISOR, not a screen, so they sit above the tree rather
  // than on some arbitrary node. Per-node checks appear as [T] on their own row.
  const harness = (gtTree.tests || []).map(t =>
    `<span class="gt-test" onclick="gtTest('','${escapeHtml(t.id)}')" title="run ${escapeHtml(t.tier || "")} check: ${escapeHtml(t.cmd || "")}">[T] ${escapeHtml(t.id)}</span>`).join(" ");
  let html = harness ? `<div class="gt-row gt-checks"><span class="gt-twist gt-twist-leaf"></span><span class="gt-label">checks: ${harness}</span></div>` : "";
  html += '<div class="gt-row gt-head"><span class="gt-twist gt-twist-leaf"></span><span class="gt-label">screen</span>';
  for (const app of appIds) html += `<span class="gt-cell gt-head-cell">${escapeHtml(gtTree.apps[app].label || app)}</span>`;
  html += "</div>";
  const rows = [];
  const walk = (node, depth) => {
    if (node.id === "root") { for (const ch of node.children || []) walk(ch, 0); return; }
    rows.push(gtRow(node, depth, appIds, cur));
    if (!gtCollapsed.has(node.id))
      for (const ch of node.children || []) walk(ch, depth + 1);
  };
  walk(gtTree.root, 0);
  host.innerHTML = html + rows.join("");
}

async function init() {
  const p = await rpc("ping");
  if (p.ok) $("backend").textContent = "backend: " + p.backend;
  $("refresh").onclick = refreshWindows;
  $("send").onclick = sendContext;
  $("applylayout").onclick = applyLayout;
  $("savelayout").onclick = saveLayout;
  document.querySelectorAll(".panel-hd .tab").forEach(b =>
    (b.onclick = () => switchPreviewTab(b.dataset.tab)));
  $("cong-capture").onclick = captureCongruence;
  $("cong-fade").oninput = _applyCrossfade;
  $("cong-sources-toggle").onchange = _applySourcesToggle;
  $("cong-sim-toggle").onchange = _applySimToggle;
  const stage = $("cong-stage");
  stage.addEventListener("wheel", _congWheel, { passive: false });
  stage.addEventListener("pointerdown", _congPanStart);
  window.addEventListener("pointermove", _congPanMove);
  window.addEventListener("pointerup", _congPanEnd);
  stage.addEventListener("contextmenu", _congContextMenu);
  stage.addEventListener("dblclick", _congFitView);
  $("cong-menu").querySelectorAll("button").forEach(b =>
    (b.onclick = () => _congCopy(b.dataset.copy)));
  document.addEventListener("pointerdown", (e) => {
    if (!$("cong-menu").hidden && !$("cong-menu").contains(e.target)) _congHideMenu();
  });
  new ResizeObserver(() => {
    if (!_congImgs) return;
    const wasFit = Math.abs(_congZoom - _congFit) < 1e-3;
    _congComputeFit();
    if (wasFit || _congZoom < _congFit) _congFitView();
    else { _congClamp(); _congApplyTransform(); }
  }).observe(stage);
  $("cong-thresh").oninput = () => {
    $("cong-thresh-val").textContent = $("cong-thresh").value;
    _buildSimilarity();
  };
  $("start-raves").onclick = startRavesLatest;
  $("start-qud-solo").onclick = () => startSolo("qud");
  $("start-raves-solo").onclick = () => startSolo("raves");
  document.querySelectorAll(".ut").forEach(b =>
    (b.onclick = () => userTestLayout(+b.dataset.w, +b.dataset.h)));
  $("raves-1to1").onclick = toggleRaves1to1;
  $("qud-godmode").onclick = qudGodmode;
  $("qud-xp").onclick = qudXp;
  $("qud-cyberchest").onclick = qudCyberChest;
  $("qud-loadout").onclick = qudLoadout;
  $("qud-cyberlicense").onclick = qudCyberLicense;
  $("qud-back").onclick = qudBack;
  $("hv-abort").onclick = async () => {
    try { await rpc("abort_control", {}); } catch (e) { alert("abort failed: " + (e.message || e)); }
  };
  $("layoutsel").onchange = showLayoutDesc;
  $("clearlog").onclick = () => (logEl().innerHTML = "");
  $("gt-ocr").onclick = () => pollGameState(true);
  $("gt-expand").onclick = () => gtSetAll(false);
  $("gt-collapse").onclick = () => gtSetAll(true);
  document.querySelectorAll(".bgn-b").forEach(b =>
    (b.onclick = () => bgnStep(b.dataset.bgn, parseFloat(b.dataset.d))));
  $("bgn-reset").onclick = () => { bgNudge.dx = 0; bgNudge.dy = 0; bgNudge.sx = 1.0; bgNudge.sy = 1.0; bgnRender(); bgnWrite(); };
  $("bgn-readout").onclick = () => navigator.clipboard.writeText($("bgn-readout").textContent);
  bgnRender();
  $("off").onclick = async () => {
    if (!confirm("Shut down the highvisor daemon? You'll restart it from a terminal.")) return;
    try { await fetch("/shutdown", { method: "POST" }); } catch {}
    document.body.classList.add("down");   // dim + show the stopped overlay
    $("conn").classList.remove("on");
  };
  $("ctx").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") sendContext();
  });
  initGutters();
  await refreshWindows();
  await refreshLayouts();
  await refreshPeers();
  await refreshPending();
  connectEvents();
  checkStale();
  setInterval(refreshPeers, 4000);
  setInterval(refreshPending, 3000);
  setInterval(refreshUserTestStatus, 3000);   // live Raves/Qud duplicate checker
  setInterval(checkStale, 5000);
  // game state tree: load structure, then poll live state (cheap fast + OCR slow)
  await loadGameTree();
  pollGameState(false);
  pollGameState(true);
  setInterval(() => pollGameState(false), 2500);   // window + Qud port — cheap, responsive
  setInterval(() => pollGameState(true), 8000);    // OCR refine of menu-side screens
  setInterval(loadGameTree, 20000);                // pick up gametree.json edits (completion, structure)
}
init();
