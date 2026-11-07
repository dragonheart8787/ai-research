"""Pinned, versioned price table. The control plane computes USD itself from tokens; adapter-reported USD is never used.

Units: integer micro-USD per million tokens. cost = ceil((tokens_in * in_price + tokens_out * out_price) / 1_000_000),
pure integer arithmetic (no floats). A model with no entry has NO price: callers must settle at worst case and flag it.
The shipped table contains only `mock` (price 0). Real model prices are business inputs a human must supply in a table
file; this repository does not guess them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from airos.canonical import canonical_json, sha256_hex
from airos.errors import SchemaMismatch

# Pin of the packaged default table. Editing prices-v1.json without bumping this pin (and the version) fails at load.
DEFAULT_PRICE_TABLE_SHA256 = "096837c0274cb9b56a2eea4bec6d048310e9fcc8b43760c1b10ee5f09b23d344"


@dataclass(frozen=True)
class ModelPrice:
    input_usd_micro_per_mtok: int
    output_usd_micro_per_mtok: int


@dataclass(frozen=True)
class PriceTable:
    version: str
    sha256: str
    models: dict[str, ModelPrice]

    def has(self, model: str | None) -> bool:
        return model is not None and model in self.models

    def cost_usd_micro(self, model: str | None, tokens_in: int, tokens_out: int) -> int | None:
        """USD in micro-USD, rounded UP; None if the model has no price entry."""
        if model is None or model not in self.models:
            return None
        p = self.models[model]
        total = tokens_in * p.input_usd_micro_per_mtok + tokens_out * p.output_usd_micro_per_mtok
        return -(-total // 1_000_000)

    @classmethod
    def from_dict(cls, data: dict[str, Any], expected_sha256: str | None = None) -> PriceTable:
        if data.get("schema") != "airos-prices/1" or data.get("unit") != "usd_micro_per_million_tokens" \
                or data.get("currency") != "USD" or not isinstance(data.get("version"), str) or not data["version"]:
            raise SchemaMismatch("price table: bad schema/unit/currency/version")
        try:
            digest = sha256_hex(canonical_json(data))
        except TypeError as exc:  # floats etc. are not allowed anywhere in a price table
            raise SchemaMismatch(f"price table: {exc}") from exc
        if expected_sha256 is not None and digest != expected_sha256:
            raise SchemaMismatch(f"price table sha256 {digest} does not match the pinned {expected_sha256}")
        models: dict[str, ModelPrice] = {}
        for name, p in data.get("models", {}).items():
            vals = (p.get("input"), p.get("output"))
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in vals) or set(p) != {"input", "output"}:
                raise SchemaMismatch(f"price table: bad price for model {name!r} (non-negative integers only)")
            models[name] = ModelPrice(int(vals[0]), int(vals[1]))
        return cls(data["version"], digest, models)

    @classmethod
    def load(cls, path: str | Path, expected_sha256: str | None = None) -> PriceTable:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), expected_sha256)

    @classmethod
    def default(cls) -> PriceTable:
        text = resources.files("airos.pricing_data").joinpath("prices-v1.json").read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text), DEFAULT_PRICE_TABLE_SHA256)
