"""Classify when an earnings release went out from SEC EDGAR acceptance times, and check
that a backtest's price bars sit on their target days."""

from __future__ import annotations

__version__ = "0.2.0"

from earnings_session.audit import audit_rows, wilson
from earnings_session.bars import check_trades, find_bar
from earnings_session.core import Classifier, EventResult, classify
from earnings_session.edgar import EdgarAccessError, EdgarClient, UserAgentError
from earnings_session.timing import (
    Session,
    classify_acceptance,
    edgar_label,
    entry_verdict,
    filing_window,
    zone_convention,
)

__all__ = [
    "Classifier",
    "EdgarAccessError",
    "EdgarClient",
    "EventResult",
    "Session",
    "UserAgentError",
    "__version__",
    "audit_rows",
    "check_trades",
    "classify",
    "classify_acceptance",
    "edgar_label",
    "entry_verdict",
    "filing_window",
    "find_bar",
    "wilson",
    "zone_convention",
]
