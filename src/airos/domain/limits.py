"""Operational limits (PROPOSAL 4.4). Defaults; hard caps are enforced in schemas."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limits:
    max_subtask_depth: int = 3
    max_children_per_task: int = 8
    max_total_tasks_per_root: int = 25  # beyond this a human `limit_increase` approval is required
    hard_max_total_tasks_per_root: int = 100  # beyond this: always rejected (PROPOSAL 4.4 HARD cap)
    max_review_cycles: int = 2
    max_concurrent_runs: int = 2
    max_timeout_retries: int = 1
    lease_ttl_ms: int = 60_000
    approval_ttl_ms: int = 72 * 3600 * 1000
    grant_ttl_ms: int = 7 * 24 * 3600 * 1000  # a granted approval is valid for 7 days (PROPOSAL T10/T11)
    review_ttl_ms: int = 72 * 3600 * 1000
    blocked_ttl_ms: int = 7 * 24 * 3600 * 1000
    backoff_base_ms: int = 30_000
    backoff_cap_ms: int = 600_000
    director_may_create_root: bool = False
    # Default budgets (PROPOSAL Q2). Real adapters start at $0 / 0 tokens until a human grants budget.
    default_root_usd_micro: int = 10_000_000
    default_root_tokens: int = 20_000_000
    default_global_daily_usd_micro: int = 25_000_000
    default_global_daily_tokens: int = 50_000_000
    # ONE ledger pool "agent_sdk_credit" per month (ADR-0011). Defaults: cap 15 USD (assumed Pro credit 20 USD, to be verified by the human),
    # Executor 10, Planner 3, AI Scientist 0, 2 unallocated. Raising any allocation needs a human approval bound to the contract hash.
    agent_sdk_credit_monthly_cap_usd_micro: int = 15_000_000
    role_allocations_usd_micro: dict[str, int] = field(default_factory=lambda: {"executor": 10_000_000, "planner": 3_000_000, "ai_scientist": 0})
    pool_token_limit: int = 10**12      # the credit is dollar-denominated; tokens are not the constraint
    director_max_runs_per_day: int = 0  # the Director has no dollar pool; the human sets a run cap (0 = cannot run)
    drift_flag_ratio_ppm: int = 150_000  # reconciliation drift above 15 % of the monthly cap is flagged

    def backoff_ms(self, attempt: int) -> int:
        """Deterministic exponential backoff (no jitter in M1; jitter is a scheduler concern)."""
        n = max(attempt, 1)
        return min(self.backoff_base_ms * (1 << (n - 1)), self.backoff_cap_ms)
