-- 0008_credit_pool: budgets gain 'pool'/'alloc' scopes (monthly agent_sdk_credit pool and per-role allocations); the ledger gains the human-only
-- 'reconciliation' kind. SQLite cannot alter CHECK constraints, so both tables are rebuilt (dependent triggers are dropped and recreated).

DROP TRIGGER tasks_requires_reservation;
DROP TRIGGER budgets_base_limit_immutable;
DROP TRIGGER budgets_no_delete;

CREATE TABLE budgets_new (
  budget_id       TEXT PRIMARY KEY,
  scope           TEXT NOT NULL CHECK (scope IN ('global_daily','root','role','pool','alloc')),
  scope_ref       TEXT NOT NULL,
  limit_usd_micro INTEGER NOT NULL CHECK (limit_usd_micro >= 0),
  limit_tokens    INTEGER NOT NULL CHECK (limit_tokens >= 0),
  state           TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','exhausted','frozen')),
  created_by      TEXT NOT NULL,
  created_at_ms   INTEGER NOT NULL
);
INSERT INTO budgets_new SELECT budget_id, scope, scope_ref, limit_usd_micro, limit_tokens, state, created_by, created_at_ms FROM budgets;
DROP TABLE budgets;
ALTER TABLE budgets_new RENAME TO budgets;
CREATE TRIGGER budgets_base_limit_immutable BEFORE UPDATE ON budgets
WHEN NEW.limit_usd_micro <> OLD.limit_usd_micro OR NEW.limit_tokens <> OLD.limit_tokens OR NEW.budget_id <> OLD.budget_id
BEGIN SELECT RAISE(ABORT, 'base budget limits are immutable; increases require a human-approved ledger grant'); END;
CREATE TRIGGER budgets_no_delete BEFORE DELETE ON budgets
BEGIN SELECT RAISE(ABORT, 'budgets are never deleted'); END;

CREATE TABLE budget_ledger_new (
  entry_id        TEXT PRIMARY KEY,
  budget_id       TEXT NOT NULL,
  task_id         TEXT REFERENCES tasks(task_id),
  run_id          TEXT REFERENCES runs(run_id),
  kind            TEXT NOT NULL CHECK (kind IN ('grant','reserve','settle','release','adjust','reconciliation')),
  usd_micro       INTEGER NOT NULL,
  tokens          INTEGER NOT NULL DEFAULT 0,
  wall_s          INTEGER NOT NULL DEFAULT 0,
  approval_id     TEXT REFERENCES approvals(approval_id),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at_ms   INTEGER NOT NULL,
  attempt         INTEGER,
  reserve_ref     TEXT,
  recorded_by     TEXT REFERENCES principals(principal_id),
  flagged         INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0,1)),   -- reconciliation: drift beyond the threshold
  CHECK (kind NOT IN ('grant','adjust') OR approval_id IS NOT NULL),
  CHECK (kind <> 'reconciliation' OR recorded_by IS NOT NULL)
);
INSERT INTO budget_ledger_new(entry_id,budget_id,task_id,run_id,kind,usd_micro,tokens,wall_s,approval_id,idempotency_key,created_at_ms,attempt,reserve_ref)
  SELECT entry_id,budget_id,task_id,run_id,kind,usd_micro,tokens,wall_s,approval_id,idempotency_key,created_at_ms,attempt,reserve_ref FROM budget_ledger;
DROP TABLE budget_ledger;
ALTER TABLE budget_ledger_new RENAME TO budget_ledger;
CREATE INDEX budget_ledger_task_attempt_idx ON budget_ledger(task_id, attempt, kind);
CREATE INDEX budget_ledger_budget_idx ON budget_ledger(budget_id);
CREATE TRIGGER budget_ledger_no_update BEFORE UPDATE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'budget_ledger is append-only'); END;
CREATE TRIGGER budget_ledger_no_delete BEFORE DELETE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'budget_ledger is append-only'); END;
CREATE TRIGGER budget_ledger_grant_requires_bound_approval BEFORE INSERT ON budget_ledger
WHEN NEW.kind IN ('grant','adjust')
 AND NOT EXISTS (
   SELECT 1 FROM approvals a JOIN tasks t ON t.task_id = a.task_id
   WHERE a.approval_id = NEW.approval_id AND a.status = 'granted' AND a.approval_type = 'budget_increase'
     AND a.task_version = t.contract_version AND a.contract_sha256 = t.contract_sha256
     AND a.budget_id = NEW.budget_id AND a.amount_usd_micro = NEW.usd_micro AND a.amount_tokens = NEW.tokens
     AND EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = a.decided_by AND p.type = 'human' AND p.enabled = 1))
BEGIN SELECT RAISE(ABORT, 'budget grant requires a human approval bound to the current contract hash and exact amounts'); END;
CREATE TRIGGER budget_ledger_reconciliation_human_only BEFORE INSERT ON budget_ledger
WHEN NEW.kind = 'reconciliation'
 AND NOT EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = NEW.recorded_by AND p.type = 'human' AND p.enabled = 1)
BEGIN SELECT RAISE(ABORT, 'a reconciliation entry can only be recorded by a human principal'); END;

CREATE TRIGGER tasks_requires_reservation BEFORE UPDATE OF state ON tasks
WHEN OLD.state <> NEW.state AND NEW.state IN ('queued','running') AND NEW.kind = 'work'
 AND NOT EXISTS (
   SELECT 1 FROM budget_ledger r
   WHERE r.task_id = NEW.task_id AND r.attempt = NEW.attempt_count + 1 AND r.kind = 'reserve'
     AND r.budget_id = 'task:' || NEW.task_id
     AND NOT EXISTS (SELECT 1 FROM budget_ledger x WHERE x.kind = 'release' AND x.reserve_ref = r.entry_id))
BEGIN SELECT RAISE(ABORT, 'queued/running requires an open budget reservation'); END;
