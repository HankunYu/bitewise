"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const tooltip = $("#tooltip");

const SLOT_NAMES = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" };
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

// Tracked nutrients; dir "min" = daily floor to reach, "max" = daily cap to stay under.
const NUTRIENTS = [
  { key: "protein_g", name: "蛋白质", unit: "g", dir: "min" },
  { key: "carbs_g", name: "碳水", unit: "g", dir: "max" },
  { key: "fat_g", name: "脂肪", unit: "g", dir: "max" },
  { key: "fiber_g", name: "膳食纤维", unit: "g", dir: "min" },
  { key: "sugar_g", name: "糖", unit: "g", dir: "max" },
  { key: "sodium_mg", name: "钠", unit: "mg", dir: "max" },
];
const NUMERIC_MEAL_FIELDS = ["kcal", ...NUTRIENTS.map((n) => n.key)];

const state = {
  view: "today",
  date: todayStr(),
  rangeDays: 30,
  pollTimer: null,
  trendData: null,
  calMonth: todayStr().slice(0, 7),
  calSelected: null,
};

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // Session missing or token revoked: back to the login form.
    location.replace("/login");
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

function apiJSON(path, method, body) {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------------- navigation ----------------

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function switchView(view) {
  state.view = view;
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  ["today", "calendar", "trends", "settings"].forEach((v) => {
    $(`#view-${v}`).hidden = v !== view;
  });
  if (view === "today") loadDay();
  if (view === "calendar") loadCalendar();
  if (view === "trends") loadTrends();
  if (view === "settings") loadSettings();
}

// ---------------- today view ----------------

$("#day-prev").addEventListener("click", () => shiftDay(-1));
$("#day-next").addEventListener("click", () => shiftDay(1));

function shiftDay(delta) {
  const d = new Date(state.date + "T12:00:00");
  d.setDate(d.getDate() + delta);
  state.date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  loadDay();
}

async function loadDay() {
  clearTimeout(state.pollTimer);
  let data;
  try {
    data = await api(`/api/day/${state.date}`);
  } catch (err) {
    $("#meals-box").textContent = `加载失败:${err.message}`;
    return;
  }
  const d = new Date(state.date + "T12:00:00");
  const isToday = state.date === todayStr();
  $("#day-label").textContent =
    `${state.date} ${WEEKDAYS[d.getDay()]}${isToday ? " · 今天" : ""}`;
  renderSummary(data);
  renderAdvice(data);
  renderMeals(data);
  const busy = data.meals.some((m) => m.status === "pending" || m.status === "processing")
    || adviceBusy(data.advice);
  // Fast poll while jobs run; slow poll otherwise so records added from other
  // entry points (shortcut, Telegram bot) show up without a manual refresh.
  if (state.view === "today") {
    state.pollTimer = setTimeout(pollDay, busy ? 8000 : 30000);
  }
}

function pollDay() {
  // Reloading rebuilds the meal list; don't clobber an open edit form.
  if ($(".edit-form")) {
    state.pollTimer = setTimeout(pollDay, 8000);
    return;
  }
  loadDay();
}

function renderSummary(data) {
  const kcal = Math.round(data.totals.kcal);
  const remain = data.target - kcal;
  const pct = Math.min(100, data.target > 0 ? (kcal / data.target) * 100 : 0);
  const deltaHtml = remain >= 0
    ? `<span class="delta">剩余 ${Math.round(remain)} kcal</span>`
    : `<span class="delta over">▲ 超出 ${Math.round(-remain)} kcal</span>`;
  const weightHtml = data.weight != null
    ? `<div class="progress-note">今日体重:${data.weight} kg</div>` : "";
  $("#summary-card").innerHTML = `
    <div class="hero">
      <span class="big">${kcal}</span><span class="unit">/ ${data.target} kcal</span>
      ${deltaHtml}
    </div>
    <div class="progress"><div style="width:${pct}%"></div></div>
    ${weightHtml}
    <div class="nutrients">${NUTRIENTS.map((n) => nutrientRow(n, data)).join("")}</div>`;
}

function nutrientStatus(dir, ratio) {
  if (dir === "min") {
    if (ratio >= 1) return { label: "达标", cls: "ok" };
    if (ratio >= 0.7) return { label: "快够了", cls: "near" };
    return { label: "还不够", cls: "low" };
  }
  if (ratio > 1) return { label: "超出", cls: "over" };
  if (ratio >= 0.8) return { label: "接近上限", cls: "near" };
  return { label: "正常", cls: "ok" };
}

function nutrientRow(n, data) {
  const value = data.totals[n.key] || 0;
  const shown = n.key === "sodium_mg" ? Math.round(value) : round1(value);
  const target = Number(data.nutrient_targets[n.key]) || 0;
  if (!target) {
    return `<div class="nutrient">
      <span class="n-name">${n.name}</span>
      <span class="n-value">${shown}<small> ${n.unit}</small></span>
      <span class="n-status none">未设目标</span>
    </div>`;
  }
  const st = nutrientStatus(n.dir, value / target);
  const pct = Math.min(100, (value / target) * 100);
  return `<div class="nutrient">
    <span class="n-name">${n.name}</span>
    <span class="n-value">${shown}<small> / ${n.dir === "min" ? "≥" : "≤"} ${target} ${n.unit}</small></span>
    <span class="n-status ${st.cls}">${st.label}</span>
    <div class="n-bar"><div class="${st.cls}" style="width:${pct}%"></div></div>
  </div>`;
}

function adviceBusy(advice) {
  return !!advice && (advice.status === "pending" || advice.status === "processing");
}

function renderAdvice(data) {
  const box = $("#advice-box");
  const hasMeals = data.totals.kcal > 0;
  const adv = data.advice;
  const busy = adviceBusy(adv);
  const failed = !!adv && adv.status === "failed";
  const parts = [];
  if (adv && adv.text) {
    parts.push(`<div class="advice-text">${escapeHtml(adv.text)}</div>`);
  } else if (!busy && !failed) {
    parts.push(`<div class="advice-empty">还没有当日建议${hasMeals ? "" : "(先记一顿饭)"}</div>`);
  }
  if (failed) {
    parts.push(`<div class="advice-empty meal-status failed">生成失败:${escapeHtml(adv.error || "未知错误")}</div>`);
  }
  if (hasMeals) {
    const label = busy ? "生成中…" : failed ? "重试" : adv && adv.text ? "重新生成" : "生成建议";
    parts.push(`<div style="margin-top:8px"><button class="link-btn" id="advice-gen"${busy ? " disabled" : ""}>${label}</button></div>`);
  }
  box.innerHTML = parts.join("");
  const btn = $("#advice-gen");
  if (btn && !busy) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await apiJSON(`/api/advice/${data.date}/generate`, "POST", {});
      } catch (err) {
        btn.textContent = `失败:${err.message},点击重试`;
        btn.disabled = false;
        return;
      }
      loadDay();
    });
  }
}

