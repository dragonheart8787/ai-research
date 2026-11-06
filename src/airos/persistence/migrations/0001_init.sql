-- 0001_init: core schema. Portable subset (see PROPOSAL 5.0). Timestamps: UTC epoch ms. Money: integer micro-USD.

CREATE TABLE principals (
  principal_id TEXT PRIMARY KEY,
  type         TEXT NOT NULL CHECK (type IN ('human','agent','system')),
  role         TEXT,
  display_name TEXT NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_at_ms INTEGER NOT NULL
);

CREATE TABLE allowed_transitions (
  from_state TEXT NOT NULL,
  to_state   TEXT NOT NULL,
  PRIMARY KEY (from_state, to_state)
);
CREATE TRIGGER allowed_transitions_no_update BEFORE UPDATE ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is read-only'); END;
CREATE TRIGGER allowed_transitions_no_delete BEFORE DELETE ON allowed_transitions
BEGIN SELECT RAISE(ABORT, 'allowed_transitions is read-only'); END;

CREATE TABLE tasks (
  task_id          TEXT PRIMARY KEY,
  idempotency_key  TEXT NOT NULL UNIQUE,
  kind             TEXT NOT NULL CHECK (kind IN ('work','objective')),
  root_task_id     TEXT NOT NULL,
  parent_task_id   TEXT REFERENCES tasks(task_id),
  depth            INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 5),
  assigned_role    TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN
    ('draft','planned','awaiting_approval','approved','queued','running','review',
     'completed','failed','timed_out','cancelled','blocked')),
  version          INTEGER NOT NULL CHECK (version >= 1),
  contract_version INTEGER NOT NULL CHECK (contract_version >= 1),
  blocked_reason   TEXT,
  blocked_from     TEXT,
  contract_json    TEXT NOT NULL CHECK (json_valid(contract_json)),
  contract_sha256  TEXT NOT NULL,
  side_effect_class TEXT NOT NULL CHECK (side_effect_class IN ('none','workspace_write','external_write','destructive')),
  attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  review_cycles    INTEGER NOT NULL DEFAULT 0 CHECK (review_cycles >= 0),
  not_before_ms    INTEGER,
  error_history_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(error_history_json)),
  created_by       TEXT NOT NULL REFERENCES principals(principal_id),
  created_at_ms    INTEGER NOT NULL,
  updated_at_ms    INTEGER NOT NULL,
  state_entered_at_ms INTEGER NOT NULL
);
CREATE INDEX tasks_state_idx  ON tasks(state, not_before_ms);
CREATE INDEX tasks_root_idx   ON tasks(root_task_id);
CREATE INDEX tasks_parent_idx ON tasks(parent_task_id);

-- Defence in depth: the database itself refuses illegal state changes and version regressions.
CREATE TRIGGER tasks_transition_guard BEFORE UPDATE OF state ON tasks
WHEN OLD.state <> NEW.state
 AND NOT EXISTS (SELECT 1 FROM allowed_transitions WHERE from_state = OLD.state AND to_state = NEW.state)
BEGIN SELECT RAISE(ABORT, 'illegal state transition'); END;

CREATE TRIGGER tasks_version_monotonic BEFORE UPDATE ON tasks
WHEN NEW.version < OLD.version OR NEW.contract_version < OLD.contract_version
BEGIN SELECT RAISE(ABORT, 'version must not decrease'); END;

CREATE TRIGGER tasks_contract_change_bumps_version BEFORE UPDATE ON tasks
WHEN NEW.contract_sha256 <> OLD.contract_sha256 AND NEW.contract_version <= OLD.contract_version
BEGIN SELECT RAISE(ABORT, 'contract change must bump contract_version'); END;

CREATE TRIGGER tasks_no_delete BEFORE DELETE ON tasks
BEGIN SELECT RAISE(ABORT, 'tasks are never deleted'); END;

CREATE TABLE task_dependencies (
  task_id            TEXT NOT NULL REFERENCES tasks(task_id),
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id),
  PRIMARY KEY (task_id, depends_on_task_id),
  CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE approvals (
  approval_id     TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES tasks(task_id),
  task_version    INTEGER NOT NULL,           -- == tasks.contract_version when requested (ADR-0009)
  contract_sha256 TEXT NOT NULL,
  approval_type   TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('pending','granted','denied','expired','revoked','invalidated')),
  requested_by    TEXT NOT NULL REFERENCES principals(principal_id),
  requested_at_ms INTEGER NOT NULL,
  expires_at_ms   INTEGER NOT NULL,
  decided_by      TEXT REFERENCES principals(principal_id),
  decided_at_ms   INTEGER,
  reason          TEXT
);
CREATE UNIQUE INDEX approvals_active_uq ON approvals(task_id, approval_type, task_version)
  WHERE status IN ('pending','granted');

-- Only a human principal may grant or deny (ADR-0008). Enforced here as well as in the service.
CREATE TRIGGER approvals_human_decider_insert BEFORE INSERT ON approvals
WHEN NEW.status IN ('granted','denied')
 AND NOT EXISTS (SELECT 1 FROM principals WHERE principal_id = NEW.decided_by AND type = 'human' AND enabled = 1)
BEGIN SELECT RAISE(ABORT, 'approvals may only be decided by a human principal'); END;
CREATE TRIGGER approvals_human_decider_update BEFORE UPDATE ON approvals
WHEN NEW.status IN ('granted','denied')
 AND NOT EXISTS (SELECT 1 FROM principals WHERE principal_id = NEW.decided_by AND type = 'human' AND enabled = 1)
