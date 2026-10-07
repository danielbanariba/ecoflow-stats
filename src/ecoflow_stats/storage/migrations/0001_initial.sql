-- Schema v1. See sdd/stats-app-v1/design-data for the authoritative
-- rationale behind every column and constraint; this file must match it.

CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
-- 'secret' (random 32 bytes, hex): signs sessions and CSRF tokens

CREATE TABLE devices (
  id                INTEGER PRIMARY KEY,
  sn                TEXT NOT NULL UNIQUE,
  name              TEXT,                 -- deviceName from device/list
  product_name      TEXT,                 -- productName from device/list, when present
  adapter_id        TEXT NOT NULL,        -- 'delta_pro', 'generic', ...
  online            INTEGER,              -- last device/list result: 1, 0 or NULL
  online_checked_at INTEGER,
  created_at        INTEGER NOT NULL
);

CREATE TABLE app_runs (                   -- lets gaps say "the app was not running"
  id           INTEGER PRIMARY KEY,
  started_at   INTEGER NOT NULL,
  last_tick_at INTEGER,                   -- updated every tick; a crash leaves the last value
  stopped_at   INTEGER,
  app_version  TEXT NOT NULL
);

CREATE TABLE samples (                    -- raw truth: never updated or deleted
  device_id      INTEGER NOT NULL REFERENCES devices(id),
  ts             INTEGER NOT NULL,        -- UTC epoch s at response receipt
  origin         INTEGER NOT NULL CHECK (origin IN (1, 2)),  -- 1 collector, 2 panel import
  soc            INTEGER,
  grid_v REAL, grid_hz REAL, ac_in_w REAL, ac_out_w REAL, in_w REAL, out_w REAL,
  solar_in_w REAL, batt_in_w REAL, batt_out_w REAL, batt_temp_c REAL,
  chg_ac_wh REAL, chg_dc_wh REAL, chg_solar_wh REAL, dsg_ac_wh REAL, dsg_dc_wh REAL,
  cycles INTEGER, soh REAL, chg_remain_min INTEGER, dsg_remain_min INTEGER,
  PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;

CREATE TABLE fetch_failures (             -- a minute without a sample while the app ran
  device_id  INTEGER NOT NULL REFERENCES devices(id),
  ts         INTEGER NOT NULL,
  outcome    TEXT NOT NULL CHECK (outcome IN
               ('timeout','network','http','api','bad_payload','internal','store')),
  code       TEXT,                        -- HTTP status or EcoFlow code, e.g. '8521'
  attempts   INTEGER NOT NULL,
  latency_ms INTEGER,
  PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;

CREATE TABLE import_runs (
  id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
  started_at INTEGER NOT NULL, finished_at INTEGER,
  source_tz TEXT, report_json TEXT
);

CREATE TABLE legacy_outages (             -- outages.log provenance; immutable except closing an open event
  id             INTEGER PRIMARY KEY,
  device_id      INTEGER NOT NULL REFERENCES devices(id),
  start_ts       INTEGER NOT NULL,
  end_ts         INTEGER,
  soc_start      INTEGER, soc_end INTEGER, logged_minutes INTEGER,
  start_line     TEXT, end_line TEXT,     -- raw lines, for audit
  source_tz      TEXT NOT NULL,
  flags          TEXT NOT NULL DEFAULT '',-- space-separated: suspected_phantom orphan_start
                                          -- orphan_end ambiguous_time nonexistent_time
  import_id      INTEGER NOT NULL REFERENCES import_runs(id),
  UNIQUE (device_id, start_ts)
);

CREATE TABLE derivations (
  device_id     INTEGER NOT NULL REFERENCES devices(id),
  name          TEXT NOT NULL CHECK (name IN ('outages', 'rollups')),
  version       INTEGER NOT NULL,         -- algorithm version that produced the rows
  params_hash   TEXT NOT NULL,            -- hash of threshold, gap threshold, tz
  checkpoint_ts INTEGER,                  -- outages: last quiescent point
  dirty_from_ts INTEGER,                  -- earliest ts needing recompute; NULL = clean
  computed_at   INTEGER NOT NULL,
  PRIMARY KEY (device_id, name)
) WITHOUT ROWID;

CREATE TABLE outage_events (              -- derived; replaced on recompute
  id                  INTEGER PRIMARY KEY,
  device_id           INTEGER NOT NULL REFERENCES devices(id),
  start_ts            INTEGER NOT NULL,   -- first below-threshold judged reading
  end_ts              INTEGER,            -- first above-threshold judged reading; NULL = ongoing
  kind                TEXT NOT NULL CHECK (kind IN ('outage', 'brief')),
  readings            INTEGER NOT NULL,   -- below-threshold judged readings
  start_uncertainty_s INTEGER,            -- start_ts minus previous judged reading
  end_uncertainty_s   INTEGER,            -- end_ts minus last below reading
  start_in_gap        INTEGER NOT NULL DEFAULT 0,
  end_in_gap          INTEGER NOT NULL DEFAULT 0,
  soc_start INTEGER, soc_end INTEGER, soc_min INTEGER, dsg_remain_min_start INTEGER,
  legacy_id           INTEGER REFERENCES legacy_outages(id),
  detector_version    INTEGER NOT NULL,
  UNIQUE (device_id, start_ts)
);

CREATE TABLE gaps (                       -- derived; replaced on recompute
  id               INTEGER PRIMARY KEY,
  device_id        INTEGER NOT NULL REFERENCES devices(id),
  start_ts         INTEGER NOT NULL,      -- last judged reading before
  end_ts           INTEGER,               -- first judged reading after; NULL = open
  cause            TEXT NOT NULL,         -- app_down timeout network http api bad_payload
                                          -- unjudgeable stale_payload mixed unknown
  state_before     TEXT, state_after TEXT,-- 'present' | 'absent'
  soc_before       INTEGER, soc_after INTEGER,
  chg_ac_wh_delta  REAL, expected_in_wh REAL,
  evidence         TEXT NOT NULL CHECK (evidence IN
                     ('likely_present', 'likely_absent', 'inconclusive')),
  failures_json    TEXT NOT NULL,         -- {"timeout": 3, "api:8521": 1, "unjudged": 2}
  detector_version INTEGER NOT NULL,
  UNIQUE (device_id, start_ts)
);

CREATE TABLE decisions (                  -- user input; survives every recompute
  id            INTEGER PRIMARY KEY,
  device_id     INTEGER NOT NULL REFERENCES devices(id),
  target        TEXT NOT NULL CHECK (target IN ('gap', 'legacy')),
  start_ts      INTEGER NOT NULL,         -- target interval when decided (the anchor)
  end_ts        INTEGER NOT NULL,
  verdict       TEXT NOT NULL CHECK (verdict IN ('outage','no_outage','real','phantom')),
  decided_at    INTEGER NOT NULL,
  superseded_by INTEGER REFERENCES decisions(id)   -- undo/change keeps the audit trail
);
CREATE INDEX decisions_active ON decisions(device_id, target, start_ts)
  WHERE superseded_by IS NULL;

CREATE TABLE daily_rollups (              -- derived; keyed by local day in the configured tz
  device_id     INTEGER NOT NULL REFERENCES devices(id),
  day           TEXT NOT NULL,            -- 'YYYY-MM-DD'
  chg_ac_wh REAL, chg_dc_wh REAL, chg_solar_wh REAL, dsg_ac_wh REAL, dsg_dc_wh REAL,
  chg_ac_est_wh REAL NOT NULL DEFAULT 0,  -- part of chg_ac_wh prorated across a gap
  energy_flags  INTEGER NOT NULL DEFAULT 0, -- 1 counter_reset, 2 implausible_jump, 4 gap_prorated
  grid_v_min REAL, grid_v_avg REAL, grid_v_max REAL,
  grid_hz_min REAL, grid_hz_avg REAL, grid_hz_max REAL,
  grid_readings INTEGER NOT NULL DEFAULT 0,
  soc_min INTEGER, soc_max INTEGER, cycles_last INTEGER, soh_last REAL, batt_temp_max REAL,
  samples INTEGER NOT NULL DEFAULT 0, judged INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (device_id, day)
) WITHOUT ROWID;

CREATE TABLE notifications (              -- alert ledger: dedupe and retry
  device_id      INTEGER NOT NULL REFERENCES devices(id),
  event_start_ts INTEGER NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('start', 'end')),
  status         TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'expired')),
  title TEXT NOT NULL, body TEXT NOT NULL,
  created_at INTEGER NOT NULL, sent_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  PRIMARY KEY (device_id, event_start_ts, kind)
) WITHOUT ROWID;