function renderMeals(data) {
  const box = $("#meals-box");
  if (!data.meals.length) {
    box.innerHTML = `<div class="advice-empty">这一天还没有记录</div>`;
    return;
  }
  box.innerHTML = "";
  data.meals.forEach((meal) => box.appendChild(mealNode(meal)));
}

function mealNode(meal) {
  const div = document.createElement("div");
  div.className = "meal";
  const time = meal.eaten_at.slice(11, 16);
  const slot = SLOT_NAMES[meal.meal_slot] || meal.meal_slot;

  if (meal.status !== "done") {
    // A non-null kcal means this meal was estimated before: it's a recalculation.
    const isRecalc = meal.kcal != null;
    const statusText = meal.status === "failed"
      ? `${isRecalc ? "重算失败" : "识别失败"}:${escapeHtml(meal.error || "未知错误")}`
      : isRecalc ? "重新计算中…" : "识别中…";
    const desc = meal.description || meal.source_text;
    div.innerHTML = `
      <div class="meal-head">
        <span class="slot-badge">${slot}</span>
        <span class="meal-time">${time}</span>
        <span class="meal-status ${meal.status}">${statusText}</span>
      </div>
      ${desc ? `<div class="meal-desc">${escapeHtml(desc)}</div>` : ""}
      <div class="meal-actions">
        ${meal.status === "failed" ? `<button data-act="retry">重试</button>` : ""}
        <button data-act="delete" class="danger">删除</button>
      </div>`;
  } else {
    const items = (meal.items || [])
      .map((it) => `${it.name}${it.amount ? ` ${it.amount}` : ""} ~${Math.round(it.kcal)}kcal`)
      .join("、");
    const macros = [
      `蛋白 ${round1(meal.protein_g)}g`,
      `碳水 ${round1(meal.carbs_g)}g`,
      `脂肪 ${round1(meal.fat_g)}g`,
    ];
    if (meal.fiber_g != null) macros.push(`纤维 ${round1(meal.fiber_g)}g`);
    if (meal.sugar_g != null) macros.push(`糖 ${round1(meal.sugar_g)}g`);
    if (meal.sodium_mg != null) macros.push(`钠 ${Math.round(meal.sodium_mg)}mg`);
    div.innerHTML = `
      <div class="meal-head">
        <span class="slot-badge">${slot}</span>
        <span class="meal-time">${time}</span>
        <span class="meal-kcal">${Math.round(meal.kcal)} kcal</span>
      </div>
      <div class="meal-desc">${escapeHtml(meal.description)}</div>
      <div class="meal-macros">${macros.join(" · ")}</div>
      ${items ? `<div class="meal-items">${escapeHtml(items)}</div>` : ""}
      ${meal.comment ? `<div class="meal-comment">💬 ${escapeHtml(meal.comment)}</div>` : ""}
      <div class="meal-actions">
        <button data-act="edit">编辑</button>
        <button data-act="delete" class="danger">删除</button>
      </div>`;
  }

  div.addEventListener("click", async (ev) => {
    const act = ev.target.dataset && ev.target.dataset.act;
    if (!act) return;
    if (act === "delete") {
      if (!confirm("确定删除这条记录?")) return;
      await api(`/api/meals/${meal.id}`, { method: "DELETE" });
      loadDay();
    } else if (act === "retry") {
      await api(`/api/meals/${meal.id}/retry`, { method: "POST" });
      loadDay();
    } else if (act === "edit") {
      showEditForm(div, meal);
    }
  });
  return div;
}

