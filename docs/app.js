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
    meta.textContent = "…";
    meta.title = `Archive unreachable: ${e.message}`;
    $("liveDot").classList.add("bad");
    const wrap = $("film");
    wrap.innerHTML = "";
    const p = document.createElement("p");
    p.className = "error";
    p.setAttribute("role", "alert");
    p.textContent = "Retry";
    const r = document.createElement("button");
    r.type = "button";
    r.textContent = "↻";
    r.setAttribute("aria-label", "Retry loading archive");
    r.onclick = () => location.reload();
    p.append(" ", r);
    wrap.append(p);
    return;
  }
  const { idx } = state;
  meta.textContent = `${shortTime(idx.updated_ist)} · ${idx.days.length}d`;
  meta.title = `${idx.radar} · ${idx.updated_ist} · ${idx.days.length} day(s)`;

  const sel = $("daySel");
  sel.innerHTML = "";
  for (const d of idx.days) {
    const o = document.createElement("option");
    o.value = d.date;
    const n = (d.frames && d.frames.length) || (d.slots && d.slots.length)
      || (d.daily_gif || d.strip ? 1 : 0);
    o.textContent = `${shortDate(d.date)} · ${n}`;
    o.title = `${d.date} · ${n} frames`;
    sel.appendChild(o);
  }
  const q = new URLSearchParams(location.search).get("date");
  if (q && [...sel.options].some((o) => o.value === q)) sel.value = q;
  else if (sel.options.length) sel.selectedIndex = 0;

  sel.onchange = () => render(true);
  // days sorted newest-first (index 0 = newest): older day is +1.
  $("prevDay").onclick = () => stepDay(1);
  $("nextDay").onclick = () => stepDay(-1);
  $("playBtn").onclick = togglePlay;
  $("scrub").oninput = (e) => { pause(); setSlot(+e.target.value); };
  $("themeBtn").onclick = () => {
    const html = document.documentElement;
    const toDark = html.dataset.theme !== "dark";
    html.dataset.theme = toDark ? "dark" : "light";
    syncThemeBtn();
    try { localStorage.setItem("kkl-theme-v2", html.dataset.theme); } catch {}
  };
  try {
    const saved = localStorage.getItem("kkl-theme-v2");
    if (saved === "dark" && document.documentElement.dataset.theme !== "dark") $("themeBtn").click();
    if (saved === "light" && document.documentElement.dataset.theme === "dark") $("themeBtn").click();
  } catch {}

  // Mobile keeps the titlebar to title + date: relocate the theme toggle
  // next to the day stepper instead.
  const narrow = matchMedia("(max-width: 720px)");
  const placeTheme = () => {
    const btn = $("themeBtn"), slot = $("themeSlot");
    if (narrow.matches) slot.append(btn);
    else document.querySelector(".top-actions").append(btn);
  };
  narrow.addEventListener?.("change", placeTheme);
  placeTheme();
  syncThemeBtn();

  // filmstrip arrow-key scrub
  $("film").addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { e.preventDefault(); pause(); setSlot(state.slotIx + 1); }
    if (e.key === "ArrowLeft") { e.preventDefault(); pause(); setSlot(state.slotIx - 1); }
  });

  // Global keys: space = play, ←/→ = frame, shift+←/→ = day.
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
    if (e.key === " ") { e.preventDefault(); togglePlay(); }
    else if (e.key === "ArrowRight" && e.shiftKey) { e.preventDefault(); stepDay(-1); }
    else if (e.key === "ArrowLeft" && e.shiftKey) { e.preventDefault(); stepDay(1); }
  });

  // Swipe on scope: horizontal = scrub, edge-swipe at ends = day.
  const scope = document.querySelector(".scope");
  let tx = null;
  scope.addEventListener("touchstart", (e) => { tx = e.touches[0].clientX; }, { passive: true });
  scope.addEventListener("touchend", (e) => {
    if (tx == null) return;
    const dx = e.changedTouches[0].clientX - tx;
    tx = null;
    if (Math.abs(dx) < 32) return;
    const n = currentSlots().length;
    if (dx < 0) {
      if (state.slotIx < n - 1) { pause(); setSlot(state.slotIx + 1); }
      else stepDay(-1);
    } else {
      if (state.slotIx > 0) { pause(); setSlot(state.slotIx - 1); }
      else stepDay(1);
    }
  }, { passive: true });

  const openHero = () => openLB($("heroImg").src, $("viewerCap").title || $("viewerCap").textContent);
  $("heroImg").addEventListener("click", openHero);
  $("heroImg").addEventListener("keydown", (e) => { if (e.key === "Enter") openHero(); });
  $("expandBtn").onclick = openHero;

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
function pause() { state.playing = false; stopTimer(); syncPlayBtn(); }
function syncPlayBtn() {
  const b = $("playBtn");
  b.setAttribute("aria-pressed", String(state.playing));
  b.setAttribute("aria-label", state.playing ? "Pause" : "Play");
  b.textContent = state.playing ? "⏸" : "▶";
}
function syncThemeBtn() {
  const b = $("themeBtn");
  const dark = document.documentElement.dataset.theme === "dark";
  b.textContent = dark ? "☀" : "●";
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

function hourOf(t) {
  const m = /^(\d{1,2}):(\d{2})/.exec(t || "");
  if (!m) return null;
  const h = +m[1], mi = +m[2];
  if (h < 0 || h > 23 || mi < 0 || mi > 59) return null;
  return { h, mi };
}
function buildDayTick() {
  const wrap = $("daySlices");
  if (!wrap) return;
  wrap.innerHTML = "";
  const hours = new Set();
  for (const s of currentSlots()) {
    const p = hourOf(s.time);
    if (p) hours.add(p.h);
  }
  for (let h = 0; h < 24; h++) {
    const i = document.createElement("i");
    if (hours.has(h)) i.className = "has";
    i.dataset.h = String(h);
    wrap.appendChild(i);
  }
}
function moveDayNeedle(t) {
  const needle = $("dayNeedle"), wrap = $("daySlices");
  if (!needle || !wrap) return;
  const p = hourOf(t);
  if (!p) { needle.style.display = "none"; return; }
  needle.style.display = "";
  const frac = (p.h * 60 + p.mi) / (24 * 60);
  needle.style.left = `calc(${(frac * 100).toFixed(2)}% - 1px)`;
  [...wrap.children].forEach((el) => {
    el.classList.toggle("now", +el.dataset.h === p.h);
  });
}
function currentSlots() {
  if (!state.day) return [];
  // All times are IST; index.json is written pre-sorted, but sort
  // defensively by full IST timestamp so the timeline can never show
  // e.g. 22:xx after 06:xx.
  const byIst = (a, b) => String(a.t_ist || a.time || "")
    .localeCompare(String(b.t_ist || b.time || ""));
  if (state.day.frames && state.day.frames.length)
    return [...state.day.frames].sort(byIst);
  if (state.day.slots && state.day.slots.length) return state.day.slots;
  // Pruned days have no frames/ but keep daily.gif/strip.jpg — fall back
  // so they don't render "No frames yet" permanently.
  if (state.day.daily_gif) {
    return [{ slot: "daily", time: "daily", t_ist: state.day.date,
              img: state.day.daily_gif, estimated: false, isDaily: true }];
  }
  if (state.day.strip) {
    return [{ slot: "strip", time: "strip", t_ist: state.day.date,
              img: state.day.strip, estimated: false, isDaily: true }];
  }
  return [];
}

function dayHasPerFrame(day) {
  return Boolean((day.frames && day.frames.length) || (day.slots && day.slots.length));
}

function render(resetSlot) {
  const sel = $("daySel");
  const day = state.idx.days.find((d) => d.date === sel.value);
  state.day = day;
  try { history.replaceState(null, "", `?date=${sel.value}`); } catch {}
  if (resetSlot) state.slotIx = 0;
  if (!day) return;

  const n = currentSlots().length;
  const pruned = !dayHasPerFrame(day) && (day.daily_gif || day.strip);
  $("filmSub").textContent = pruned ? "daily" : `${n}`;
  $("filmSub").title = pruned ? "Pruned daily summary" : `${n} frames`;

  buildDayTick();
  buildFilm();
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
    b.className = "reveal";
    b.style.animationDelay = `${Math.min(i * 0.05, 0.6)}s`;
    b.setAttribute("aria-label", `${s.time} (${i + 1} of ${slots.length})${s.estimated ? ", estimated" : ""}`);
    const img = document.createElement("img");
    img.loading = "lazy"; img.src = s.img; img.alt = "";
    const cap = document.createElement("span");
    cap.textContent = s.time;
    if (s.estimated) {
      const dot = document.createElement("span");
      dot.className = "est-dot";
      dot.setAttribute("aria-hidden", "true");
      dot.title = "estimated";
      cap.append(" ", dot);
    }
    b.append(img, cap);
    b.onclick = () => { pause(); setSlot(i); };
    b.ondblclick = () => openLB(s.img, `${s.time} · ${i + 1}/${slots.length}`);
    film.appendChild(b);
    const o = document.createElement("option"); o.value = String(i); o.label = s.slot;
    ticks.appendChild(o);
  });
  if (!slots.length) film.innerHTML = `<p class="empty">—</p>`;
}

