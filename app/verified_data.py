"""Verified stock table (fictional) and deterministic outlet/SKU matching.

Matching is intentionally conservative: a row is linked to a field update
only when BOTH its outlet and its SKU are named. We would rather ask the rep
"which SKU?" than verify a claim against the wrong row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "verified_stock.json"

_TOKEN_RE = re.compile(r"[a-z]+|\d+")


@dataclass(frozen=True)
class StockRow:
    row_id: str
    outlet: str
    sku: str
    system_stock: int


@dataclass(frozen=True)
class Mention:
    canonical: str
    start: int
    end: int
    kind: str  # "exact" or "generic"


@dataclass(frozen=True)
class RowMatch:
    row: StockRow
    sku_match: str  # "exact" or "generic" (e.g. "cola" -> "Cola 500ml")


def _alias_pattern(alias: str) -> re.Pattern[str]:
    """'cola 500ml' -> matches 'Cola 500ml', 'cola 500 ml', 'cola-500ml', 'COLA500ML'."""
    tokens = _TOKEN_RE.findall(alias.lower())
    body = r"[\s\-_]*".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)


def _find_all(patterns: list[tuple[re.Pattern[str], str, str]], text: str) -> list[Mention]:
    found: list[Mention] = []
    for pattern, canonical, kind in patterns:
        for m in pattern.finditer(text):
            found.append(Mention(canonical=canonical, start=m.start(), end=m.end(), kind=kind))
    # Drop mentions fully contained in a longer mention (e.g. generic "cola" inside "cola 500ml").
    found.sort(key=lambda x: (x.start, -(x.end - x.start)))
    kept: list[Mention] = []
    for mention in found:
        if any(k.start <= mention.start and mention.end <= k.end for k in kept):
            continue
        kept.append(mention)
    return kept


class VerifiedData:
    def __init__(self, rows: list[StockRow], outlet_aliases: dict, sku_aliases: dict):
        self.rows = rows
        self._by_id = {r.row_id: r for r in rows}
        self._outlet_patterns: list[tuple[re.Pattern[str], str, str]] = []
        for outlet, aliases in outlet_aliases.items():
            for alias in {outlet.lower(), *aliases}:
                self._outlet_patterns.append((_alias_pattern(alias), outlet, "exact"))
        self._sku_patterns: list[tuple[re.Pattern[str], str, str]] = []
        for sku, groups in sku_aliases.items():
            for alias in {sku.lower(), *groups.get("exact", [])}:
                self._sku_patterns.append((_alias_pattern(alias), sku, "exact"))
            for alias in groups.get("generic", []):
                self._sku_patterns.append((_alias_pattern(alias), sku, "generic"))

    @classmethod
    def load(cls, path: Path = DATA_PATH) -> "VerifiedData":
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [StockRow(**r) for r in raw["rows"]]
        return cls(rows, raw.get("outlet_aliases", {}), raw.get("sku_aliases", {}))

    @property
    def row_ids(self) -> list[str]:
        return [r.row_id for r in self.rows]

    def get(self, row_id: str) -> StockRow | None:
        return self._by_id.get(row_id)

    def find_outlets(self, text: str) -> list[Mention]:
        return _find_all(self._outlet_patterns, text)

    def find_skus(self, text: str) -> list[Mention]:
        return _find_all(self._sku_patterns, text)

    def match_rows(self, text: str) -> tuple[list[Mention], list[Mention], list[RowMatch]]:
        outlets = self.find_outlets(text)
        skus = self.find_skus(text)
        outlet_names = {m.canonical for m in outlets}
        exact_skus = {m.canonical for m in skus if m.kind == "exact"}
        generic_skus = {m.canonical for m in skus if m.kind == "generic"}
        matches: list[RowMatch] = []
        for row in self.rows:
            if row.outlet not in outlet_names:
                continue
            if row.sku in exact_skus:
                matches.append(RowMatch(row=row, sku_match="exact"))
            elif row.sku in generic_skus:
                matches.append(RowMatch(row=row, sku_match="generic"))
        return outlets, skus, matches

    def as_prompt_table(self) -> str:
        lines = ["row_id | outlet | sku | system_stock"]
        lines += [f"{r.row_id} | {r.outlet} | {r.sku} | {r.system_stock}" for r in self.rows]
        return "\n".join(lines)

    def as_public_list(self) -> list[dict]:
        return [
            {"row_id": r.row_id, "outlet": r.outlet, "sku": r.sku, "system_stock": r.system_stock}
            for r in self.rows
        ]


@lru_cache(maxsize=1)
def get_verified_data() -> VerifiedData:
    return VerifiedData.load()
