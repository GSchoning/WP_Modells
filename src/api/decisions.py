"""Decision audit trail for the GABORA regulatory tool.

Event-sourced, append-only storage: every action is one JSON line in
`var/decision_events.jsonl` —

    {"event": "decision", "id": "dec_00001", "decision": "approve", ...}
    {"event": "rollback", "target_id": "dec_00001", "regulator": ..., "at": ...}

The visible decision list (with `status` per decision) is *derived* by
folding the events in order. Nothing is ever rewritten or deleted, which
is the property an audit trail actually needs: a partial write can at
worst truncate the final line (ignored on load), never corrupt history.

Statuses derived by the fold:
    - "active":      approve decision that counts toward the current state.
    - "rolled_back": approve decision superseded by a later rollback.
    - "rejected":    the regulator rejected the scenario at the time.

This remains a UI-level audit trail: the MODFLOW baseline is not yet
re-derived from active decisions. Events store the full scenario change
set so that integration needs no schema change.

Migration: if the legacy full-document store (outputs/decisions.json)
exists and the events file does not, its decisions are imported as
events once on first read.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()

LEGACY_JSON_PATH = Path("outputs") / "decisions.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from an interrupted write — skip it.
                continue
    return events


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _migrate_legacy(path: Path) -> None:
    """One-time import of the old outputs/decisions.json full-document store."""
    if path.exists() or not LEGACY_JSON_PATH.exists():
        return
    try:
        with LEGACY_JSON_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        decisions = sorted(data.get("decisions", []), key=lambda d: d.get("seq", 0))
    except (json.JSONDecodeError, OSError):
        return

    for d in decisions:
        event = {k: v for k, v in d.items()
                 if k not in ("status", "rolled_back_at", "rolled_back_by", "rolled_back_to")}
        event["event"] = "decision"
        _append_event(path, event)
    # Reconstruct rollback events from the rolled_back_to markers.
    seen_targets: set[str] = set()
    for d in decisions:
        target = d.get("rolled_back_to")
        if target and target not in seen_targets:
            seen_targets.add(target)
            _append_event(path, {
                "event": "rollback",
                "target_id": target,
                "regulator": d.get("rolled_back_by", "unknown"),
                "at": d.get("rolled_back_at", _now_iso()),
            })


def _fold(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold the event log into the decision list with derived statuses."""
    decisions: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for ev in events:
        kind = ev.get("event")
        if kind == "decision":
            d = {k: v for k, v in ev.items() if k != "event"}
            d["status"] = "active" if d.get("decision") == "approve" else "rejected"
            decisions.append(d)
            by_id[d.get("id", "")] = d
        elif kind == "rollback":
            target = by_id.get(ev.get("target_id", ""))
            if target is None or target.get("decision") != "approve":
                continue
            target_seq = int(target.get("seq", 0))
            for d in decisions:
                if d.get("decision") != "approve":
                    continue
                if int(d.get("seq", 0)) > target_seq and d.get("status") == "active":
                    d["status"] = "rolled_back"
                    d["rolled_back_at"] = ev.get("at")
                    d["rolled_back_by"] = ev.get("regulator", "unknown")
                    d["rolled_back_to"] = ev.get("target_id")
            # Restore the target itself (it may have been rolled back earlier).
            target["status"] = "active"
            for k in ("rolled_back_at", "rolled_back_by", "rolled_back_to"):
                target.pop(k, None)
    return decisions


def _load_folded(path: Path) -> list[dict[str, Any]]:
    _migrate_legacy(path)
    return _fold(_read_events(path))


def list_decisions(path: Path) -> list[dict[str, Any]]:
    """Return all decisions (newest-first)."""
    with _LOCK:
        out = _load_folded(path)
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
    """Append a decision event and return the folded record."""
    if decision not in ("approve", "reject"):
        raise ValueError(f"invalid decision {decision!r}")

    with _LOCK:
        existing = _load_folded(path)
        next_seq = max((int(d.get("seq", 0)) for d in existing), default=0) + 1
        event = {
            "event": "decision",
            "id": f"dec_{next_seq:05d}",
            "seq": next_seq,
            "decision": decision,
            "regulator": regulator or "unknown",
            "created_at": _now_iso(),
            "note": note,
            "scenario": scenario,
            "summary": summary,
        }
        _append_event(path, event)
        record = {k: v for k, v in event.items() if k != "event"}
        record["status"] = "active" if decision == "approve" else "rejected"
        return record


def rollback_to(path: Path, decision_id: str, regulator: str) -> dict[str, Any]:
    """Append a rollback event making `decision_id` the active head.

    Later active approvals fold to "rolled_back". The target must be an
    approve decision. Returns {"head": <target>, "n_rolled_back": n}.
    """
    with _LOCK:
        before = _load_folded(path)
        target = next((d for d in before if d.get("id") == decision_id), None)
        if target is None:
            raise KeyError(decision_id)
        if target.get("decision") != "approve":
            raise ValueError("can only roll back to an approve decision")

        _append_event(path, {
            "event": "rollback",
            "target_id": decision_id,
            "regulator": regulator or "unknown",
            "at": _now_iso(),
        })
        after = _load_folded(path)

    head = next(d for d in after if d.get("id") == decision_id)
    by_id_before = {d.get("id"): d for d in before}
    n_rolled = sum(
        1 for a in after
        if a.get("status") == "rolled_back"
        and by_id_before.get(a.get("id"), {}).get("status") == "active"
    )
    return {"head": head, "n_rolled_back": n_rolled}


def active_approved_ids(path: Path) -> list[str]:
    """IDs of approve decisions still active (i.e. in the legislative state)."""
    with _LOCK:
        decisions = _load_folded(path)
    return [
        d["id"] for d in decisions
        if d.get("decision") == "approve" and d.get("status") == "active"
    ]
