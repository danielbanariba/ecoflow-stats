// ecoflow-stats client-side enhancement, loaded as an external script
// only (strict CSP: script-src 'self'; no inline scripts, no `hx-on`
// attributes, no `js:` values, no `style="..."` attributes — dynamic
// values are only ever set through CSSOM `el.style.setProperty` below,
// never through an inline `style` attribute).
//
// Progressive enhancement only, end to end: the device selector's plain
// <form> + <button>, the gap-review `hx-get` expansion, every chart's
// `<details>` fallback table, and the battery ring's server-rendered SVG
// already work with JavaScript disabled. When JS *is* available, this
// file adds auto-submit, the glass-nav scroll effect, scroll-reveal
// entrances, the ring's animated fill, and the four ECharts visuals —
// every animated branch checks `prefersReducedMotion()` and skips itself
// entirely when the user asked for less motion (design redesign01).
//
// P-02 (orchestrator QA): `.reveal` elements are fully visible by
// default in `app.css` -- only `html.js .reveal` is hidden pending its
// entrance animation. This line is the first thing the script does, so
// a JS-enabled visitor gets that hidden-then-revealed treatment and a
// no-JS (or JS-failed) visitor never does.
document.documentElement.classList.add("js");

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ---- Device selector: progressive enhancement only (works with JS off) ----
document.addEventListener("change", (event) => {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.name === "device") {
    target.form?.requestSubmit();
  }
});

// ---- Glass nav: frosted bar intensifies once the page has scrolled ----
function initNavScroll() {
  const nav = document.querySelector(".glass-nav");
  if (!nav) return;
  const onScroll = () => {
    nav.classList.toggle("is-scrolled", window.scrollY > 8);
  };
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });
}

// ---- Scroll reveal: fade-up with per-group stagger, IntersectionObserver ----
function revealGroup(items) {
  items.forEach((el, i) => {
    el.style.transitionDelay = i * 70 + "ms";
    el.classList.add("is-visible");
  });
}

function initReveal(root = document) {
  const items = Array.from(root.querySelectorAll(".reveal:not(.is-visible)"));
  if (!items.length) return;

  if (prefersReducedMotion() || !("IntersectionObserver" in window)) {
    items.forEach((el) => el.classList.add("is-visible"));
    return;
  }

  // Stagger within each reveal "group" (siblings sharing a parent), not
  // globally — a bento row staggers together, not the whole page.
  const groups = new Map();
  items.forEach((el) => {
    const parent = el.parentElement;
    if (!groups.has(parent)) groups.set(parent, []);
    groups.get(parent).push(el);
  });

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const group = groups.get(entry.target.parentElement) || [entry.target];
          revealGroup(group);
          group.forEach((el) => observer.unobserve(el));
        }
      });
    },
    { threshold: 0.01, rootMargin: "0px 0px 80px 0px" }
  );
  items.forEach((el) => observer.observe(el));
}

// ---- Activity ring: the SVG circle is already server-rendered at its
// correct final `stroke-dashoffset` (CSP-safe SVG presentation
// attributes, not an inline `style`), so the page is correct and fully
// informative with JS disabled or reduced motion requested. With motion
// allowed, this collapses the ring to empty and animates it back in via
// CSSOM once it scrolls into view — the same "fill in" moment the
// design mockup specifies, layered on top of an already-correct page
// rather than depending on JS to render the number at all. ----
function initRings() {
  if (prefersReducedMotion() || !("IntersectionObserver" in window)) return;
  const hosts = document.querySelectorAll("[data-ring-offset]");
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const host = entry.target;
        const progress = host.querySelector(".ring-progress");
        const track = progress && progress.getAttribute("stroke-dasharray");
        if (progress && track) {
          const target = host.dataset.ringOffset;
          progress.style.transitionDuration = "0.001ms";
          progress.setAttribute("stroke-dashoffset", track);
          requestAnimationFrame(() => {
            progress.style.removeProperty("transition-duration");
            progress.setAttribute("stroke-dashoffset", target);
          });
        }
        io.unobserve(host);
      });
    },
    { threshold: 0.4 }
  );
  hosts.forEach((host) => io.observe(host));
}