function setSlot(i) {
  const frames = currentSlots();
  if (!frames.length) return;
  state.slotIx = (i + frames.length) % frames.length;
  const s = frames[state.slotIx];
  const hero = $("heroImg"), scrub = $("scrub");

  scrub.value = String(state.slotIx);
  scrub.setAttribute("aria-valuetext", `${s.time}, ${state.slotIx + 1} of ${frames.length}`);
  const pos = `${state.slotIx + 1}/${frames.length}`;
  $("scrubVal").textContent = `${s.time} · ${pos}`;
  [...$("film").children].forEach((el, k) => {
    if (el.setAttribute) el.setAttribute("aria-current", String(k === state.slotIx));
  });

  hero.src = s.img;
  hero.alt = `${s.time}, ${pos}`;
  $("viewerCap").textContent = s.time;
  $("viewerCap").title = `${state.day.date} · ${s.time} IST · ${pos}${s.estimated ? " · estimated" : ""}`;
  $("heroBadge").textContent = state.playing ? `▶ ${pos}` : pos;
  $("heroBadge").classList.toggle("still", !state.playing);
  moveDayNeedle(s.time);
  // Preload neighbours for instant scrub.
  for (const d of [-1, 1]) {
    const nx = frames[(state.slotIx + d + frames.length) % frames.length];
    if (nx && nx.img) { const im = new Image(); im.src = nx.img; }
  }
  const film = $("film");
  const active = film.querySelector('[aria-current="true"]');
  if (active) {
    // Scroll the strip itself, never the page — keeps the hero image
    // visible on mobile instead of yanking the viewport to the timeline.
    const target = active.offsetLeft - film.clientWidth / 2 + active.clientWidth / 2;
    film.scrollTo({ left: target, behavior: reduceMotion ? "auto" : "smooth" });
  }
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
