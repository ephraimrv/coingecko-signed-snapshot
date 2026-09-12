# coingecko-signed-snapshot

Retrieve a cross-sectional CoinGecko market snapshot — many coins at one
instant — with signed percentage changes and a capture timestamp on every row.

[![CI](https://github.com/ephraimrv/coingecko-signed-snapshot/actions/workflows/ci.yml/badge.svg)](https://github.com/ephraimrv/coingecko-signed-snapshot/actions/workflows/ci.yml)

---

## Why this exists

CoinGecko's website does offer CSV downloads, but only **per coin, as a time
series**. The Bitcoin export looks like this:

```
event_date,close_price_usd,market_cap_usd,volume_usd
2013-04-28 00:00:00 UTC,141.96,1500517590,0
...
2026-09-11 00:00:00 UTC,77203.53444805603,1537413069633.6875,30257901047.74022
```

Four columns, one coin, 4,885 daily rows. That is a **longitudinal** dataset,
and it is well-formed.

What the website does **not** offer is a download of the cross-sectional view —
the ranked table of many coins as they stand right now, with their 1h, 24h, 7d
and 30d changes, fully diluted valuations and supply figures. That table is
rendered on the page, but there is no export button for it.

To approximate one such snapshot by hand:

| | Via the website | Via this script |
|---|---|---|
| Manual downloads for 100 coins | 100 | 0 |
| Percentage changes | not included — compute them yourself from closes | included, signed |
| Fully diluted valuation | not available | included |
| Circulating / total / max supply | not available | included |
| Market-cap rank | sort them yourself | included |
| Capture timestamp | none | on every row |

This script makes one API call and writes the whole thing.

## The problem that prompted it

Lacking an export for the ranked table, the obvious workaround is to scrape the
rendered page with a browser table-extraction extension. That was tried first,
on 11 September 2026, and it failed in a way that was not visible in the output.

On the page, price direction is conveyed by **colour**: red for a fall, green
for a rise. The digits themselves carry no sign. A DOM scrape captures the text
and discards the styling, so every percentage arrives as a bare magnitude:

| Coin | Scraped table (7d) | REST API (7d) |
|------|-------------------:|--------------:|
| BTC  |             `5.1%` |        `-5.0` |
| ETH  |             `2.2%` |        `-2.3` |
| BNB  |             `0.8%` |        `-0.8` |
| XRP  |             `7.9%` |        `-8.2` |
| SOL  |             `4.7%` |        `-4.7` |

Every one of those weekly changes was negative. Read at face value, the scraped
file said the market had risen.

Counting across the whole scrape makes it unambiguous:

```
Negative values in the scraped table (100 coins, 400 cells):   0
Negative values in this script's output (20 coins,  80 cells): 46
```

Zero negatives in four hundred cells is not a market condition. The scrape had
silently dropped information that existed only in the presentation layer.

**This is not a defect in CoinGecko's data or in their CSV export.** It is a
general property of scraping rendered HTML: anything encoded in colour,
position or styling is lost when you take the text. The REST API returns the
same figures as signed floats, because an API has no presentation layer to
lose.

## What it produces

The standard `/coins/markets` fields, plus:

| Column | Meaning |
|---|---|
| `price_change_percentage_{1h,24h,7d,30d}_in_currency` | Signed floats, as returned by the API |
| `last_updated` | CoinGecko's own record-update time |
| `captured_at_utc` | Stamped at fetch time, so the snapshot is dateable |
| `mc_fdv_ratio` | Market cap ÷ fully diluted valuation. Below 1.0 means supply is still locked |
| `volume_pct_of_mcap` | 24h volume as a percentage of market cap — a liquidity proxy |

### A note on comparing figures across sources

The API's `price_change_percentage_7d_in_currency` measures back from the moment
of the request, not from a 00:00 UTC daily close, and it is denominated in
whatever `--vs-currency` was requested. Computing a seven-day change from the
daily closes in CoinGecko's per-coin USD export will therefore give a different
number — for Bitcoin on 11 September 2026, −3.10% from USD daily closes against
−5.0% from the PHP-denominated API field. The direction agrees; the magnitudes
should not be expected to. The two are not interchangeable.

## Installation

No third-party dependencies. Python 3.12 or newer, standard library only.

```bash
git clone https://github.com/ephraimrv/coingecko-signed-snapshot.git
cd coingecko-signed-snapshot
```

## Usage

```bash
# Top 100 coins priced in Philippine pesos
python gecko_snapshot.py --vs-currency php --per-page 100 --out snapshot.csv

# Top 20 in US dollars
python gecko_snapshot.py --vs-currency usd --per-page 20 --out top20.csv

# With a free CoinGecko Demo API key, to avoid rate limiting
python gecko_snapshot.py --api-key "$COINGECKO_KEY" --out snapshot.csv
```

| Option | Default | Notes |
|---|---|---|
| `--vs-currency` | `php` | Any currency CoinGecko supports |
| `--per-page` | `100` | 1–250 |
| `--api-key` | none | CoinGecko Demo key; optional |
| `--out` | `gecko_snapshot.csv` | Parent directories are created if absent |
| `--timeout` | `30` | HTTP timeout in seconds |

Exit codes: `0` success, `1` a reported error, `2` bad arguments.

Unauthenticated requests are rate-limited. If you hit that limit the script
reports it in plain language rather than raising a traceback; a free Demo key
is available from CoinGecko and removes the problem.

## Example output

`data/` contains a snapshot of the top 100 coins priced in pesos, taken on
11 September 2026, generated by this script.

## Development

```bash
python -m pip install -e ".[dev]"

ruff check .            # lint
ruff format --check .   # formatting
mypy --strict gecko_snapshot.py test_gecko_snapshot.py
pytest                  # 28 tests
```

All four run in CI on every push.

### Notes on the test suite

The tests deliberately target inputs that could break the script rather than
inputs that confirm it works. Each of the following corresponds to a defect
found by exercising an earlier version:

- **A non-JSON response body.** CoinGecko serves an HTML page when rate
  limiting. The first version raised a bare `JSONDecodeError`.
- **A missing output directory.** Writing to `data/out.csv` before `data/`
  existed raised an uncaught `FileNotFoundError`.
- **A legitimate zero.** A token with no 24h volume must yield `0.0`, not
  `None`. Testing truthiness on the numerator silently discarded the real
  value — LEO Token turns over 0.002% of its market cap, so this is not
  hypothetical.
- **Booleans in numeric fields.** `isinstance(True, int)` is `True` in Python,
  because `bool` subclasses `int`. Without an explicit guard, a JSON `true`
  would be treated as `1`.
- **A null `fully_diluted_valuation`.** Null for many coins; must not raise.
- **An empty result array.** Produces a header-only file, which the script now
  warns about rather than reporting as success.

## Background

Written while completing a DICT blockchain training exercise that required
market research before a trade. The ranked market table was the data wanted;
scraping it from the page was the first approach; the missing signs were caught
only by noticing that not one of four hundred percentage cells was negative
during a week in which the market had fallen.

The lesson generalises well beyond this API: **presentation is not data.**
Where a value's meaning depends on how it is displayed, scraping the display
loses it, and the resulting file looks complete while being wrong. Where a
structured source exists, use it.

## Licence

MIT. See [LICENSE](LICENSE).

## Author

Jan Ephraim R. Vallente