function showEditForm(container, meal) {
  const old = $(".edit-form", container);
  if (old) { old.remove(); return; }
  const form = document.createElement("div");
  form.className = "edit-form";
  form.innerHTML = `
    <div class="full"><label>描述</label><input name="description" value="${escapeAttr(meal.description)}"></div>
    <div><label>卡路里 (kcal)</label><input name="kcal" type="number" step="1" value="${meal.kcal ?? 0}"></div>
    <div><label>餐次</label><select name="meal_slot">
      ${Object.entries(SLOT_NAMES).map(([k, v]) =>
        `<option value="${k}" ${k === meal.meal_slot ? "selected" : ""}>${v}</option>`).join("")}
    </select></div>
    ${NUTRIENTS.map((n) => `<div><label>${n.name} (${n.unit})</label>
      <input name="${n.key}" type="number" step="${n.key === "sodium_mg" ? 1 : 0.1}" value="${meal[n.key] ?? 0}"></div>`).join("")}
    <div><label>时间</label><input name="eaten_at" type="datetime-local" value="${meal.eaten_at.slice(0, 16)}"></div>
    <div class="full"><label>点评</label><input name="comment" value="${escapeAttr(meal.comment)}"></div>
    <div class="full">
      <button class="primary" data-save="1">保存</button>
      <button class="ghost" data-recalc="1" title="按更正后的描述让 AI 重新估算营养">重新计算</button>
    </div>`;
  form.addEventListener("click", async (ev) => {
    if (ev.target.dataset.recalc) {
      // Persist the fields recalculation won't recompute, then re-estimate.
      const kept = {
        description: $('input[name="description"]', form).value,
        eaten_at: $('input[name="eaten_at"]', form).value,
        meal_slot: $('select[name="meal_slot"]', form).value,
      };
      try {
        await apiJSON(`/api/meals/${meal.id}`, "PATCH", kept);
        await api(`/api/meals/${meal.id}/recalc`, { method: "POST" });
        loadDay();
      } catch (err) {
        alert(`重新计算失败:${err.message}`);
      }
      return;
    }
    if (!ev.target.dataset.save) return;
    const fields = {};
    form.querySelectorAll("input, select").forEach((input) => {
      let value = input.value;
      if (NUMERIC_MEAL_FIELDS.includes(input.name)) value = Number(value);
      fields[input.name] = value;
    });
    try {
      await apiJSON(`/api/meals/${meal.id}`, "PATCH", fields);
      loadDay();
    } catch (err) {
      alert(`保存失败:${err.message}`);
    }
  });
  container.appendChild(form);
}

// ---------------- calendar view ----------------

$("#cal-prev").addEventListener("click", () => shiftMonth(-1));
$("#cal-next").addEventListener("click", () => shiftMonth(1));

function shiftMonth(delta) {
  const d = new Date(state.calMonth + "-15T12:00:00");
  d.setMonth(d.getMonth() + delta);
  state.calMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  state.calSelected = null;
  loadCalendar();
}

async function loadCalendar() {
  let data;
  try {
    data = await api(`/api/calendar/${state.calMonth}`);
  } catch (err) {
    $("#cal-grid").textContent = `加载失败:${err.message}`;
    return;
  }
  $("#cal-label").textContent = `${state.calMonth.slice(0, 4)} 年 ${Number(state.calMonth.slice(5))} 月`;
  renderCalGrid(data);
  if (state.calSelected) {
    loadCalDetail(state.calSelected);
  } else {
    $("#cal-detail").innerHTML = `<div class="card advice-empty">点一天查看吃了什么</div>`;
  }
}

