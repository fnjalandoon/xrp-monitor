"""Check the XRP price, keep a 7-day history, and send a push alert when the
price enters your buy zone or sell zone.

Settings (GitHub repo Variables / Secrets, never stored in the public files):
  XRP_BUY_BELOW   alert when price is at or below this (USD), default 1.00
  XRP_SELL_ABOVE  alert when price is at or above this (USD), default 2.00
  NTFY_TOPIC      your private ntfy.sh topic name (Secret). No topic = no alerts sent.
  XRP_TEST_NOTIFY set to "1" to send a test notification and change nothing else.
  XRP_MOCK_PRICE  testing only: use this price instead of calling an exchange.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path("xrp_data.json")
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


def main():
    buy = float(os.environ.get("XRP_BUY_BELOW") or 1.00)
    sell = float(os.environ.get("XRP_SELL_ABOVE") or 2.00)
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
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
    zone = decide_zone(price, buy, sell, data.get("zone"))
    alerted = data.get("alerted_zone", "hold")

    if zone == "hold":
        alerted = "hold"
    elif zone != alerted:
        if not topic:
            print(f"In {zone} zone but NTFY_TOPIC is not set, alert not sent.")
        else:
            if zone == "buy":
                title = f"XRP buy zone: ${price:.4f}"
                msg = f"XRP is ${price:.4f} ({source}), at or below your buy price of ${buy:.2f}."
                tags = "chart_with_downwards_trend,moneybag"
            else:
                title = f"XRP sell zone: ${price:.4f}"
                msg = f"XRP is ${price:.4f} ({source}), at or above your sell price of ${sell:.2f}."
                tags = "chart_with_upwards_trend,moneybag"
            if notify(topic, title, msg, tags=tags):
                alerted = zone
                data["last_alert"] = now
                print("Alert sent:", title)
            else:
                print("Alert failed, will retry next run.")

    data["points"] = [p for p in data.get("points", []) if now - p[0] <= KEEP_SECONDS]
    data["points"].append([now, round(price, 4)])
    data.update({"price": round(price, 4), "source": source, "zone": zone,
                 "alerted_zone": alerted, "updated": now})
    DATA.write_text(json.dumps(data, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
