"""Check the XRP price, keep history, work out market indicators, and send a push
alert when the price enters your buy zone or sell zone, or when an indicator
gives a notable signal.

Settings (GitHub repo Variables / Secrets):
  XRP_BUY_BELOW         alert when price is at or below this (USD), default 1.00
  XRP_SELL_ABOVE        alert when price is at or above this (USD), default 2.00
  NTFY_TOPIC            your private ntfy.sh topic name (Secret). No topic = no alerts sent.
  XRP_INDICATOR_ALERTS  set to "0" to turn off indicator alerts (RSI, 200-day average,
                        50/200 cross, Fear and Greed). Buy and sell zone alerts stay on.
  XRP_TEST_NOTIFY       set to "1" to send a test notification and change nothing else.
  XRP_MOCK_PRICE        testing only: use this price instead of calling an exchange.

The buy and sell prices are written to xrp_data.json so the web page can draw them
on the chart. The topic is never written to any file.
Information only. Not financial advice.
"""
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("xrp_data.json")
DAILY = Path("xrp_daily.json")
MARKET = Path("xrp_market.json")
KEEP_DAYS = 800
KEEP_SECONDS = 7 * 24 * 3600
HYSTERESIS = 0.01  # price must move 1% back past a line before the zone resets


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 xrp-monitor"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_price():
    mock = os.environ.get("XRP_MOCK_PRICE")
    if mock:
        return float(mock), "mock"
    sources = [
        ("Coinbase", "https://api.coinbase.com/v2/prices/XRP-USD/spot",
         lambda d: float(d["data"]["amount"])),
        ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XRPUSD",
         lambda d: float(next(iter(d["result"].values()))["c"][0])),
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd",
         lambda d: float(d["ripple"]["usd"])),
    ]
    errors = []
    for name, url, parse in sources:
        try:
            p = parse(http_json(url))
            if 0.01 < p < 100:
                return p, name
            errors.append(f"{name}: implausible price {p}")
        except Exception as e:  # try the next source
            errors.append(f"{name}: {e}")
    raise RuntimeError("No price source worked: " + "; ".join(errors))


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def backfill_days():
    """Daily candles (UTC days). Kraken first, CoinGecko as fallback."""
    out = {}
    try:
        j = http_json("https://api.kraken.com/0/public/OHLC?pair=XRPUSD&interval=1440")
        rows = next(v for k, v in j["result"].items() if k != "last")
        for t, o, h, l, c, *_ in rows:
            out[utc_day(int(t))] = {"o": round(float(o), 4), "h": round(float(h), 4),
                                    "l": round(float(l), 4), "c": round(float(c), 4)}
        if len(out) > 300:
            return out
    except Exception as e:
        print("Kraken history failed:", e)
    j = http_json("https://api.coingecko.com/api/v3/coins/ripple/market_chart"
                  "?vs_currency=usd&days=365&interval=daily")
    out = {}
    for ms, p in j["prices"]:
        v = round(float(p), 4)
        out[utc_day(int(ms / 1000))] = {"o": v, "h": v, "l": v, "c": v}
    return out


def update_daily(price, now):
    d = json.loads(DAILY.read_text()) if DAILY.exists() else {"days": {}}
    days = d["days"]
    today = utc_day(now)
    if not d.get("backfilled"):
        try:
            for day, rec in backfill_days().items():
                if day != today:
                    days[day] = rec
            d["backfilled"] = True
            print(f"History backfilled: {len(days)} days")
        except Exception as e:
            print("History backfill failed, will retry next run:", e)
    rec = days.get(today)
    p = round(price, 4)
    if rec is None:
        days[today] = {"o": p, "h": p, "l": p, "c": p}
    else:
        rec["h"], rec["l"], rec["c"] = max(rec["h"], p), min(rec["l"], p), p
    d["days"] = dict(sorted(days.items())[-KEEP_DAYS:])
    DAILY.write_text(json.dumps(d, separators=(",", ":")))
    return d