function renderCalGrid(data) {
  const byDate = {};
  data.days.forEach((d) => { byDate[d.date] = d; });
  const first = new Date(state.calMonth + "-01T12:00:00");
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const lead = (first.getDay() + 6) % 7; // Monday-first offset
  const today = todayStr();

  let html = ["一", "二", "三", "四", "五", "六", "日"]
    .map((w) => `<div class="cal-head">${w}</div>`).join("");
  html += `<div class="cal-pad"></div>`.repeat(lead);
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${state.calMonth}-${String(day).padStart(2, "0")}`;
    const rec = byDate[dateStr];
    const cls = [
      "cal-cell",
      rec ? (rec.kcal > data.target ? "over" : "ok") : "empty",
      dateStr === today ? "today" : "",
      dateStr === state.calSelected ? "selected" : "",
      dateStr > today ? "future" : "",
    ].filter(Boolean).join(" ");
    html += `<div class="${cls}" data-date="${dateStr}">
      <span class="cal-day">${day}</span>
      ${rec ? `<span class="cal-kcal">${Math.round(rec.kcal)}</span>` : ""}
    </div>`;
  }
  const grid = $("#cal-grid");
  grid.innerHTML = html;
  grid.querySelectorAll(".cal-cell").forEach((cell) => {
    cell.addEventListener("click", () => {
      state.calSelected = cell.dataset.date;
      grid.querySelectorAll(".cal-cell").forEach((c) =>
        c.classList.toggle("selected", c === cell));
      loadCalDetail(cell.dataset.date);
    });
  });
}

async function loadCalDetail(dateStr) {
  const box = $("#cal-detail");
  let data;
  try {
    data = await api(`/api/day/${dateStr}`);
  } catch (err) {
    box.innerHTML = `<div class="card advice-empty">加载失败:${escapeHtml(err.message)}</div>`;
    return;
  }
  const d = new Date(dateStr + "T12:00:00");
  const done = data.meals.filter((m) => m.status === "done");
  if (!done.length) {
    box.innerHTML = `<div class="card advice-empty">${dateStr} 没有记录</div>`;
    return;
  }
  const meals = done.map((m) => `
    <div class="cal-meal">
      ${m.image_path ? `<img class="cal-thumb" src="/${m.image_path}" loading="lazy" alt="" data-full="/${m.image_path}">` : `<div class="cal-thumb none">📝</div>`}
      <div class="cal-meal-body">
        <div class="meal-head">
          <span class="slot-badge">${SLOT_NAMES[m.meal_slot] || m.meal_slot}</span>
          <span class="meal-time">${m.eaten_at.slice(11, 16)}</span>
          <span class="meal-kcal">${Math.round(m.kcal)} kcal</span>
        </div>
        <div class="meal-desc">${escapeHtml(m.description)}</div>
        ${m.comment ? `<div class="meal-comment">💬 ${escapeHtml(m.comment)}</div>` : ""}
      </div>
    </div>`).join("");
  const advice = data.advice && data.advice.text
    ? `<div class="cal-advice"><h2>当日建议</h2><div class="advice-text">${escapeHtml(data.advice.text)}</div></div>`
    : "";
  box.innerHTML = `
    <div class="card">
      <h2>${dateStr} ${WEEKDAYS[d.getDay()]} · ${Math.round(data.totals.kcal)} / ${data.target} kcal</h2>
      ${meals}
      ${advice}
    </div>`;
  box.querySelectorAll(".cal-thumb[data-full]").forEach((img) =>
    img.addEventListener("click", () => openLightbox(img.dataset.full)));
}

// ---------------- lightbox ----------------

const lightbox = $("#lightbox");
let lightboxHideTimer = null;

function openLightbox(src) {
  clearTimeout(lightboxHideTimer);
  $("img", lightbox).src = src;
  lightbox.hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => lightbox.classList.add("show"));
}

function closeLightbox() {
  lightbox.classList.remove("show");
  document.body.style.overflow = "";
  lightboxHideTimer = setTimeout(() => {
    lightbox.hidden = true;
    $("img", lightbox).src = "";
  }, 200);
}

lightbox.addEventListener("click", closeLightbox);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !lightbox.hidden) closeLightbox();
});

// ---------------- trends view ----------------

document.querySelectorAll("#range-row button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.rangeDays = Number(btn.dataset.days);
    document.querySelectorAll("#range-row button").forEach((b) =>
      b.classList.toggle("active", b === btn));
    loadTrends();
  });
});

async function loadTrends() {
  let data;
  try {
    data = await api(`/api/trends?days=${state.rangeDays}`);
  } catch (err) {
    $("#chart-kcal").innerHTML = `<div class="chart-empty">加载失败:${err.message}</div>`;
    return;
  }
  state.trendData = data;
  renderTrendCharts();
  renderWeightList(data.weights);
}

function renderTrendCharts() {
  const data = state.trendData;
  if (!data) return;
  const dates = dateRange(data.start, data.end);
  const byDate = Object.fromEntries(data.days.map((d) => [d.date, d]));

  drawBarChart($("#chart-kcal"), dates, byDate, data.target);
  drawMacroChart($("#chart-macros"), dates, byDate);
  drawWeightChart($("#chart-weight"), dates, data.weights);

  $("#legend-macros").innerHTML = [
    ["--series-1", "蛋白质"], ["--series-2", "碳水"], ["--series-3", "脂肪"],
  ].map(([c, n]) => `<span><span class="dot" style="background:var(${c})"></span>${n}</span>`).join("");
}

function dateRange(start, end) {
  const out = [];
  const cur = new Date(start + "T12:00:00");
  const stop = new Date(end + "T12:00:00");
  while (cur <= stop) {
    out.push(`${cur.getFullYear()}-${String(cur.getMonth() + 1).padStart(2, "0")}-${String(cur.getDate()).padStart(2, "0")}`);
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

const MARGIN = { t: 12, r: 14, b: 24, l: 40 };
const CHART_H = 210;

function chartFrame(container) {
  const width = container.clientWidth || 600;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${CHART_H}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", CHART_H);
  container.innerHTML = "";
  container.appendChild(svg);
  return { svg, width, plotW: width - MARGIN.l - MARGIN.r, plotH: CHART_H - MARGIN.t - MARGIN.b };
}

function svgEl(svg, tag, attrs, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  svg.appendChild(el);
  return el;
}

function niceMax(value) {
  if (value <= 0) return 100;
  const mag = Math.pow(10, Math.floor(Math.log10(value)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (m * mag >= value) return m * mag;
  }
  return 10 * mag;
}

function drawGrid(svg, plotW, plotH, yMax, yMin = 0) {
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const value = yMin + ((yMax - yMin) * i) / ticks;
    const y = MARGIN.t + plotH - (plotH * i) / ticks;
    if (i > 0) {
      svgEl(svg, "line", {
        x1: MARGIN.l, x2: MARGIN.l + plotW, y1: y, y2: y,
        stroke: cssVar("--grid"), "stroke-width": 1,
      });
    }
    svgEl(svg, "text", {
      x: MARGIN.l - 6, y: y + 4, "text-anchor": "end",
      "font-size": 11, fill: cssVar("--muted"),
    }, formatNum(value));
  }
  svgEl(svg, "line", {
    x1: MARGIN.l, x2: MARGIN.l + plotW,
    y1: MARGIN.t + plotH, y2: MARGIN.t + plotH,
    stroke: cssVar("--baseline"), "stroke-width": 1,
  });
}

function drawXLabels(svg, dates, xOf, plotH) {
  const step = Math.max(1, Math.ceil(dates.length / 6));
  dates.forEach((date, i) => {
    if (i % step !== 0) return;
    svgEl(svg, "text", {
      x: xOf(i), y: MARGIN.t + plotH + 16, "text-anchor": "middle",
      "font-size": 11, fill: cssVar("--muted"),
    }, date.slice(5).replace("-", "/"));
  });
}

function drawBarChart(container, dates, byDate, target) {
  const { svg, plotW, plotH } = chartFrame(container);
  const values = dates.map((d) => (byDate[d] ? byDate[d].kcal : null));
  const dataMax = Math.max(target, ...values.filter((v) => v != null), 0);
  const yMax = niceMax(dataMax * 1.05);
  const slot = plotW / dates.length;
  const barW = Math.max(1, Math.min(slot - 2, 40));
  const xOf = (i) => MARGIN.l + slot * i + slot / 2;
  const yOf = (v) => MARGIN.t + plotH - (plotH * v) / yMax;

  drawGrid(svg, plotW, plotH, yMax);
  drawXLabels(svg, dates, xOf, plotH);

  values.forEach((v, i) => {
    if (v == null || v <= 0) return;
    const x = xOf(i) - barW / 2;
    const top = yOf(v);
    const h = MARGIN.t + plotH - top;
    const r = Math.min(4, barW / 2, h);
    svgEl(svg, "path", {
      d: `M${x},${MARGIN.t + plotH} v${-(h - r)} q0,${-r} ${r},${-r} h${barW - 2 * r} q${r},0 ${r},${r} v${h - r} z`,
      fill: cssVar("--series-1"), "fill-opacity": 0.92, "data-bar": i,
    });
  });

  // target reference line
  const ty = yOf(Math.min(target, yMax));
  svgEl(svg, "line", {
    x1: MARGIN.l, x2: MARGIN.l + plotW, y1: ty, y2: ty,
    stroke: cssVar("--muted"), "stroke-width": 1, "stroke-dasharray": "4 3",
  });
  svgEl(svg, "text", {
    x: MARGIN.l + plotW, y: ty - 4, "text-anchor": "end",
    "font-size": 11, fill: cssVar("--muted"),
  }, `目标 ${target}`);

  // hover hit areas
  dates.forEach((date, i) => {
    const hit = svgEl(svg, "rect", {
      x: MARGIN.l + slot * i, y: MARGIN.t, width: slot, height: plotH,
      fill: "transparent",
    });
    hit.addEventListener("pointerenter", (ev) => {
      const v = values[i];
      const bar = svg.querySelector(`[data-bar="${i}"]`);
      if (bar) bar.setAttribute("fill-opacity", 1);
      showTooltip(ev, `<div class="tt-title">${date}</div>
        <div class="tt-row"><span class="dot" style="background:var(--series-1)"></span>摄入
          <span class="tt-val">${v == null ? "无记录" : Math.round(v) + " kcal"}</span></div>
        ${v != null ? `<div class="tt-row"><span style="width:8px"></span>${v > target ? "超出" : "剩余"}
          <span class="tt-val">${Math.abs(Math.round(target - v))} kcal</span></div>` : ""}`);
    });
    hit.addEventListener("pointermove", moveTooltip);
    hit.addEventListener("pointerleave", () => {
      const bar = svg.querySelector(`[data-bar="${i}"]`);
      if (bar) bar.setAttribute("fill-opacity", 0.92);
      hideTooltip();
    });
  });
}

const MACRO_SERIES = [
  { key: "protein_g", name: "蛋白质", color: "--series-1" },
  { key: "carbs_g", name: "碳水", color: "--series-2" },
  { key: "fat_g", name: "脂肪", color: "--series-3" },
];

function drawMacroChart(container, dates, byDate) {
  const { svg, plotW, plotH } = chartFrame(container);
  const allValues = dates.flatMap((d) =>
    byDate[d] ? MACRO_SERIES.map((s) => byDate[d][s.key] || 0) : []);
  if (!allValues.length) {
    container.innerHTML = `<div class="chart-empty">暂无数据</div>`;
    return;
  }
  const yMax = niceMax(Math.max(...allValues) * 1.1);
  const xOf = dates.length === 1
    ? () => MARGIN.l + plotW / 2
    : (i) => MARGIN.l + (plotW - 46) * (i / (dates.length - 1));
  const yOf = (v) => MARGIN.t + plotH - (plotH * v) / yMax;

  drawGrid(svg, plotW, plotH, yMax);
  drawXLabels(svg, dates, xOf, plotH);

  MACRO_SERIES.forEach((series) => {
    let d = "";
    let last = null;
    dates.forEach((date, i) => {
      const v = byDate[date] ? byDate[date][series.key] : null;
      if (v == null) { d += "|"; return; }
      d += (d === "" || d.endsWith("|") ? `M` : `L`) + `${xOf(i)},${yOf(v)}`;
      last = { i, v };
    });
    d = d.replaceAll("|", "");
    if (!d) return;
    svgEl(svg, "path", {
      d, fill: "none", stroke: cssVar(series.color), "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    });
    // direct label at line end (relief for low-contrast hues)
    if (last) {
      svgEl(svg, "circle", {
        cx: xOf(last.i), cy: yOf(last.v), r: 3, fill: cssVar(series.color),
      });
      svgEl(svg, "text", {
        x: xOf(last.i) + 7, y: yOf(last.v) + 4,
        "font-size": 11, fill: cssVar("--ink-2"),
      }, series.name);
    }
  });

  attachCrosshair(svg, plotW, plotH, dates, xOf, (date) => {
    const row = byDate[date];
    if (!row) return null;
    return MACRO_SERIES.map((s) => ({
      color: s.color, name: s.name,
      value: `${round1(row[s.key])} g`, y: yOf(row[s.key] || 0),
    }));
  });
}

function drawWeightChart(container, dates, weights) {
  if (!weights.length) {
    container.innerHTML = `<div class="chart-empty">暂无体重记录,发送「体重 65.5」或在下方输入</div>`;
    return;
  }
  const { svg, plotW, plotH } = chartFrame(container);
  // latest measurement per day
  const byDate = {};
  weights.forEach((w) => { byDate[w.measured_at.slice(0, 10)] = w.weight_kg; });
  const values = Object.values(byDate);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = Math.max(0.5, (max - min) * 0.2);
  const yMin = Math.floor((min - pad) * 2) / 2;
  const yMax = Math.ceil((max + pad) * 2) / 2;
  const xOf = dates.length === 1
    ? () => MARGIN.l + plotW / 2
    : (i) => MARGIN.l + plotW * (i / (dates.length - 1));
  const yOf = (v) => MARGIN.t + plotH - (plotH * (v - yMin)) / (yMax - yMin);

  drawGrid(svg, plotW, plotH, yMax, yMin);
  drawXLabels(svg, dates, xOf, plotH);

  let d = "";
  const points = [];
  dates.forEach((date, i) => {
    const v = byDate[date];
    if (v == null) return;
    d += (d === "" ? "M" : "L") + `${xOf(i)},${yOf(v)}`;
    points.push({ i, v });
  });
  svgEl(svg, "path", {
    d, fill: "none", stroke: cssVar("--series-1"), "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  });
  points.forEach((p) => {
    svgEl(svg, "circle", {
      cx: xOf(p.i), cy: yOf(p.v), r: 3, fill: cssVar("--series-1"),
      stroke: cssVar("--surface"), "stroke-width": 2,
    });
  });

  attachCrosshair(svg, plotW, plotH, dates, xOf, (date) => {
    const v = byDate[date];
    if (v == null) return null;
    return [{ color: "--series-1", name: "体重", value: `${v} kg`, y: yOf(v) }];
  });
}

function attachCrosshair(svg, plotW, plotH, dates, xOf, rowsFor) {
  const cross = svgEl(svg, "line", {
    x1: 0, x2: 0, y1: MARGIN.t, y2: MARGIN.t + plotH,
    stroke: cssVar("--baseline"), "stroke-width": 1, visibility: "hidden",
  });
  const markers = [];
  const overlay = svgEl(svg, "rect", {
    x: MARGIN.l, y: MARGIN.t, width: plotW, height: plotH, fill: "transparent",
  });
  overlay.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    let best = 0, bestDist = Infinity;
    dates.forEach((_, i) => {
      const dist = Math.abs(xOf(i) - px);
      if (dist < bestDist) { bestDist = dist; best = i; }
    });
    const rows = rowsFor(dates[best]);
    markers.forEach((m) => m.remove());
    markers.length = 0;
    if (!rows) { cross.setAttribute("visibility", "hidden"); hideTooltip(); return; }
    cross.setAttribute("x1", xOf(best));
    cross.setAttribute("x2", xOf(best));
    cross.setAttribute("visibility", "visible");
    rows.forEach((r) => {
      markers.push(svgEl(svg, "circle", {
        cx: xOf(best), cy: r.y, r: 4, fill: cssVar(r.color),
        stroke: cssVar("--surface"), "stroke-width": 2, "pointer-events": "none",
      }));
    });
    showTooltip(ev, `<div class="tt-title">${dates[best]}</div>` + rows.map((r) =>
      `<div class="tt-row"><span class="dot" style="background:var(${r.color})"></span>${r.name}
       <span class="tt-val">${r.value}</span></div>`).join(""));
  });
  overlay.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    markers.forEach((m) => m.remove());
    markers.length = 0;
    hideTooltip();
  });
}

function renderWeightList(weights) {
  const recent = weights.slice(-5).reverse();
  $("#weight-list").innerHTML = recent.length
    ? `<ul>${recent.map((w) => `<li><span>${w.measured_at.slice(0, 16).replace("T", " ")}</span>
        <span>${w.weight_kg} kg <button class="link-btn" data-id="${w.id}">×</button></span></li>`).join("")}</ul>`
    : "";
  $("#weight-list").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/weights/${btn.dataset.id}`, { method: "DELETE" });
      loadTrends();
    });
  });
}