// ---- UI2-04 (qa-report-ui-02.md): the language switch posts a
// boosted form that swaps the entire <body>, so the button a
// keyboard user just activated is destroyed and recreated -- the
// browser's default response to a focused element leaving the DOM is
// to drop focus onto <body> itself, silently stranding that user at
// the very top of the page with no visible focus indicator at all.
// `htmx:beforeRequest` fires with the triggering element as its
// target; if that element is a lang-switch form (identified by its
// own hidden `lang` field, never by a CSS selector built from
// attacker- or request-influenced text), remember which language was
// submitted so `afterSettle` below can refocus the equivalent button
// in the freshly swapped markup -- same visual position, now simply
// carrying `aria-current="true"` instead of not. Any other boosted
// or swapped request (a plain button, a review-row form with no
// `lang` field) resets this to null, so it never fires for them. ----
let _pendingLangFocusValue = null;

document.body.addEventListener("htmx:beforeRequest", (event) => {
  const form = event.target instanceof HTMLFormElement ? event.target : null;
  const langInput = form?.querySelector('input[name="lang"]');
  _pendingLangFocusValue = langInput ? langInput.value : null;
});

function restorePendingLangFocus() {
  if (_pendingLangFocusValue === null) return;
  const value = _pendingLangFocusValue;
  _pendingLangFocusValue = null;
  for (const input of document.querySelectorAll('input[name="lang"]')) {
    if (input.value === value) {
      input.closest("form")?.querySelector('button[type="submit"]')?.focus();
      return;
    }
  }
}

// ---- Gap/legacy review: re-run reveal + ring + chart init on content
// htmx swaps in (the gap-review list, a confirmed/rejected row) so newly
// inserted markup animates in the same way the initial page load does. ----
document.body.addEventListener("htmx:afterSettle", (event) => {
  hideHtmxErrorBanner();
  initReveal(event.target instanceof Element ? event.target : document);
  initRings();
  initCharts();
  restorePendingLangFocus();
});

// ---- HTMX failure feedback (UI-15, qa-report-ui-01.md: "a failed HTMX
// mutating request shows no feedback"): `#htmx-error-banner` is already
// server-rendered with `role="alert"`/`aria-live="assertive"` and its
// own translated text in `base.html`, so this only ever toggles the
// `hidden` property and swaps `textContent` -- never an inline
// script/style (CSP-safe). A later successful swap (the handler above)
// clears a stale banner; the timeout below is the fallback for when no
// further request follows. ----
function showHtmxErrorBanner(message) {
  const banner = document.getElementById("htmx-error-banner");
  if (!banner || !message) return;
  banner.textContent = message;
  banner.hidden = false;
  window.clearTimeout(banner._hideTimer);
  banner._hideTimer = window.setTimeout(() => {
    banner.hidden = true;
  }, 6000);
}

function hideHtmxErrorBanner() {
  const banner = document.getElementById("htmx-error-banner");
  if (!banner) return;
  window.clearTimeout(banner._hideTimer);
  banner.hidden = true;
}

document.body.addEventListener("htmx:responseError", () => {
  const banner = document.getElementById("htmx-error-banner");
  showHtmxErrorBanner(banner && banner.dataset.responseMessage);
});

document.body.addEventListener("htmx:sendError", () => {
  const banner = document.getElementById("htmx-error-banner");
  showHtmxErrorBanner(banner && banner.dataset.sendMessage);
});

// ---- ECharts restyle: dark-theme colors, glass tooltip, smooth lines —
// design part 3 "ECharts" / redesign01. `echarts.min.js` loads
// separately, deferred, only on pages that need it — `DOMContentLoaded`
// fires only after every deferred script has run, so `window.echarts` is
// guaranteed defined here regardless of script tag order in the document.

