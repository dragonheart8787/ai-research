-- 0004_worst_case: an approval records the contract's worst-case total spend, so the human sees what they approve.
ALTER TABLE approvals ADD COLUMN worst_case_usd_micro INTEGER;
ALTER TABLE approvals ADD COLUMN worst_case_tokens INTEGER;
CREATE TRIGGER approvals_worst_case_immutable BEFORE UPDATE ON approvals
WHEN NEW.worst_case_usd_micro IS NOT OLD.worst_case_usd_micro OR NEW.worst_case_tokens IS NOT OLD.worst_case_tokens
BEGIN SELECT RAISE(ABORT, 'approval worst-case totals are immutable'); END;
