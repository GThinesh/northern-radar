/* KKL rain table — places × IST spectrum columns from data/rain.json.
   Each column is a time spectrum, not an average: every radar frame in
   the bucket paints its own slice (wet = radar-legend color, dry =
   white). No deps. */
const $ = (id) => document.getElementById(id);

const MODES = [
  { min: 240, label: "4H" },
  { min: 60, label: "1H" },
  { min: 30, label: "30M" },
  { min: 15, label: "15M" },
];

const PAC_LIVE = "https://mausam.imd.gov.in/Radar/pac_kkl.gif";
const state = { rain: null, pac: null, day: null, res: 60, wetOnly: false, district: "all", place: "all", q: "", mode: "hourly", week: 0 };

async function load() {
  const meta = $("meta");
  try {
    const res = await fetch("data/rain.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`rain ${res.status}`);
    state.rain = await res.json();
  } catch (e) {
    meta.textContent = "…";
    meta.title = `Rain table unreachable: ${e.message}`;
    $("liveDot").classList.add("bad");
    $("rainBody").innerHTML = `<tr><td class="empty-cell" colspan="97">Couldn't load the rain table. <button type="button" onclick="location.reload()">Retry</button></td></tr>`;
    return;
  }
  const { rain } = state;
  meta.textContent = `${shortTime(rain.updated_ist)} · ${rain.days.length}d`;
  meta.title = `${rain.radar} · ${rain.updated_ist} · ${rain.days.length} day(s)`;

  const sel = $("daySel");
  sel.innerHTML = "";
  for (const d of rain.days) {
    const o = document.createElement("option");
    o.value = d.date;
    const wet = d.rows.filter((r) => r.max != null).length;
    o.textContent = `${shortDate(d.date)} · ${wet}`;
    o.title = `${d.date} · ${wet} of ${d.rows.length} places rained`;
    sel.appendChild(o);
  }
  const q = new URLSearchParams(location.search);
  if (q.get("date") && [...sel.options].some((o) => o.value === q.get("date"))) sel.value = q.get("date");
  else if (sel.options.length) sel.selectedIndex = 0;
  if (q.get("wet") === "1") { state.wetOnly = true; $("wetOnly").checked = true; }

  const dist = $("distSel");
  dist.innerHTML = "";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "All districts";
  dist.appendChild(all);
  for (const name of [...new Set(rain.places.map((p) => p.district))]) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name;
    dist.appendChild(o);
  }
  if (q.get("district") && [...dist.options].some((o) => o.value === q.get("district"))) {
    dist.value = q.get("district");
    state.district = dist.value;
  }

  const psel = $("placeSel");
  psel.innerHTML = "";
  const allP = document.createElement("option");
  allP.value = "all";
  allP.textContent = "All places";
  psel.appendChild(allP);
  for (const p of [...rain.places].sort((a, b) => a.name_en.localeCompare(b.name_en))) {
    const o = document.createElement("option");
    o.value = p.name_en;
    o.textContent = `${p.name_en} · ${p.district}`;
    o.title = `${p.name_en}, ${p.district}`;
    psel.appendChild(o);
  }
  if (q.get("place") && [...psel.options].some((o) => o.value === q.get("place"))) {
    psel.value = q.get("place");
    state.place = psel.value;
  }
  if (q.get("q")) {
    state.q = q.get("q");
    $("q").value = state.q;
  }
  const wantRes = parseInt(q.get("res") || "", 10);
  if (MODES.some((m) => m.min === wantRes)) state.res = wantRes;

  sel.onchange = () => render();
  // days sorted newest-first (index 0 = newest): older day is +1.
  $("prevDay").onclick = () => stepDay(1);
  $("nextDay").onclick = () => stepDay(-1);
  $("wetOnly").onchange = (e) => { state.wetOnly = e.target.checked; render(); };
  dist.onchange = () => { state.district = dist.value; render(); };
  psel.onchange = () => { state.place = psel.value; render(); };
  $("q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
  try {
    const pr = await fetch("data/pac.json", { cache: "no-store" });
    if (pr.ok) state.pac = await pr.json();
  } catch {}
  if (q.get("mode") === "accum") state.mode = "accum";
  syncResSeg();
  syncModeSeg();
  $("resSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-res]");
    if (!b) return;
    const v = parseInt(b.dataset.res, 10);
    if (v !== state.res) { state.res = v; syncResSeg(); render(); }
  });
  $("modeSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    if (b.dataset.mode !== state.mode) {
      state.mode = b.dataset.mode;
      state.week = 0;
      syncModeSeg();
      render();
    }
  });
  $("prevWeek").onclick = () => stepWeek(1);
  $("nextWeek").onclick = () => stepWeek(-1);
  $("themeBtn").onclick = toggleTheme;
  try {
    const saved = localStorage.getItem("kkl-theme-v2");
    if (saved === "dark" && document.documentElement.dataset.theme !== "dark") toggleTheme();
    if (saved === "light" && document.documentElement.dataset.theme === "dark") toggleTheme();
  } catch {}

  // Mobile keeps the titlebar to title + date: relocate the page links and
  // theme toggle next to the day stepper instead.
  const narrow = matchMedia("(max-width: 720px)");
  const placeTheme = () => {
    const btn = $("themeBtn"), slot = $("themeSlot"),
      links = document.querySelector(".pagelinks"),
      bar = document.querySelector(".top-actions");
    if (narrow.matches) { slot.append(links, btn); }
    else { bar.prepend(links); bar.append(btn); }
  };
  narrow.addEventListener?.("change", placeTheme);
  placeTheme();
  syncThemeBtn();

  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    if (e.key === "ArrowRight" && e.shiftKey) { e.preventDefault(); stepDay(-1); }
    else if (e.key === "ArrowLeft" && e.shiftKey) { e.preventDefault(); stepDay(1); }
  });

  buildScale(rain.lut || []);
  render();
}