def update_market(now):
    """Bitcoin daily closes and the crypto Fear and Greed Index. Failures keep old data."""
    m = json.loads(MARKET.read_text()) if MARKET.exists() else {}
    btc, fng = m.get("btc", {}), m.get("fng", {})
    try:
        j = http_json("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440")
        rows = next(v for k, v in j["result"].items() if k != "last")
        for r in rows:
            btc[utc_day(int(r[0]))] = round(float(r[4]), 2)
    except Exception as e:
        print("Bitcoin history from Kraken failed:", e)
        try:  # at least keep today's Bitcoin price current
            j = http_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            btc[utc_day(now)] = round(float(j["data"]["amount"]), 2)
        except Exception as e2:
            print("Bitcoin price from Coinbase failed:", e2)
    try:
        j = http_json("https://api.alternative.me/fng/?limit=400")
        for r in j["data"]:
            fng[utc_day(int(r["timestamp"]))] = [int(r["value"]), r["value_classification"]]
    except Exception as e:
        print("Fear and Greed Index failed:", e)
    m = {"btc": dict(sorted(btc.items())[-KEEP_DAYS:]),
         "fng": dict(sorted(fng.items())[-KEEP_DAYS:]), "updated": now}
    MARKET.write_text(json.dumps(m, separators=(",", ":")))
    return m


# ---------- indicators (the web page uses the same formulas) ----------

def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def rsi(v, n=14):
    """Wilder's RSI over the whole series."""
    if len(v) < n + 1:
        return None
    gains = [max(v[i] - v[i - 1], 0) for i in range(1, len(v))]
    losses = [max(v[i - 1] - v[i], 0) for i in range(1, len(v))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def bollinger(v, n=20, k=2):
    if len(v) < n:
        return None
    m = sum(v[-n:]) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v[-n:]) / n)
    return m - k * sd, m, m + k * sd


def pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def indicators(daily, market, price):
    """A snapshot of every indicator. Today's candle uses the current price."""
    closes = [r["c"] for _, r in sorted(daily["days"].items())]
    if closes:
        closes[-1] = round(price, 4)
    done = closes[:-1]  # completed UTC days only, used for crossing signals
    ind = {"rsi": rsi(closes), "ma50": sma(closes, 50), "ma200": sma(closes, 200),
           "done_close": done[-1] if done else None,
           "done_ma50": sma(done, 50), "done_ma200": sma(done, 200)}
    bb = bollinger(closes)
    if bb:
        ind["bb_lo"], ind["bb_mid"], ind["bb_hi"] = bb
    ind["xrp_7d"] = pct(price, closes[-8]) if len(closes) >= 8 else None
    ind["xrp_30d"] = pct(price, closes[-31]) if len(closes) >= 31 else None
    b = [c for _, c in sorted(market.get("btc", {}).items())]
    ind["btc_7d"] = pct(b[-1], b[-8]) if len(b) >= 8 else None
    ind["btc_30d"] = pct(b[-1], b[-31]) if len(b) >= 31 else None
    f = sorted(market.get("fng", {}).items())
    if f:
        ind["fng"], ind["fng_label"] = f[-1][1]

    # Leans: each of three signals leans buy, sell, or neutral.
    leans = {}
    if ind["rsi"] is not None:
        leans["rsi"] = "buy" if ind["rsi"] < 30 else "sell" if ind["rsi"] > 70 else "neutral"
    if bb:
        leans["bands"] = "buy" if price <= bb[0] else "sell" if price >= bb[2] else "neutral"
    if "fng" in ind:
        leans["fng"] = "buy" if ind["fng"] <= 25 else "sell" if ind["fng"] >= 75 else "neutral"
    ind["leans"] = leans
    ind["lean_buy"] = sum(1 for x in leans.values() if x == "buy")
    ind["lean_sell"] = sum(1 for x in leans.values() if x == "sell")
    ind["lean_total"] = len(leans)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ind.items()}


