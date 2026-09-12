/* KKL ops log — day viewer with scrub + lightbox. No deps. */
const $ = (id) => document.getElementById(id);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = { idx: null, day: null, slotIx: 0, playing: !reduceMotion };
let playTimer = null;
const FRAME_MS = 900;
function stopTimer() { if (playTimer) { clearInterval(playTimer); playTimer = null; } }

async function load() {
  const meta = $("meta");
  try {
    const res = await fetch("data/index.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`index ${res.status}`);
    state.idx = await res.json();
  } catch (e) {
    meta.textContent = "Archive unreachable — check Pages / data/index.json";
    $("liveDot").classList.add("bad");
    $("slots").innerHTML = `<p class="error" role="alert">Failed to load data/index.json: ${e.message}</p>`;
    return;
  }
  const { idx } = state;
  meta.textContent = `${idx.radar} · ${idx.updated_ist} · ${idx.days.length} day(s)`;

  const sel = $("daySel");
  sel.innerHTML = "";
  for (const d of idx.days) {
    const o = document.createElement("option");
    o.value = d.date;
    o.textContent = `${d.date} · ${((d.frames && d.frames.length ? d.frames : d.slots) || []).length} frames`;
    sel.appendChild(o);
  }
  const q = new URLSearchParams(location.search).get("date");
  if (q && [...sel.options].some((o) => o.value === q)) sel.value = q;
  else if (sel.options.length) sel.selectedIndex = 0;

  sel.onchange = () => render(true);
  $("prevDay").onclick = () => stepDay(-1);
  $("nextDay").onclick = () => stepDay(1);
  $("playBtn").onclick = togglePlay;
  $("scrub").oninput = (e) => { state.playing = false; stopTimer(); syncPlayBtn(); setSlot(+e.target.value); };
  $("themeBtn").onclick = () => {
    const html = document.documentElement;
    const light = html.dataset.theme !== "light";
    html.dataset.theme = light ? "light" : "dark";
    $("themeBtn").textContent = light ? "● night" : "☀ paper";
    $("themeBtn").setAttribute("aria-pressed", String(light));
    try { localStorage.setItem("kkl-theme-v2", html.dataset.theme); } catch {}
  };
  try {
    if (localStorage.getItem("kkl-theme-v2") === "dark") $("themeBtn").click();
  } catch {}

  // filmstrip arrow-key scrub
  $("film").addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { e.preventDefault(); setSlot(state.slotIx + 1); }
    if (e.key === "ArrowLeft") { e.preventDefault(); setSlot(state.slotIx - 1); }
  });

  render(true);
}

function stepDay(dir) {
  const sel = $("daySel");
  const n = sel.selectedIndex + dir;
  if (n >= 0 && n < sel.options.length) { sel.selectedIndex = n; render(true); }
}

function togglePlay() {
  state.playing = !state.playing;
  syncPlayBtn();
  if (state.playing) {
    setSlot(state.slotIx);
    stopTimer();
    playTimer = setInterval(() => setSlot(state.slotIx + 1), FRAME_MS);
  } else {
    stopTimer();
    setSlot(state.slotIx);
  }
}
function syncPlayBtn() {
  const b = $("playBtn");
  b.setAttribute("aria-pressed", String(state.playing));
  b.textContent = state.playing ? "⏸ pause" : "▶ play story";
}

function currentSlots() {
  if (!state.day) return [];
  if (state.day.frames && state.day.frames.length) return state.day.frames;
  return state.day.slots || [];
}

function render(resetSlot) {
  const sel = $("daySel");
  const day = state.idx.days.find((d) => d.date === sel.value);
  state.day = day;
  history.replaceState(null, "", `?date=${sel.value}`);
  if (resetSlot) state.slotIx = 0;
  if (!day) return;

  $("slotsSub").textContent = `${currentSlots().length} distinct frames · press play for the story`;

  buildFilm();
  buildSlots();
  syncPlayBtn();
  stopTimer();
  if (state.playing && currentSlots().length > 1) {
    setSlot(Math.min(state.slotIx, currentSlots().length - 1));
    playTimer = setInterval(() => setSlot(state.slotIx + 1), FRAME_MS);
  } else {
    setSlot(Math.min(state.slotIx, Math.max(0, currentSlots().length - 1)));
  }
}

