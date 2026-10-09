"""Five-report model test: `python -m silong.bench --model TAG [--out PATH]`.

Unloads any loaded Ollama models, records the environment, times one warm-up
(cold load), then runs the synthetic fixture reports through `extract.extract`
one at a time and compares fields and code-computed priority with the
expected values. Prints Markdown and appends it to `--out`. Every time shown
is measured here; failures are recorded, never dropped.
"""

import argparse
import json
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from silong import extract, rules

DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "five_reports.json"
UNLOAD_WAIT_S = 30.0
SCORED_FIELDS = tuple(name for name in rules.FIELD_NAMES if name != "summary")

FILE_HEADER = """\
# 02 Five-report model test

Synthetic Taglish reports from `tests/fixtures/five_reports.json`, run by `python -m silong.bench`.
Each run unloads loaded models first, then times one warm-up request (cold load) and each extraction.
`llm_ms` is wall-clock time around one `extract.extract` call, measured on the machine running the bench.

Field scoring: `location` matches when equal after lowercasing and removing punctuation ("partial" when one
contains the other, not counted as a match); `injury_note` scores only whether a note is present; `summary`
is shown but not scored; other fields must be equal. Priority is computed by `silong.rules.classify`.
"""

_WINDOWS_PROBE = (
    "$o=Get-CimInstance Win32_OperatingSystem; $c=Get-CimInstance Win32_Processor; "
    "[pscustomobject]@{os=$o.Caption; version=$o.Version; total_kb=$o.TotalVisibleMemorySize; "
    "free_kb=$o.FreePhysicalMemory; cpu=$c.Name} | ConvertTo-Json -Compress"
)


def load_fixtures(path: Path = DEFAULT_FIXTURES) -> list:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["reports"]


def windows_info() -> Optional[dict]:
    """Windows OS, CPU and memory through WSL interop, or None when unavailable."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return None
    try:
        done = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PROBE],
            capture_output=True, text=True, timeout=60, check=True,
        )
        info = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def local_info() -> dict:
    """CPU, OS and memory of the machine running the bench (Linux /proc when present)."""
    info = {"os": platform.platform(), "cpu": platform.processor() or None, "cpus": None,
            "total_kb": None, "available_kb": None}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = f"{line.split('=', 1)[1].strip().strip(chr(34))}, kernel {platform.release()}"
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            names = [line.split(":", 1)[1].strip() for line in fh if line.startswith("model name")]
        info["cpu"], info["cpus"] = (names[0] if names else info["cpu"]), len(names) or None
        with open("/proc/meminfo", encoding="utf-8") as fh:
            mem = dict(line.split(":", 1) for line in fh)
        info["total_kb"] = int(mem["MemTotal"].split()[0])
        info["available_kb"] = int(mem["MemAvailable"].split()[0])
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return info


def _get(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _loaded(client: httpx.Client) -> list:
    return [m.get("name") or m.get("model") for m in _get(client, "/api/ps").get("models", [])]


def unload_all(client: httpx.Client, *, sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> dict:
    """Ask Ollama to unload every loaded model; return what was loaded before and after."""
    before = _loaded(client)
    for name in before:
        client.post("/api/generate", json={"model": name, "keep_alive": 0}).raise_for_status()
    after = _loaded(client)
    deadline = clock() + UNLOAD_WAIT_S
    while after and clock() < deadline:
        sleep(1.0)
        after = _loaded(client)
    return {"before": before, "after": after}


def _norm(text: Optional[str]) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def compare(name: str, expected: Any, actual: Any) -> str:
    """'match', 'no', 'partial' (location only) or 'not scored' (summary)."""
    if name == "summary":
        return "not scored"
    if name == "injury_note":
        return "match" if (expected is None) == (actual is None) else "no"
    if name == "location" and expected is not None and actual is not None:
        want, got = _norm(expected), _norm(actual)
        if want == got:
            return "match"
        return "partial" if want and got and (want in got or got in want) else "no"
    return "match" if expected == actual else "no"


def run(model: str, client: httpx.Client, fixtures: list, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        windows: Optional[Callable[[], Optional[dict]]] = None,
        local: Optional[Callable[[], dict]] = None) -> dict:
    """Run the test for `model` against Ollama at `client`'s base URL; return the results."""
    windows, local = windows or windows_info, local or local_info
    result = {
        "model": model,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "address": str(client.base_url).rstrip("/"),
        "version": _get(client, "/api/version").get("version"),
        "tag": next((m for m in _get(client, "/api/tags").get("models", []) if m.get("name") == model), None),
        "unload": unload_all(client, sleep=sleep, clock=clock),
        "windows_before": windows(),
        "local_before": local(),
    }

    start = clock()
    try:
        extract.warm_up(model, client=client)
        result["warm_up_error"] = None
    except extract.ExtractionError as exc:
        result["warm_up_error"] = str(exc)
    result["cold_load_ms"] = round((clock() - start) * 1000)

    rows = []
    for report in fixtures:
        expected = report["expected"]
        row = {"id": report["id"], "text": report["text"], "expected": expected,
               "fields": None, "urgency": None, "reason": None, "error": None}
        start = clock()
        try:
            row["fields"] = extract.extract(report["text"], model, client=client)
        except extract.ExtractionError as exc:
            row["error"] = str(exc)
        row["llm_ms"] = round((clock() - start) * 1000)
        if row["fields"] is not None:
            row["urgency"], row["reason"] = rules.classify(row["fields"])
            row["matches"] = {name: compare(name, expected["fields"][name], row["fields"][name])
                              for name in rules.FIELD_NAMES}
        rows.append(row)
    result["reports"] = rows
    result["ps_after"] = _get(client, "/api/ps").get("models", [])
    result["windows_after"] = windows()
    return result


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def _mib(kb: Optional[int]) -> str:
    return "not recorded" if kb is None else f"{kb / 1024:,.0f} MiB"


