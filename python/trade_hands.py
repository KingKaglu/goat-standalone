"""trade_status: the voice answers "do we have any profit?" itself
(2026-11-02, from his chat log).

2026-11-01 23:13-23:19, while the demo BTC and SOL trades were open, he asked
eight money questions in six minutes: "How much demo money do we have right
now?", "Do we have any profit so far?", "How long before we see results?",
"If the first trade hits its mark, how much would our profit be?", "how many
did we put in?", "How much USDT is that?", "how much would we lose if the
target is below...". Every one went to Claude and back, slowly, and the
voice could only relay. This hand answers all of them at once, with no
Claude usage:

  - the open trades, read from workspace/trading/demo-journal.md (the trading
    skill logs every order there: pair, size, entry, stop, target, Result)
  - the live price from Binance's public market API (read-only, no key, no
    account access; the demo market follows the live price)
  - per trade and in total: money in, value now, profit/loss now, profit at
    the target, loss at the stop, how far price is from each
  - whether price has already TOUCHED the stop or the target since the trade
    opened (1 h candles), because an OCO then closed it on Binance even if
    the journal still says Open

Read-only by design: it never places, changes or cancels an order. Orders
stay with Claude and the trading skill, and each one needs his yes.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(ROOT, "workspace", "trading", "demo-journal.md")
API = "https://api.binance.com/api/v3"
FEE = 0.001          # Binance spot taker fee per side, for the "after fees" note

NAMES = {"trade_status"}
DECLS = [
    {"name": "trade_status",
     "description": "His open Binance demo trades, live: money in, value now, profit "
                    "or loss now, profit if each hits its target, loss if it hits its "
                    "stop, distance to both, and whether price already touched a stop "
                    "or target (so the order may have closed). Use it yourself for "
                    "any 'do we have profit', 'how are my trades', 'how much did we "
                    "put in', 'how much would we make / lose'. Read-only: it never "
                    "places or changes orders.",
     "parameters": {"type": "object", "properties": {
         "pair": {"type": "string",
                  "description": "optional, e.g. BTC or SOLUSDT; empty = all open trades"}}}},
]

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _num(cell: str) -> float | None:
    m = _NUM.search(cell or "")
    return float(m.group(0).replace(",", "")) if m else None


def open_trades(text: str) -> list:
    """Rows of the journal table whose Result starts with 'Open'."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        when, pair, side, size, entry, stop, target, _why, result = cells[:9]
        if not result.lower().startswith("open"):
            continue
        sym = pair.replace("/", "").replace(" ", "").upper()
        qty, px = _num(size), _num(entry)
        if not (sym and qty and px):
            continue
        try:
            t0 = _dt.datetime.strptime(when[:16], "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            t0 = None
        out.append({"when": when, "t0": t0, "pair": pair, "symbol": sym,
                    "long": not side.lower().startswith("sell"),
                    "qty": qty, "entry": px, "stop": _num(stop), "target": _num(target)})
    return out


def _get(path: str, timeout: float = 8.0):
    with urllib.request.urlopen(f"{API}/{path}", timeout=timeout) as r:
        return json.load(r)


def prices(symbols: list) -> dict:
    q = urllib.parse.quote(json.dumps(sorted(set(symbols)), separators=(",", ":")))
    return {d["symbol"]: float(d["price"]) for d in _get(f"ticker/price?symbols={q}")}


def touched(symbol: str, t0: float | None) -> tuple:
    """(lowest low, highest high) since t0, from 1 h candles; (None, None) if
    unknown. Candles are coarse: they say a level was reached, not when."""
    if not t0:
        return None, None
    rows = _get(f"klines?symbol={symbol}&interval=1h&startTime={int(t0 * 1000)}&limit=1000")
    if not rows:
        return None, None
    return min(float(r[3]) for r in rows), max(float(r[2]) for r in rows)


def _usd(x: float) -> str:
    return f"{'+' if x > 0 else '-' if x < 0 else ''}{abs(x):,.2f} USDT"


def _pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%"


def report(trades: list, px: dict, ranges: dict | None = None) -> str:
    ranges = ranges or {}
    lines, tot_in, tot_now, tot_win, tot_loss = [], 0.0, 0.0, 0.0, 0.0
    for t in trades:
        p = px.get(t["symbol"])
        if p is None:
            lines.append(f"{t['pair']}: no live price from Binance right now.")
            continue
        sign = 1 if t["long"] else -1
        cost = t["qty"] * t["entry"]
        pnl = sign * (p - t["entry"]) * t["qty"]
        tot_in += cost
        tot_now += pnl
        s = (f"{t['pair']} {'long' if t['long'] else 'short'} {t['qty']:g} since "
             f"{t['when']}: put in {cost:,.2f} USDT at {t['entry']:,.2f}; now "
             f"{p:,.2f} ({_pct(p, t['entry'])}), worth {t['qty'] * p:,.2f} USDT, "
             f"profit/loss now {_usd(pnl)}")
        if t["target"]:
            win = sign * (t["target"] - t["entry"]) * t["qty"]
            tot_win += win
            s += (f"; at target {t['target']:,.2f} {_usd(win)} "
                  f"(price must move {_pct(t['target'], p)})")
        if t["stop"]:
            loss = sign * (t["stop"] - t["entry"]) * t["qty"]
            tot_loss += loss
            s += (f"; at stop {t['stop']:,.2f} {_usd(loss)} "
                  f"({_pct(t['stop'], p)} away)")
        lo, hi = ranges.get(t["symbol"], (None, None))
        hit_stop = lo is not None and t["stop"] and (
            lo <= t["stop"] if t["long"] else hi >= t["stop"])
        hit_tgt = hi is not None and t["target"] and (
            hi >= t["target"] if t["long"] else lo <= t["target"])
        if hit_tgt:
            s += (". WARNING: price already reached the target since the trade "
                  "opened, so the order has probably CLOSED in profit; check Binance "
                  "Orders before quoting it as open")
        elif hit_stop:
            s += (". WARNING: price already reached the stop since the trade "
                  "opened, so the stop has probably CLOSED it at a loss; check "
                  "Binance Orders before quoting it as open")
        lines.append(s + ".")
    if len(trades) > 1 and tot_in:
        lines.append(f"TOTAL: put in {tot_in:,.2f} USDT; profit/loss now {_usd(tot_now)}; "
                     f"if every target hits {_usd(tot_win)}; if every stop hits "
                     f"{_usd(tot_loss)}.")
    if tot_in:
        lines.append(f"(Before fees: about {tot_in * FEE * 2:,.2f} USDT for buying and "
                     "selling. Prices are Binance's live market; the demo account "
                     "follows it. Demo money, not real. The demo account's total "
                     "balance is only on the Binance page.)")
    return "\n".join(lines)


def trade_status(args: dict | None = None, journal: str = JOURNAL) -> str:
    args = args or {}
    try:
        with open(journal, encoding="utf-8") as f:
            trades = open_trades(f.read())
    except OSError:
        return "ERROR: no trade journal yet (workspace/trading/demo-journal.md)"
    want = re.sub(r"[^A-Z]", "", str(args.get("pair") or "").upper())
    if want:
        trades = [t for t in trades if t["symbol"].startswith(want)
                  or t["symbol"] == want]
    if not trades:
        return ("No open trades in the journal" + (f" for {want}" if want else "")
                + ". (Closed ones are marked there with their result.)")
    try:
        px = prices([t["symbol"] for t in trades])
    except Exception as e:  # noqa: BLE001
        return f"ERROR: Binance price lookup failed ({e})"
    ranges = {}
    for t in trades:
        try:
            ranges[t["symbol"]] = touched(t["symbol"], t["t0"])
        except Exception:  # noqa: BLE001 — the hit check is extra, never fatal
            pass
    return report(trades, px, ranges)


def execute(name: str, args: dict) -> str:
    if name == "trade_status":
        return trade_status(args)
    return f"ERROR: unknown tool {name}"


if __name__ == "__main__":
    print(trade_status())