function buildFilm() {
  const film = $("film"), scrub = $("scrub"), ticks = $("slotTicks");
  film.innerHTML = ""; ticks.innerHTML = "";
  const slots = currentSlots();
  scrub.max = String(Math.max(0, slots.length - 1));
  slots.forEach((s, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", `Frame ${s.time} IST${s.estimated ? ", estimated" : ""} (${i + 1} of ${slots.length})`);
    const img = document.createElement("img");
    img.loading = "lazy"; img.src = s.img; img.alt = "";
    const cap = document.createElement("span");
    cap.innerHTML = `${s.time}${s.estimated ? ' <span class="est">~est</span>' : ""}`;
    b.append(img, cap);
    b.onclick = () => { state.playing = false; stopTimer(); syncPlayBtn(); setSlot(i); };
    b.ondblclick = () => openLB(s.img, `${s.time} IST`);
    film.appendChild(b);
    const o = document.createElement("option"); o.value = String(i); o.label = s.slot;
    ticks.appendChild(o);
  });
  if (!slots.length) film.innerHTML = `<p class="empty">No frames yet today — check back after the next 3-hourly snap.</p>`;
}

function setSlot(i) {
  const frames = currentSlots();
  if (!frames.length) return;
  state.slotIx = (i + frames.length) % frames.length;
  const s = frames[state.slotIx];
  const hero = $("heroImg"), scrub = $("scrub");

  scrub.value = String(state.slotIx);
  $("scrubVal").textContent = `${s.time} IST · ${state.slotIx + 1}/${frames.length}`;
  [...$("film").children].forEach((el, k) => {
    if (el.setAttribute) el.setAttribute("aria-current", String(k === state.slotIx));
  });

  hero.src = s.img;
  hero.alt = `Radar reflectivity ${s.time} IST, frame ${state.slotIx + 1} of ${frames.length}`;
  $("viewerCap").textContent = `${state.day.date} · ${s.time} IST · ${state.slotIx + 1}/${frames.length}${s.estimated ? " · ~estimated" : ""}`;
  $("heroBadge").textContent = state.playing ? `PLAYING ${state.slotIx + 1}/${frames.length}` : "STILL";
  $("heroBadge").classList.toggle("still", !state.playing);
  hero.onclick = () => openLB(hero.src, $("viewerCap").textContent);
}

function buildSlots() {
  const wrap = $("slots");
  wrap.innerHTML = "";
  const slots = currentSlots();
  if (!slots.length) { wrap.innerHTML = `<p class="empty">No 30-min frames yet.</p>`; return; }
  slots.forEach((s, i) => {
    const f = document.createElement("figure");
    f.className = "slot reveal";
    f.style.animationDelay = `${Math.min(i * 0.05, 0.6)}s`;
    const img = document.createElement("img");
    img.loading = "lazy"; img.src = s.img; img.alt = `Radar ${s.time} IST (${i + 1} of ${slots.length})`;
    const cap = document.createElement("figcaption");
    cap.innerHTML = `<span><b>${s.slot}</b> · ${s.time}${s.estimated ? ' <span class="est">~est</span>' : ""}</span><span>#${String(i).padStart(2, "0")}</span>`;
    f.append(img, cap);
    f.tabIndex = 0;
    f.setAttribute("role", "button");
    f.setAttribute("aria-label", `Show frame ${s.time} IST full size`);
    const open = () => openLB(s.img, `${s.time} IST`);
    f.onclick = () => { state.playing = false; stopTimer(); syncPlayBtn(); setSlot(i); open(); };
    f.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); f.onclick(); } };
    wrap.appendChild(f);
  });
}

function openLB(src, cap) {
  const dlg = $("lightbox");
  $("lbImg").src = src;
  $("lbImg").alt = cap;
  $("lbCap").textContent = cap;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else window.open(src, "_blank");
}

load().catch((e) => {
  $("meta").textContent = "Failed: " + e;
  $("liveDot").classList.add("bad");
});