$("#weight-save").addEventListener("click", async () => {
  const input = $("#weight-input");
  const kg = Number(input.value);
  if (!kg) return;
  await apiJSON("/api/weights", "POST", { weight_kg: kg });
  input.value = "";
  loadTrends();
});

// ---------------- tooltip ----------------

function showTooltip(ev, html) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  moveTooltip(ev);
}

function moveTooltip(ev) {
  const pad = 14;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
  if (x + w > window.innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = ev.clientY - h - pad;
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
}

function hideTooltip() {
  tooltip.style.display = "none";
}

// ---------------- settings view ----------------

const SLOT_ORDER = ["breakfast", "lunch", "dinner"];

async function loadSettings() {
  const [s, me] = await Promise.all([api("/api/settings"), api("/api/me")]);
  $("#account-user").textContent = me.user;
  $("#set-target").value = s.daily_kcal_target;
  $("#set-goal").value = s.goal_prompt;
  $("#set-advice-time").value = s.advice_time;
  $("#set-model").value = s.model;
  let slots = {};
  try { slots = JSON.parse(s.meal_slots); } catch (e) { /* ignore */ }
  $("#slot-rows").innerHTML = SLOT_ORDER.map((key) => {
    const [start, end] = slots[key] || ["00:00", "00:00"];
    return `<div class="slot-row" data-slot="${key}">
      <span>${SLOT_NAMES[key]}</span>
      <input type="time" data-k="start" value="${start}"> –
      <input type="time" data-k="end" value="${end}">
    </div>`;
  }).join("");
  renderNutrientInputs(s.nutrient_targets);
  const job = targetsJob(s);
  if (job.status === "pending" || job.status === "processing") {
    setTargetsBusy(true);
    pollTargetsJob();
  }
}

function renderNutrientInputs(targetsJson) {
  let targets = {};
  try { targets = JSON.parse(targetsJson); } catch (e) { /* ignore */ }
  $("#nutrient-rows").innerHTML = NUTRIENTS.map((n) => `
    <div>
      <label>${n.name} ${n.dir === "min" ? "≥" : "≤"} (${n.unit})</label>
      <input type="number" min="0" step="${n.key === "sodium_mg" ? 50 : 1}"
        data-nutrient="${n.key}" value="${targets[n.key] ?? ""}">
    </div>`).join("");
}

function targetsJob(s) {
  try { return JSON.parse(s.nutrient_targets_job); } catch (e) { return { status: "idle" }; }
}

function setTargetsBusy(busy) {
  $("#targets-generate").disabled = busy;
  const note = $("#targets-note");
  note.classList.remove("error");
  note.textContent = busy ? "生成中…" : "";
}

let targetsTimer = null;

function pollTargetsJob() {
  clearTimeout(targetsTimer);
  targetsTimer = setTimeout(async () => {
    if (state.view !== "settings") return;
    let s;
    try { s = await api("/api/settings"); } catch (e) { pollTargetsJob(); return; }
    const job = targetsJob(s);
    if (job.status === "pending" || job.status === "processing") { pollTargetsJob(); return; }
    setTargetsBusy(false);
    const note = $("#targets-note");
    if (job.status === "failed") {
      note.classList.add("error");
      note.textContent = `生成失败:${job.error || "未知错误"}`;
    } else {
      renderNutrientInputs(s.nutrient_targets);
      note.textContent = "已生成,可微调后保存 ✓";
    }
  }, 3000);
}

$("#targets-generate").addEventListener("click", async () => {
  try {
    await api("/api/nutrient-targets/generate", { method: "POST" });
  } catch (err) {
    const note = $("#targets-note");
    note.classList.add("error");
    note.textContent = `生成失败:${err.message}`;
    return;
  }
  setTargetsBusy(true);
  pollTargetsJob();
});

$("#settings-save").addEventListener("click", async () => {
  const slots = {};
  document.querySelectorAll(".slot-row").forEach((row) => {
    slots[row.dataset.slot] = [
      $('input[data-k="start"]', row).value,
      $('input[data-k="end"]', row).value,
    ];
  });
  const targets = {};
  document.querySelectorAll("#nutrient-rows input").forEach((input) => {
    const v = Number(input.value);
    if (input.value.trim() !== "" && v > 0) targets[input.dataset.nutrient] = v;
  });
  const note = $("#settings-note");
  try {
    await apiJSON("/api/settings", "PUT", {
      daily_kcal_target: $("#set-target").value,
      goal_prompt: $("#set-goal").value,
      advice_time: $("#set-advice-time").value,
      model: $("#set-model").value,
      meal_slots: slots,
      nutrient_targets: targets,
    });
    note.textContent = "已保存 ✓";
    setTimeout(() => { note.textContent = ""; }, 2000);
  } catch (err) {
    note.textContent = `保存失败:${err.message}`;
  }
});

$("#logout").addEventListener("click", async () => {
  await fetch("/api/session", { method: "DELETE" });
  location.replace("/login");
});

// ---------------- misc ----------------

function round1(v) {
  return Math.round((v || 0) * 10) / 10;
}

function formatNum(v) {
  return Math.abs(v) >= 1000 ? `${Math.round(v / 100) / 10}k` : String(Math.round(v * 10) / 10);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(s) {
  return escapeHtml(s);
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.view === "trends") renderTrendCharts();
  }, 200);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.view === "trends") renderTrendCharts();
});

// Refresh immediately when the page returns to the foreground (e.g. after
// logging a meal from the shortcut or Telegram and switching back).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.view === "today" && !$(".edit-form")) {
    loadDay();
  }
});

// boot
loadDay();