const CHART_COLOR = {
  present: "#30d158",
  absent: "#ff6b4a",
  unknown: "#9a9aa0",
  cool: "#64d2ff",
  warm: "#30d158",
  solar: "#ffd60a",
  // F3 (orchestrator QA batch F): used to share `unknown` with the
  // grid chart's own axis-line/split-line gray, making the frequency
  // series invisible against its own gridlines even before the axis-
  // scale fix below. Matches `--frequency` in app.css.
  frequency: "#bf5af2",
  text: "rgba(245,245,247,0.85)",
  textMuted: "rgba(245,245,247,0.5)",
  axisLine: "rgba(255,255,255,0.08)",
  splitLine: "rgba(255,255,255,0.06)",
};

// UI-07 (qa-report-ui-01.md): the browser's own console showed a real
// `style-src 'self'` CSP violation every time a tooltip appeared --
// ECharts' default "html" tooltip renders a floating `<div>` and sets
// its background/position/`extraCssText` via `el.style.cssText = ...`,
// which is just as governed by `style-src` as a literal `style="..."`
// attribute. `renderMode: "richText"` switches the tooltip to the same
// zrender/SVG painter the chart itself already uses (`renderer: "svg"`
// below) -- the tooltip becomes drawn SVG content, not a styled DOM
// node, so there is no `style` attribute left for the CSP to block.
// The one real cost: richText tooltips are canvas/SVG-painted boxes,
// so CSS `backdrop-filter` blur (the glass look) cannot be reproduced;
// `shadowBlur`/`shadowColor` approximate the card's own elevation
// instead, and `borderColor`/`borderWidth` replace the lost border.
// `extraCssText` (the actual CSP offender) is dropped, never ported.
const CHART_TOOLTIP_RICH = { b: { fontWeight: 700 } };

const CHART_TOOLTIP_BASE = {
  renderMode: "richText",
  backgroundColor: "rgba(22,22,26,0.92)",
  borderColor: "rgba(255,255,255,0.08)",
  borderWidth: 1,
  borderRadius: 14,
  padding: [10, 14],
  shadowBlur: 24,
  shadowColor: "rgba(0,0,0,0.5)",
  textStyle: {
    color: CHART_COLOR.text,
    fontFamily: "Inter, sans-serif",
    fontSize: 12,
    rich: CHART_TOOLTIP_RICH,
  },
};

function chartGradient(colorTop, colorBottom) {
  return new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: colorTop },
    { offset: 1, color: colorBottom },
  ]);
}

// A Wh-to-kWh (or similar) division on a float counter almost never
// lands on a round number -- without this, the energy/grid chart
// tooltips leaked binary noise like "5.494109999999986" instead of a
// clean "5.49" (the same class of defect `views.py`'s own
// `_AUTONOMY_HOURS_DECIMALS` rounds away server-side for the battery
// autonomy table, visual-QA batch fix01 fix 2). `null`/`undefined`
// pass through unrounded so a chart's own `connectNulls: false` still
// sees a real gap, never a fabricated `0`.
function round2(value) {
  return value === null || value === undefined ? value : Math.round(value * 100) / 100;
}

// F3/F4 (orchestrator QA batch F): a line chart's own y-axis left at
// ECharts' default (forced to include 0 unless `scale: true`) flattens
// any series whose real range sits well away from 0 -- grid voltage
// around 120 V on a 0-140 axis, or a state-of-health percentage that
// only moves a fraction of a point on a 0-100 axis. Computes a
// [min, max] fitted to the series' own real values, with `paddingRatio`
// of headroom on each side so the line never touches the plot edge;
// a flat series (every value equal, or a single point) gets a fixed
// absolute padding instead of a zero-width range. Returns
// `{min: undefined, max: undefined}` for an all-null/empty series, so
// the caller can hand that straight to an ECharts axis and fall back
// to its own default scaling rather than a fabricated range.
function fitAxisRange(values, paddingRatio = 0.1) {
  const nums = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (nums.length === 0) {
    return { min: undefined, max: undefined };
  }
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min;
  const pad = span > 0 ? span * paddingRatio : Math.max(Math.abs(max) * paddingRatio, 1);
  return { min: min - pad, max: max + pad };
}

