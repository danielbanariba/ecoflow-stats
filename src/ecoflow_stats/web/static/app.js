// ecoflow-stats client-side enhancement, loaded as an external script
// only (strict CSP: script-src 'self'; no inline scripts, no `hx-on`
// attributes, no `js:` values — design part 3, section 4 "HTMX").
//
// Progressive enhancement only: the device selector's plain <form> +
// <button> already works with JavaScript disabled (web-ui "Switching
// the selector switches the shown data" does not require JS). When JS
// *is* available, auto-submit on change removes the extra click.
document.addEventListener("change", (event) => {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.name === "device") {
    target.form?.requestSubmit();
  }
});

// Chart initialization for `[data-chart]` elements (design part 3,
// section 4 "ECharts"): `echarts.min.js` loads separately, deferred,
// only on pages that need it (outages.html) -- `DOMContentLoaded`
// fires only after every deferred script has run, so `window.echarts`
// is guaranteed defined here regardless of script tag order in the
// document.

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function readChartJsonData(el) {
  const targetId = el.dataset.json;
  if (!targetId) {
    return null;
  }
  const node = document.getElementById(targetId);
  if (!node) {
    return null;
  }
  try {
    return JSON.parse(node.textContent);
  } catch {
    return null;
  }
}

// The heatmap endpoint (`/api/v1/outages/heatmap`) exposes two
// independent marginal distributions (hour-of-day, day-of-week), never
// a joint matrix -- rendered here as two 1-row heatmap strips, not a
// fabricated weekday-by-hour grid. Below 600px, the strips transpose
// (rows become columns) so hour/weekday labels stay legible on a phone.
function initHeatmapChart(el) {
  const data = readChartJsonData(el);
  if (!data) {
    return;
  }
  const narrow = window.innerWidth < 600;
  const hourCells = data.hour_of_day.map((count, hour) => [hour, 0, count]);
  const weekdayCells = data.day_of_week.map((count, day) => [day, 1, count]);
  let cells = hourCells.concat(weekdayCells);
  if (narrow) {
    cells = cells.map(([x, y, value]) => [y, x, value]);
  }
  const chart = window.echarts.init(el, null, { renderer: "svg" });
  chart.setOption({
    animation: !prefersReducedMotion(),
    tooltip: {},
    grid: { containLabel: true },
    xAxis: { type: "category", data: narrow ? [0, 1] : undefined },
    yAxis: { type: "category", data: narrow ? undefined : [0, 1] },
    visualMap: { min: 0, max: Math.max(1, ...cells.map((cell) => cell[2])), show: false },
    series: [{ type: "heatmap", data: cells }],
  });
  window.addEventListener("resize", () => chart.resize());
}

// The mains-strip chart is client-fetched from its own `data-src`
// (design part 3 "ECharts": the UI dogfoods the public API) and
// downsampled with `lttb` so a long range stays smooth to render.
function initMainsStripChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  const stateValue = { present: 1, unknown: 0.5, absent: 0 };
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const points = body.series.map(([start, , state]) => [start * 1000, stateValue[state]]);
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        tooltip: {},
        xAxis: { type: "time" },
        yAxis: { show: false, min: 0, max: 1 },
        series: [
          {
            type: "line",
            step: "end",
            showSymbol: false,
            sampling: "lttb",
            data: points,
          },
        ],
      });
      window.addEventListener("resize", () => chart.resize());
    })
    .catch(() => {
      el.setAttribute("data-chart-error", "true");
    });
}

// The battery charge (SoC) line is client-fetched from its own
// `data-src` (`/api/v1/battery/series`, battery requirement "Charge
// History"), matching the mains-strip chart's own fetch-then-render
// pattern. `point.soc` is `null` on a sample that never reported a
// charge reading -- left as a `null` data point rather than coerced to
// `0`, so ECharts breaks the line there instead of drawing a fabricated
// drop to empty.
function initSocLineChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const points = body.points.map((point) => [point.ts * 1000, point.soc]);
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        tooltip: {},
        xAxis: { type: "time" },
        yAxis: { min: 0, max: 100 },
        series: [
          {
            type: "line",
            showSymbol: false,
            sampling: "lttb",
            data: points,
          },
        ],
      });
      window.addEventListener("resize", () => chart.resize());
    })
    .catch(() => {
      el.setAttribute("data-chart-error", "true");
    });
}

// The battery cycle-count / state-of-health trend is client-fetched
// from its own `data-src` (`/api/v1/battery/trends`, battery
// requirement "Cycle Count and State-of-Health Trends") -- two series
// on independent y-axes, since a cycle count and a SoH percentage share
// no common scale.
function initBatteryTrendChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const days = body.days.map((day) => day.day);
      const cycles = body.days.map((day) => day.cycles_last);
      const soh = body.days.map((day) => day.soh_last);
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        tooltip: { trigger: "axis" },
        legend: {},
        grid: { containLabel: true },
        xAxis: { type: "category", data: days },
        yAxis: [
          { type: "value", name: "cycles" },
          { type: "value", name: "SoH %", min: 0, max: 100 },
        ],
        series: [
          { name: "cycles", type: "line", yAxisIndex: 0, data: cycles },
          { name: "SoH", type: "line", yAxisIndex: 1, data: soh },
        ],
      });
      window.addEventListener("resize", () => chart.resize());
    })
    .catch(() => {
      el.setAttribute("data-chart-error", "true");
    });
}

function initCharts() {
  if (typeof window.echarts === "undefined") {
    return; // this page did not load echarts.min.js
  }
  document.querySelectorAll("[data-chart]").forEach((el) => {
    if (el.dataset.chartInitialized === "true") {
      return;
    }
    if (el.dataset.chart === "heatmap") {
      initHeatmapChart(el);
    } else if (el.dataset.chart === "mains-strip") {
      initMainsStripChart(el);
    } else if (el.dataset.chart === "soc-line") {
      initSocLineChart(el);
    } else if (el.dataset.chart === "battery-trend") {
      initBatteryTrendChart(el);
    }
    el.dataset.chartInitialized = "true";
  });
}

document.addEventListener("DOMContentLoaded", initCharts);
// Design part 3 "ECharts": "charts re-init on htmx:afterSettle".
document.body.addEventListener("htmx:afterSettle", initCharts);
