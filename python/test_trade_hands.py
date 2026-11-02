"""trade_status offline: his real journal rows, fixed prices, no network.
Run: py -3.13 test_trade_hands.py"""
from __future__ import annotations

import os
import sys
import tempfile

import trade_hands as T
import voice_hands

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


JOURNAL = """# Demo trading journal
| Time | Pair | Side | Size | Entry | Stop | Target | Reason | Result |
|---|---|---|---|---|---|---|---|---|
| 2026-11-01 22:11 | BTC/USDT | Buy (market) | 0.01732 | 77,928.63 | 75,900 trig / 75,800 lim (OCO placed 23:12:40) | 82,000 | bounce | Open. Risk ~37 USDT |
| 2026-11-01 23:16 | SOL/USDT | Buy (market) | 18.68 (1,755.93 USDT) | ~93.90 | 91.90 trig / 91.80 lim (OCO 23:17:06) | 98.50 | oversold | Open. Risk ~43 USDT |
| 2026-10-30 10:00 | ETH/USDT | Buy (market) | 1 | 3,000 | 2,900 | 3,200 | test | Closed +200 USDT |
"""

tr = T.open_trades(JOURNAL)
check("reads the two open trades, skips the closed one",
      [t["symbol"] for t in tr] == ["BTCUSDT", "SOLUSDT"], tr)
btc, sol = tr
check("BTC row parsed: size, entry, stop trigger, target",
      (btc["qty"], btc["entry"], btc["stop"], btc["target"]) == (0.01732, 77928.63, 75900.0, 82000.0),
      btc)
check("SOL size is the coin amount, not the USDT in brackets; '~' entry read",
      (sol["qty"], sol["entry"]) == (18.68, 93.90), sol)

r = T.report(tr, {"BTCUSDT": 78228.01, "SOLUSDT": 94.36})
check("profit now per trade and in total", "+5.19 USDT" in r and "+8.59 USDT" in r
      and "profit/loss now +13.78 USDT" in r, r)
check("profit at target and loss at stop", "+70.52 USDT" in r and "-35.14 USDT" in r
      and "if every target hits +156.44 USDT" in r and "if every stop hits -72.50 USDT" in r, r)
check("money put in", "put in 3,103.78 USDT" in r, r)
check("no false 'closed' warning when nothing was touched", "WARNING" not in r)

r = T.report([btc], {"BTCUSDT": 76500.0}, {"BTCUSDT": (75850.0, 78500.0)})
check("stop touched since entry -> says it has probably closed at a loss",
      "reached the stop" in r and "CLOSED" in r, r)
r = T.report([sol], {"SOLUSDT": 97.0}, {"SOLUSDT": (93.0, 98.6)})
check("target touched since entry -> says it has probably closed in profit",
      "reached the target" in r, r)
check("a loss reads as a loss", "-49.58 USDT" in T.report([btc], {"BTCUSDT": 75065.97}), "")

with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "j.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(JOURNAL)
    T.prices = lambda syms: {"BTCUSDT": 78000.0, "SOLUSDT": 94.0}
    T.touched = lambda sym, t0: (None, None)
    out = T.trade_status({"pair": "sol"}, journal=p)
    check("pair filter: 'sol' gives only SOL", "SOL/USDT" in out and "BTC/USDT" not in out, out)
    out = T.trade_status({"pair": "DOGE"}, journal=p)
    check("no open trade for a pair says so", out.startswith("No open trades"), out)

    def boom(syms):
        raise OSError("offline")
    T.prices = boom
    check("network down -> a clear ERROR, never a crash",
          T.trade_status({}, journal=p).startswith("ERROR: Binance price lookup failed"))

check("the voice has the hand", "trade_status" in voice_hands.NAMES
      and any(d["name"] == "trade_status" for d in voice_hands.DECLS))
src = open(T.__file__, encoding="utf-8").read()
check("read-only: no order endpoints in the code",
      "/order" not in src and "api/v3/order" not in src and "X-MBX-APIKEY" not in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