// F3/F4 follow-up: `fitAxisRange`'s min/max are real floating-point
// arithmetic (a padded range's edges), so ECharts' own tick values
// along that axis inherit binary-float noise ("118.75999999999999"
// instead of "118.76"). Rounds a tick's label to 2 decimal places --
// enough to show real sub-unit variation (a 0.1 Hz frequency swing,
// a fractional state-of-health point) without the noise.
function formatAxisTick(value) {
  return String(Math.round(value * 100) / 100);
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

// The heatmap endpoint exposes a real joint weekday x hour matrix
// (DATA-03, qa-report-data-01.md: replaces the two independent 1D
// marginal strips this used to render, which could never show a real
// pattern like "always on Monday afternoons" -- only that outages
// happen on Mondays sometimes and at 14:00 sometimes). Rendered as a
// true 24 (hour) x 7 (weekday) grid; `weekday_labels` comes from the
// server's own translated `weekday.N` strings, never hardcoded here.
function initHeatmapChart(el) {
  const data = readChartJsonData(el);
  if (!data || !data.matrix) {
    return;
  }
  const weekdayLabels = data.weekday_labels || [0, 1, 2, 3, 4, 5, 6];
  const cells = [];
  data.matrix.forEach((row, day) => {
    row.forEach((count, hour) => {
      cells.push([hour, day, count]);
    });
  });
  const chart = window.echarts.init(el, null, { renderer: "svg" });
  chart.setOption({
    animation: !prefersReducedMotion(),
    animationDuration: 600,
    animationEasing: "cubicOut",
    tooltip: {
      ...CHART_TOOLTIP_BASE,
      formatter: (p) => {
        const [hour, day, count] = p.data;
        return weekdayLabels[day] + " " + hour + ":00\n{b|" + count + "}";
      },
    },
    grid: { containLabel: true, left: 8, right: 8, top: 8, bottom: 24 },
    xAxis: {
      type: "category",
      data: Array.from({ length: 24 }, (_, hour) => hour),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { interval: 2 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "category",
      data: weekdayLabels,
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
    },
    visualMap: {
      min: 0,
      max: Math.max(1, ...cells.map((cell) => cell[2])),
      show: false,
      inRange: { color: ["rgba(255,107,74,0.07)", CHART_COLOR.absent] },
    },
    series: [
      {
        type: "heatmap",
        data: cells,
        itemStyle: { borderRadius: 4, borderColor: "rgba(0,0,0,0.5)", borderWidth: 1 },
        emphasis: { itemStyle: { shadowBlur: 12, shadowColor: "rgba(255,107,74,0.5)" } },
      },
    ],
  });
  window.addEventListener("resize", () => chart.resize());
}

// The mains-strip chart is client-fetched from its own `data-src`
// (design part 3 "ECharts": the UI dogfoods the public API). Rendered as
// a custom series of colored rects spanning each segment's exact time
// range, full-height, so the strip reads as "solid when present, cut on
// a confirmed outage, hatched when genuinely unknown" — never a
// height/area encoding that could be mistaken for a magnitude.
function initMainsStripChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  const fillFor = { present: CHART_COLOR.present, absent: CHART_COLOR.absent, unknown: CHART_COLOR.unknown };
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const points = body.series;
      const rangeStart = points.length ? points[0][0] * 1000 : undefined;
      const rangeEnd = points.length ? points[points.length - 1][1] * 1000 : undefined;
      const data = points.map(([start, end, state]) => ({
        value: [start * 1000, end * 1000, state],
        itemStyle:
          state === "unknown"
            ? {
                color: fillFor[state],
                opacity: 0.4,
                decal: {
                  symbol: "line",
                  dashArrayX: [1, 0],
                  dashArrayY: [4, 4],
                  rotation: Math.PI / 4,
                  color: "rgba(255,255,255,0.35)",
                },
              }
            : { color: fillFor[state], opacity: state === "present" ? 0.9 : 0.95 },
      }));
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        animationDuration: 500,
        animationEasing: "cubicOut",
        tooltip: {
          ...CHART_TOOLTIP_BASE,
          formatter: (p) => {
            const [start, end, state] = p.data.value;
            const fmt = (ts) =>
              new Date(ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
            return fmt(start) + " – " + fmt(end) + "\n{b|" + state + "}";
          },
        },
        grid: { left: 8, right: 8, top: 16, bottom: 28, containLabel: true },
        xAxis: {
          type: "time",
          min: rangeStart,
          max: rangeEnd,
          axisLine: { lineStyle: { color: CHART_COLOR.axisLine } },
          axisTick: { show: false },
          // hideOverlap: ECharts drops whichever auto-generated time ticks
          // would collide instead of letting them crowd together — needed
          // at mobile widths where adjacent date labels would overlap.
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 11, hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: { show: false, min: 0, max: 1 },
        series: [
          {
            type: "custom",
            renderItem: (params, api) => {
              const start = api.value(0);
              const end = api.value(1);
              const p1 = api.coord([start, 0]);
              const p2 = api.coord([end, 1]);
              return {
                type: "rect",
                shape: {
                  x: p1[0],
                  y: Math.min(p1[1], p2[1]),
                  width: Math.max(1, p2[0] - p1[0]),
                  height: Math.abs(p1[1] - p2[1]),
                },
                style: api.style(),
              };
            },
            data,
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
// `data-src` (battery requirement "Charge History"). `point.soc` is
// `null` on a sample that never reported a charge reading — left as a
// `null` data point rather than coerced to `0`, so ECharts breaks the
// line there instead of drawing a fabricated drop to empty.
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
        animationDuration: 900,
        animationEasing: "cubicOut",
        tooltip: {
          ...CHART_TOOLTIP_BASE,
          trigger: "axis",
          formatter: (params) => {
            const p = params[0];
            return (
              new Date(p.data[0]).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) +
              "\n{b|" + p.data[1] + "%}"
            );
          },
        },
        grid: { left: 8, right: 12, top: 16, bottom: 28, containLabel: true },
        xAxis: {
          type: "time",
          axisLine: { lineStyle: { color: CHART_COLOR.axisLine } },
          axisTick: { show: false },
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 11, hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: {
          min: 0,
          max: 100,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 11, formatter: "{value}%" },
          splitLine: { lineStyle: { color: CHART_COLOR.splitLine } },
        },
        series: [
          {
            type: "line",
            showSymbol: false,
            sampling: "lttb",
            smooth: 0.3,
            connectNulls: false,
            lineStyle: { width: 3, color: chartGradient(CHART_COLOR.cool, CHART_COLOR.warm) },
            areaStyle: { color: chartGradient("rgba(100,210,255,0.28)", "rgba(48,209,88,0.02)") },
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

// The battery cycle-count / state-of-health trend is client-fetched from
// its own `data-src` — two series on independent y-axes, since a cycle
// count and a SoH percentage share no common scale.
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
      const cyclesRange = fitAxisRange(cycles);
      const sohRange = fitAxisRange(soh);
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        animationDuration: 800,
        animationEasing: "cubicOut",
        tooltip: { ...CHART_TOOLTIP_BASE, trigger: "axis" },
        // No built-in echarts legend: the chart card's own `.legend-row`
        // (styled to match the design system) already labels the two
        // series — a second, differently-styled legend would duplicate it.
        grid: { left: 8, right: 8, top: 16, bottom: 28, containLabel: true },
        xAxis: {
          type: "category",
          data: days,
          axisLine: { lineStyle: { color: CHART_COLOR.axisLine } },
          axisTick: { show: false },
          // interval:"auto" + hideOverlap: let ECharts pick a tick density
          // that actually fits the rendered width instead of a fixed
          // interval, which overlaps into unreadable mush on mobile.
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, interval: "auto", hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: [
          {
            type: "value",
            name: "cycles",
            // F4 (orchestrator QA batch F): fitted to this range's own
            // cycle count instead of ECharts' default 0-forced scale,
            // which flattened a change of a handful of cycles against
            // a count in the hundreds.
            min: cyclesRange.min,
            max: cyclesRange.max,
            nameTextStyle: { color: CHART_COLOR.textMuted, fontSize: 10 },
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, formatter: formatAxisTick },
            splitLine: { lineStyle: { color: CHART_COLOR.splitLine } },
          },
          {
            type: "value",
            name: "SoH %",
            // F4: a 0-100 axis flattened a state-of-health line that
            // only ever moves a fraction of a percentage point within
            // one range -- fitted to this range's own values instead.
            min: sohRange.min,
            max: sohRange.max,
            nameTextStyle: { color: CHART_COLOR.textMuted, fontSize: 10 },
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, formatter: formatAxisTick },
            splitLine: { show: false },
          },
        ],
        series: [
          {
            name: "cycles",
            type: "line",
            yAxisIndex: 0,
            showSymbol: false,
            smooth: 0.3,
            lineStyle: { width: 2, color: CHART_COLOR.solar },
            data: cycles,
          },
          {
            name: "SoH",
            type: "line",
            yAxisIndex: 1,
            showSymbol: false,
            smooth: 0.3,
            lineStyle: { width: 2.5, color: CHART_COLOR.warm },
            areaStyle: { color: chartGradient("rgba(48,209,88,0.22)", "rgba(48,209,88,0)") },
            data: soh,
          },
        ],
      });
      window.addEventListener("resize", () => chart.resize());
    })
    .catch(() => {
      el.setAttribute("data-chart-error", "true");
    });
}

// The energy page's bar chart is client-fetched from its own `data-src`
// (the same `/api/v1/energy/daily` JSON the periods table itself is
// built from server-side -- never a second copy of that computation).
// Two bars per period: total energy in (every charge source summed) and
// total energy out (every discharge source summed) -- the per-source
// breakdown stays in the fallback table, this chart only shows the two
// headline flows so it reads clearly at a glance.
function initEnergyBarChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const periods = body.periods.map((period) => period.period);
      const energyIn = body.periods.map((period) =>
        round2((period.chg_ac_wh + period.chg_dc_wh + period.chg_solar_wh) / 1000),
      );
      const energyOut = body.periods.map((period) =>
        round2((period.dsg_ac_wh + period.dsg_dc_wh) / 1000),
      );
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        animationDuration: 600,
        animationEasing: "cubicOut",
        tooltip: { ...CHART_TOOLTIP_BASE, trigger: "axis" },
        grid: { left: 8, right: 8, top: 16, bottom: 28, containLabel: true },
        xAxis: {
          type: "category",
          data: periods,
          axisLine: { lineStyle: { color: CHART_COLOR.axisLine } },
          axisTick: { show: false },
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, interval: "auto", hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: {
          type: "value",
          name: "kWh",
          nameTextStyle: { color: CHART_COLOR.textMuted, fontSize: 10 },
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10 },
          splitLine: { lineStyle: { color: CHART_COLOR.splitLine } },
        },
        series: [
          {
            name: "in",
            type: "bar",
            itemStyle: { color: CHART_COLOR.present, borderRadius: 3 },
            data: energyIn,
          },
          {
            name: "out",
            type: "bar",
            itemStyle: { color: CHART_COLOR.absent, borderRadius: 3 },
            data: energyOut,
          },
        ],
      });
      window.addEventListener("resize", () => chart.resize());
    })
    .catch(() => {
      el.setAttribute("data-chart-error", "true");
    });
}

