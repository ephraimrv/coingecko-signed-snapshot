#!/usr/bin/env python3
"""Fetch a CoinGecko market snapshot with signed percentages and a capture timestamp.

THE PROBLEM (upstream, not in this script)
------------------------------------------
CoinGecko's website offers a "download CSV" button. The browser shows price
direction as red or green text, but the exported file keeps only the magnitude.
A cell reading "5.1%" in that download may mean +5.1% or -5.1%, and nothing in
the file distinguishes them. The export also carries no capture timestamp, so a
snapshot cannot be dated or reproduced afterwards.

This is not cosmetic. In a snapshot taken on 11 September 2026, the website
export showed Bitcoin's seven-day change as "5.1%". The true value was -5.0%.

THE FIX (what this script writes)
---------------------------------
The same data, retrieved from CoinGecko's public REST API, where percentage
changes are returned as signed floats. Every output row additionally carries
CoinGecko's own ``last_updated`` value and a ``captured_at_utc`` stamp written
at fetch time, so the snapshot is dateable and reproducible.

Two derived columns are added because they are useful and sign-independent:

``mc_fdv_ratio``
    Market cap divided by fully diluted valuation. Below 1.0 means supply is
    still locked and will enter circulation later.

``volume_pct_of_mcap``
    Twenty-four hour volume as a percentage of market cap. A proxy for
    liquidity; very low values indicate a thin market.

Usage
-----
    python gecko_snapshot.py --vs-currency php --per-page 100 --out snapshot.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

__version__ = "1.0.0"
__author__ = "Jan Ephraim R. Vallente"
__license__ = "MIT"

BASE_URL = "https://api.coingecko.com/api/v3/coins/markets"
WINDOWS: tuple[str, ...] = ("1h", "24h", "7d", "30d")
MAX_PER_PAGE = 250
DEFAULT_TIMEOUT = 30

EXIT_OK = 0
EXIT_ERROR = 1

FIELDS: list[str] = [
    "market_cap_rank",
    "id",
    "symbol",
    "name",
    "current_price",
    *[f"price_change_percentage_{window}_in_currency" for window in WINDOWS],
    "total_volume",
    "market_cap",
    "fully_diluted_valuation",
    "circulating_supply",
    "total_supply",
    "max_supply",
    "last_updated",
]

DERIVED_FIELDS: list[str] = ["mc_fdv_ratio", "volume_pct_of_mcap", "captured_at_utc"]


class SnapshotError(Exception):
    """A recoverable failure worth reporting to the user without a traceback.

    Raised instead of calling ``SystemExit`` from within library functions, so
    that those functions stay importable and testable. ``main`` is the only
    place that decides to terminate the process.
    """


def _is_number(value: Any) -> TypeGuard[float]:
    """True for a real int or float, excluding bool.

    ``isinstance(True, int)`` is True in Python, because ``bool`` subclasses
    ``int``. Without the explicit exclusion, a JSON ``true`` in a numeric field
    would be silently treated as 1.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_url(
    vs_currency: str,
    per_page: int,
    api_key: str | None = None,
    page: int = 1,
) -> str:
    """Assemble the /coins/markets request URL."""
    params = {
        "vs_currency": vs_currency,
        "order": "market_cap_desc",
        "per_page": str(per_page),
        "page": str(page),
        "price_change_percentage": ",".join(WINDOWS),
    }
    if api_key:
        params["x_cg_demo_api_key"] = api_key
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """Retrieve and decode the market list.

    Raises:
        SnapshotError: on transport failure, a non-JSON body, or a payload that
            is not the expected JSON array.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 429:
            hint = " Rate limited: wait, or supply a free Demo key via --api-key."
        elif exc.code in (401, 403):
            hint = " Check the API key supplied to --api-key."
        raise SnapshotError(f"CoinGecko returned HTTP {exc.code}: {exc.reason}.{hint}") from exc
    except urllib.error.URLError as exc:
        raise SnapshotError(f"Could not reach CoinGecko: {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A rate-limit or maintenance page is served as HTML, not JSON. Without
        # this branch the caller sees a bare JSONDecodeError traceback.
        preview = raw[:120].decode("utf-8", errors="replace").strip()
        raise SnapshotError(
            f"CoinGecko did not return JSON (starts with: {preview!r}). "
            "This usually means a rate-limit or error page was served."
        ) from exc

    if not isinstance(payload, list):
        raise SnapshotError(
            f"Expected a JSON array of coins, got {type(payload).__name__}. "
            f"Body: {str(payload)[:200]}"
        )

    return payload


def derive(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *row* with the two derived ratio columns added.

    The input is not mutated. Denominators must be present and non-zero;
    numerators need only be present, so a legitimate 0.0 volume is preserved
    rather than being discarded as though it were missing.
    """
    enriched = dict(row)

    market_cap = enriched.get("market_cap")
    fdv = enriched.get("fully_diluted_valuation")
    volume = enriched.get("total_volume")

    enriched["mc_fdv_ratio"] = (
        round(market_cap / fdv, 4)
        if _is_number(market_cap) and _is_number(fdv) and fdv != 0
        else None
    )
    enriched["volume_pct_of_mcap"] = (
        round(volume / market_cap * 100, 3)
        if _is_number(volume) and _is_number(market_cap) and market_cap != 0
        else None
    )
    return enriched


def write_csv(rows: list[dict[str, Any]], path: Path, captured_at: str | None = None) -> int:
    """Write *rows* to *path* as CSV and return the number of data rows written.

    Missing parent directories are created. Raises SnapshotError if the file
    cannot be written.
    """
    captured = captured_at or datetime.now(UTC).isoformat(timespec="seconds")
    columns = [*FIELDS, *DERIVED_FIELDS]

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                enriched = derive(row)
                enriched["captured_at_utc"] = captured
                writer.writerow(enriched)
    except OSError as exc:
        raise SnapshotError(f"Could not write {path}: {exc}") from exc

    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gecko_snapshot",
        description=__doc__,
        # The default formatter re-wraps the description into one block, which
        # destroys the paragraph structure above.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--vs-currency",
        default="php",
        help="Quote currency, e.g. php, usd, eur (default: %(default)s)",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help=f"Number of coins to retrieve, 1-{MAX_PER_PAGE} (default: %(default)s)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="CoinGecko Demo API key. Optional, but avoids rate limiting.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("gecko_snapshot.csv"),
        help="Output CSV path (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not 1 <= args.per_page <= MAX_PER_PAGE:
        parser.error(f"--per-page must be between 1 and {MAX_PER_PAGE}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.vs_currency.strip():
        parser.error("--vs-currency must not be empty")

    url = build_url(args.vs_currency.strip().lower(), args.per_page, args.api_key)

    try:
        rows = fetch(url, timeout=args.timeout)
        written = write_csv(rows, args.out)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if written == 0:
        print(
            f"warning: CoinGecko returned no coins; {args.out} contains headers only. "
            f"Check that --vs-currency '{args.vs_currency}' is supported.",
            file=sys.stderr,
        )
    else:
        print(f"Wrote {written} rows to {args.out}", file=sys.stderr)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
