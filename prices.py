"""
Котировки ключевых инструментов (1-минутные свечи) для:
  • алертов о резком движении цены;
  • отчёта «что произошло» после важного события.

Источники: Coinbase Exchange (крипта), Yahoo Finance chart API (фьючерсы, FX, DXY).
Оба бесплатные и без ключей. Ошибка одного инструмента не ломает остальные.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import requests

# key, название, источник, символ, порог движения за 15 мин (%), знаков после запятой
ASSETS = [
    ("BTC",    "BTC",          "coinbase", "BTC-USD",  1.5,  0),
    ("ETH",    "ETH",          "coinbase", "ETH-USD",  2.0,  1),
    ("ES",     "S&P 500 фьюч", "yahoo",    "ES=F",     0.6,  2),
    ("NQ",     "Nasdaq фьюч",  "yahoo",    "NQ=F",     0.8,  2),
    ("GOLD",   "Золото",       "yahoo",    "GC=F",     0.7,  1),
    ("OIL",    "Нефть WTI",    "yahoo",    "CL=F",     1.5,  2),
    ("DXY",    "DXY",          "yahoo",    "DX-Y.NYB", 0.35, 2),
    ("EURUSD", "EUR/USD",      "yahoo",    "EURUSD=X", 0.35, 4),
    ("USDJPY", "USD/JPY",      "yahoo",    "JPY=X",    0.5,  2),
]
ASSET_BY_KEY = {a[0]: a for a in ASSETS}

YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]


def _coinbase(symbol: str, headers: dict) -> list[tuple[datetime, float]]:
    r = requests.get(f"https://api.exchange.coinbase.com/products/{symbol}/candles",
                     params={"granularity": 60}, headers=headers, timeout=20)
    r.raise_for_status()
    # формат Coinbase: [time, low, high, open, close, volume], новые сверху
    rows = [(datetime.fromtimestamp(c[0], timezone.utc), float(c[4])) for c in r.json()]
    return sorted(rows)


def _yahoo(symbol: str, headers: dict) -> list[tuple[datetime, float]]:
    last_err = None
    for host in YAHOO_HOSTS:
        try:
            r = requests.get(f"{host}/v8/finance/chart/{symbol}",
                             params={"interval": "1m", "range": "1d"}, headers=headers, timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0].get("close") or []
            return [(datetime.fromtimestamp(t, timezone.utc), float(c))
                    for t, c in zip(ts, closes) if c is not None]
        except Exception as e:
            last_err = e
    raise RuntimeError(last_err)


def fetch_all(headers: dict) -> dict[str, list[tuple[datetime, float]]]:
    out = {}
    for key, _, source, symbol, _, _ in ASSETS:
        try:
            series = _coinbase(symbol, headers) if source == "coinbase" else _yahoo(symbol, headers)
            if series:
                out[key] = series
        except Exception as e:
            print(f"[warn] price {key}: {e}", file=sys.stderr)
    return out


def price_at(series, t: datetime):
    """Последняя цена закрытия не позже t."""
    best = None
    for ts, c in series:
        if ts <= t:
            best = c
        else:
            break
    return best


def is_fresh(series, now: datetime, max_age_min: int = 15) -> bool:
    return bool(series) and (now - series[-1][0]) <= timedelta(minutes=max_age_min)


def pct(a, b) -> float:
    return (b - a) / a * 100 if a else 0.0


def fmt_price(key: str, v: float) -> str:
    dec = ASSET_BY_KEY[key][5]
    s = f"{v:,.{dec}f}".replace(",", " ")
    return s


def detect_moves(data: dict, now: datetime, sensitivity: float = 1.0):
    """Ищет резкие движения за 15 и 60 минут.
    Возвращает список (key, name, window_min, pct, p_from, p_to, ratio_to_threshold)."""
    moves = []
    for key, name, _, _, thr15, _ in ASSETS:
        s = data.get(key)
        if not is_fresh(s, now):
            continue
        last_t, p_now = s[-1]
        best = None
        for window, thr in ((15, thr15 * sensitivity), (60, thr15 * 1.8 * sensitivity)):
            p_then = price_at(s, last_t - timedelta(minutes=window))
            if not p_then:
                continue
            ch = pct(p_then, p_now)
            ratio = abs(ch) / thr
            if ratio >= 1 and (best is None or ratio > best[6]):
                best = (key, name, window, ch, p_then, p_now, ratio)
        if best:
            moves.append(best)
    return moves


def reaction(data: dict, start: datetime, now: datetime):
    """Реакция рынка на событие: изменение с момента за 5 мин до события,
    максимальный размах и текущая «скорость» (последние 5 мин).
    Возвращает список (key, name, ch_total, max_up, max_down, ch_last5, thr15)."""
    out = []
    base_t = start - timedelta(minutes=5)
    for key, name, _, _, thr15, _ in ASSETS:
        s = data.get(key)
        if not is_fresh(s, now, 20):
            continue
        base = price_at(s, base_t)
        if not base:
            continue
        after = [c for t, c in s if t >= base_t]
        if not after:
            continue
        p_now = s[-1][1]
        p5 = price_at(s, s[-1][0] - timedelta(minutes=5)) or p_now
        out.append((key, name, pct(base, p_now), pct(base, max(after)), pct(base, min(after)),
                    pct(p5, p_now), thr15))
    return out