BEGIN SELECT RAISE(ABORT, 'approvals may only be decided by a human principal'); END;
CREATE TRIGGER approvals_binding_immutable BEFORE UPDATE ON approvals
WHEN NEW.task_version <> OLD.task_version OR NEW.contract_sha256 <> OLD.contract_sha256
  OR NEW.task_id <> OLD.task_id OR NEW.approval_type <> OLD.approval_type
BEGIN SELECT RAISE(ABORT, 'approval binding is immutable'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals are never deleted'); END;

CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL REFERENCES tasks(task_id),
  attempt           INTEGER NOT NULL,
  agent_id          TEXT NOT NULL REFERENCES principals(principal_id),
  state             TEXT NOT NULL CHECK (state IN ('running','succeeded','failed','timed_out','cancelled','abandoned')),
  lease_owner       TEXT NOT NULL,
  lease_expires_at_ms INTEGER NOT NULL,
  heartbeat_at_ms   INTEGER NOT NULL,
  started_at_ms     INTEGER NOT NULL,
  ended_at_ms       INTEGER,
  error_class       TEXT,
  error_detail      TEXT,
  UNIQUE (task_id, attempt)
);
CREATE UNIQUE INDEX runs_one_active_per_task ON runs(task_id) WHERE state = 'running';
CREATE INDEX runs_lease_idx ON runs(state, lease_expires_at_ms);

CREATE TABLE task_events (
  seq         INTEGER PRIMARY KEY,
  event_id    TEXT NOT NULL UNIQUE,
  ts_ms       INTEGER NOT NULL,
  task_id     TEXT,
  run_id      TEXT,
  event_type  TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  actor_kind  TEXT NOT NULL,
  from_state  TEXT,
  to_state    TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  prev_hash   TEXT NOT NULL,
  entry_hash  TEXT NOT NULL UNIQUE
);
CREATE INDEX task_events_task_idx ON task_events(task_id, seq);
CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;

CREATE TABLE budget_ledger (
  entry_id        TEXT PRIMARY KEY,
  budget_id       TEXT NOT NULL,
  task_id         TEXT REFERENCES tasks(task_id),
  run_id          TEXT REFERENCES runs(run_id),
  kind            TEXT NOT NULL CHECK (kind IN ('grant','reserve','settle','release','adjust')),
  usd_micro       INTEGER NOT NULL,
  tokens          INTEGER NOT NULL DEFAULT 0,
  wall_s          INTEGER NOT NULL DEFAULT 0,
  approval_id     TEXT REFERENCES approvals(approval_id),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at_ms   INTEGER NOT NULL,
  CHECK (kind NOT IN ('grant','adjust') OR approval_id IS NOT NULL)
);
CREATE TRIGGER budget_ledger_no_update BEFORE UPDATE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'budget_ledger is append-only'); END;
CREATE TRIGGER budget_ledger_no_delete BEFORE DELETE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'budget_ledger is append-only'); END;

CREATE TABLE artifacts (
  artifact_id     TEXT PRIMARY KEY,
  sha256          TEXT NOT NULL,
  size_bytes      INTEGER NOT NULL,
  media_type      TEXT NOT NULL,
  artifact_class  TEXT NOT NULL,
  logical_name    TEXT NOT NULL,
  storage_uri     TEXT NOT NULL,
  producer_run_id TEXT REFERENCES runs(run_id),
  producer_principal_id TEXT REFERENCES principals(principal_id),
  trust_level     TEXT NOT NULL CHECK (trust_level IN ('trusted','reviewed','untrusted','quarantined')),
  is_mock         INTEGER NOT NULL CHECK (is_mock IN (0,1)),
  provenance_hash TEXT NOT NULL,
  created_at_ms   INTEGER NOT NULL,
  CHECK ((producer_run_id IS NULL) <> (producer_principal_id IS NULL))
);
CREATE UNIQUE INDEX artifacts_producer_name_uq ON artifacts(producer_run_id, logical_name)
  WHERE producer_run_id IS NOT NULL;
CREATE TRIGGER artifacts_immutable BEFORE UPDATE ON artifacts
WHEN NEW.sha256 <> OLD.sha256 OR NEW.size_bytes <> OLD.size_bytes OR NEW.artifact_class <> OLD.artifact_class
  OR NEW.logical_name <> OLD.logical_name OR NEW.storage_uri <> OLD.storage_uri
  OR NEW.provenance_hash <> OLD.provenance_hash OR NEW.created_at_ms <> OLD.created_at_ms
  OR NEW.is_mock <> OLD.is_mock
  OR IFNULL(NEW.producer_run_id,'') <> IFNULL(OLD.producer_run_id,'')
  OR IFNULL(NEW.producer_principal_id,'') <> IFNULL(OLD.producer_principal_id,'')
BEGIN SELECT RAISE(ABORT, 'artifact provenance is immutable (only trust_level may change)'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are never deleted'); END;

CREATE TABLE artifact_lineage (
  child_artifact_id  TEXT NOT NULL REFERENCES artifacts(artifact_id),
  parent_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  relation           TEXT NOT NULL CHECK (relation IN ('derived_from','input_to')),
  PRIMARY KEY (child_artifact_id, parent_artifact_id, relation),
  CHECK (child_artifact_id <> parent_artifact_id)
);
CREATE TRIGGER artifact_lineage_no_update BEFORE UPDATE ON artifact_lineage
BEGIN SELECT RAISE(ABORT, 'artifact_lineage is immutable'); END;
CREATE TRIGGER artifact_lineage_no_delete BEFORE DELETE ON artifact_lineage
BEGIN SELECT RAISE(ABORT, 'artifact_lineage is immutable'); END;
