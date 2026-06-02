"""Live leaderboard runner — produces real, daily-refreshed standings for the
reference ("house") bots on LIVE market data. A GitHub Action runs this each
market day and commits leaderboard.json; the site reads it.

This is honest content, not fakery:
  • The bots are the real reference strategies in this repo.
  • Numbers are COMPUTED from running them on real daily bars (yfinance), never hardcoded.
  • It reports each bot's TRAILING 60-trading-day performance on live data (refreshed
    daily) — clearly labeled on the site as reference-bot form, not faked competition wins.

It reuses the same fill model and metrics as preview.py, so a bot scores here the
same way it would in the real eval.

    python live_runner.py            # writes leaderboard.json

Needs: yfinance (installed in the Action). Not part of the no-dep builder workflow.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).parent
OUT = HERE / "leaderboard.json"

UNIVERSE = [
    "SPY", "QQQ", "SMH", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP",
    "XLU", "XLRE", "XLC", "KRE", "JPM", "TQQQ", "SOXL", "NVDA", "MSFT", "AAPL", "META",
]

# The live field — file -> (display name, label). House/reference bots set the
# bar; real Round 1 entrants are labeled as such, never disguised as house bots.
FIELD = [
    ("seed_dual_momentum.py",        "dual-momentum-rotation", "house · all-weather"),
    ("ai_momentum.py",               "ai-momentum-basket",     "house · aggressive"),
    ("example_sector_rotation.py",   "sector-rotation",        "reference"),
    ("example_vol_target.py",        "vol-target",             "reference"),
    # Real Round 1 entrants (NOT house bots) — scored on the same live window.
    ("opu_agent.py",                 "opu",                    "round 1 · entrant"),
    ("robert_agent.py",              "robert",                 "round 1 · entrant"),
]

EVAL_DAYS = 60       # trailing trading-day window (matches the 60-day live horizon)
WARMUP_DAYS = 220    # extra history so 200-day signals work
START_CASH = 100_000.0
SLIP_EQUITY = 0.0005
SLIP_LEVERAGED = 0.0010
BETA_3X = {"TQQQ", "SOXL", "UPRO", "SPXL", "TNA", "FAS", "TECL", "LABU", "CURE", "DRN", "UDOW", "NAIL"}
BETA_2X = {"QLD", "SSO", "DDM", "ROM", "UWM", "AGQ"}


def beta(t: str) -> float:
    return 3.0 if t in BETA_3X else 2.0 if t in BETA_2X else 1.0


def load_decide(filename: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.decide


def fetch_bars() -> dict[str, list[dict]]:
    """Fetch daily bars for the universe (enough history for warmup + eval)."""
    need = EVAL_DAYS + WARMUP_DAYS + 30
    bars: dict[str, list[dict]] = {}
    for t in UNIVERSE:
        try:
            df = yf.download(t, period="2y", interval="1d", auto_adjust=True,
                             progress=False, threads=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [str(c[0]).lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        rows = []
        for ts, r in df.iterrows():
            try:
                o, h, l, c = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                v = int(r["volume"]) if r["volume"] == r["volume"] else 0
            except (KeyError, ValueError, TypeError):
                continue
            if any(x != x for x in (o, h, l, c)):
                continue
            rows.append({"ts": ts.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
        if len(rows) >= need - 60:  # tolerate short histories (e.g. XLC) but require most
            bars[t] = rows[-need:]
    return bars


def run_bot(decide, bars: dict[str, list[dict]]) -> dict:
    all_dates = sorted({b["ts"] for rows in bars.values() for b in rows})
    eval_dates = all_dates[-EVAL_DAYS:]
    cash = START_CASH
    positions: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    curve: list[float] = []
    trades = 0
    pending: list[dict] = []

    def price(t, date, field):
        for b in bars.get(t, []):
            if b["ts"] == date:
                return b[field]
        return None

    for date in eval_dates:
        for o in pending:
            px = price(o["ticker"], date, "open")
            if px is None:
                continue
            slip = SLIP_LEVERAGED if beta(o["ticker"]) > 1 else SLIP_EQUITY
            if o["side"] == "buy":
                fill = px * (1 + slip)
                qty = o["quantity"]
                if fill * qty > cash:
                    qty = cash / fill if fill > 0 else 0
                if qty <= 0:
                    continue
                held = positions.get(o["ticker"], 0.0)
                avg_cost[o["ticker"]] = (avg_cost.get(o["ticker"], 0.0) * held + fill * qty) / (held + qty)
                positions[o["ticker"]] = held + qty
                cash -= fill * qty
                trades += 1
            else:
                held = positions.get(o["ticker"], 0.0)
                qty = min(o["quantity"], held)
                if qty <= 0:
                    continue
                cash += px * (1 - slip) * qty
                positions[o["ticker"]] = held - qty
                trades += 1
        pending = []

        prices = {t: price(t, date, "close") for t in bars}
        prices = {t: p for t, p in prices.items() if p is not None}
        equity = max(cash + sum(positions.get(t, 0.0) * prices.get(t, 0.0) for t in positions), 1e-9)
        curve.append(equity)

        market_state = {t: [b for b in bars[t] if b["ts"] <= date] for t in bars}
        portfolio_state = {
            "cash": cash,
            "positions": [{"ticker": t, "quantity": q, "avg_cost": avg_cost.get(t, 0.0)}
                          for t, q in positions.items() if q > 0],
            "last_prices": prices,
        }
        try:
            orders = decide(market_state, portfolio_state, cash) or []
        except Exception:
            orders = []
        for o in orders:
            try:
                if o["side"] in ("buy", "sell") and float(o["quantity"]) > 0 and o["ticker"] in bars:
                    pending.append({"ticker": o["ticker"], "side": o["side"], "quantity": float(o["quantity"])})
            except (KeyError, TypeError, ValueError):
                pass

    ret = curve[-1] / START_CASH - 1 if curve else 0.0
    mdd = _mdd(curve)
    ann = (1 + ret) ** (252 / max(len(curve), 1)) - 1
    calmar = ann / mdd if mdd > 1e-9 else 0.0
    return {"ret": ret, "mdd": mdd, "sharpe": _sharpe(curve), "calmar": calmar, "trades": trades}


def _mdd(curve):
    peak, mdd = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _sharpe(curve):
    if len(curve) < 3:
        return 0.0
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    return (mean / sd) * math.sqrt(252) if sd > 1e-12 else 0.0


def main() -> int:
    bars = fetch_bars()
    if len(bars) < 12:
        print(f"fetched only {len(bars)} tickers — refusing to overwrite leaderboard.json")
        return 1
    asof = sorted({b["ts"] for rows in bars.values() for b in rows})[-1]
    rows = []
    for filename, name, label in FIELD:
        try:
            m = run_bot(load_decide(filename), bars)
        except Exception as e:  # noqa: BLE001
            print(f"skip {filename}: {e!r}")
            continue
        rows.append({"name": name, "label": label, **{k: round(v, 4) for k, v in m.items()}})
        print(f"  {name:24s} Calmar={m['calmar']:.2f} Ret={m['ret']*100:.2f}% MDD={m['mdd']*100:.2f}% Trades={m['trades']}")
    rows.sort(key=lambda r: r["calmar"], reverse=True)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_market_date": asof,
        "window_trading_days": EVAL_DAYS,
        "note": "House/reference bots set the bar; Round 1 entrants are labeled as entrants. All scored on the same trailing 60 trading days of live market data, refreshed each market day. Ranked by Calmar.",
        "bots": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT} ({len(rows)} bots, as of {asof})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