def summary_text(ind, price):
    """One short paragraph for push messages."""
    parts = []
    if ind.get("rsi") is not None:
        r = ind["rsi"]
        parts.append(f"RSI {r:.0f}" + (" (oversold)" if r < 30 else " (overbought)" if r > 70 else ""))
    if ind.get("ma200"):
        parts.append(("Above" if price >= ind["ma200"] else "Below") +
                     f" 200-day average (${ind['ma200']:.4f})")
    if "fng" in ind:
        parts.append(f"Fear and Greed {ind['fng']} ({ind['fng_label']})")
    if ind.get("lean_total"):
        parts.append(f"{ind['lean_buy']} of {ind['lean_total']} signals lean buy, "
                     f"{ind['lean_sell']} lean sell")
    return ". ".join(parts) + ". Not financial advice." if parts else "Not financial advice."


def band_state(v, prev, lo, hi, gap):
    """low / mid / high with a buffer so it does not flip back and forth."""
    if v is None:
        return prev
    if prev == "low" and v < lo + gap:
        return "low"
    if prev == "high" and v > hi - gap:
        return "high"
    if v <= lo:
        return "low"
    if v >= hi:
        return "high"
    return "mid"


def indicator_events(ind, sig):
    """Work out new signal states and which of them deserve a push alert."""
    new, events = dict(sig), []
    r = band_state(ind.get("rsi"), sig.get("rsi", "mid"), 30, 70, 5)
    new["rsi"] = r
    if r != sig.get("rsi", "mid") and r != "mid":
        events.append(("rsi", r,
                       f"XRP RSI {'oversold' if r == 'low' else 'overbought'}: {ind['rsi']:.0f}",
                       "RSI is below 30. The price has fallen fast and may be stretched to the "
                       "downside." if r == "low" else
                       "RSI is above 70. The price has risen fast and may be stretched to the upside."))
    f = band_state(ind.get("fng"), sig.get("fng", "mid"), 20, 80, 5)
    new["fng"] = f
    if f != sig.get("fng", "mid") and f != "mid":
        events.append(("fng", f, f"Crypto market mood: {ind['fng_label']} ({ind['fng']})",
                       "Extreme fear has often come near price lows." if f == "low" else
                       "Extreme greed has often come near price highs."))
    if ind.get("done_ma200") and ind.get("done_close"):
        t = "above" if ind["done_close"] >= ind["done_ma200"] else "below"
        new["trend"] = t
        if sig.get("trend") and t != sig["trend"]:
            events.append(("trend", t, f"XRP closed {t} its 200-day average",
                           f"Daily close ${ind['done_close']:.4f}, 200-day average "
                           f"${ind['done_ma200']:.4f}. " +
                           ("The long-term trend may be turning up." if t == "above" else
                            "The long-term trend may be turning down.")))
    if ind.get("done_ma50") and ind.get("done_ma200"):
        c = "golden" if ind["done_ma50"] >= ind["done_ma200"] else "death"
        new["cross"] = c
        if sig.get("cross") and c != sig["cross"]:
            events.append(("cross", c, f"XRP {c} cross",
                           "The 50-day average moved above the 200-day average (a golden cross), "
                           "often read as a bullish sign." if c == "golden" else
                           "The 50-day average moved below the 200-day average (a death cross), "
                           "often read as a bearish sign."))
    return new, events


def decide_zone(price, buy, sell, prev):
    if prev == "buy" and price <= buy * (1 + HYSTERESIS):
        return "buy"
    if prev == "sell" and price >= sell * (1 - HYSTERESIS):
        return "sell"
    if price <= buy:
        return "buy"
    if price >= sell:
        return "sell"
    return "hold"


