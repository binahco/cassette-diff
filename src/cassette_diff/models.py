from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DriftReport(BaseModel):
    """Veredicto de deriva entre dos sesiones (`drift-report-v1`)."""

    summary: str
    verdict: Literal["sin_cambio", "drift_menor", "drift_mayor"]
    changed_cases: list[str] = []
