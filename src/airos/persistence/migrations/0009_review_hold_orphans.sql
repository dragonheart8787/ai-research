-- 0009: credit-exhausted attempts do not count toward the retry limit; billing-integrity review hold; orphaned artifacts.
ALTER TABLE tasks ADD COLUMN uncounted_attempts INTEGER NOT NULL DEFAULT 0 CHECK (uncounted_attempts >= 0);
ALTER TABLE tasks ADD COLUMN review_hold INTEGER NOT NULL DEFAULT 0 CHECK (review_hold IN (0,1));

ALTER TABLE artifacts ADD COLUMN orphaned INTEGER NOT NULL DEFAULT 0 CHECK (orphaned IN (0,1));
ALTER TABLE artifacts ADD COLUMN orphaned_at_ms INTEGER;
ALTER TABLE artifacts ADD COLUMN orphaned_reason TEXT;
ALTER TABLE artifacts ADD COLUMN removed_at_ms INTEGER;
ALTER TABLE artifacts ADD COLUMN removed_by TEXT REFERENCES principals(principal_id);
-- orphaning is one-way; removal is only possible for an orphaned artifact and only by a human principal
CREATE TRIGGER artifacts_orphan_one_way BEFORE UPDATE OF orphaned ON artifacts
WHEN OLD.orphaned = 1 AND NEW.orphaned = 0
BEGIN SELECT RAISE(ABORT, 'an orphaned artifact can never be un-orphaned'); END;
CREATE TRIGGER artifacts_removal_rules BEFORE UPDATE OF removed_at_ms ON artifacts
WHEN NEW.removed_at_ms IS NOT NULL AND (
       NEW.orphaned = 0
    OR NOT EXISTS (SELECT 1 FROM principals p WHERE p.principal_id = NEW.removed_by AND p.type = 'human' AND p.enabled = 1))
BEGIN SELECT RAISE(ABORT, 'only an orphaned artifact can be removed, and only by a human principal'); END;
