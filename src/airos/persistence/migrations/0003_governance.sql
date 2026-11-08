-- 0003_governance: budgets, reservation-aware ledger, approval amounts, DB-level gates, denial audit state.

CREATE TABLE budgets (
  budget_id       TEXT PRIMARY KEY,
  scope           TEXT NOT NULL CHECK (scope IN ('global_daily','root','role')),
  scope_ref       TEXT NOT NULL,
  limit_usd_micro INTEGER NOT NULL CHECK (limit_usd_micro >= 0),
  limit_tokens    INTEGER NOT NULL CHECK (limit_tokens >= 0),
  state           TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','exhausted','frozen')),
  created_by      TEXT NOT NULL,
  created_at_ms   INTEGER NOT NULL
);
CREATE TRIGGER budgets_base_limit_immutable BEFORE UPDATE ON budgets
WHEN NEW.limit_usd_micro <> OLD.limit_usd_micro OR NEW.limit_tokens <> OLD.limit_tokens OR NEW.budget_id <> OLD.budget_id
BEGIN SELECT RAISE(ABORT, 'base budget limits are immutable; increases require a human-approved ledger grant'); END;
CREATE TRIGGER budgets_no_delete BEFORE DELETE ON budgets
BEGIN SELECT RAISE(ABORT, 'budgets are never deleted'); END;

ALTER TABLE budget_ledger ADD COLUMN attempt INTEGER;
ALTER TABLE budget_ledger ADD COLUMN reserve_ref TEXT;
CREATE INDEX budget_ledger_task_attempt_idx ON budget_ledger(task_id, attempt, kind);
CREATE INDEX budget_ledger_budget_idx ON budget_ledger(budget_id);

ALTER TABLE approvals ADD COLUMN budget_id TEXT;
ALTER TABLE approvals ADD COLUMN amount_usd_micro INTEGER;
ALTER TABLE approvals ADD COLUMN amount_tokens INTEGER;
DROP INDEX approvals_active_uq;
CREATE UNIQUE INDEX approvals_active_uq2 ON approvals(task_id, approval_type, task_version, IFNULL(budget_id, ''))
  WHERE status IN ('pending','granted');

-- A grant/adjust must reference a budget_increase approval that is granted by a human, bound to the task's CURRENT
-- contract_version + contract_sha256, and whose budget/amounts equal the ledger entry (ADR-0009).
CREATE TRIGGER budget_ledger_grant_requires_bound_approval BEFORE INSERT ON budget_ledger
WHEN NEW.kind IN ('grant','adjust')
 AND NOT EXISTS (
   SELECT 1 FROM approvals a JOIN tasks t ON t.task_id = a.task_id
   WHERE a.approval_id = NEW.approval_id AND a.status = 'granted' AND a.approval_type = 'budget_increase'
     AND a.task_version = t.contract_version AND a.contract_sha256 = t.contract_sha256
     AND a.budget_id = NEW.budget_id AND a.amount_usd_micro = NEW.usd_micro AND a.amount_tokens = NEW.tokens
     AND EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = a.decided_by AND p.type = 'human' AND p.enabled = 1))
BEGIN SELECT RAISE(ABORT, 'budget grant requires a human approval bound to the current contract hash and exact amounts'); END;

-- No work task may enter queued/running without an OPEN task-level reservation row for its next attempt.
CREATE TRIGGER tasks_requires_reservation BEFORE UPDATE OF state ON tasks
WHEN OLD.state <> NEW.state AND NEW.state IN ('queued','running') AND NEW.kind = 'work'
 AND NOT EXISTS (
   SELECT 1 FROM budget_ledger r
   WHERE r.task_id = NEW.task_id AND r.attempt = NEW.attempt_count + 1 AND r.kind = 'reserve'
     AND r.budget_id = 'task:' || NEW.task_id
     AND NOT EXISTS (SELECT 1 FROM budget_ledger x WHERE x.kind = 'release' AND x.reserve_ref = r.entry_id))
BEGIN SELECT RAISE(ABORT, 'queued/running requires an open budget reservation'); END;

-- No task may become approved/queued/running while any required approval lacks a human grant bound to the
-- current contract_version and contract_sha256.
CREATE TRIGGER tasks_requires_approvals BEFORE UPDATE OF state ON tasks
WHEN OLD.state <> NEW.state AND NEW.state IN ('approved','queued','running')
 AND EXISTS (
   SELECT 1 FROM json_each(NEW.contract_json, '$.approval_requirements') r
   WHERE NOT EXISTS (
     SELECT 1 FROM approvals a
     WHERE a.task_id = NEW.task_id AND a.status = 'granted' AND a.approval_type = json_extract(r.value, '$.type')
       AND a.task_version = NEW.contract_version AND a.contract_sha256 = NEW.contract_sha256
       AND EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = a.decided_by AND p.type = 'human')))
BEGIN SELECT RAISE(ABORT, 'task has unmet approval requirements'); END;

-- Authorization-denial audit throttling state (bounded: one row per principal-bucket x action; one row per window).
CREATE TABLE authz_denial_state (
  key       TEXT PRIMARY KEY,
  window_id INTEGER NOT NULL,
  count     INTEGER NOT NULL,
  total     INTEGER NOT NULL
);
CREATE TABLE authz_denial_global (
  window_id  INTEGER PRIMARY KEY,
  events     INTEGER NOT NULL,
  suppressed INTEGER NOT NULL
);
