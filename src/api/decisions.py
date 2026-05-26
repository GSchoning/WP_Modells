"""Decision audit-trail storage for the GABORA regulatory tool.

Decisions (approve / reject of a proposed scenario) are persisted as a
JSON file alongside the workspace. Each entry captures the full
scenario metadata, the result summary, and a `status` field that the
regulator can flip via the rollback endpoint:

    - "active":    counts toward the current legislative state.
    - "rolled_back": superseded by a later rollback, kept for audit.
    - "rejected":  the regulator rejected the scenario at the time.

This is a UI-level audit trail. The actual MODFLOW baseline is not yet
re-derived from the active decisions — that integration is a separate
piece of work. Decisions are still stored with enough detail (wells
run, per-output-year exceedance counts) that the hydrogeological
plumbing can read this file later without a schema change.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"decisions": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "decisions" not in data:
            return {"decisions": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"decisions": []}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".decisions.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_decisions(path: Path) -> list[dict[str, Any]]:
    """Return all decisions (newest-first)."""
    with _LOCK:
        data = _load_raw(path)
    out = list(data.get("decisions", []))
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return out


def record_decision(
    path: Path,
    *,
    decision: str,
    regulator: str,
    scenario: dict[str, Any],
    summary: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    """Append a new decision to the store and return the persisted record.

    `decision` must be "approve" or "reject".
    `scenario`  carries the request that produced the result.
    `summary`   carries the headline result metrics.
    """
    if decision not in ("approve", "reject"):
        raise ValueError(f"invalid decision {decision!r}")

    with _LOCK:
        data = _load_raw(path)
        decisions: list[dict[str, Any]] = list(data.get("decisions", []))
        # Sequential id; ordering by created_at handles concurrent writes.
        next_seq = (max((d.get("seq", 0) for d in decisions), default=0)) + 1
        record = {
            "id": f"dec_{next_seq:05d}",
            "seq": next_seq,
            "decision": decision,
            "status": "active" if decision == "approve" else "rejected",
            "regulator": regulator or "unknown",
            "created_at": _now_iso(),
            "note": note,
            "scenario": scenario,
            "summary": summary,
        }
        decisions.append(record)
        data["decisions"] = decisions
        _atomic_write(path, data)
        return record


def rollback_to(path: Path, decision_id: str, regulator: str) -> dict[str, Any]:
    """Mark every active approval *after* `decision_id` as rolled_back.

    Returns the updated head decision (the one rolled back to). Decisions
    with status "rejected" are left alone. Already-rolled-back entries
    remain rolled_back. The target decision itself is forced to "active".
    """
    with _LOCK:
        data = _load_raw(path)
        decisions: list[dict[str, Any]] = list(data.get("decisions", []))
        target = next((d for d in decisions if d.get("id") == decision_id), None)
        if target is None:
            raise KeyError(decision_id)
        if target.get("decision") != "approve":
            raise ValueError("can only roll back to an approve decision")

        target_seq = int(target.get("seq", 0))
        changed = 0
        for d in decisions:
            if d.get("decision") != "approve":
                continue
            seq = int(d.get("seq", 0))
            if seq > target_seq and d.get("status") == "active":
                d["status"] = "rolled_back"
                d["rolled_back_at"] = _now_iso()
                d["rolled_back_by"] = regulator or "unknown"
                d["rolled_back_to"] = decision_id
                changed += 1
        target["status"] = "active"
        if "rolled_back_at" in target:
            target.pop("rolled_back_at", None)
            target.pop("rolled_back_by", None)
            target.pop("rolled_back_to", None)

        data["decisions"] = decisions
        _atomic_write(path, data)
        return {"head": target, "n_rolled_back": changed}


def active_approved_ids(path: Path) -> list[str]:
    """IDs of approve decisions still active (i.e. in the legislative state)."""
    with _LOCK:
        data = _load_raw(path)
    return [
        d["id"]
        for d in data.get("decisions", [])
        if d.get("decision") == "approve" and d.get("status") == "active"
    ]