def _windows_line(info: Optional[dict]) -> str:
    if not info:
        return "not recorded (Windows interop unavailable)"
    return f"total {_mib(info.get('total_kb'))}, free {_mib(info.get('free_kb'))}"


def _seconds(ms: int) -> str:
    return f"{ms:,} ms ({ms / 1000:.1f} s)"


def render(result: dict) -> str:
    """Markdown for one run."""
    tag = result["tag"] or {}
    details = tag.get("details") or {}
    windows, local = result["windows_before"] or {}, result["local_before"]
    unload = result["unload"]
    lines = [
        f"## `{result['model']}`, run {result['started']}",
        "",
        "### Environment",
        "",
        f"- Ollama: {result['address']}, version {result['version']} (from `/api/version`)",
        (f"- Model: `{result['model']}`, digest `{tag.get('digest', '')[:12]}`, {tag.get('size', 0):,} bytes, "
         f"{details.get('parameter_size', '?')}, {details.get('quantization_level', '?')} (from `/api/tags`)"
         if tag else f"- Model: `{result['model']}` not listed by `/api/tags`"),
        f"- CPU: {windows.get('cpu', '').strip() or local['cpu'] or 'not recorded'}",
        f"- Ollama host OS: {windows.get('os', 'not recorded')} {windows.get('version', '')}".rstrip(),
        f"- Bench OS: {local['os']}; {local['cpus'] or '?'} logical CPUs visible",
        f"- Loaded before unload (`/api/ps`): {', '.join(unload['before']) or 'none'}; "
        f"after unload: {', '.join(unload['after']) or 'none'}",
        f"- Windows memory before warm-up: {_windows_line(result['windows_before'])}",
        f"- Bench machine memory before warm-up: total {_mib(local['total_kb'])}, "
        f"available {_mib(local['available_kb'])}",
        f"- Cold load (one warm-up request): {_seconds(result['cold_load_ms'])}"
        + (f", failed: {result['warm_up_error']}" if result["warm_up_error"] else ""),
    ]
    for model in result["ps_after"]:
        lines.append(f"- Loaded after run (`/api/ps`): `{model.get('name')}`, size {model.get('size', 0):,} bytes, "
                     f"size_vram {model.get('size_vram', 0):,}, context {model.get('context_length', '?')}")
    lines.append(f"- Windows memory after run: {_windows_line(result['windows_after'])}")

    rows = result["reports"]
    times = [row["llm_ms"] for row in rows if row["error"] is None]
    priority_ok = sum(row["urgency"] == row["expected"]["urgency"] for row in rows)
    fields_ok = sum(row["matches"][name] == "match" for row in rows if row["error"] is None
                    for name in SCORED_FIELDS)
    lines += [
        "",
        "### Summary",
        "",
        "| # | Report | Expected priority | Actual priority | Scored fields matched | llm_ms |",
        "|---|---|---|---|---|---|",
    ]
    for number, row in enumerate(rows, 1):
        matched = (f"{sum(row['matches'][n] == 'match' for n in SCORED_FIELDS)}/{len(SCORED_FIELDS)}"
                   if row["error"] is None else "failed")
        actual = row["urgency"] or f"failed: {row['error']}"
        lines.append(f"| {number} | {row['id']} | {row['expected']['urgency']} | {_cell(actual)} | "
                     f"{matched} | {row['llm_ms']:,} |")
    lines += [
        "",
        f"Priority matched: {priority_ok}/{len(rows)}. Scored fields matched: {fields_ok}/"
        f"{len(SCORED_FIELDS) * len(rows)}. Failed extractions: {len(rows) - len(times)}.",
    ]
    if times:
        lines.append(f"Successful `llm_ms`: min {min(times):,}, median {statistics.median(times):,.0f}, "
                     f"max {max(times):,}, total {sum(times):,}.")

    for number, row in enumerate(rows, 1):
        expected = row["expected"]
        lines += [
            "",
            f"### {number}. {row['id']}",
            "",
            f"> {row['text']}",
            "",
            f"- Expected priority: {expected['urgency']} ({expected['reason']})",
        ]
        if row["error"] is not None:
            lines += [f"- Actual: **failed**: {row['error']}", f"- llm_ms: {row['llm_ms']:,}"]
            continue
        lines += [
            f"- Actual priority: {row['urgency']} ({row['reason']})"
            + ("" if row["urgency"] == expected["urgency"] else " **differs**"),
            f"- llm_ms: {row['llm_ms']:,}",
            "",
            "| Field | Expected | Actual | Match |",
            "|---|---|---|---|",
        ]
        for name in rules.FIELD_NAMES:
            lines.append(f"| {name} | {_cell(expected['fields'][name])} | {_cell(row['fields'][name])} | "
                         f"{row['matches'][name]} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m silong.bench", description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="Ollama model tag, for example qwen3:4b")
    parser.add_argument("--out", type=Path, help="Markdown file to append the results to")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)

    try:
        base_url = extract.ollama_url()
    except extract.ExtractionError as exc:
        print(exc, file=sys.stderr)
        return 2
    with httpx.Client(base_url=base_url, timeout=extract.TIMEOUT) as client:
        try:
            result = run(args.model, client, load_fixtures(args.fixtures))
        except httpx.HTTPError as exc:
            print(f"Could not use Ollama at {base_url}: {exc}", file=sys.stderr)
            return 1
    text = render(result)
    print(text)
    if args.out:
        new_file = not args.out.exists() or args.out.stat().st_size == 0
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write((FILE_HEADER if new_file else "") + "\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
