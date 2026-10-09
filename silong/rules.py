"""Report fields, code-based urgency and board order. Pure functions, no I/O.

`validate_fields` is the single validator for model output and staff edits.
`classify` applies the PRD urgency rules (first match wins). `order_reports`
returns stored rows in the order the board shows them.
"""

import json
from datetime import datetime
from typing import Any, Iterable

VULNERABLE = ("senior", "pwd", "infant", "child", "pregnant", "sick")
NEEDS = ("rescue", "medical", "medicine", "food", "water", "milk_formula", "hygiene", "other")
FIELD_NAMES = (
    "location",
    "in_center",
    "on_behalf",
    "headcount",
    "vulnerable",
    "injured",
    "injury_note",
    "needs",
    "summary",
)
SUMMARY_MAX_WORDS = 20

# Needs that make a vulnerable person's report High.
SUPPLY_NEEDS = ("medicine", "food", "water", "milk_formula")
URGENCY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _optional_text(name: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text or null")
    return value.strip() or None


def _optional_bool(name: str, value: Any) -> Any:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be true, false or null")
    return value


def _choices(name: str, value: Any, allowed: tuple) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    for item in value:
        if item not in allowed:
            raise ValueError(f"{name} contains {item!r}; allowed: {', '.join(allowed)}")
    return [choice for choice in allowed if choice in value]


def validate_fields(obj: Any, *, truncate_summary: bool = False) -> dict:
    """Return a cleaned copy of `obj`, or raise ValueError naming the problem.

    Strings are stripped and blank ones become null, except `summary`, which
    is always a string ("" when blank or null). Lists are de-duplicated in
    canonical order. A summary over 20 words is cut to its first 20 words when
    `truncate_summary` is set (model output) and rejected otherwise (staff).
    """
    if not isinstance(obj, dict):
        raise ValueError("fields must be an object")
    missing = [name for name in FIELD_NAMES if name not in obj]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
    extra = sorted(str(key) for key in obj if key not in FIELD_NAMES)
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")

    headcount = obj["headcount"]
    if headcount is not None:
        if not isinstance(headcount, int) or isinstance(headcount, bool):
            raise ValueError("headcount must be an integer or null")
        if headcount < 0:
            raise ValueError("headcount must not be negative")

    if not isinstance(obj["injured"], bool):
        raise ValueError("injured must be true or false")

    summary = obj["summary"]
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        raise ValueError("summary must be text")
    summary = summary.strip()
    words = summary.split()
    if len(words) > SUMMARY_MAX_WORDS:
        if not truncate_summary:
            raise ValueError(f"summary must be at most {SUMMARY_MAX_WORDS} words")
        summary = " ".join(words[:SUMMARY_MAX_WORDS])

    return {
        "location": _optional_text("location", obj["location"]),
        "in_center": _optional_bool("in_center", obj["in_center"]),
        "on_behalf": _optional_bool("on_behalf", obj["on_behalf"]),
        "headcount": headcount,
        "vulnerable": _choices("vulnerable", obj["vulnerable"], VULNERABLE),
        "injured": obj["injured"],
        "injury_note": _optional_text("injury_note", obj["injury_note"]),
        "needs": _choices("needs", obj["needs"], NEEDS),
        "summary": summary,
    }


def classify(fields: dict) -> tuple[str, str]:
    """Return (urgency, reason) for validated fields; the first matching rule wins."""
    needs = fields["needs"]
    if "rescue" in needs and fields["in_center"] is False:
        return "critical", "Critical: rescue needed outside center"
    if "rescue" in needs:
        return "critical", "Critical: rescue needed"
    if "medical" in needs:
        return "critical", "Critical: medical need"
    if fields["injured"]:
        return "critical", "Critical: injury reported"
    supplies = [need for need in SUPPLY_NEEDS if need in needs]
    if fields["vulnerable"] and supplies:
        return "high", f"High: {', '.join(fields['vulnerable'])} + {', '.join(supplies)}"
    if needs:
        return "medium", f"Medium: needs {', '.join(needs)}"
    return "low", "Low: no need stated"


def _received(row: dict) -> float:
    return datetime.fromisoformat(row["created_at"]).timestamp()


def sort_key(row: dict) -> tuple:
    """Board order for a stored report row (as returned by `db.list_reports`).

    Unresolved classified reports first, by urgency, oldest receipt, larger
    headcount (unknown last), id; then unresolved unclassified, oldest first;
    then resolved, newest receipt first.
    """
    if row["resolved"]:
        return (2, -_received(row), -row["id"])
    if row["urgency"] is None:
        return (1, _received(row), row["id"])
    fields = json.loads(row["fields_json"]) if row["fields_json"] else {}
    headcount = fields.get("headcount")
    by_headcount = (1, 0) if headcount is None else (0, -headcount)
    return (0, URGENCY_RANK[row["urgency"]], _received(row), by_headcount, row["id"])


def order_reports(rows: Iterable[dict]) -> list[dict]:
    """Stored report rows in board order; see `sort_key`."""
    return sorted(rows, key=sort_key)
