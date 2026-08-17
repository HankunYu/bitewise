"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);
const tooltip = $("#tooltip");

const SLOT_NAMES = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" };
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

const state = {
  view: "today",
  date: todayStr(),
  rangeDays: 30,
  pollTimer: null,
  trendData: null,
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
  ["today", "trends", "settings"].forEach((v) => {
    $(`#view-${v}`).hidden = v !== view;
  });
  if (view === "today") loadDay();
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
  const busy = data.meals.some((m) => m.status === "pending" || m.status === "processing");
  if (busy && state.view === "today") {
    state.pollTimer = setTimeout(loadDay, 8000);
  }
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
    <div class="tiles">
      <div class="tile"><div class="label"><span class="dot" style="background:var(--series-1)"></span>蛋白质</div>
        <div class="value">${round1(data.totals.protein_g)}<small> g</small></div></div>
      <div class="tile"><div class="label"><span class="dot" style="background:var(--series-2)"></span>碳水</div>
        <div class="value">${round1(data.totals.carbs_g)}<small> g</small></div></div>
      <div class="tile"><div class="label"><span class="dot" style="background:var(--series-3)"></span>脂肪</div>
        <div class="value">${round1(data.totals.fat_g)}<small> g</small></div></div>
    </div>`;
}

function renderAdvice(data) {
  const box = $("#advice-box");
  const hasMeals = data.totals.kcal > 0;
  const btnLabel = data.advice ? "重新生成" : "生成建议";
  box.innerHTML = `
    ${data.advice ? `<div class="advice-text">${escapeHtml(data.advice)}</div>`
                  : `<div class="advice-empty">还没有当日建议${hasMeals ? "" : "(先记一顿饭)"}</div>`}
    ${hasMeals ? `<div style="margin-top:8px"><button class="link-btn" id="advice-gen">${btnLabel}</button></div>` : ""}`;
  const btn = $("#advice-gen");
  if (btn) {
    btn.addEventListener("click", async () => {
      btn.textContent = "生成中…(约半分钟)";
      btn.disabled = true;
      try {
        await apiJSON(`/api/advice/${data.date}/generate`, "POST", {});
        loadDay();
      } catch (err) {
        btn.textContent = `失败:${err.message},点击重试`;
        btn.disabled = false;
      }
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
    const statusText = meal.status === "failed"
      ? `识别失败:${escapeHtml(meal.error || "未知错误")}`
      : "识别中…";
    div.innerHTML = `
      <div class="meal-head">
        <span class="slot-badge">${slot}</span>
        <span class="meal-time">${time}</span>
        <span class="meal-status ${meal.status}">${statusText}</span>
      </div>
      ${meal.source_text ? `<div class="meal-desc">${escapeHtml(meal.source_text)}</div>` : ""}
      <div class="meal-actions">
        ${meal.status === "failed" ? `<button data-act="retry">重试</button>` : ""}
        <button data-act="delete" class="danger">删除</button>
      </div>`;
  } else {
    const items = (meal.items || [])
      .map((it) => `${it.name}${it.amount ? ` ${it.amount}` : ""} ~${Math.round(it.kcal)}kcal`)
      .join("、");
    div.innerHTML = `
      <div class="meal-head">
        <span class="slot-badge">${slot}</span>
        <span class="meal-time">${time}</span>
        <span class="meal-kcal">${Math.round(meal.kcal)} kcal</span>
      </div>
      <div class="meal-desc">${escapeHtml(meal.description)}</div>
      <div class="meal-macros">蛋白 ${round1(meal.protein_g)}g · 碳水 ${round1(meal.carbs_g)}g · 脂肪 ${round1(meal.fat_g)}g</div>
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
    <div><label>蛋白质 (g)</label><input name="protein_g" type="number" step="0.1" value="${meal.protein_g ?? 0}"></div>
    <div><label>碳水 (g)</label><input name="carbs_g" type="number" step="0.1" value="${meal.carbs_g ?? 0}"></div>
    <div><label>脂肪 (g)</label><input name="fat_g" type="number" step="0.1" value="${meal.fat_g ?? 0}"></div>
    <div><label>时间</label><input name="eaten_at" type="datetime-local" value="${meal.eaten_at.slice(0, 16)}"></div>
    <div class="full"><label>点评</label><input name="comment" value="${escapeAttr(meal.comment)}"></div>
    <div class="full"><button class="primary" data-save="1">保存</button></div>`;
  form.addEventListener("click", async (ev) => {
    if (!ev.target.dataset.save) return;
    const fields = {};
    form.querySelectorAll("input, select").forEach((input) => {
      let value = input.value;
      if (["kcal", "protein_g", "carbs_g", "fat_g"].includes(input.name)) value = Number(value);
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
  const s = await api("/api/settings");
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
}

$("#settings-save").addEventListener("click", async () => {
  const slots = {};
  document.querySelectorAll(".slot-row").forEach((row) => {
    slots[row.dataset.slot] = [
      $('input[data-k="start"]', row).value,
      $('input[data-k="end"]', row).value,
    ];
  });
  const note = $("#settings-note");
  try {
    await apiJSON("/api/settings", "PUT", {
      daily_kcal_target: $("#set-target").value,
      goal_prompt: $("#set-goal").value,
      advice_time: $("#set-advice-time").value,
      model: $("#set-model").value,
      meal_slots: slots,
    });
    note.textContent = "已保存 ✓";
    setTimeout(() => { note.textContent = ""; }, 2000);
  } catch (err) {
    note.textContent = `保存失败:${err.message}`;
  }
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

// boot
loadDay();