// The grid page's voltage/frequency chart is client-fetched from its
// own `data-src` (the same `/api/v1/grid/series` JSON the chart's own
// fallback table is built from, bucketed at 5 min/1 h/1 day depending
// on range length -- API-picked, never re-derived here). Voltage and
// frequency share no common scale, so this mirrors
// `initBatteryTrendChart`'s dual-y-axis pattern rather than forcing
// both onto one axis.
function initGridSeriesChart(el) {
  const src = el.dataset.src;
  if (!src) {
    return;
  }
  // F3 (orchestrator QA batch F): the server already omits/nulls a
  // bucket with no grid-present reading at all, but a per-sample
  // guard here is the literal second half of the fix -- any point at
  // or below the configured outage threshold renders as a gap
  // (`connectNulls: false` below then draws a real break), never a
  // value dragged down toward the threshold.
  const thresholdV = parseFloat(el.dataset.outageThresholdV);
  fetch(src)
    .then((response) => response.json())
    .then((body) => {
      const voltage = body.points.map((point) => {
        const v = round2(point.grid_v_avg);
        const belowThreshold = v !== null && !Number.isNaN(thresholdV) && v <= thresholdV;
        return [point.ts * 1000, belowThreshold ? null : v];
      });
      const frequency = body.points.map((point) => [point.ts * 1000, round2(point.grid_hz_avg)]);
      const voltageRange = fitAxisRange(voltage.map((p) => p[1]));
      const frequencyRange = fitAxisRange(frequency.map((p) => p[1]));
      const chart = window.echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        animation: !prefersReducedMotion(),
        animationDuration: 800,
        animationEasing: "cubicOut",
        tooltip: { ...CHART_TOOLTIP_BASE, trigger: "axis" },
        grid: { left: 8, right: 8, top: 16, bottom: 28, containLabel: true },
        xAxis: {
          type: "time",
          axisLine: { lineStyle: { color: CHART_COLOR.axisLine } },
          axisTick: { show: false },
          axisLabel: { color: CHART_COLOR.textMuted, fontSize: 11, hideOverlap: true },
          splitLine: { show: false },
        },
        yAxis: [
          {
            type: "value",
            name: "V",
            // F3: fitted to this range's own voltage instead of a
            // 0-forced axis, which compressed a real ~109-124 V range
            // into a sliver at the top of a 0-140 V scale.
            min: voltageRange.min,
            max: voltageRange.max,
            nameTextStyle: { color: CHART_COLOR.textMuted, fontSize: 10 },
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, formatter: formatAxisTick },
            splitLine: { lineStyle: { color: CHART_COLOR.splitLine } },
          },
          {
            type: "value",
            name: "Hz",
            // F3: fitted the same way -- a 0-forced frequency axis sat
            // the real ~59.95-60.05 Hz range directly on top of
            // voltage's own line at this chart's old shared 0-based
            // scale.
            min: frequencyRange.min,
            max: frequencyRange.max,
            nameTextStyle: { color: CHART_COLOR.textMuted, fontSize: 10 },
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: CHART_COLOR.textMuted, fontSize: 10, formatter: formatAxisTick },
            splitLine: { show: false },
          },
        ],
        series: [
          {
            name: "voltage",
            type: "line",
            yAxisIndex: 0,
            showSymbol: false,
            connectNulls: false,
            smooth: 0.3,
            lineStyle: { width: 2.5, color: CHART_COLOR.present },
            data: voltage,
          },
          {
            name: "frequency",
            type: "line",
            yAxisIndex: 1,
            showSymbol: false,
            connectNulls: false,
            smooth: 0.3,
            // F3: its own distinct color, matching `.legend-chip__dot
            // --frequency` -- used to share `CHART_COLOR.unknown`
            // (the same gray as the axis gridlines) with the legend.
            lineStyle: { width: 2, color: CHART_COLOR.frequency },
            data: frequency,
          },
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
    } else if (el.dataset.chart === "energy-bars") {
      initEnergyBarChart(el);
    } else if (el.dataset.chart === "grid-series") {
      initGridSeriesChart(el);
    }
    el.dataset.chartInitialized = "true";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initNavScroll();
  initReveal();
  initRings();
  initCharts();
});
// Design part 3 "ECharts": "charts re-init on htmx:afterSettle" (handled
// together with reveal/ring re-init above).
