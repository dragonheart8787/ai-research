-- 0002_run_seq: monotonic per-database run sequence so recovery order is deterministic for identical DB state.
ALTER TABLE runs ADD COLUMN run_seq INTEGER;
UPDATE runs SET run_seq = rowid;
CREATE UNIQUE INDEX runs_run_seq_uq ON runs(run_seq);
CREATE TRIGGER runs_run_seq_immutable BEFORE UPDATE OF run_seq ON runs
WHEN OLD.run_seq IS NOT NULL AND NEW.run_seq IS NOT OLD.run_seq
BEGIN SELECT RAISE(ABORT, 'run_seq is immutable'); END;
CREATE TRIGGER runs_run_seq_monotonic BEFORE INSERT ON runs
WHEN NEW.run_seq IS NULL OR NEW.run_seq <= IFNULL((SELECT MAX(run_seq) FROM runs), 0)
BEGIN SELECT RAISE(ABORT, 'run_seq must be strictly increasing'); END;
