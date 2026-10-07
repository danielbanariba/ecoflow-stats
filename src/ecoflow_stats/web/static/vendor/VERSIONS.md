# Vendored static assets

Every asset under `static/vendor/` and `static/fonts/` is downloaded once
at implementation time and served from this application's own origin
(web-ui requirement "All Assets Served Locally"). Nothing here is
fetched from a CDN at runtime.

| Asset | Version | Source | License |
|---|---|---|---|
| `vendor/htmx.min.js` | 2.0.11 | `https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js` | 0BSD |
| `vendor/echarts.min.js` | 5.6.0 | `https://unpkg.com/echarts@5.6.0/dist/echarts.min.js` | Apache-2.0 |
| `fonts/InterVariable.woff2` | Inter 4.66 (variable, weights 100–900, roman only) | `https://rsms.me/inter/font-files/InterVariable.woff2` (official rsms/inter distribution) | SIL OFL 1.1 — `fonts/OFL.txt` |

`static/vendor/echarts.min.js` lands with the outages page (design
delivery slice 7a / tasks-deliver Phase 15 PR ii, slice 24), the first
page that actually renders a chart.

**redesign01** (dark/glass visual refresh): replaced the four Atkinson
Hyperlegible / Barlow Semi Condensed weights with a single Inter
variable file — the real app never needs italics or a separate mono
cut, so one variable woff2 (weights 100–900) covers headings, body
copy, and tabular numerals (`font-variant-numeric: tabular-nums`)
alike. Downloaded 2026-10-06, roman cut only.
