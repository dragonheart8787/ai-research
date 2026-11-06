-- 0005_timeouts: wall-clock timeout retries are limited separately (PROPOSAL 4.4: max_timeout_retries = 1).
ALTER TABLE tasks ADD COLUMN timeout_retries INTEGER NOT NULL DEFAULT 0 CHECK (timeout_retries >= 0);
