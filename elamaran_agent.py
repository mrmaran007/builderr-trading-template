"""Calmar Rotation Hybrid.

Contest objective: maximize 60-day forward Calmar, not raw return.

The agent uses only the provided daily bars. It has no network calls, no LLM,
no API keys, and no dependencies outside the Python standard library.

Core idea:
  * Risk-off when SPY/QQQ lose their 50-day trends or QQQ volatility is high.
  * Risk-on rotates into the strongest broad/sector/mega-cap sleeves.
  * A small 2x ETF overlay is allowed only in calm QQQ uptrends.
  * Every target is capped below 24% and beta-adjusted gross is scaled below 1.35x.
"""
from __future__ import annotations

from math import sqrt
from statistics import mean, pstdev
from typing import Any

RISK_CANDIDATES = (
    "SPY", "QQQ", "DIA", "IWM",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "SMH",
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
)
DEFENSIVE_WEIGHTS = (
    ("XLP", 0.24),
    ("XLU", 0.24),
    ("XLV", 0.20),
    ("XLE", 0.12),
)
BETA_MULTIPLE = {
    "TQQQ": 3.0, "SOXL": 3.0, "UPRO": 3.0, "SPXL": 3.0, "TNA": 3.0,
    "FAS": 3.0, "TECL": 3.0, "LABU": 3.0, "CURE": 3.0, "DRN": 3.0,
    "UDOW": 3.0, "NAIL": 3.0,
    "QLD": 2.0, "SSO": 2.0, "DDM": 2.0, "ROM": 2.0, "UWM": 2.0, "AGQ": 2.0,
}

REBALANCE_EVERY_DAYS = 5
MAX_WEIGHT = 0.24
DRIFT_LIMIT = 0.27
MAX_BETA_GROSS = 1.35
MIN_TRADE_PCT = 0.015

# --- Drawdown-aware de-risking -------------------------------------------
# The contest scores on 60-day forward Calmar (return / max drawdown), so
# controlling drawdown is worth as much as generating return. Once the
# portfolio itself is underwater vs. its own peak, gross exposure is trimmed
# regardless of what the market signals say. This tapers linearly and never
# goes fully to zero exposure.
DRAWDOWN_TAPER_START = 0.05   # start trimming once 5% off peak equity
DRAWDOWN_TAPER_FULL = 0.15    # fully tapered by 15% off peak
DRAWDOWN_FLOOR_SCALE = 0.55   # never scale exposure below this

# --- Diversification guard -------------------------------------------------
# Prevents the top-5 momentum picker from handing back 5 correlated names
# (e.g. five megacap tech names) which would understate real portfolio risk.
BUCKETS = {
    "SPY": "broad", "QQQ": "broad", "DIA": "broad", "IWM": "broad",
    "XLK": "sector", "XLF": "sector", "XLE": "sector", "XLV": "sector",
    "XLI": "sector", "XLY": "sector", "XLP": "sector", "XLU": "sector",
    "XLRE": "sector", "XLC": "sector", "SMH": "sector",
    "AAPL": "megacap", "MSFT": "megacap", "GOOGL": "megacap", "AMZN": "megacap",
    "META": "megacap", "NVDA": "megacap", "TSLA": "megacap",
}
MAX_PER_BUCKET = 3

_last_rebalance_bar_date: str | None = None
_last_targets: dict[str, float] = {}
_equity_peak: float | None = None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _drawdown_scale(total_equity: float) -> float:
    """Return a 0-1 multiplier that trims gross exposure as the portfolio's
    own equity curve draws down from its running peak. This is independent
    of the market-timing signal below and acts as a second, orthogonal line
    of defense against large drawdowns."""
    global _equity_peak
    if total_equity <= 0:
        return 1.0
    if _equity_peak is None or total_equity > _equity_peak:
        _equity_peak = total_equity
        return 1.0
    dd = (_equity_peak - total_equity) / _equity_peak
    if dd <= DRAWDOWN_TAPER_START:
        return 1.0
    if dd >= DRAWDOWN_TAPER_FULL:
        return DRAWDOWN_FLOOR_SCALE
    span = DRAWDOWN_TAPER_FULL - DRAWDOWN_TAPER_START
    frac = (dd - DRAWDOWN_TAPER_START) / span
    return 1.0 - frac * (1.0 - DRAWDOWN_FLOOR_SCALE)


