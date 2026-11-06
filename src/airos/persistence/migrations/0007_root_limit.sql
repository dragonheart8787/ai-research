-- 0007_root_limit: a task created beyond max_total_tasks_per_root is flagged and needs a human `limit_increase`
-- approval (bound to its contract hash) before it can become approved/queued/running.
ALTER TABLE tasks ADD COLUMN over_root_limit INTEGER NOT NULL DEFAULT 0 CHECK (over_root_limit IN (0,1));

DROP TRIGGER tasks_requires_approvals;
CREATE TRIGGER tasks_requires_approvals BEFORE UPDATE OF state ON tasks
WHEN OLD.state <> NEW.state AND NEW.state IN ('approved','queued','running')
 AND (
   EXISTS (
     SELECT 1 FROM json_each(NEW.contract_json, '$.approval_requirements') r
     WHERE NOT EXISTS (
       SELECT 1 FROM approvals a
       WHERE a.task_id = NEW.task_id AND a.status = 'granted' AND a.approval_type = json_extract(r.value, '$.type')
         AND a.task_version = NEW.contract_version AND a.contract_sha256 = NEW.contract_sha256
         AND EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = a.decided_by AND p.type = 'human')))
   OR (NEW.over_root_limit = 1 AND NOT EXISTS (
     SELECT 1 FROM approvals a
     WHERE a.task_id = NEW.task_id AND a.status = 'granted' AND a.approval_type = 'limit_increase'
       AND a.task_version = NEW.contract_version AND a.contract_sha256 = NEW.contract_sha256
       AND EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = a.decided_by AND p.type = 'human')))
 )
BEGIN SELECT RAISE(ABORT, 'task has unmet approval requirements'); END;
