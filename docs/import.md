# Importing history

If you ran the `ecoflow-panel` watcher (or any tool producing the same
`samples.db`/`outages.log` pair) before switching to this app,
`ecoflow-stats import` brings that history in. It is a manual, explicit
CLI command — never run automatically, and never part of `serve`.

## What it reads

| Source | What it is | Flag |
|---|---|---|
| `samples.db` | A SQLite database of per-minute device readings, same 21-column schema this app itself uses | `--samples <path>` |
| `outages.log` | A tab-separated log of `corte` (power out) / `retorno` (power back) lines, written by the legacy watcher | `--outage-log <path>` |

At least one of `--samples` or `--outage-log` is required; you can import
just one if that's all you have. Both are read from a verified **read-only
snapshot** — the command copies each file (and `samples.db`'s `-wal`
sidecar, if present) before reading it, runs `PRAGMA quick_check` on the
copy (retrying up to 3 times), and verifies the schema version — so the
import never touches your original files and never reads a half-written
database mid-copy.

## Command

```sh
ecoflow-stats import \
  --serial <SN> \
  --source-tz <IANA timezone> \
  --samples <path to samples.db> \
  --outage-log <path to outages.log> \
  [--dry-run]
```

| Flag | Required | Meaning |
|---|---|---|
| `--serial` | yes | The device serial this history belongs to. Must be one of the serials listed in `ECOFLOW_DEVICES`. |
| `--source-tz` | yes | The IANA timezone `outages.log`'s timestamps were written in (e.g. `America/Tegucigalpa`). The log stores naive local timestamps with no timezone of its own — this flag is what makes them interpretable. |
| `--samples` | no* | Path to `samples.db`. |
| `--outage-log` | no* | Path to `outages.log`. |
| `--dry-run` | no | Runs the full import against a throwaway database and prints the report, without writing anything to the real application database. |

\* At least one of `--samples`/`--outage-log` must be given; the command
exits with an error if neither is.

Neither source has a default path. Earlier versions defaulted to
container bind-mount paths, which crashed when the command was run outside
a container — now a missing path is just a clear error.

## Local path vs. Docker volume path

Running locally (`uv run ecoflow-stats import` or the installed
`ecoflow-stats` script), use the real filesystem path on your machine:

```sh
ecoflow-stats import \
  --serial R331ZEB4SF7A0001 \
  --source-tz America/Tegucigalpa \
  --samples ~/.local/share/ecoflow/samples.db \
  --outage-log ~/.local/share/ecoflow/outages.log
```

Running inside the container (via `docker compose run`), mount the legacy
data directory read-only and use its **container** path instead — this is
the mount `compose.example.yaml` already has commented out:

```sh
docker compose run --rm \
  -v ~/.local/share/ecoflow:/import:ro \
  ecoflow-stats import \
  --serial R331ZEB4SF7A0001 \
  --source-tz America/Tegucigalpa \
  --samples /import/samples.db \
  --outage-log /import/outages.log
```

The server container (`docker compose up`) can keep running normally
while a `docker compose run` import executes — they are separate,
short-lived container invocations.

## Idempotency

The import is safe to re-run. Samples use the same insert-or-ignore
semantics as live collection: a sample for a `(device, minute)` that
already exists (from either the app's own collector or an earlier import)
is skipped, and the app's own collected sample always wins over a
same-minute legacy one. Outage-log events use a unique constraint on their
identity, so re-importing the same log changes nothing on a second run.

The printed report always separates **newly inserted** rows from
**already present** ones, so a re-run tells you plainly whether it found
anything new:

```
samples read:                            41280
samples inserted:                        41280
samples skipped (app's own sample wins):  0
samples skipped (already imported):       0
samples invalid:                          0
outage events imported:                   6
  of which newly inserted:                 6
  of which already present:                0
  of which suspected phantom:              1
outage log lines skipped as malformed:    0
covered range (UTC epoch s):              1759200000 .. 1759286400
```

Running the exact same command again reports the same `imported` total,
but with `newly inserted` at `0` and `already present` equal to the full
count — proof nothing was duplicated.

## "Suspected phantom" events

A `corte` (outage-start) line logged with `soc == 0` is flagged
`suspected_phantom`: a battery reporting exactly 0% at the instant power
cut is a common false reading from the legacy watcher, not necessarily a
real outage. A suspected-phantom event is imported and counted, but
excluded from outage statistics until a person reviews and confirms it as
real from the Outages page's legacy-event review queue — it is never
silently deleted, and never silently counted as real.

## After importing

An import marks the device's outage derivation dirty from the earliest
imported sample, so the next scheduled recompute (or
`ecoflow-stats recompute`, to force one immediately) re-derives outages
and rollups across the newly available history. Recomputed outages are
authoritative wherever the app's own live-detected data and the imported
legacy data overlap for the same device and time — the import never
creates duplicate outage events for a period the app already judged
itself.
