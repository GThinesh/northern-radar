/* KKL rain table — places × IST hours from data/rain.json. No deps. */
const $ = (id) => document.getElementById(id);

const state = { rain: null, day: null, wetOnly: false, district: "all", place: "all", q: "" };

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
    $("rainBody").innerHTML = `<tr><td class="empty-cell" colspan="25">Couldn't load the rain table. <button type="button" onclick="location.reload()">Retry</button></td></tr>`;
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

  sel.onchange = () => render();
  // days sorted newest-first (index 0 = newest): older day is +1.
  $("prevDay").onclick = () => stepDay(1);
  $("nextDay").onclick = () => stepDay(-1);
  $("wetOnly").onchange = (e) => { state.wetOnly = e.target.checked; render(); };
  dist.onchange = () => { state.district = dist.value; render(); };
  psel.onchange = () => { state.place = psel.value; render(); };
  $("q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
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

function render() {
  const sel = $("daySel");
  const day = state.rain.days.find((d) => d.date === sel.value);
  state.day = day;
  try {
    const p = new URLSearchParams({ date: sel.value });
    if (state.wetOnly) p.set("wet", "1");
    if (state.district !== "all") p.set("district", state.district);
    if (state.place !== "all") p.set("place", state.place);
    if (state.q) p.set("q", state.q);
    history.replaceState(null, "", `?${p}`);
  } catch {}
  if (!day) return;

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
  $("daySummary").textContent =
    `${wet} of ${day.rows.length} rained · ${day.n_frames_hours} of 24 hours` +
    (filtered ? ` · ${rows.length} shown` : "");

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
    th.title = has ? `${String(h).padStart(2, "0")}:00 IST` : `No frames at ${String(h).padStart(2, "0")}:00 IST`;
    head.appendChild(th);
  });

  const body = $("rainBody");
  body.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 25;
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
    r.cells.forEach((c) => {
      const td = document.createElement("td");
      if (c == null) {
        td.className = "nodata"; // hour with no frame coverage
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
  $("tablewrap").scrollTo({ left: 0, top: 0 });
}

load().catch((e) => {
  $("meta").textContent = "Failed: " + e;
  $("liveDot").classList.add("bad");
});
