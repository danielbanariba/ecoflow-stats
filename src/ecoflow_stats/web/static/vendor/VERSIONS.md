# Vendored static assets

Every asset under `static/vendor/` and `static/fonts/` is downloaded once
at implementation time and served from this application's own origin
(web-ui requirement "All Assets Served Locally"). Nothing here is
fetched from a CDN at runtime.

| Asset | Version | Source | License |
|---|---|---|---|
| `vendor/htmx.min.js` | 2.0.11 | `https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js` | 0BSD |
| `vendor/echarts.min.js` | 5.6.0 | `https://unpkg.com/echarts@5.6.0/dist/echarts.min.js` | Apache-2.0 |
| `fonts/atkinson-hyperlegible-next.woff2` | v7 (variable, weights 400–700), Latin subset | Google Fonts (`fonts.googleapis.com`/`fonts.gstatic.com`) | OFL 1.1 — `fonts/OFL.txt` |
| `fonts/atkinson-hyperlegible-mono.woff2` | v8, weight 400, Latin subset | Google Fonts | OFL 1.1 — `fonts/OFL.txt` |
| `fonts/barlow-semi-condensed-600.woff2` | v16, weight 600, Latin subset | Google Fonts | OFL 1.1 — `fonts/OFL.txt` |
| `fonts/barlow-semi-condensed-700.woff2` | v16, weight 700, Latin subset | Google Fonts | OFL 1.1 — `fonts/OFL.txt` |

The Latin subset (`U+0000-00FF`) covers every character English and
Latin American Spanish need, including `á é í ó ú ñ ü ¿ ¡`.

`static/vendor/echarts.min.js` lands with the outages page (design
delivery slice 7a / tasks-deliver Phase 15 PR ii, slice 24), the first
page that actually renders a chart.
