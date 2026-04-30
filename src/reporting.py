"""Markdown + JSON report generation (CLAUDE.md §6.6)."""
from __future__ import annotations

from pathlib import Path


def write_validation_report(findings: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Input validation report", ""]
    if not findings:
        lines.append("All checks passed.")
    else:
        for f in findings:
            lines.append(f"- {f}")
    path.write_text("\n".join(lines) + "\n")


def write_impact_report(*args, **kwargs) -> None:
    raise NotImplementedError(
        "reporting.write_impact_report is not yet implemented; depends on scenarios."
    )