function stepDay(dir) {
  const sel = $("daySel");
  const n = sel.selectedIndex + dir;
  if (n >= 0 && n < sel.options.length) { sel.selectedIndex = n; render(); }
}

function toggleTheme() {
  const html = document.documentElement;
  html.dataset.theme = html.dataset.theme !== "dark" ? "dark" : "light";
  syncThemeBtn();
  try { localStorage.setItem("kkl-theme-v2", html.dataset.theme); } catch {}
}
function syncThemeBtn() {
  const b = $("themeBtn");
  const dark = document.documentElement.dataset.theme === "dark";
  b.textContent = dark ? "Dark" : "Light";
  b.setAttribute("aria-pressed", String(dark));
}
function shortDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return iso || "";
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${+m[3]} ${months[+m[2] - 1]}`;
}
function shortTime(s) {
  const m = /(\d{2}):(\d{2})/.exec(s || "");
  return m ? `${m[1]}:${m[2]}` : (s || "…");
}

/* Readable ink over a saturated legend color: whichever of black / white
   has the higher WCAG contrast ratio. */
function inkFor(hex) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "";
  const v = parseInt(m[1], 16);
  const lin = (c) => {
    c /= 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const L = 0.2126 * lin((v >> 16) & 255)
    + 0.7152 * lin((v >> 8) & 255) + 0.0722 * lin(v & 255);
  const white = 1.05 / (L + 0.05), black = (L + 0.05) / 0.05;
  return white > black ? "#ffffff" : "#000000";
}

function buildScale(lut) {
  const bar = $("dbscale");
  bar.innerHTML = "";
  // lut is high -> low; paint left (20) -> right (60).
  for (const s of [...lut].reverse()) {
    const i = document.createElement("i");
    i.style.background = s.color;
    bar.appendChild(i);
  }
}

function syncResSeg() {
  for (const b of $("resSeg").querySelectorAll("button[data-res]")) {
    const on = parseInt(b.dataset.res, 10) === state.res;
    b.setAttribute("aria-checked", String(on));
    b.classList.toggle("on", on);
  }
}

/* Nearest radar-legend color for a dBZ value (lut is high -> low). */
function lutColor(dbz) {
  const lut = state.rain.lut || [];
  let best = null, bd = Infinity;
  for (const s of lut) {
    const d = Math.abs(s.dbz - dbz);
    if (d < bd) { bd = d; best = s.color; }
  }
  return best || "#888";
}

function catFor(dbz) {
  if (dbz == null) return "no echo (<20 dBZ)";
  if (dbz >= 50) return "very heavy (>50 dBZ)";
  if (dbz >= 40) return "heavy (40-50 dBZ)";
  if (dbz >= 30) return "moderate (30-40 dBZ)";
  return "light (20-30 dBZ)";
}

function fmtHM(mins) {
  const h = Math.floor(mins / 60) % 24, m = Math.floor(mins % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/* Time-proportional gradient: one equal slice per frame in time order.
   Wet slice = legend color, dry slice = pure white. */
function spectrumGradient(segs) {
  const n = segs.length;
  const stops = segs.map((s, i) => {
    const c = s.dbz == null ? "#ffffff" : lutColor(s.dbz);
    const a = (i * 100 / n).toFixed(2), z = ((i + 1) * 100 / n).toFixed(2);
    return `${c} ${a}% ${z}%`;
  });
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

function bucketTip(segs, peak) {
  const head = peak != null ? `peak ${peak} dBZ · ${catFor(peak)}` : "no echo";
  const parts = segs.slice(0, 10).map((s) =>
    s.dbz == null ? `${s.t} dry` : `${s.t} ${s.dbz}`);
  if (segs.length > 10) parts.push(`…${segs.length - 10} more`);
  return `${head} — ${parts.join(", ")}`;
}

function syncModeSeg() {
  for (const b of $("modeSeg").querySelectorAll("button[data-mode]")) {
    const on = b.dataset.mode === state.mode;
    b.setAttribute("aria-checked", String(on));
    b.classList.toggle("on", on);
  }
  const accum = state.mode === "accum";
  // Hourly = minimal bar: day stepper + mode + search only.
  // Accum = full filters: week stepper + wet/district/place + search.
  // Detail (res) stays hidden: hourly is fixed at 1H for a responsive table.
  $("weekGroup").hidden = !accum;
  $("resGroup").hidden = true;
  $("wetGroup").hidden = !accum;
  $("distGroup").hidden = !accum;
  $("placeGroup").hidden = !accum;
  $("pacCard").hidden = !accum;
  $("daySel").disabled = accum;
  $("prevDay").disabled = accum;
  $("nextDay").disabled = accum;
  $("dayGroup").style.opacity = accum ? ".45" : "";
}

function stepWeek(dir) {
  const n = (state.pac?.days?.length || 0);
  const maxW = Math.max(0, Math.ceil(n / 7) - 1);
  state.week = Math.min(maxW, Math.max(0, state.week + dir));
  render();
}

function istToday() {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
    }).format(new Date());
  } catch { return ""; }
}

function renderAccum() {
  const days = [...(state.pac?.days || [])].sort((a, b) => b.date < a.date ? -1 : 1);
  const weekDays = days.slice(state.week * 7, state.week * 7 + 7).reverse();
  const maxW = Math.max(0, Math.ceil(days.length / 7) - 1);
  $("weekLabel").textContent = weekDays.length
    ? `${weekDays[0].date} to ${weekDays[weekDays.length - 1].date}`
    : "no data yet";
  $("prevWeek").disabled = state.week >= maxW;
  $("nextWeek").disabled = state.week <= 0;
  const byDate = new Map(days.map((d) => [d.date, d]));
  const needle = state.q;
  const places = (state.pac?.places || state.rain.places || [])
    .map((p) => ({ name_en: p.name_en ?? p.place, district: p.district }))
    .filter((p) =>
      (state.district === "all" || p.district === state.district) &&
      (state.place === "all" || p.name_en === state.place) &&
      (!needle || p.name_en.toLowerCase().includes(needle) ||
        p.district.toLowerCase().includes(needle)))
    .sort((a, b) => a.name_en.localeCompare(b.name_en));
  let rows = places.map((p) => {
    let total = 0, wet = 0;
    const cells = weekDays.map((d) => {
      const r = (byDate.get(d.date)?.rows || [])
        .find((x) => x.place === p.name_en && x.district === p.district);
      if (r?.mm != null) { total += r.mm; wet++; return r; }
      return null;
    });
    return { p, cells, total, wet };
  });
  if (state.wetOnly) rows = rows.filter((r) => r.wet > 0);
  $("daySummary").textContent =
    `${rows.filter((r) => r.wet > 0).length} of ${places.length} rained this week · mm total per day`;
  const head = $("rainHead");
  head.innerHTML = "";
  const corner = document.createElement("th");
  corner.scope = "col";
  corner.className = "corner";
  corner.textContent = "Place";
  head.appendChild(corner);
  for (const d of weekDays) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = shortDate(d.date);
    th.title = d.date + (d.captured_ist ? ` · frozen ${d.captured_ist}` : "");
    head.appendChild(th);
  }
  const body = $("rainBody");
  body.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.className = "place";
    const nm = document.createElement("span");
    nm.className = "pname";
    nm.textContent = r.p.name_en ?? r.p.place ?? "";
    const ds = document.createElement("span");
    ds.className = "pdist";
    ds.textContent = `${r.p.district} · Σ${Math.round(r.total * 10) / 10}`;
    th.append(nm, ds);
    tr.appendChild(th);
    for (const c of r.cells) {
      const td = document.createElement("td");
      if (c == null || c.mm == null) {
        td.className = "dry";
        td.textContent = "–";
      } else {
        td.className = "wet";
        td.style.background = c.color || "#888";
        td.style.color = inkFor(c.color);
        td.textContent = String(c.mm >= 100 ? "100+" : c.mm);
        td.title = `${c.mm} mm`;
      }
      tr.appendChild(td);
    }
    frag.appendChild(tr);
  }
  body.appendChild(frag);
  const show = weekDays[weekDays.length - 1] || days[0];
  const img = $("pacImg"), cap = $("pacCap");
  if (show) {
    const today = istToday();
    const isToday = show.date === today;
    img.src = isToday ? PAC_LIVE : show.image;
    img.alt = `PAC 24H accumulation for ${show.date}`;
    cap.textContent = isToday
      ? `${show.date} · live now · frozen ${show.captured_ist || "pending"}`
      : `${show.date} · frozen ${show.captured_ist || ""}`;
  } else {
    img.removeAttribute("src");
    cap.textContent = "No PAC day frozen yet. The first run after midnight IST creates it.";
  }
  try {
    const p = new URLSearchParams({ mode: "accum" });
    if (state.week) p.set("week", String(state.week));
    history.replaceState(null, "", `?${p}`);
  } catch {}
}

function render() {
  if (state.mode === "accum") return renderAccum();
  const sel = $("daySel");
  const day = state.rain.days.find((d) => d.date === sel.value);
  state.day = day;
  try {
    const p = new URLSearchParams({ date: sel.value });
    if (state.res !== 60) p.set("res", String(state.res));
    if (state.wetOnly) p.set("wet", "1");
    if (state.district !== "all") p.set("district", state.district);
    if (state.place !== "all") p.set("place", state.place);
    if (state.q) p.set("q", state.q);
    history.replaceState(null, "", `?${p}`);
  } catch {}
  if (!day) return;

  const res = state.res;
  const nB = 1440 / res;

  // Legacy file without frame grain: fall back to hourly max cells.
  if (!day.frames) return renderLegacy(day, sel);

  // Bucket frame indices in time order.
  const buckets = Array.from({ length: nB }, () => []);
  (day.frames || []).forEach((f, j) => {
    const b = Math.min(nB - 1, Math.floor(f.min / res));
    if (b >= 0) buckets[b].push(j);
  });
  const nCovered = buckets.filter((b) => b.length).length;

  const needle = state.q;
  const rows = day.rows.filter((r) =>
    (state.district === "all" || r.district === state.district) &&
    (state.place === "all" || r.place === state.place) &&
    (!state.wetOnly || r.max != null) &&
    (!needle || r.place.toLowerCase().includes(needle) ||
      r.district.toLowerCase().includes(needle)))
    .sort((a, b) => a.place.localeCompare(b.place));
  const wet = day.rows.filter((r) => r.max != null).length;
  const filtered = state.wetOnly || state.district !== "all" || state.place !== "all" || needle;
  const modeLabel = (MODES.find((m) => m.min === res) || {}).label || `${res}M`;
  $("daySummary").textContent =
    `${wet} of ${day.rows.length} rained · ${nCovered} of ${nB} ${modeLabel} slots` +
    (filtered ? ` · ${rows.length} shown` : "");

  const table = $("raintable");
  table.dataset.res = String(res);
  const head = $("rainHead");
  head.innerHTML = "";
  const corner = document.createElement("th");
  corner.scope = "col";
  corner.className = "corner";
  corner.textContent = "Place";
  head.appendChild(corner);
  buckets.forEach((js, b) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.className = "h" + (js.length ? "" : " nodata");
    th.textContent = res >= 60 ? String(Math.floor(b * res / 60)).padStart(2, "0") : fmtHM(b * res);
    th.title = js.length
      ? `${fmtHM(b * res)}–${fmtHM((b + 1) * res)} IST · ${js.length} frame(s)`
      : `No frames ${fmtHM(b * res)}–${fmtHM((b + 1) * res)} IST`;
    head.appendChild(th);
  });

  const body = $("rainBody");
  body.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = nB + 1;
    td.className = "empty-cell";
    td.textContent = (state.wetOnly || state.district !== "all" || state.place !== "all" || state.q)
      ? "No places match this filter."
      : "No rain recorded this day.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.className = "place";
    const nm = document.createElement("span");
    nm.className = "pname";
    nm.textContent = r.place;
    const ds = document.createElement("span");
    ds.className = "pdist";
    ds.textContent = r.district;
    th.append(nm, ds);
    if (r.max != null) th.title = `${r.place} · peak ${r.max} dBZ`;
    tr.appendChild(th);
    const echo = new Map(r.spec || []);
    buckets.forEach((js) => {
      const td = document.createElement("td");
      if (!js.length) {
        td.className = "nodata"; // bucket with no frame coverage
      } else {
        const segs = js.map((j) => ({
          t: day.frames[j].t,
          dbz: echo.has(j) ? echo.get(j) : null,
        }));
        const peak = segs.reduce((m, s) =>
          (s.dbz != null && (m == null || s.dbz > m)) ? s.dbz : m, null);
        td.className = "spec" + (peak == null ? " alldry" : "");
        td.style.background = spectrumGradient(segs);
        td.title = bucketTip(segs, peak);
      }
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  }
  body.appendChild(frag);
  $("tablewrap").scrollTo({ left: 0, top: 0 });
}

/* Fallback for rain.json files without frame grain (old cache). */
function renderLegacy(day, sel) {
  const table = $("raintable");
  table.dataset.res = "60";
  const needle = state.q;
  const rows = day.rows.filter((r) =>
    (state.district === "all" || r.district === state.district) &&
    (state.place === "all" || r.place === state.place) &&
    (!state.wetOnly || r.max != null) &&
    (!needle || r.place.toLowerCase().includes(needle) ||
      r.district.toLowerCase().includes(needle)))
    .sort((a, b) => a.place.localeCompare(b.place));
  const wet = day.rows.filter((r) => r.max != null).length;
  $("daySummary").textContent =
    `${wet} of ${day.rows.length} rained · ${day.n_frames_hours} of 24 hours`;
  const head = $("rainHead");
  head.innerHTML = "";
  const corner = document.createElement("th");
  corner.scope = "col";
  corner.className = "corner";
  corner.textContent = "Place";
  head.appendChild(corner);
  (day.hours_with_data || []).forEach((has, h) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.className = "h" + (has ? "" : " nodata");
    th.textContent = String(h).padStart(2, "0");
    head.appendChild(th);
  });
  const body = $("rainBody");
  body.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.className = "place";
    const nm = document.createElement("span");
    nm.className = "pname";
    nm.textContent = r.place;
    const ds = document.createElement("span");
    ds.className = "pdist";
    ds.textContent = r.district;
    th.append(nm, ds);
    tr.appendChild(th);
    (r.cells || []).forEach((c) => {
      const td = document.createElement("td");
      if (c == null) {
        td.className = "nodata";
      } else if (c.dbz == null) {
        td.className = "dry";
        td.textContent = "–";
      } else {
        td.className = "wet";
        td.style.background = c.color;
        td.style.color = inkFor(c.color);
        td.textContent = String(Math.round(c.dbz));
        td.title = `${c.dbz} dBZ · ${c.cat}`;
      }
      tr.appendChild(td);
    });
    frag.appendChild(tr);
  }
  body.appendChild(frag);
}

load().catch((e) => {
  $("meta").textContent = "Failed: " + e;
  $("liveDot").classList.add("bad");
});
