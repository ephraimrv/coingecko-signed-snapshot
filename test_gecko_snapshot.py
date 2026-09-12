"""Tests for gecko_snapshot.

The emphasis is on inputs that could plausibly break the script, rather than on
the happy path. Each of the failure-mode tests below corresponds to a defect
found by exercising an earlier version of this code.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import pytest
from pytest import CaptureFixture

import gecko_snapshot as gs


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patched_urlopen(body: bytes) -> Any:
    return patch("urllib.request.urlopen", return_value=_FakeResponse(body))


BTC_ROW = {
    "market_cap_rank": 1,
    "id": "bitcoin",
    "symbol": "btc",
    "name": "Bitcoin",
    "current_price": 4818321,
    "price_change_percentage_1h_in_currency": -0.1,
    "price_change_percentage_24h_in_currency": -1.11366,
    "price_change_percentage_7d_in_currency": -5.0,
    "price_change_percentage_30d_in_currency": 20.2,
    "total_volume": 1821667681613,
    "market_cap": 96765316370803,
    "fully_diluted_valuation": 96765422374232,
    "circulating_supply": 20082718.0,
    "total_supply": 20082787.0,
    "max_supply": 21000000.0,
    "last_updated": "2026-09-11T11:01:10.000Z",
}


# --------------------------------------------------------------------------
# build_url
# --------------------------------------------------------------------------
def test_build_url_includes_all_four_windows() -> None:
    url = gs.build_url("php", 100)
    assert "price_change_percentage=1h%2C24h%2C7d%2C30d" in url
    assert "vs_currency=php" in url
    assert "order=market_cap_desc" in url


def test_build_url_omits_api_key_when_absent() -> None:
    assert "x_cg_demo_api_key" not in gs.build_url("usd", 10)


def test_build_url_includes_api_key_when_present() -> None:
    assert "x_cg_demo_api_key=secret" in gs.build_url("usd", 10, api_key="secret")


# --------------------------------------------------------------------------
# fetch — failure modes
# --------------------------------------------------------------------------
def test_fetch_raises_snapshot_error_on_html_body() -> None:
    """A rate-limit page is HTML. This must not surface as a JSONDecodeError."""
    with (
        _patched_urlopen(b"<html><body>429</body></html>"),
        pytest.raises(gs.SnapshotError, match="did not return JSON"),
    ):
        gs.fetch("http://example.invalid")


def test_fetch_raises_snapshot_error_on_json_object() -> None:
    with (
        _patched_urlopen(b'{"status": {"error_code": 429}}'),
        pytest.raises(gs.SnapshotError, match="Expected a JSON array"),
    ):
        gs.fetch("http://example.invalid")


def test_fetch_raises_snapshot_error_on_empty_body() -> None:
    with _patched_urlopen(b""), pytest.raises(gs.SnapshotError):
        gs.fetch("http://example.invalid")


def test_fetch_hints_at_rate_limiting_on_429() -> None:
    error = urllib.error.HTTPError("http://x", 429, "Too Many Requests", Message(), None)
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(gs.SnapshotError, match="Rate limited"),
    ):
        gs.fetch("http://example.invalid")


def test_fetch_reports_unreachable_host() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")),
        pytest.raises(gs.SnapshotError, match="Could not reach"),
    ):
        gs.fetch("http://example.invalid")


def test_fetch_returns_empty_list_unchanged() -> None:
    with _patched_urlopen(b"[]"):
        assert gs.fetch("http://example.invalid") == []


def test_fetch_parses_valid_array() -> None:
    with _patched_urlopen(json.dumps([BTC_ROW]).encode()):
        rows = gs.fetch("http://example.invalid")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "btc"


# --------------------------------------------------------------------------
# derive — boundary values
# --------------------------------------------------------------------------
def test_derive_computes_both_ratios() -> None:
    out = gs.derive(BTC_ROW)
    assert out["mc_fdv_ratio"] == 1.0
    assert out["volume_pct_of_mcap"] == pytest.approx(1.883, abs=0.001)


def test_derive_preserves_legitimate_zero_volume() -> None:
    """A token with no 24h volume must yield 0.0, not None.

    Testing truthiness on the numerator would discard this real value.
    """
    out = gs.derive({"market_cap": 1000, "fully_diluted_valuation": 1000, "total_volume": 0})
    assert out["volume_pct_of_mcap"] == 0.0


def test_derive_returns_none_for_zero_denominator() -> None:
    out = gs.derive({"market_cap": 0, "fully_diluted_valuation": 1000, "total_volume": 50})
    assert out["volume_pct_of_mcap"] is None


def test_derive_returns_none_for_null_fdv() -> None:
    """fully_diluted_valuation is null for many coins; it must not raise."""
    out = gs.derive({"market_cap": 1000, "fully_diluted_valuation": None, "total_volume": 50})
    assert out["mc_fdv_ratio"] is None
    assert out["volume_pct_of_mcap"] == 5.0


def test_derive_handles_missing_keys() -> None:
    out = gs.derive({})
    assert out["mc_fdv_ratio"] is None
    assert out["volume_pct_of_mcap"] is None


def test_derive_rejects_booleans() -> None:
    """bool subclasses int, so isinstance(True, int) is True. Guard against it."""
    out = gs.derive({"market_cap": True, "fully_diluted_valuation": 1000, "total_volume": 50})
    assert out["mc_fdv_ratio"] is None
    assert out["volume_pct_of_mcap"] is None


def test_derive_does_not_mutate_input() -> None:
    original = dict(BTC_ROW)
    gs.derive(original)
    assert "mc_fdv_ratio" not in original


# --------------------------------------------------------------------------
# write_csv
# --------------------------------------------------------------------------
def test_write_csv_preserves_signs(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.csv"
    gs.write_csv([BTC_ROW], out)
    row = next(csv.DictReader(out.open(encoding="utf-8")))
    assert row["price_change_percentage_7d_in_currency"] == "-5.0"
    assert row["price_change_percentage_30d_in_currency"] == "20.2"


def test_write_csv_stamps_every_row(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.csv"
    gs.write_csv([BTC_ROW, BTC_ROW], out, captured_at="2026-09-11T11:03:58+00:00")
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["captured_at_utc"] for r in rows] == ["2026-09-11T11:03:58+00:00"] * 2


def test_write_csv_creates_missing_parent_directories(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "snapshot.csv"
    assert gs.write_csv([BTC_ROW], out) == 1
    assert out.exists()


def test_write_csv_writes_header_only_for_empty_input(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.csv"
    assert gs.write_csv([], out) == 0
    assert out.read_text(encoding="utf-8").strip().startswith("market_cap_rank,")


def test_write_csv_ignores_unexpected_fields(tmp_path: Path) -> None:
    """CoinGecko may add fields; extras must not raise ValueError."""
    out = tmp_path / "snapshot.csv"
    gs.write_csv([{**BTC_ROW, "some_new_field": 1}], out)
    assert out.exists()


# --------------------------------------------------------------------------
# main — exit codes
# --------------------------------------------------------------------------
def test_main_returns_error_code_on_fetch_failure(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    with patch.object(gs, "fetch", side_effect=gs.SnapshotError("boom")):
        code = gs.main(["--out", str(tmp_path / "x.csv")])
    assert code == gs.EXIT_ERROR
    assert "error: boom" in capsys.readouterr().err


def test_main_succeeds_and_reports_row_count(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    out = tmp_path / "x.csv"
    with patch.object(gs, "fetch", return_value=[BTC_ROW]):
        code = gs.main(["--out", str(out)])
    assert code == gs.EXIT_OK
    assert "Wrote 1 rows" in capsys.readouterr().err


def test_main_warns_when_no_coins_returned(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    with patch.object(gs, "fetch", return_value=[]):
        code = gs.main(["--out", str(tmp_path / "x.csv")])
    assert code == gs.EXIT_OK
    assert "warning" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [["--per-page", "0"], ["--per-page", "251"], ["--timeout", "0"]])
def test_main_rejects_out_of_range_arguments(bad: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        gs.main(bad)
    assert exc.value.code == 2
