> **Write for the reviewer.** The description should read in a minute: what changes, why,
> and what to look at carefully. No tables, no decorative sections, no repeating what the
> diff already shows. If it does not fit in a few lines, the PR is too big: split it.
> Technical detail belongs in the issue or the commit message.
>
> **A diagram when it explains better than text.** For flows, execution order or
> relationships between components, a diagram replaces paragraphs: include it instead of
> describing them in prose, not in addition to it.

## 📝 Description
<!-- Summary of the change in the commit template format (templates/commit-template.en.git.txt) -->
<!-- Example: :sparkles: feat(energy): add monthly cost totals -->

**Detailed context:**
<!-- What changes and why, in a few lines. Link related issues (Closes #123) -->

---

## 🧪 Technical verification
- [ ] **Tests**: `uv run pytest -q` passes locally.
- [ ] **Lint and format**: `uv run ruff check .` and `uv run ruff format --check .` pass.
- [ ] **Dependencies**: Did `pyproject.toml` or `uv.lock` change? If so, rebuild the Docker image.
- [ ] **i18n**: Every new UI string has both an `en` and an `es` key.
- [ ] **CSP**: No inline scripts or styles; assets stay vendored and local.
- [ ] **Derived data**: Schema or derivation changes bump the version or mark derivations dirty, so existing deployments recompute.
- [ ] **Privacy**: No real device serials, API keys or personal data in fixtures, screenshots or logs.

---

## 📸 Attachments (optional)
<!-- Screenshots of the affected pages or relevant logs, if applicable -->