def notify(topic, title, message, priority="high", tags="rotating_light"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags,
                 "User-Agent": "xrp-monitor"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return 200 <= r.status < 300


def safe_notify(topic, title, message, **kw):
    try:
        return notify(topic, title, message, **kw)
    except Exception as e:
        print("Notification failed:", e)
        return False


def main():
    buy = float(os.environ.get("XRP_BUY_BELOW") or 1.00)
    sell = float(os.environ.get("XRP_SELL_ABOVE") or 2.00)
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    ind_alerts = os.environ.get("XRP_INDICATOR_ALERTS", "1").strip() != "0"
    if buy >= sell:
        print(f"Settings error: buy price {buy} must be lower than sell price {sell}")
        return 1

    price, source = fetch_price()
    print(f"XRP ${price:.4f} from {source}; buy <= {buy}, sell >= {sell}")

    if os.environ.get("XRP_TEST_NOTIFY") == "1":
        if not topic:
            print("NTFY_TOPIC secret is not set, cannot send a test.")
            return 1
        notify(topic, "XRP alerts are working",
               f"Test message. XRP is ${price:.4f}. Buy alert at or below ${buy:.2f}, "
               f"sell alert at or above ${sell:.2f}.", priority="default", tags="white_check_mark")
        print("Test notification sent.")
        return 0

    data = json.loads(DATA.read_text()) if DATA.exists() else {"points": []}
    now = int(time.time())

    # History and indicators first, so alerts can include them. Never let these block alerts.
    ind = {}
    try:
        daily = update_daily(price, now)
    except Exception as e:
        print("Daily history update failed:", e)
        daily = None
    try:
        market = update_market(now)
    except Exception as e:
        print("Market data update failed:", e)
        market = {}
    try:
        if daily:
            ind = indicators(daily, market, price)
    except Exception as e:
        print("Indicator calculation failed:", e)
    summary = summary_text(ind, price) if ind else "Not financial advice."
    print("Indicators:", summary)

    # Buy and sell zone alerts
    zone = decide_zone(price, buy, sell, data.get("zone"))
    alerted = data.get("alerted_zone", "hold")
    if zone == "hold":
        alerted = "hold"
    elif zone != alerted:
        if not topic:
            print(f"In {zone} zone but NTFY_TOPIC is not set, alert not sent.")
        else:
            if zone == "buy":
                title = f"XRP buying opportunity: ${price:.4f}"
                msg = f"XRP is ${price:.4f} ({source}), at or below your buy price of ${buy:.2f}."
                tags = "chart_with_downwards_trend,moneybag"
            else:
                title = f"XRP selling opportunity: ${price:.4f}"
                msg = f"XRP is ${price:.4f} ({source}), at or above your sell price of ${sell:.2f}."
                tags = "chart_with_upwards_trend,moneybag"
            if safe_notify(topic, title, msg + "\n" + summary, tags=tags):
                alerted = zone
                data["last_alert"] = now
                print("Alert sent:", title)
            else:
                print("Alert failed, will retry next run.")

    # Indicator alerts. A state only moves forward once its alert is sent (or alerts are off).
    sig = data.get("sig", {})
    if ind:
        new_sig, events = indicator_events(ind, sig)
        for key, state, title, msg in events:
            if not ind_alerts:
                continue
            if topic and safe_notify(topic, title, f"{msg}\nXRP ${price:.4f}. {summary}",
                                     priority="default", tags="bar_chart"):
                print("Indicator alert sent:", title)
            else:
                print("Indicator alert not sent, will retry next run:", title)
                new_sig[key] = sig.get(key, "mid")
        sig = new_sig

    data["points"] = [p for p in data.get("points", []) if now - p[0] <= KEEP_SECONDS]
    data["points"].append([now, round(price, 4)])
    data.update({"price": round(price, 4), "source": source, "zone": zone,
                 "alerted_zone": alerted, "updated": now, "buy": buy, "sell": sell,
                 "ind": ind, "sig": sig})
    DATA.write_text(json.dumps(data, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
