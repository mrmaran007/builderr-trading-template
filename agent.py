import json, os, random

# Load your trained Q-table
_Q = {}
_path = os.path.join(os.path.dirname(__file__), "agent.json")
if os.path.exists(_path):
    with open(_path) as f:
        data = json.load(f)
        _Q = {eval(k): v for k, v in data["q_table"].items()}

def _sma(p, n): return sum(p[-n:])/n if len(p)>=n else p[-1]
def _rsi(p, n=14):
    if len(p)<n+1: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    ag=sum(x for x in d[-n:] if x>0)/n
    al=sum(abs(x) for x in d[-n:] if x<0)/n
    return 100-100/(1+ag/(al+1e-9))
def _macd(p): return (_sma(p,12)-_sma(p,26)) if len(p)>=26 else 0
def _mom(p,n=5): return (p[-1]-p[-n-1])/p[-n-1] if len(p)>n else 0

def _state(p):
    if len(p)<10: return (0,1,0,1,0)
    trend=1 if p[-1]>_sma(p,10) else 0
    r=_rsi(p); rz=0 if r<35 else (2 if r>65 else 1)
    ms=1 if _macd(p)>0 else 0
    bb_p=0.5  # simplified
    md=1 if _mom(p)>0 else 0
    return (trend,rz,ms,1,md)

def decide(market_state, portfolio_state, cash):
    orders = []
    for ticker, data in market_state.items():
        prices = data.get("prices", [])
        if len(prices) < 5:
            continue
        state = _state(prices)
        if state in _Q:
            q = _Q[state]
            action = max(q, key=lambda a: q[a])
        else:
            action = 0
        qty = int(cash * 0.1 / prices[-1]) if cash > prices[-1] else 0
        if str(action) == "1" and qty > 0:
            orders.append({"ticker": ticker, "side": "buy", "quantity": qty})
        elif str(action) == "2":
            held = portfolio_state.get(ticker, {}).get("quantity", 0)
            if held > 0:
                orders.append({"ticker": ticker, "side": "sell", "quantity": held})
    return orders
