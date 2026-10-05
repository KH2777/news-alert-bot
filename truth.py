"""
Мониторинг постов Трампа в Truth Social.

Источник 1: RSS-архив trumpstruth.org (стабильно доступен с серверов).
Источник 2 (запасной): публичный API Truth Social.
Каждый новый пост проверяется по ключевым словам и получает уровень импакта.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

RSS_URL = "https://trumpstruth.org/feed"
API_URL = "https://truthsocial.com/api/v1/accounts/107780257626128497/statuses?limit=20&exclude_replies=true"
NS = {"truth": "https://truthsocial.com/ns"}

# (тема, уровень, регулярка, флаги, затронутые активы)
TOPICS = [
    ("Тарифы / торговля", 3, r"\btariffs?\b|\btrade (deal|war|deficit)|\bduties\b|\bimport tax|reciprocal|liberation day", re.I,
     "индексы США, USD/CNH, EUR/USD, USD/MXN, USD/CAD, медь, BTC"),
    ("ФРС / ставки", 3, r"\bFed\b|\bFOMC\b|(?i:federal reserve|\bpowell\b|\bwarsh\b|interest rates?|rate cuts?)", 0,
     "DXY, US Treasuries, золото, S&P 500 / Nasdaq, BTC/ETH"),
    ("Китай", 3, r"\bchina\b|\bchinese\b|\bXi\b|\bbeijing\b|\btaiwan\b", re.I,
     "USD/CNH, AUD/USD, медь, Hang Seng, индексы США"),
    ("Крипта", 3, r"\bbitcoin\b|\bcrypto|\bBTC\b|stablecoin|\bdigital assets?\b", re.I,
     "BTC, ETH, альткоины, COIN / MSTR"),
    ("Нефть / энергетика", 3, r"\boil\b|\bopec\b|\bdrill|\bgasoline\b|gas prices|\benergy prices|\bLNG\b", re.I,
     "Brent/WTI, USD/CAD, NOK, энергетический сектор"),
    ("Геополитика / война", 3, r"\bwar\b|\bstrikes?\b|\bmilitary\b|\bmissiles?\b|\bnuclear\b|\bceasefire\b|\binvasion\b|\battack", re.I,
     "золото, нефть, JPY, CHF, индексы, BTC"),
    ("Россия / Украина", 3, r"\brussia|\bputin\b|\bukrain|\bzelensk|\bkremlin\b", re.I,
     "нефть, газ, EUR, золото, пшеница"),
    ("Ближний Восток", 3, r"\biran\b|\bisrael\b|\bhamas\b|\bhezbollah\b|\bgaza\b|\bsaudi\b|\bhormuz\b", re.I,
     "нефть, золото, индексы"),
    ("Санкции", 3, r"\bsanction", re.I, "нефть, затронутые валюты, золото"),
    ("Фондовый рынок", 2, r"stock market|\bdow\b|\bS&P\b|\bnasdaq\b|all[- ]time high|\bstocks\b|\bmarkets?\b", re.I,
     "S&P 500, Nasdaq, Dow"),
    ("Доллар / долг / бюджет", 2, r"\bdollar\b|\btreasury\b|\bdebt\b|\bdeficit\b|\bshutdown\b|\bbudget\b|\btax cuts?\b|\bIRS\b", re.I,
     "DXY, US Treasuries, золото"),
    ("Экономика", 2, r"\beconomy\b|\binflation\b|\bjobs\b|\bunemployment\b|\bGDP\b|\brecession\b|\bprices\b", re.I,
     "USD, индексы, золото"),
    ("Страны-партнёры", 2, r"\bEU\b|(?i:european union|\bjapan|\bmexic|\bcanad|\bindia\b|\bkorea|\bvenezuela|\bbrazil)", 0,
     "EUR, JPY, MXN, CAD, INR, KRW"),
    ("Отрасли", 2, r"semiconductor|\bchips?\b|\bsteel\b|\balumin|\bautos?\b|\bpharma|\bdrug prices|\bnvidia\b|\btesla\b|\bapple\b", re.I,
     "секторные акции, индексы"),
    ("Указы / заявления", 2, r"executive order|\bannounce|\bemergency\b|\bbreaking\b", re.I,
     "зависит от темы — проверьте текст"),
]


def _clean(raw: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", raw or "", flags=re.I)
    text = re.sub(r'<span class="invisible">.*?</span>', "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def fetch_posts(headers: dict, timeout: int = 25) -> list[dict]:
    """Возвращает посты (новые сверху): {id, created, text, url}."""
    errors = []
    try:
        r = requests.get(RSS_URL, headers=headers, timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        posts = []
        for item in root.iter("item"):
            pid = item.findtext("truth:originalId", namespaces=NS) or item.findtext("guid") or ""
            url = item.findtext("truth:originalUrl", namespaces=NS) or item.findtext("link") or ""
            created = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
            text = _clean(item.findtext("description") or item.findtext("title") or "")
            posts.append({"id": pid.strip(), "created": created, "text": text, "url": url.strip()})
        if posts:
            return posts
    except Exception as e:
        errors.append(f"rss: {e}")
    try:
        r = requests.get(API_URL, headers=headers, timeout=timeout)
        r.raise_for_status()
        posts = []
        for s in r.json():
            src = s.get("reblog") or s
            posts.append({
                "id": str(s["id"]),
                "created": datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")),
                "text": ("🔁 Репост: " if s.get("reblog") else "") + _clean(src.get("content", "")),
                "url": s.get("url") or src.get("url", ""),
            })
        return posts
    except Exception as e:
        errors.append(f"api: {e}")
    raise RuntimeError("; ".join(errors))


def classify(text: str):
    """Возвращает (level, [темы], активы) — level 0, если пост не про рынки."""
    hits = []
    for name, level, pattern, flags, assets in TOPICS:
        if re.search(pattern, text, flags):
            hits.append((level, name, assets))
    if not hits:
        return 0, [], ""
    level = max(h[0] for h in hits)
    # Несколько рыночных тем в одном посте = сильнее
    if level == 2 and len(hits) >= 3:
        level = 3
    if len(text) < 40 and level == 3 and len(hits) == 1:
        level = 2  # короткая ссылка/репост без контекста
    names = [h[1] for h in sorted(hits, key=lambda h: -h[0])]
    items = [a.strip() for h in sorted(hits, key=lambda h: -h[0])[:3] for a in h[2].split(",")]
    assets = ", ".join(dict.fromkeys(items))
    return level, names, assets
