# Configuration

Every setting is an environment variable, validated on startup by
`src/ecoflow_stats/config.py`. A misconfigured deployment reports every
failing variable at once (never just the first one found) and the process
exits with code 2 — nothing partially starts.

Any variable listed below as a **secret** also accepts a `<NAME>_FILE`
variant pointing at a file whose (trimmed) contents are used instead —
useful for Docker secrets. For example, `ECOFLOW_ACCESS_KEY_FILE=/run/secrets/access_key`
works in place of `ECOFLOW_ACCESS_KEY`. This applies to exactly three
variables: `ECOFLOW_ACCESS_KEY`, `ECOFLOW_SECRET_KEY`, and
`ECOFLOW_STATS_PASSWORD`.

## Required

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_ACCESS_KEY` | — (required) | non-empty string (secret) | Your EcoFlow IoT Open API access key. |
| `ECOFLOW_SECRET_KEY` | — (required) | non-empty string (secret) | Your EcoFlow IoT Open API secret key, used to HMAC-sign every cloud request. |
| `ECOFLOW_DEVICES` | — (required) | comma-separated `SERIAL[:adapter_id]` entries; each serial matches `[A-Z0-9]{8,32}`, each adapter id (if given) matches `[a-z][a-z0-9_]*` and must be a registered adapter | Which EcoFlow device(s) to poll. Example: `ECOFLOW_DEVICES=R331ZEB4SF7A0001` or `R331ZEB4SF7A0001:delta_pro,R331ZEB4SF7A0002`. An omitted `:adapter_id` resolves automatically (see [`docs/adding-a-model.md`](adding-a-model.md)). |

## Server

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_HOST` | `0.0.0.0` | any string | Interface the web server binds to. |
| `ECOFLOW_STATS_PORT` | `8080` | `1`–`65535` | Port the web server listens on. |
| `ECOFLOW_STATS_DATA_DIR` | `/data` | an existing path that is not a file | Where the SQLite database and import snapshots are written. |
| `ECOFLOW_STATS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Root logger level. Secrets and device serials are always redacted from log output regardless of level. |
| `ECOFLOW_API_HOST` | `https://api.ecoflow.com` | an `https://` URL whose host is `ecoflow.com` or ends in `.ecoflow.com` | The EcoFlow cloud API base URL. Only needed if EcoFlow assigns your account a regional host (e.g. `api-e.ecoflow.com`). |

## Access and security

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_PASSWORD` | unset | at least 8 characters (secret) | When set, every page and API route except `/healthz`, `/login` and `/static/*` requires a signed session cookie. When unset, the app instead relies on the LAN guard below, and a warning is logged at startup. |
| `ECOFLOW_STATS_ALLOWED_NETWORKS` | `127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,169.254.0.0/16,fc00::/7,fe80::/10` | comma-separated CIDR networks | **Only enforced when no password is configured.** Client addresses outside this list get `403` instead of the app. The default covers loopback, private, CGNAT and link-local ranges. |
| `ECOFLOW_STATS_TRUSTED_PROXIES` | empty (no proxy trusted) | comma-separated CIDR networks | Needed only behind a reverse proxy. When set, uvicorn trusts `X-Forwarded-For`/`X-Forwarded-Proto` **only from these addresses**, so the LAN guard and login throttle see the real client address rather than the proxy's. When unset, no `X-Forwarded-For` header is trusted from anyone. |
| `ECOFLOW_STATS_SESSION_DAYS` | `30` | `1`–`365` | How long a login session cookie stays valid. |

**Security notes:**

- Sessions are signed with an HMAC key derived from the configured password
  plus a persisted revocation generation counter. Changing the password, or
  clicking **Logout**, invalidates *every* previously issued session
  cookie — not just the one in the current browser — immediately, and this
  revocation survives a process restart.
- CSRF protection (a double-submit token plus `Origin`/`Sec-Fetch-Site`
  checks) is always enforced on every state-changing request, independent
  of whether a password is configured.
- A failed login is throttled: at most 10 failures per 5 minutes per client
  address, after which the route returns `429` with `Retry-After` instead
  of accepting more attempts.
- If this app sits behind a reverse proxy (nginx, Traefik, Caddy), set
  `ECOFLOW_STATS_TRUSTED_PROXIES` to that proxy's address or subnet.
  Without it, every request appears to originate from the proxy's own
  address, which defeats both the LAN guard and the login throttle.

## Outages and grid

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_OUTAGE_THRESHOLD_V` | `50` | `1`–`200` | AC input voltage at or below which the grid is judged absent. Also the threshold the Grid page's summary and chart reference. |
| `ECOFLOW_STATS_GAP_THRESHOLD` | `150` (seconds) | `90`–`3600` | A stretch of more than this many seconds between judged readings becomes a gap (needing review), not an inferred outage. |
| `ECOFLOW_STATS_STALE_THRESHOLD` | `3 ×` the configured poll interval (seconds) | poll interval – `86400` | How old the latest sample must be before the Overview page marks it stale. |

## Energy and tariff

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_TARIFF` | unset | a non-negative decimal | Flat price per kWh, applied to AC charge-in energy to compute cost on the Energy page. With no tariff configured, kWh totals still show and cost is reported as unavailable — never a fabricated zero. |
| `ECOFLOW_STATS_CURRENCY` | empty string | at most 8 characters | Currency label shown next to computed costs (e.g. `USD`, `HNL`). Purely cosmetic; does not affect the computed amount. |

## Polling

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_POLL_INTERVAL` | `60` (seconds) | `30`–`3600` | How often the collector polls the EcoFlow cloud for each configured device. |
| `ECOFLOW_STATS_POLL_OFFSET` | `30` (seconds) | `0`–`59` | Second-of-minute the collector aligns its polls to (kept apart from a legacy watcher's own schedule, if one is still running in parallel). |

## Timezone and language

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_TZ` | the `TZ` environment variable, else `UTC` | a known IANA timezone name | Local timezone used to cut calendar days for energy accounting, rollups, and displayed dates/times. |
| `ECOFLOW_STATS_DEFAULT_LANG` | `en` | `en` or `es` | Fallback UI language when no `lang` cookie or browser `Accept-Language` is negotiated. |
| `ECOFLOW_STATS_NOTIFY_LANG` | `en` | `en` or `es` | Language used for ntfy push notification text (independent of the UI language). |

## Notifications (optional)

| Variable | Default | Valid range | Effect |
|---|---|---|---|
| `ECOFLOW_STATS_NTFY_TOPIC` | unset (notifications disabled) | non-empty string (secret — anyone who knows it can read your alerts) | [ntfy](https://ntfy.sh) topic to publish outage start/end alerts to. Setting this is what turns notifications on at all. |
| `ECOFLOW_STATS_NTFY_URL` | `https://ntfy.sh` | an `http://` or `https://` URL | ntfy server base URL, for a self-hosted ntfy instance. |
| `ECOFLOW_STATS_NTFY_TOKEN` | unset | non-empty string (secret) | Bearer token for an authenticated ntfy topic, if required. |

## Reserved: `.env`

This repository does not ship a committed `.env` file; copy
`.env.example` to `.env` (or set variables directly in your deployment
environment) before starting the app. `compose.example.yaml` reads its
environment from `.env` via `env_file:`.
