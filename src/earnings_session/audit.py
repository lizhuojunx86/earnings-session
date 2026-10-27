"""Vendor label vs EDGAR: confusion matrix and look-ahead share, with Wilson intervals.

Works on batch output rows (the CSV columns of ``core.CSV_COLUMNS`` plus
``vendor_label``). Filings are grouped by how they were found: the 8-K Item
2.02 rows are the main read; other 8-Ks / 10-Q / 10-K and 6-K proxies are
reported separately because their acceptance is a looser bound.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from math import sqrt
from typing import Any

from earnings_session.core import RULE_6K_PROXY, RULE_ITEM_202, RULE_OTHER_8K, RULE_PERIODIC
from earnings_session.timing import AFTER_ENTRY, EDGAR_LABELS, VERDICTS, normalize_label

GROUPS: dict[str, tuple[str, ...]] = {
    "8-K Item 2.02": (RULE_ITEM_202,),
    "fallback (other 8-K, 10-Q, 10-K)": (RULE_OTHER_8K, RULE_PERIODIC),
    "6-K proxy (low confidence)": (RULE_6K_PROXY,),
}
VENDOR_ROWS = ("bmo", "dmh", "amc", "(none)")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (nan, nan if n == 0)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def share(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "share": k / n if n else None, "wilson95": [lo, hi] if n else None}


def _vendor(row: Mapping[str, str]) -> str:
    return normalize_label(row.get("vendor_label")) or "(none)"


def _group(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    confusion = {v: {e: 0 for e in EDGAR_LABELS} for v in VENDOR_ROWS}
    for r in rows:
        confusion[_vendor(r)][r["edgar_label"]] += 1
    n = len(rows)
    verdicts = Counter(r["verdict"] for r in rows)
    by_label: dict[str, Any] = {}
    for v in VENDOR_ROWS:
        sub = [r for r in rows if _vendor(r) == v]
        if not sub:
            continue
        rec: dict[str, Any] = {
            "after_entry": share(sum(r["verdict"] == AFTER_ENTRY for r in sub), len(sub)),
        }
        if v != "(none)":
            rec["agrees"] = share(sum(r["edgar_label"] == v for r in sub), len(sub))
        by_label[v] = rec
    return {
        "n": n,
        "confusion": confusion,
        "verdict": {v: share(verdicts[v], n) for v in VERDICTS},
        "by_vendor_label": by_label,
    }


def audit_rows(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    missing = {"vendor_label", "rule", "verdict", "edgar_label", "reason"} - set(
        rows[0].keys() if rows else ()
    )
    if rows and missing:
        raise ValueError(f"not batch output: missing column(s) {', '.join(sorted(missing))}")
    unresolved = [r for r in rows if not r["rule"]]
    out: dict[str, Any] = {
        "n_events": len(rows),
        "resolved": {g: sum(r["rule"] in rules for r in rows) for g, rules in GROUPS.items()},
        "unresolved": {
            "n": len(unresolved),
            "reasons": dict(Counter(r["reason"] for r in unresolved).most_common()),
            "by_vendor_label": dict(Counter(_vendor(r) for r in unresolved).most_common()),
        },
        "groups": {},
    }
    for g, rules in GROUPS.items():
        sub = [r for r in rows if r["rule"] in rules]
        if sub:
            out["groups"][g] = _group(sub)
    return out


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _share_text(s: Mapping[str, Any]) -> str:
    if not s["n"]:
        return "-"
    lo, hi = s["wilson95"]
    return f"{s['k']}/{s['n']} = {_pct(s['share'])} ({_pct(lo)}-{_pct(hi)})"


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"events: {report['n_events']}"]
    lines.append("resolved: " + ", ".join(f"{g} {n}" for g, n in report["resolved"].items()))
    un = report["unresolved"]
    reasons = ", ".join(f"{k} {v}" for k, v in un["reasons"].items())
    lines.append(f"unresolved: {un['n']}" + (f" ({reasons})" if reasons else ""))
    for g, rep in report["groups"].items():
        lines += ["", f"== {g}: n = {rep['n']} =="]
        lines.append("vendor label x EDGAR acceptance session (relative to the event date)")
        width = max(len(c) for c in EDGAR_LABELS) + 2
        lines.append(" " * 8 + "".join(c.rjust(width) for c in EDGAR_LABELS))
        for v, cols in rep["confusion"].items():
            if any(cols.values()):
                lines.append(v.ljust(8) + "".join(str(cols[c]).rjust(width) for c in EDGAR_LABELS))
        lines.append("verdict vs entry open (Wilson 95%)")
        for v, s in rep["verdict"].items():
            lines.append(f"  {v:<14}{_share_text(s)}")
        lines.append("by vendor label")
        for v, rec in rep["by_vendor_label"].items():
            parts = [f"after_entry {_share_text(rec['after_entry'])}"]
            if "agrees" in rec:
                parts.insert(0, f"agrees {_share_text(rec['agrees'])}")
            lines.append(f"  {v:<8}" + "; ".join(parts))
    return "\n".join(lines)