def closes(bars: list[dict[str, Any]] | None) -> list[float]:
    if not bars:
        return []
    out: list[float] = []
    for bar in bars:
        try:
            close = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return []
        if close <= 0:
            return []
        out.append(close)
    return out


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return mean(values[-n:])


def momentum(values: list[float], n: int) -> float | None:
    if len(values) <= n:
        return None
    start = values[-(n + 1)]
    if start <= 0:
        return None
    return values[-1] / start - 1.0


def realized_vol(values: list[float], n: int) -> float | None:
    if len(values) <= n:
        return None
    window = values[-(n + 1):]
    rets = []
    for i in range(1, len(window)):
        prev = window[i - 1]
        if prev <= 0:
            return None
        rets.append(window[i] / prev - 1.0)
    if len(rets) < 5:
        return None
    return pstdev(rets) * sqrt(252.0)


def current_positions(portfolio_state: dict[str, Any]) -> dict[str, dict[str, float]]:
    positions: dict[str, dict[str, float]] = {}
    for raw in portfolio_state.get("positions", []) or []:
        ticker = str(raw.get("ticker", "")).upper()
        if not ticker:
            continue
        try:
            qty = float(raw.get("quantity", 0.0))
            avg_cost = float(raw.get("avg_cost", 0.0))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        existing = positions.setdefault(ticker, {"quantity": 0.0, "avg_cost": avg_cost})
        existing["quantity"] += qty
        existing["avg_cost"] = avg_cost or existing["avg_cost"]
    return positions


def equity(portfolio_state: dict[str, Any], cash: float) -> float:
    try:
        total = float(portfolio_state.get("cash", cash))
    except (TypeError, ValueError):
        total = float(cash or 0.0)
    last_prices = portfolio_state.get("last_prices", {}) or {}
    for ticker, pos in current_positions(portfolio_state).items():
        try:
            price = float(last_prices.get(ticker, pos["avg_cost"]))
        except (TypeError, ValueError):
            price = pos["avg_cost"]
        total += pos["quantity"] * max(price, 0.0)
    return max(total, 0.0)


def _latest_bar_date(market_state: dict[str, list[dict[str, Any]]]) -> str | None:
    bars = market_state.get("SPY") or market_state.get("QQQ") or []
    if not bars:
        return None
    ts = bars[-1].get("ts")
    if ts is None:
        return str(len(bars))
    return str(ts)[:10]


def _days_since_rebalance(market_state: dict[str, list[dict[str, Any]]]) -> int | None:
    if _last_rebalance_bar_date is None:
        return None
    bars = market_state.get("SPY") or market_state.get("QQQ") or []
    dates = [str(b.get("ts", i))[:10] for i, b in enumerate(bars)]
    if not dates or _last_rebalance_bar_date not in dates:
        return None
    return len(dates) - dates.index(_last_rebalance_bar_date) - 1


