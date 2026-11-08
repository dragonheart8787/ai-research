-- 0006_pricing: the run record carries how its cost was derived (model, pinned price table version + hash, basis).
ALTER TABLE runs ADD COLUMN model TEXT;
ALTER TABLE runs ADD COLUMN cost_usd_micro INTEGER;
ALTER TABLE runs ADD COLUMN cost_basis TEXT CHECK (cost_basis IN ('priced','worst_case'));
ALTER TABLE runs ADD COLUMN price_table_version TEXT;
ALTER TABLE runs ADD COLUMN price_table_sha256 TEXT;
