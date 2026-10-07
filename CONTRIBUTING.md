# Contributing

Thanks for considering a contribution. This project runs strict TDD and a
hexagonal architecture with a few hard rules enforced by tests, not just
convention — read this before your first change.

## Dev setup

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repository>
cd ecoflow-stats
uv sync                 # installs runtime + dev dependency groups, creates .venv
uv run pytest -q        # run the full test suite
uv run ruff check .     # lint
uv run ruff format --check .   # formatting gate (uv run ruff format . to fix)
```

All three commands (`pytest`, `ruff check`, `ruff format --check`) must
pass before you open a pull request. There is no separate `mypy`/`pyright`
gate in this project yet.

## Strict TDD

Every behavior change follows RED → GREEN → REFACTOR:

1. Write a failing test first. Confirm it actually fails for the reason
   you expect — not from a typo or an unrelated import error. The project's
   convention for proving a RED is real: temporarily stash away the
   production code the test exercises (`git stash push -u -m <tag> -- <exact files>`),
   run the test to see it fail honestly, then restore
   (`git stash apply` and drop that exact stash slot — never a bare
   `git stash pop`, which can grab the wrong slot when more than one is
   pending).
2. Write the minimum code to make it pass (GREEN).
3. Refactor with the test suite green throughout.

### The test-value bar

A test earns its place only if you can answer, concretely: **what single
change to the implementation would make this test fail?** If the honest
answer is "editing the same literal the test asserts" (a config-echo test
that just restates a decorator's own default), or "nothing realistic turns
it red" (asserting framework behavior, or encoding a one-off incident that
cannot recur), delete it or rewrite it — it protects nothing and costs
maintenance forever. A test that drives real code through a seam and
asserts an actual observable output, call, or error survives this bar;
one where deleting a line of implementation still leaves it green does
not.

When a PR adds tests, be ready to state each new test's defect-under-test
in one sentence. This project's own commit history keeps a short table of
exactly that for review (see any `apply-progress-*` note in the project's
own change history for the pattern), and reviewers will ask for it if it's
missing.

## Architecture: hexagonal, pure core

The source tree under `src/ecoflow_stats/` is organized by capability
(`devices`, `outages`, `energy`, `battery`, `grid`, `acquisition`,
`history_import`, `notifications`, `live_status`, `web`, `storage`), not
by layer. Inside each capability package, the split that matters is:

- **Pure core** — `devices`, `outages`, `energy`, `battery`, `grid`: no
  I/O. These packages may not import `sqlite3`, `httpx`, `fastapi`,
  `starlette`, `jinja2`, or any of this project's own I/O-boundary
  packages (`storage`, `acquisition`, `web`, `notifications`,
  `history_import`, `bootstrap`, `jobs`, `cli`) — by their bare name or by
  their fully-qualified dotted path. `tests/contract/test_pure_core_imports.py`
  enforces this with an AST scan over every file in those five packages,
  including inside `if TYPE_CHECKING:` blocks (a type-checking-only guard
  offers no exemption there). If your change needs an import that trips
  this test, the logic almost certainly belongs in a `service.py` module
  instead, or the dependency needs to flow the other way (inject a port,
  don't import an adapter).
- **Services** (`service.py` modules) — the application layer: they orchestrate
  pure-core functions and call out to ports.
- **Ports** (`ports.py`) — every driven dependency (storage, the cloud
  client, the notifier, the clock) is a `typing.Protocol`. Services take
  ports through their constructor; tests pass fakes (see
  `tests/fakes.py`) or a real SQLite database in `tmp_path`.
- **Adapters** — the only things allowed to import `sqlite3`/`httpx`/etc.:
  `storage/*` (every SQL statement lives here, one module per table),
  `acquisition/ecoflow_client.py`, `notifications/ntfy.py`, `clock.py`.
- **`bootstrap.py`** is the single composition root: it is the only place
  that wires adapters into services into the app/jobs.

This means: a contributor adding a device model touches only `devices/`
(see [`docs/adding-a-model.md`](docs/adding-a-model.md)); a reviewer finds
every SQL statement under `storage/`; the core is testable with zero I/O.

### The import contract (`history_import`)

Importing legacy history follows the same boundary: `history_import/`
reads from an already-verified, already-copied snapshot path (never the
live legacy files directly — `panel_samples.make_snapshot` owns the
copy-and-verify step), and writes through the same `storage/` ports the
live collector uses (`SampleStore`, `LegacyStore`). It never opens a
database connection of its own outside those ports, and it never
re-derives outages itself — it only marks the relevant derivation dirty so
the next scheduled recompute picks the new history up.

## CSP and local assets

The app serves a strict Content-Security-Policy (no inline scripts, no
`'unsafe-eval'`) and never loads anything from an external host:
`static/vendor/htmx.min.js` and `static/vendor/echarts.min.js` are
vendored, not CDN-loaded, and self-hosted WOFF2 fonts live under
`static/fonts/`. `tests/contract/test_local_assets_only.py` scans every
template and stylesheet for an `http(s)://` reference and fails the build
if one is found. If you add a new third-party script, font, or stylesheet,
vendor it under `static/vendor/` (or `static/fonts/`) rather than
reference a CDN.

## i18n parity

UI strings live in two flat JSON catalogs, `src/ecoflow_stats/web/i18n/en.json`
and `es.json`. Both files must always have exactly the same key set, and
every `t('...')` key referenced from a template must exist in both —
`tests/contract/test_translation_parity.py` enforces this. Add a new
string to both catalogs in the same commit that references it; a
translation you cannot write accurately is still better covered by a
placeholder in both files than by a missing key in one.

## No secrets, no real device data

- `tests/contract/test_no_secrets_exposed.py` checks that configured
  secrets (access key, secret key, password, ntfy topic/token) never reach
  a rendered page, an API response, or a captured log line. Templates
  receive a `PublicSettings` view model, never the full `Settings` —
  follow that pattern for any new template context.
- `tests/contract/test_no_real_serials.py` scans the fixtures tree for
  anything matching a real EcoFlow serial pattern. **Never commit a real
  device serial, a real API key, or a real payload captured from an actual
  device.** Every fixture under `tests/fixtures/` is synthetic — built by
  hand or via `tests/builders.py`, shaped like real EcoFlow data but with
  invented values. When adding a new device model, write its payload
  fixture the same way (see
  [`docs/adding-a-model.md`](docs/adding-a-model.md)).

## Commit conventions

Gitmoji plus Conventional Commits (`:sparkles: feat: ...`, `:bug: fix: ...`,
`:memo: docs: ...`), following
[`templates/commit-template.en.git.txt`](templates/commit-template.en.git.txt),
one coherent change per commit with tests and docs
alongside the behavior they cover, not split out into separate
"add tests" or "update docs" commits. No AI-tool attribution or
co-authorship credit of any kind in any commit message — this is
enforced by a commit hook.

## Opening a change

1. Branch from the current default branch.
2. Make your change with tests first, keeping the suite green.
3. Run the full verification gate: `uv run pytest -q`, `uv run ruff check .`,
   `uv run ruff format --check .`.
4. Open a pull request describing what changed and why, using
   [`.github/pull_request_template.md`](.github/pull_request_template.md).