def _market_prices(market_state: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for ticker, bars in market_state.items():
        cs = closes(bars)
        if cs:
            prices[ticker.upper()] = cs[-1]
    return prices


def _risk_off_targets(market_state: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    return {ticker: weight for ticker, weight in DEFENSIVE_WEIGHTS if closes(market_state.get(ticker))}


def _scale_caps(weights: dict[str, float]) -> dict[str, float]:
    capped = {t: min(max(w, 0.0), MAX_WEIGHT) for t, w in weights.items() if w > 0.0}
    beta_gross = sum(w * BETA_MULTIPLE.get(t, 1.0) for t, w in capped.items())
    if beta_gross > MAX_BETA_GROSS:
        scale = MAX_BETA_GROSS / beta_gross
        capped = {t: w * scale for t, w in capped.items()}
    return {t: round(w, 6) for t, w in capped.items() if w > 0.001}


def target_weights(market_state: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    spy = closes(market_state.get("SPY"))
    qqq = closes(market_state.get("QQQ"))
    if len(spy) < 50 or len(qqq) < 50:
        return {}

    spy_sma50 = sma(spy, 50)
    qqq_sma50 = sma(qqq, 50)
    qqq_vol20 = realized_vol(qqq, 20)
    risk_on = bool(
        spy_sma50 is not None
        and qqq_sma50 is not None
        and qqq_vol20 is not None
        and spy[-1] > spy_sma50
        and qqq[-1] > qqq_sma50
        and qqq_vol20 < 0.35
    )
    if not risk_on:
        return _scale_caps(_risk_off_targets(market_state))

    # Conviction: how strongly the trend/vol gate is satisfied, not just
    # whether it's satisfied. A market that just barely crossed its 50-day
    # SMA gets a smaller risk-asset budget than one in a clean, low-vol
    # uptrend, and the remainder is parked in the defensive sleeve instead
    # of being thrown fully at momentum names. This smooths the transition
    # in/out of risk-on regimes and reduces whipsaw-driven drawdown.
    trend_strength = min(
        _clamp((spy[-1] / spy_sma50 - 1.0) / 0.04, 0.0, 1.0),
        _clamp((qqq[-1] / qqq_sma50 - 1.0) / 0.04, 0.0, 1.0),
    )
    vol_headroom = _clamp((0.35 - qqq_vol20) / 0.20, 0.0, 1.0)
    conviction = 0.5 + 0.5 * min(trend_strength, vol_headroom)  # in [0.5, 1.0]

    scored: list[tuple[float, str]] = []
    for ticker in RISK_CANDIDATES:
        values = closes(market_state.get(ticker))
        if len(values) < 61:
            continue
        mom60 = momentum(values, 60)
        mom20 = momentum(values, 20)
        trend50 = sma(values, 50)
        vol20 = realized_vol(values, 20)
        if mom60 is None or mom20 is None or trend50 is None or vol20 is None:
            continue
        trend_gap = values[-1] / trend50 - 1.0
        raw_score = (0.55 * mom60) + (0.25 * mom20) + (0.20 * trend_gap)
        # Divide by (1 + vol) rather than subtracting a flat vol penalty:
        # this is a proper risk-adjustment (Sharpe-like) so a very high-vol
        # name needs proportionally more raw momentum to rank highly,
        # instead of a fixed haircut that a strong-enough trend can swamp.
        score = raw_score / (1.0 + vol20)
        if score > 0.0:
            scored.append((score, ticker))

    scored.sort(reverse=True)

    # Bucket-capped selection: walk the ranked list but skip a candidate if
    # its bucket (broad / sector / megacap) is already at MAX_PER_BUCKET
    # among the picks so far, so momentum can't hand back 5 correlated names.
    winners: list[str] = []
    bucket_counts: dict[str, int] = {}
    for _, ticker in scored:
        if len(winners) >= 5:
            break
        bucket = BUCKETS.get(ticker, "other")
        if bucket_counts.get(bucket, 0) >= MAX_PER_BUCKET:
            continue
        winners.append(ticker)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    if not winners:
        return _scale_caps(_risk_off_targets(market_state))

    qqq_sma20 = sma(qqq, 20)
    qqq_mom20 = momentum(qqq, 20)
    overlay_on = bool(
        qqq_sma20 is not None
        and qqq_sma50 is not None
        and qqq_mom20 is not None
        and qqq_sma20 > qqq_sma50
        and qqq_mom20 > 0.0
        and qqq_vol20 < 0.28
        and closes(market_state.get("QLD"))
        and closes(market_state.get("SSO"))
    )

    weights: dict[str, float] = {}
    base_budget = (0.76 if overlay_on else 0.92) * conviction
    # Rank-weight instead of equal-weight: the strongest-scoring name gets
    # meaningfully more capital than the fifth pick, rather than splitting
    # the budget evenly across the whole shortlist.
    n = len(winners)
    rank_weights = [n - i for i in range(n)]  # e.g. [5,4,3,2,1] for n=5
    rank_total = sum(rank_weights)
    cap = MAX_WEIGHT - 0.02
    for ticker, rw in zip(winners, rank_weights):
        weights[ticker] = min(cap, base_budget * rw / rank_total)

    if overlay_on:
        weights["QLD"] = weights.get("QLD", 0.0) + 0.11 * conviction
        weights["SSO"] = weights.get("SSO", 0.0) + 0.07 * conviction

    # Whatever conviction didn't allocate to risk assets goes to the
    # defensive sleeve at a reduced size, instead of sitting fully in cash
    # or fully in momentum names on a marginal signal.
    leftover = 1.0 - conviction
    if leftover > 0.001:
        for ticker, w in DEFENSIVE_WEIGHTS:
            if closes(market_state.get(ticker)):
                weights[ticker] = weights.get(ticker, 0.0) + w * leftover * 0.8

    return _scale_caps(weights)


def orders_to_rebalance(
    targets: dict[str, float],
    positions: dict[str, dict[str, float]],
    total_equity: float,
    prices: dict[str, float],
    cash_available: float,
) -> list[dict[str, object]]:
    if total_equity <= 0:
        return []

    min_trade = total_equity * MIN_TRADE_PCT
    orders: list[dict[str, object]] = []
    sell_proceeds = 0.0

    for ticker, pos in positions.items():
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue
        qty = pos["quantity"]
        current_value = qty * price
        target_value = total_equity * targets.get(ticker, 0.0)
        delta = target_value - current_value
        if ticker not in targets:
            sell_qty = int(qty)
            if sell_qty > 0 and current_value >= min_trade:
                orders.append({"ticker": ticker, "side": "sell", "quantity": sell_qty})
                sell_proceeds += sell_qty * price
        elif delta < -min_trade:
            sell_qty = min(int(abs(delta) // price), int(qty))
            if sell_qty > 0:
                orders.append({"ticker": ticker, "side": "sell", "quantity": sell_qty})
                sell_proceeds += sell_qty * price

    spendable = max(float(cash_available), 0.0) + (sell_proceeds * 0.98)

    for ticker, weight in sorted(targets.items()):
        price = prices.get(ticker)
        if price is None or price <= 0:
            continue
        current_qty = positions.get(ticker, {}).get("quantity", 0.0)
        current_value = current_qty * price
        target_value = total_equity * weight
        delta = target_value - current_value
        if delta < min_trade:
            continue
        buy_value = min(delta, spendable)
        buy_qty = int(buy_value // price)
        if buy_qty > 0:
            orders.append({"ticker": ticker, "side": "buy", "quantity": buy_qty})
            spendable -= buy_qty * price

    return orders[:45]


def _has_position_drifted(portfolio_state: dict[str, Any], total_equity: float) -> bool:
    if total_equity <= 0:
        return False
    last_prices = portfolio_state.get("last_prices", {}) or {}
    for ticker, pos in current_positions(portfolio_state).items():
        try:
            price = float(last_prices.get(ticker, pos["avg_cost"]))
        except (TypeError, ValueError):
            price = pos["avg_cost"]
        if price > 0 and (pos["quantity"] * price / total_equity) > DRIFT_LIMIT:
            return True
    return False


def decide(
    market_state: dict,
    portfolio_state: dict,
    cash: float,
) -> list[dict]:
    """Return a list of long-only buy/sell orders."""
    global _last_rebalance_bar_date, _last_targets

    if not market_state:
        return []

    latest_date = _latest_bar_date(market_state)
    if latest_date is None:
        return []

    total_equity = equity(portfolio_state, cash)
    days_since = _days_since_rebalance(market_state)
    drifted = _has_position_drifted(portfolio_state, total_equity)
    should_rebalance = (
        _last_rebalance_bar_date is None
        or days_since is None
        or days_since >= REBALANCE_EVERY_DAYS
        or drifted
    )
    if not should_rebalance:
        return []

    targets = target_weights(market_state)
    if not targets:
        return []

    dd_scale = _drawdown_scale(total_equity)
    if dd_scale < 1.0:
        targets = {t: round(w * dd_scale, 6) for t, w in targets.items()}

    prices = _market_prices(market_state)
    positions = current_positions(portfolio_state)
    orders = orders_to_rebalance(targets, positions, total_equity, prices, cash)
    if orders:
        _last_rebalance_bar_date = latest_date
        _last_targets = targets
    return orders
