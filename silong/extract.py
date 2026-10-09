"""Field extraction through a local Ollama model.

One fixed prompt prefix (system rules and synthetic Taglish examples) is sent
byte-identical for every report, with the report text as the last message, so
Ollama can reuse the cached prefix. The reply is constrained by `SCHEMA` and
checked by `rules.validate_fields`; urgency is computed by `rules`, never here.
"""

import json
import os
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from silong import rules

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11434
# Listen-all addresses (such as OLLAMA_HOST=0.0.0.0 set for the Ollama server)
# are not connectable addresses; a client on the same machine uses loopback.
LISTEN_ALL_HOSTS = ("0.0.0.0", "::")
# CPU cold loads can take minutes; failing to connect should not.
TIMEOUT = httpx.Timeout(300.0, connect=10.0)
ERROR_DETAIL_CHARS = 200

_NULLABLE_TEXT = {"type": ["string", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}

# JSON schema passed as Ollama's `format`. Negative headcounts and duplicate
# list items are left to `rules.validate_fields`.
SCHEMA = {
    "type": "object",
    "properties": {
        "location": _NULLABLE_TEXT,
        "in_center": _NULLABLE_BOOL,
        "on_behalf": _NULLABLE_BOOL,
        "headcount": {"type": ["integer", "null"]},
        "vulnerable": {"type": "array", "items": {"type": "string", "enum": list(rules.VULNERABLE)}},
        "injured": {"type": "boolean"},
        "injury_note": _NULLABLE_TEXT,
        "needs": {"type": "array", "items": {"type": "string", "enum": list(rules.NEEDS)}},
        "summary": {"type": "string"},
    },
    "required": list(rules.FIELD_NAMES),
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You extract facts from reports sent to an evacuation center during a typhoon or flood. Reports are in Taglish (Tagalog mixed with English). Reply with one JSON object that matches the schema.

Rules:
- Use only what the report states. Never guess. Unknown text, numbers and yes/no fields are null; unknown lists are empty.
- location: where the people in need are, which may differ from where the reporter is. Copy the place from the report. null if no place is stated.
- in_center: true if the people in need are inside the evacuation center, false if they are outside it (for example at home, on a roof or stranded), null if the report does not say.
- on_behalf: true if the reporter is reporting for other people and is not one of the people in need (family members such as anak, tatay or lola are other people), false if the reporter is one of the people in need, null if unclear.
- headcount: number of people in need, only when the report states the number. Otherwise null. Never estimate.
- vulnerable: each stated kind of vulnerable person: senior (lolo, lola, matanda), pwd, infant (baby, sanggol), child (bata), pregnant (buntis), sick (may sakit, lagnat).
- injured: true only if an injury is stated (sugat, nabalian, dumudugo); otherwise false. injury_note: a few English words about the injury, or null.
- needs: each stated need: rescue (trapped, stranded or must be evacuated), medical (needs a doctor, nurse or first aid), medicine (gamot), food (pagkain), water (tubig), milk_formula (gatas), hygiene (diaper, napkin, sabon), other (any other need).
- summary: one English sentence of at most 20 words."""


def _example(report: str, fields: dict) -> tuple:
    return (
        {"role": "user", "content": report},
        {"role": "assistant", "content": json.dumps(fields, ensure_ascii=False)},
    )


# Synthetic examples: an outside-center rescue reported for neighbors, an
# in-center infant with supply needs where the reporter is among those in
# need, and a report only about the reporter's family members with unknown
# location and headcount.
EXAMPLES = (
    _example(
        "Tulong po! Yung kapitbahay namin sa Purok 3, Brgy. San Roque, nasa bubong na sila, "
        "5 tao kasama si lola. Mataas na tubig, hindi sila makalabas.",
        {
            "location": "Purok 3, Brgy. San Roque",
            "in_center": False,
            "on_behalf": True,
            "headcount": 5,
            "vulnerable": ["senior"],
            "injured": False,
            "injury_note": None,
            "needs": ["rescue"],
            "summary": "Five neighbors including a grandmother are stranded on a roof in Purok 3 by high floodwater.",
        },
    )
    + _example(
        "Nandito kami sa Classroom 4 ng evacuation center. Yung baby ko 6 months, "
        "ubos na gatas niya. Wala na rin kaming tubig.",
        {
            "location": "Classroom 4, evacuation center",
            "in_center": True,
            "on_behalf": False,
            "headcount": None,
            "vulnerable": ["infant"],
            "injured": False,
            "injury_note": None,
            "needs": ["water", "milk_formula"],
            "summary": "Family in Classroom 4 has a six-month-old baby out of milk formula and no water.",
        },
    )
    + _example(
        "Yung mga anak ko nagtatae na, tsaka si tatay ko nadulas, may sugat sa paa. "
        "Kailangan po ng gamot at first aid.",
        {
            "location": None,
            "in_center": None,
            "on_behalf": True,
            "headcount": None,
            "vulnerable": ["child", "sick"],
            "injured": True,
            "injury_note": "father's foot wound from a fall",
            "needs": ["medical", "medicine"],
            "summary": "Reporter's children have diarrhea and father has a foot wound from a fall; they need medicine and first aid.",
        },
    )
)

# Everything before the report text; identical for every request.
PROMPT_PREFIX = ({"role": "system", "content": SYSTEM_PROMPT},) + EXAMPLES


class ExtractionError(Exception):
    """Extraction failed; the message is short and readable by staff."""


def ollama_url(environ: Optional[Mapping[str, str]] = None) -> str:
    """Ollama's base URL from OLLAMA_HOST (defaults to os.environ).

    Unset or blank gives http://127.0.0.1:11434. A bare `host[:port]` gets
    `http://` and port 11434 when none is given; a full URL keeps its scheme
    and port. A listen-all host (0.0.0.0 or ::) becomes 127.0.0.1.
    """
    if environ is None:
        environ = os.environ
    raw = environ.get("OLLAMA_HOST", "").strip()
    if not raw:
        return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    if "://" in raw:
        url, default_port = raw, None
    else:
        # A bare IPv6 address such as "::" has no brackets.
        bare = f"[{raw}]" if raw.count(":") > 1 and not raw.startswith("[") else raw
        url, default_port = f"http://{bare}", DEFAULT_PORT
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port or default_port
    except ValueError:
        host = None
    if not host or parts.scheme not in ("http", "https"):
        raise ExtractionError(f"OLLAMA_HOST is not a usable address: {raw!r}")
    if host in LISTEN_ALL_HOSTS:
        host = DEFAULT_HOST
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}"


def _new_client() -> httpx.Client:
    return httpx.Client(base_url=ollama_url(), timeout=TIMEOUT)


def _http_error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("error")
    except (ValueError, AttributeError):
        detail = None
    if not isinstance(detail, str) or not detail.strip():
        detail = response.text.strip() or response.reason_phrase
    return detail[:ERROR_DETAIL_CHARS]


def _chat(client: Optional[httpx.Client], body: dict) -> dict:
    """POST `body` to /api/chat and return the decoded reply, or raise ExtractionError."""
    own_client = client is None
    if own_client:
        client = _new_client()
    address = str(client.base_url).rstrip("/")
    try:
        response = client.post("/api/chat", json=body)
    except httpx.ConnectTimeout:
        raise ExtractionError(f"Ollama is not reachable at {address} (connection timed out)") from None
    except httpx.ConnectError:
        raise ExtractionError(f"Ollama is not reachable at {address}") from None
    except httpx.TimeoutException:
        raise ExtractionError(
            f"Ollama at {address} did not reply within {TIMEOUT.read:.0f} s"
        ) from None
    except httpx.HTTPError as exc:
        raise ExtractionError(f"Could not talk to Ollama at {address}: {exc}") from None
    finally:
        if own_client:
            client.close()
    if response.is_error:
        raise ExtractionError(
            f"Ollama returned HTTP {response.status_code}: {_http_error_detail(response)}"
        )
    try:
        reply = response.json()
    except ValueError:
        raise ExtractionError("Ollama sent a reply that is not JSON") from None
    if not isinstance(reply, dict):
        raise ExtractionError("Ollama sent a reply without a message")
    return reply


def request_body(text: str, model: str) -> dict:
    """The /api/chat request for one report: fixed prefix, report text last."""
    return {
        "model": model,
        "messages": [*PROMPT_PREFIX, {"role": "user", "content": text}],
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "options": {"temperature": 0},
    }


def extract(text: str, model: str, *, client: Optional[httpx.Client] = None) -> dict:
    """Extract validated fields from report `text` with Ollama `model`.

    `client` must have Ollama's base URL; by default one is made from
    OLLAMA_HOST for this call. Raises ExtractionError on any failure.
    """
    reply = _chat(client, request_body(text, model))
    message = reply.get("message")
    content: Any = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ExtractionError("Ollama sent a reply without a message")
    try:
        fields = json.loads(content)
    except ValueError:
        raise ExtractionError("Model reply was not valid JSON") from None
    try:
        return rules.validate_fields(fields, truncate_summary=True)
    except ValueError as exc:
        raise ExtractionError(f"Model reply did not match the report fields: {exc}") from None


def warm_up(model: str, *, client: Optional[httpx.Client] = None) -> None:
    """Load `model` into memory and keep it loaded. Raises ExtractionError on failure."""
    _chat(client, {"model": model, "messages": [], "stream": False, "keep_alive": -1})
