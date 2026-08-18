# Cross-Platform Prediction Market Arbitrage Scanner

Polls **Kalshi** and **Polymarket** public APIs, matches equivalent contracts across venues, and flags live quote combinations where buying both sides of the same event would cost less than the $1 payout.

This tool **detects** opportunities from public market data. It does **not** place trades.

## What arbitrage is

Arbitrage is buying and selling equivalent exposure so that profit does not depend on which outcome occurs. In a binary prediction market, a YES contract and a NO contract on the same event are complementary: exactly one of them pays $1 at settlement (the other pays $0).

If you can buy YES on one venue and NO on the other for a combined price below $1 (after fees, slippage, and any rules mismatch), the pair is fully hedged. One leg wins $1; the other expires worthless; your profit is `$1 − combined cost`.

Kalshi and Polymarket are separate venues, not a single order book. You cannot move a Kalshi contract onto Polymarket. Cross-platform arb therefore means holding **offsetting claims**, not transferring a position.

### Worked example

Suppose both venues list the same question: “Will Candidate X win?”

| Venue       | What you can buy | Ask price |
| ----------- | ---------------- | --------- |
| Kalshi      | YES              | $0.41     |
| Polymarket  | NO               | $0.52     |

You buy 100 Kalshi YES contracts for $41 and 100 Polymarket NO shares for $52. Combined cost is $93.

- If X wins, Kalshi YES pays $100 and Polymarket NO pays $0.
- If X loses, Polymarket NO pays $100 and Kalshi YES pays $0.

Either way you receive $100 on a $93 outlay — **$7 gross**, before trading fees, deposit/withdrawal costs, and fill risk. If estimated taker fees are ~$2, net edge is about $5 on 100 contracts (~5¢ per contract).

The inverse book also matters: Kalshi NO + Polymarket YES. The scanner checks both directions, plus same-side dislocations (buy YES cheaper on A than you can sell YES on B). Same-side prints are informational; they are not a locked payoff unless you can actually sell the other venue’s contract.

### Why a print is not free money

- **Rules risk:** titles can look identical while resolution sources, cutoff times, or edge cases differ.
- **Leg risk:** one fill can land while the other misses or walks.
- **Fees and size:** Kalshi and Polymarket both charge taker fees that peak near 50¢ prices. Top-of-book size may be tiny.
- **Capital and settlement:** USD on a regulated exchange vs on-chain collateral; withdrawals and resolution lag tie up funds on both sides.

Treat every hit as a quote snapshot that still needs a human rules check.

## How the scanner works

1. Download open Kalshi events (nested markets) and active Polymarket markets.
2. Fuzzy-match questions (and candidate names for multi-outcome events).
3. Optionally search Polymarket for high-volume Kalshi titles that did not match in the local catalog.
4. Refresh live top-of-book quotes (Kalshi market snapshots + Polymarket CLOB books).
5. Flag complementary buys where `yes_ask + no_ask + estimated fees < 1`, and same-side `ask < other_bid`.

Public endpoints (no API keys):

- Kalshi Trade API: `https://external-api.kalshi.com/trade-api/v2`
- Polymarket Gamma: `https://gamma-api.polymarket.com`
- Polymarket CLOB: `https://clob.polymarket.com`

## Setup

```bash
cd PredictionMarket_Arbitrage
python3 -m pip install -r requirements.txt
```

## Usage

Single scan:

```bash
python3 arb_scanner.py --once
```

Poll every 20 seconds:

```bash
python3 arb_scanner.py --interval 20
```

Stricter matching, ignore sub-2¢ edges, include sports:

```bash
python3 arb_scanner.py --once --min-match 0.72 --min-edge 0.02 --include-sports
```

JSON lines to stdout (plus a human summary on stderr):

```bash
python3 arb_scanner.py --once --json
```

Pin known equivalents instead of (or in addition to) fuzzy matching:

```bash
python3 arb_scanner.py --once --pairs mappings.json
```

`mappings.json` format:

```json
[
  {
    "label": "OpenAI IPOs first",
    "kalshi_ticker": "KXOAIANTH-40-OAI",
    "poly_yes_token": "<clob token id for OpenAI>",
    "poly_no_token": "<clob token id for Anthropic>"
  }
]
```

### Useful flags

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `--interval` | `20` | Seconds between quote refreshes |
| `--once` | off | Run one catalog+quote pass and exit |
| `--min-edge` | `0.005` | Minimum net edge per contract (dollars) |
| `--min-match` | `0.64` | Fuzzy-match score threshold (0–1) |
| `--kalshi-events` | `800` | Max Kalshi events to page in |
| `--poly-markets` | `400` | Max Polymarket markets by 24h volume |
| `--include-sports` | off | Include Kalshi Sports category (noisier matches) |
| `--search-unmatched` | `15` | Extra Polymarket keyword searches for unmatched Kalshi titles |
| `--same-side` | off | Also print buy-YES / sell-YES dislocations (not a $1 lock) |
| `--no-fees` | off | Skip fee estimates (gross edge only) |

## Disclaimer

Educational market-data tool. Not trading advice. Venue terms, geo restrictions, and applicable law still apply. Fee formulas are estimates and can change by series or category.
