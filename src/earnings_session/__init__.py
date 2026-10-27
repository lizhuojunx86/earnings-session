"""Classify when an earnings release went out from SEC EDGAR acceptance times."""

from __future__ import annotations

__version__ = "0.1.0"

from earnings_session.audit import audit_rows, wilson
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
    "classify",
    "classify_acceptance",
    "edgar_label",
    "entry_verdict",
    "filing_window",
    "wilson",
    "zone_convention",
]
