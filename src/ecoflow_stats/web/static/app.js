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
// section 4 "ECharts") lands with the outages page (tasks-deliver
// Phase 15 PR ii) -- no `[data-chart]` element exists yet.
