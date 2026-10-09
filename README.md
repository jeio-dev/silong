# Silong

Offline evacuation-center hub. A computer at the center serves a local web app and runs all AI locally; phones on the same Wi-Fi submit reports without internet access. See `PRD.md` for the product.

Status: under construction (phase 01, durable text intake). The web app and launcher are not available yet.

## Setup

Requires Python 3.11 or newer. Run from the repository root.

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

On Debian/Ubuntu, `python3 -m venv` fails if the `python3-venv` package is missing. Install that package, or create the environment with [uv](https://docs.astral.sh/uv/) instead: `uv venv --seed .venv`, then activate it and install as above.

Faster-whisper, qrcode and Ollama are not needed for the base install.

## Configuration

Set these environment variables before starting the hub. Blank or absent values use the defaults.

| Variable | Values | Default |
| --- | --- | --- |
| `SILONG_MODEL` | Ollama model tag | none (AI triage not configured) |
| `SILONG_WHISPER` | `small`, `base`, `off` | `off` |
| `SILONG_PORT` | 1–65535 | `8000` |
| `SILONG_DB` | SQLite file path | `silong.db` |

An invalid `SILONG_PORT` or `SILONG_WHISPER` is rejected with a message naming the variable.

## Checks

```sh
python -m unittest discover -s tests -v
```
