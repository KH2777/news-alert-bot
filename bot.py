#!/usr/bin/env python3
"""
Market Event Alert Bot — Telegram-помощник риск-менеджера.

Что делает при каждом запуске (GitHub Actions, каждые 5 минут):
  1. Экономический календарь (ForexFactory) + расписание Трампа (Белый дом)
     → предупреждение за ~60 мин и финальное за 15 мин до важных событий.
  2. Truth Social → мгновенный алерт, если Трамп написал о рынках.
  3. Новые пункты в расписании Трампа → сообщение, как только их опубликовали.
  4. Утренний дайджест на сегодня и вечерний — на завтра.
  5. Команды в чате: /today /tomorrow /week /next /truth /help.
Состояние (что уже отправлено) хранится в state.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import impact
import prices
import truth

# ---------------------------------------------------------------- настройки
def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")           # опционально: AI-комментарий
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "") or "claude-sonnet-4-5"

DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "") or "Europe/London")
LEAD_MINUTES = env_int("LEAD_MINUTES", 60)          # основное предупреждение
FINAL_MINUTES = env_int("FINAL_MINUTES", 15)        # финальное напоминание (0 = выкл)
MIN_LEVEL = env_int("MIN_LEVEL", 2)                 # 1=средний, 2=высокий, 3=критический
FINAL_MIN_LEVEL = env_int("FINAL_MIN_LEVEL", 3)     # для каких уровней финальное напоминание
DIGEST_HOUR = env_int("DIGEST_HOUR", 7)             # утренний дайджест, -1 = выкл
EVENING_HOUR = env_int("EVENING_HOUR", 21)          # вечерний дайджест на завтра, -1 = выкл
TRUMP_SENSITIVE = os.getenv("TRUMP_SENSITIVE", "1") != "0"   # любое публичное событие с прессой = важно
TRUTH_ENABLED = os.getenv("TRUTH_ENABLED", "1") != "0"
TRUTH_MIN_LEVEL = env_int("TRUTH_MIN_LEVEL", 2)     # 0 = присылать все посты (нерыночные — беззвучно)
TRUTH_MAX_AGE_MIN = env_int("TRUTH_MAX_AGE_MIN", 180)  # старше — не присылаем (защита от флуда)
QUIET_HOURS = os.getenv("QUIET_HOURS", "") or "0-7"     # некритичное в эти часы — без звука
PRICE_ALERTS = os.getenv("PRICE_ALERTS", "1") != "0"         # алерты о резком движении цены
PRICE_SENSITIVITY = float(os.getenv("PRICE_SENSITIVITY", "") or 1.0)  # 0.7 = чувствительнее, 1.5 = реже
PRICE_COOLDOWN_MIN = env_int("PRICE_COOLDOWN_MIN", 45)      # не повторять алерт по тому же активу
REPORT_MIN_LEVEL = env_int("REPORT_MIN_LEVEL", 2)            # отчёт «после события» для этих уровней (0 = выкл)
REPORT_DELAY_MIN = env_int("REPORT_DELAY_MIN", 10)           # через сколько минут после события
WEEKLY_DAY = env_int("WEEKLY_DAY", 6)                        # день недельного обзора: 0=Пн … 6=Вс, -1 = выкл
WEEKLY_HOUR = env_int("WEEKLY_HOUR", 18)
COUNTRIES = set((os.getenv("COUNTRIES", "") or "USD,EUR,GBP,JPY,CNY,CAD,AUD,CHF,All").split(","))
DRY_RUN = os.getenv("DRY_RUN") == "1"

STATE_FILE = Path(os.getenv("STATE_FILE", "") or Path(__file__).with_name("state.json"))
FIXTURES = os.getenv("FIXTURES_DIR")   # тесты: JSON/XML из папки вместо сети
NOW_OVERRIDE = os.getenv("NOW")        # тесты: ISO-время "сейчас"

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
TRUMP_URL = "https://media-cdn.factba.se/rss/json/trump/calendar-full.json"
ET = ZoneInfo("America/New_York")
LOCAL_TAGS = {  # "[CDT Local]" в расписании Белого дома
    "EDT": "America/New_York", "EST": "America/New_York",
    "CDT": "America/Chicago", "CST": "America/Chicago",
    "MDT": "America/Denver", "MST": "America/Denver",
    "PDT": "America/Los_Angeles", "PST": "America/Los_Angeles",
    "BST": "Europe/London", "GMT": "Europe/London", "CEST": "Europe/Paris", "CET": "Europe/Paris",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (market-alert-bot; risk management alerts)"}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass
class Event:
    when: datetime            # aware, UTC
    source: str               # "calendar" | "trump"
    title: str
    country: str
    level: int
    label: str
    note: str
    assets: str
    forecast: str = ""
    previous: str = ""
    location: str = ""
    time_note: str = ""
    uid: str = field(default="")

    def __post_init__(self):
        if not self.uid:
            raw = f"{self.source}|{self.title}|{self.country}|{self.when.isoformat()}"
            self.uid = hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- загрузка
_cache: dict = {}


def fetch_json(url: str):
    if url in _cache:
        return _cache[url]
    if FIXTURES:
        p = Path(FIXTURES) / url.rsplit("/", 1)[-1]
        data = json.loads(p.read_text()) if p.exists() else []
    else:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    _cache[url] = data
    return data


def load_calendar() -> list[Event]:
    events = []
    for url in FF_URLS:
        try:
            data = fetch_json(url)
        except Exception as e:  # nextweek бывает 404 до конца недели — это нормально
            print(f"[warn] {url}: {e}", file=sys.stderr)
            continue
        for item in data:
            country = item.get("country", "")
            if country not in COUNTRIES or item.get("impact") in ("Holiday", "Non-Economic"):
                continue
            cls = impact.classify_calendar(item.get("title", ""), country, item.get("impact", ""))
            if not cls:
                continue
            try:
                when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
            except Exception:
                continue
            level, label, note = cls
            events.append(Event(
                when=when, source="calendar", title=item["title"], country=country,
                level=level, label=label, note=note,
                assets=impact.CCY_ASSETS.get(country, country),
                forecast=item.get("forecast", ""), previous=item.get("previous", ""),
            ))
    return events


def trump_raw() -> list[dict]:
    try:
        return fetch_json(TRUMP_URL)
    except Exception as e:
        print(f"[warn] trump schedule: {e}", file=sys.stderr)
        return []


def clean_details(d: str) -> str:
    return re.sub(r"^TBD:\s*|\s*\[[^\]]*Local\]", "", (d or "").strip())


def load_trump() -> list[Event]:
    events = []
    for item in trump_raw():
        if not item.get("time") or not item.get("date"):
            continue  # без времени — только в дайджест и в «новое в расписании»
        details = (item.get("details") or "").strip()
        cls = impact.classify_trump(details, item.get("coverage"), TRUMP_SENSITIVE)
        if not cls:
            continue
        # Поле time в расписании Белого дома всегда по ET; в скобках — местное время площадки
        tz = ET
        m = re.search(r"\[([^\]]*\d[^\]]*) Local\]", details)
        time_note = f"местное время площадки: {m.group(1)}" if m else ""
        try:
            naive = datetime.fromisoformat(f"{item['date']}T{item['time']}")
        except Exception:
            continue
        level, label, note = cls
        events.append(Event(
            when=naive.replace(tzinfo=tz).astimezone(timezone.utc), source="trump",
            title=clean_details(details), country="USA", level=level,
            label=f"Трамп: {label}", note=note, assets=impact.TRUMP_ASSETS,
            location=item.get("location") or "", time_note=time_note,
        ))
    return events


def trump_untimed(day_et: str) -> list[str]:
    """События Трампа на дату (ET) без точного времени (TBD)."""
    out = []
    for item in trump_raw():
        if item.get("date") == day_et and not item.get("time"):
            d = item.get("details") or ""
            if impact.classify_trump(d, item.get("coverage"), TRUMP_SENSITIVE):
                out.append(clean_details(d))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- AI-комментарий
def ask_ai(prompt: str) -> str:
    if not ANTHROPIC_API_KEY or DRY_RUN:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 350,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:
        print(f"[warn] AI comment failed: {e}", file=sys.stderr)
        return ""


ROLE = "Ты помощник риск-менеджера проп-фирмы (крипта, forex, индексы, сырьё). "


def ai_event_comment(group: list[Event]) -> str:
    desc = "\n".join(f"- {e.label}: {e.title} ({e.country}); прогноз {e.forecast or 'н/д'}, "
                     f"пред. {e.previous or 'н/д'}" for e in group)
    return ask_ai(ROLE + f"Через ~час события:\n{desc}\n\nДай 2–3 коротких пункта на русском: "
                  "какие инструменты затронет сильнее всего, сценарии сюрприза (выше/ниже прогноза), "
                  "на что обратить внимание. До 60 слов, без заголовков.")


def ai_truth_comment(text: str) -> str:
    return ask_ai(ROLE + f"Трамп только что опубликовал в Truth Social:\n«{text[:1500]}»\n\n"
                  "Оцени на русском в 2–3 коротких пунктах: есть ли тут новая рыночная информация "
                  "(или это повтор/шум), какие инструменты и в какую сторону может двинуть, "
                  "насколько срочно реагировать. До 60 слов, без заголовков.")


# ---------------------------------------------------------------- форматирование
def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(DISPLAY_TZ).strftime("%H:%M")


def fmt_day(dt: datetime) -> str:
    d = dt.astimezone(DISPLAY_TZ)
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def format_alert(group: list[Event], now: datetime, final: bool) -> str:
    top = max(e.level for e in group)
    mins = max(0, round((group[0].when - now).total_seconds() / 60))
    head = "⏰ ФИНАЛЬНОЕ НАПОМИНАНИЕ" if final else "⚠️ ВАЖНОЕ СОБЫТИЕ"
    lines = [
        f"{impact.LEVEL_ICON[top]} <b>{head} через {mins} мин</b>",
        f"🕐 <b>{fmt_time(group[0].when)}</b> ({DISPLAY_TZ.key}) · {group[0].when.astimezone(ET).strftime('%H:%M')} ET",
        "",
    ]
    for e in sorted(group, key=lambda x: -x.level):
        lines.append(f"{impact.LEVEL_ICON[e.level]} <b>{esc(e.label)}</b> — {esc(e.title)} [{e.country}]")
        if e.forecast or e.previous:
            lines.append(f"   прогноз: <b>{esc(e.forecast or '—')}</b> · пред.: {esc(e.previous or '—')}")
        if e.location:
            lines.append(f"   📍 {esc(e.location)}")
        if e.time_note:
            lines.append(f"   {esc(e.time_note)}")
    if final:
        lines += ["", f"🛡 {esc(impact.risk_advice(top))}"]
        return "\n".join(lines)
    main = max(group, key=lambda x: x.level)
    assets = "; ".join(dict.fromkeys(e.assets for e in group))
    lines += [
        "",
        f"<b>Импакт: {impact.LEVEL_NAME[top]}</b>",
        f"💡 {esc(main.note)}",
        f"📊 Затронет: {esc(assets)}",
        f"🛡 <b>Действие:</b> {esc(impact.risk_advice(top))}",
    ]
    comment = ai_event_comment(group)
    if comment:
        lines += ["", f"🤖 <i>{esc(comment)}</i>"]
    return "\n".join(lines)


def format_digest(events: list[Event], untimed: list[str], day: datetime, title: str, footer: bool = True) -> str:
    lines = [f"<b>{title}</b> — {fmt_day(day)}", ""]
    if not events and not untimed:
        lines.append("Важных событий не запланировано 🙂")
    for e in sorted(events, key=lambda x: x.when):
        extra = f" · прогноз {esc(e.forecast)}" if e.forecast else ""
        lines.append(f"{impact.LEVEL_ICON[e.level]} <b>{fmt_time(e.when)}</b> {esc(e.title)} [{e.country}]{extra}")
    if untimed:
        lines += ["", "🇺🇸 <b>Трамп, время ещё не объявлено:</b>"]
        lines += [f"• {esc(t)}" for t in untimed]
    if footer:
        lines += ["", f"🔴🔴 критический · 🔴 высокий · 🟠 средний (без отдельного алерта)",
                  f"Время: {DISPLAY_TZ.key}. Алерты за {LEAD_MINUTES} мин до 🔴 и 🔴🔴."]
    return "\n".join(lines)


def format_truth(post: dict, level: int, topics: list[str], assets: str, now: datetime) -> str:
    age = max(0, round((now - post["created"]).total_seconds() / 60))
    text = post["text"]
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0] + "…"
    if level >= 2:
        head = f"{impact.LEVEL_ICON[level]} <b>TRUTH SOCIAL · Трамп о рынках</b>"
    else:
        head = "💬 <b>Truth Social · новый пост Трампа</b>"
    lines = [head, f"🕐 {fmt_time(post['created'])} ({age} мин назад)", "", f"«{esc(text)}»"]
    if level >= 2:
        lines += ["", f"🏷 Темы: {esc(', '.join(topics))}", f"📊 Может задеть: {esc(assets)}"]
        if level >= 3:
            lines.append("🛡 <b>Проверьте позиции</b> в затронутых инструментах — возможен резкий импульс.")
        comment = ai_truth_comment(post["text"])
        if comment:
            lines += ["", f"🤖 <i>{esc(comment)}</i>"]
    if post.get("url"):
        lines += ["", f'<a href="{esc(post["url"])}">Открыть пост</a>']
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
def in_quiet_hours(now: datetime) -> bool:
    try:
        a, b = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        return False
    h = now.astimezone(DISPLAY_TZ).hour
    return a <= h < b if a <= b else (h >= a or h < b)


def send(text: str, silent: bool = False) -> None:
    if DRY_RUN or not TG_TOKEN:
        plain = re.sub(r"</?[bi]>|<a [^>]+>|</a>", "", text)
        plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        print("=" * 60 + (" [без звука]" if silent else "") + "\n" + plain + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True, "disable_notification": silent},
        timeout=30,
    )
    if not r.ok:
        print(f"[error] telegram: {r.status_code} {r.text}", file=sys.stderr)
        r.raise_for_status()


HELP = (
    "🤖 <b>Бот риск-менеджера на страже</b>\n\n"
    "<b>Команды</b> (ответ в течение ~5 мин):\n"
    "/next — 5 ближайших важных событий\n"
    "/today — все события сегодня\n"
    "/tomorrow — события на завтра\n"
    "/week — важное на 7 дней\n"
    "/truth — последние 5 постов Трампа\n"
    "/market — цены и движение за 15 мин / 1 час\n"
    "/help — эта справка\n\n"
    "<b>Что приходит само:</b>\n"
    "☀️ дайджест утром · 🌙 план на завтра вечером\n"
    "⚠️ алерт за ~60 мин до 🔴/🔴🔴 · ⏰ напоминание за 15 мин до 🔴🔴\n"
    "🚨 посты Трампа о рынках в Truth Social\n"
    "🆕 новые события в расписании Трампа\n"
    "⚡ резкие движения BTC, ETH, S&P, Nasdaq, золота, нефти, DXY, EUR/USD, USD/JPY\n"
    "📊 итог через 10 мин после важного события — можно ли возвращать плечи\n"
    "🗓 обзор недели в воскресенье вечером"
)


def handle_commands(state: dict, events: list[Event], now: datetime) -> None:
    if DRY_RUN or not TG_TOKEN:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": state.get("tg_offset", 0), "timeout": 0}, timeout=30)
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[warn] getUpdates: {e}", file=sys.stderr)
        return
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT_ID):
            continue  # отвечаем только своему чату
        cmd = (msg.get("text") or "").split("@")[0].split()[0:1]
        cmd = cmd[0].lower() if cmd else ""
        try:
            run_command(cmd, events, now)
        except Exception as e:
            print(f"[warn] command {cmd}: {e}", file=sys.stderr)


def day_events(events: list[Event], day) -> list[Event]:
    return [e for e in events if e.when.astimezone(DISPLAY_TZ).date() == day and e.level >= 1]


def run_command(cmd: str, events: list[Event], now: datetime) -> None:
    local_now = now.astimezone(DISPLAY_TZ)
    if cmd == "/today":
        send(format_digest(day_events(events, local_now.date()),
                           trump_untimed(now.astimezone(ET).date().isoformat()), local_now, "📅 События сегодня", False))
    elif cmd == "/tomorrow":
        tm = local_now + timedelta(days=1)
        send(format_digest(day_events(events, tm.date()),
                           trump_untimed((now.astimezone(ET) + timedelta(days=1)).date().isoformat()),
                           tm, "📅 События завтра", False))
    elif cmd == "/week":
        wk = sorted([e for e in events if now <= e.when <= now + timedelta(days=7) and e.level >= 2],
                    key=lambda x: x.when)
        lines = ["📆 <b>Важное на 7 дней</b>", ""]
        lines += [f"{impact.LEVEL_ICON[e.level]} {fmt_day(e.when)} {fmt_time(e.when)} {esc(e.title)} [{e.country}]"
                  for e in wk[:60]]
        send("\n".join(lines) if wk else "На неделю важных событий не найдено.")
    elif cmd == "/next":
        nxt = sorted([e for e in events if e.when > now and e.level >= 2], key=lambda x: x.when)[:5]
        lines = ["⏭ <b>Ближайшие важные события</b>", ""]
        lines += [f"{impact.LEVEL_ICON[e.level]} {fmt_day(e.when)} <b>{fmt_time(e.when)}</b> {esc(e.title)} [{e.country}]"
                  for e in nxt]
        send("\n".join(lines) if nxt else "Ближайших важных событий нет.")
    elif cmd == "/truth":
        try:
            posts = truth.fetch_posts(HEADERS)[:5]
        except Exception as e:
            send(f"Не удалось получить посты: {esc(str(e))[:200]}")
            return
        lines = ["📣 <b>Последние посты Трампа</b>", ""]
        for p in posts:
            lvl, topics, _ = truth.classify(p["text"])
            icon = impact.LEVEL_ICON.get(lvl, "💬")
            t = p["text"][:220] + ("…" if len(p["text"]) > 220 else "")
            lines.append(f"{icon} <b>{fmt_day(p['created'])} {fmt_time(p['created'])}</b> {esc(t)}")
        send("\n\n".join([lines[0]] + lines[2:]))
    elif cmd in ("/market", "/prices"):
        send(market_snapshot(now))
    elif cmd in ("/start", "/help"):
        send(HELP)


# ---------------------------------------------------------------- блоки логики
def do_digests(state: dict, sent: dict, events: list[Event], now: datetime) -> None:
    local_now = now.astimezone(DISPLAY_TZ)
    today = local_now.date()
    if DIGEST_HOUR >= 0 and local_now.hour >= DIGEST_HOUR and f"digest:{today}" not in sent:
        send(format_digest(day_events(events, today),
                           trump_untimed(now.astimezone(ET).date().isoformat()), local_now, "☀️ План на день"))
        sent[f"digest:{today}"] = now.isoformat()
    if EVENING_HOUR >= 0 and local_now.hour >= EVENING_HOUR and f"evening:{today}" not in sent:
        tm = local_now + timedelta(days=1)
        msg = format_digest(day_events(events, tm.date()),
                            trump_untimed((now.astimezone(ET) + timedelta(days=1)).date().isoformat()),
                            tm, "🌙 План на завтра", footer=False)
        msg += "\n\nРасписание Трампа на завтра часто публикуют поздно вечером — новые пункты пришлю отдельно 🆕"
        send(msg, silent=in_quiet_hours(now))
        sent[f"evening:{today}"] = now.isoformat()


def do_event_alerts(sent: dict, events: list[Event], now: datetime) -> None:
    def due(kind: str, minutes: int, min_level: int) -> list[Event]:
        return [e for e in events
                if e.level >= min_level and f"{kind}:{e.uid}" not in sent
                and timedelta(0) < (e.when - now) <= timedelta(minutes=minutes)]

    for kind, minutes, min_level in (("lead", LEAD_MINUTES, MIN_LEVEL),
                                     ("final", FINAL_MINUTES, FINAL_MIN_LEVEL)):
        if minutes <= 0:
            continue
        groups: dict[datetime, list[Event]] = {}
        for e in due(kind, minutes, min_level):
            groups.setdefault(e.when, []).append(e)
        for when in sorted(groups):
            g = groups[when]
            if kind == "final" and any(f"lead:{e.uid}" not in sent for e in g):
                continue
            send(format_alert(g, now, final=(kind == "final")))
            for e in g:
                sent[f"{kind}:{e.uid}"] = now.isoformat()
            if kind == "lead" and (when - now) <= timedelta(minutes=FINAL_MINUTES):
                for e in g:  # полный алерт уже внутри окна финального — второй не нужен
                    sent[f"final:{e.uid}"] = now.isoformat()


def do_trump_schedule_updates(state: dict, now: datetime) -> None:
    """Сообщает о новых пунктах в расписании Трампа на сегодня/завтра/послезавтра."""
    raw = trump_raw()
    if not raw:
        return
    today_et = now.astimezone(ET).date()
    horizon = {(today_et + timedelta(days=i)).isoformat() for i in range(3)}
    known: dict = state.setdefault("trump_known", {})
    first_run = not state.get("trump_init")
    state["trump_init"] = True
    new_items = []
    for item in raw:
        if item.get("date") not in horizon:
            continue
        d = item.get("details") or ""
        cls = impact.classify_trump(d, item.get("coverage"), TRUMP_SENSITIVE)
        if not cls or cls[0] < 2:
            continue
        key = hashlib.sha1(f"{item['date']}|{item.get('time')}|{clean_details(d)}".encode()).hexdigest()[:16]
        if key in known:
            continue
        known[key] = item["date"]
        if item.get("time"):
            try:
                t = datetime.fromisoformat(f"{item['date']}T{item['time']}").replace(tzinfo=ET)
                if t < now - timedelta(hours=1):
                    continue  # уже прошло
            except Exception:
                pass
        new_items.append((item, cls))
    # чистим старое
    for k, v in list(known.items()):
        if v < (today_et - timedelta(days=2)).isoformat():
            del known[k]
    if first_run or not new_items:
        return
    lines = ["🆕 <b>Новое в расписании Трампа</b>", ""]
    top = 1
    for item, (level, label, _) in sorted(new_items, key=lambda x: (x[0]["date"], x[0].get("time") or "99")):
        top = max(top, level)
        if item.get("time"):
            when = datetime.fromisoformat(f"{item['date']}T{item['time']}").replace(tzinfo=ET)
            t = f"{fmt_day(when)} <b>{fmt_time(when)}</b>"
        else:
            d = datetime.fromisoformat(item["date"]).replace(tzinfo=ET) + timedelta(hours=12)
            t = f"{fmt_day(d)} <b>время TBD</b>"
        lines.append(f"{impact.LEVEL_ICON[level]} {t} — {esc(clean_details(item.get('details')))}")
        if item.get("location"):
            lines.append(f"   📍 {esc(item['location'])}")
    lines += ["", f"Алерт за {LEAD_MINUTES} мин придёт автоматически, когда будет известно время."]
    send("\n".join(lines), silent=in_quiet_hours(now) and top < 3)


def do_truth(state: dict, now: datetime) -> None:
    if not TRUTH_ENABLED:
        return
    try:
        if FIXTURES and (Path(FIXTURES) / "truth.json").exists():
            posts = json.loads((Path(FIXTURES) / "truth.json").read_text())
            for p in posts:
                p["created"] = datetime.fromisoformat(p["created"])
        else:
            posts = truth.fetch_posts(HEADERS)
    except Exception as e:
        print(f"[warn] truth social: {e}", file=sys.stderr)
        return
    seen: list = state.setdefault("truth_seen", [])
    first_run = not state.get("truth_init")
    state["truth_init"] = True
    for p in sorted(posts, key=lambda x: x["created"]):
        if not p["id"] or p["id"] in seen:
            continue
        seen.append(p["id"])
        if first_run or (now - p["created"]) > timedelta(minutes=TRUTH_MAX_AGE_MIN):
            continue  # при первом запуске не спамим старыми постами
        level, topics, assets = truth.classify(p["text"])
        if level < max(TRUTH_MIN_LEVEL, 0) and TRUTH_MIN_LEVEL > 0:
            continue
        if level >= 2:
            recent = state.setdefault("truth_recent", [])
            recent.append({"t": p["created"].isoformat(), "topics": topics[:3], "text": p["text"][:90]})
            state["truth_recent"] = recent[-10:]
        silent = level < 2 or (in_quiet_hours(now) and level < 3)
        send(format_truth(p, level, topics, assets, now), silent=silent)
    state["truth_seen"] = seen[-300:]


# ---------------------------------------------------------------- цены
_prices: dict | None = None


def get_prices() -> dict:
    global _prices
    if _prices is None:
        if FIXTURES and (Path(FIXTURES) / "prices.json").exists():
            raw = json.loads((Path(FIXTURES) / "prices.json").read_text())
            _prices = {k: [(datetime.fromisoformat(t), float(c)) for t, c in v] for k, v in raw.items()}
        else:
            _prices = prices.fetch_all(HEADERS)
        print(f"[info] котировки получены: {', '.join(_prices) or 'нет'}")
    return _prices


def sign(v: float) -> str:
    return ("+" if v >= 0 else "−") + f"{abs(v):.2f}%"


def possible_causes(state: dict, events: list[Event], now: datetime) -> list[str]:
    out = []
    for e in sorted(events, key=lambda x: x.when):
        if now - timedelta(minutes=35) <= e.when <= now + timedelta(minutes=5) and e.level >= 1:
            out.append(f"{impact.LEVEL_ICON[e.level]} {fmt_time(e.when)} {esc(e.title)} [{e.country}]")
    for p in state.get("truth_recent", []):
        t = datetime.fromisoformat(p["t"])
        if now - timedelta(minutes=40) <= t <= now:
            out.append(f"📣 {fmt_time(t)} пост Трампа ({esc(', '.join(p['topics']))}): «{esc(p['text'])}…»")
    return out


def do_price_alerts(state: dict, events: list[Event], now: datetime) -> None:
    if not PRICE_ALERTS:
        return
    data = get_prices()
    cd: dict = state.setdefault("price_cd", {})
    fresh = []
    for key, name, window, ch, p_from, p_to, ratio in prices.detect_moves(data, now, PRICE_SENSITIVITY):
        last = cd.get(key)
        if last:
            t_last, dir_last = datetime.fromisoformat(last["t"]), last["dir"]
            same_dir = (ch > 0) == (dir_last > 0)
            if same_dir and now - t_last < timedelta(minutes=PRICE_COOLDOWN_MIN):
                continue
        cd[key] = {"t": now.isoformat(), "dir": 1 if ch > 0 else -1}
        fresh.append((key, name, window, ch, p_from, p_to, ratio))
    if not fresh:
        return
    top = 3 if any(m[6] >= 2 for m in fresh) else 2
    lines = [f"{impact.LEVEL_ICON[top]} <b>⚡ РЕЗКОЕ ДВИЖЕНИЕ РЫНКА</b>", ""]
    for key, name, window, ch, p_from, p_to, ratio in sorted(fresh, key=lambda m: -m[6]):
        arrow = "📈" if ch > 0 else "📉"
        lines.append(f"{arrow} <b>{name} {sign(ch)}</b> за {window} мин "
                     f"({prices.fmt_price(key, p_from)} → {prices.fmt_price(key, p_to)})")
    causes = possible_causes(state, events, now)
    lines.append("")
    if causes:
        lines.append("<b>Возможная причина:</b>")
        lines += causes[:4]
    else:
        lines.append("🔎 В календаре и Truth Social причины нет — проверьте новостную ленту "
                     "(заголовки, ликвидации, крупные ордера).")
    lines += ["", f"🛡 {esc(impact.risk_advice(top))}"]
    send("\n".join(lines), silent=in_quiet_hours(now) and top < 3)


def do_event_reports(sent: dict, events: list[Event], now: datetime) -> None:
    """Через REPORT_DELAY_MIN после важного события — как отреагировал рынок."""
    if REPORT_MIN_LEVEL <= 0:
        return
    groups: dict[datetime, list[Event]] = {}
    for e in events:
        if e.level < REPORT_MIN_LEVEL or f"report:{e.uid}" in sent:
            continue
        if e.source == "trump" and e.level < 3:
            continue  # по рядовым событиям Трампа итог не шлём, чтобы не шуметь
        age = now - e.when
        if timedelta(minutes=REPORT_DELAY_MIN) <= age <= timedelta(minutes=REPORT_DELAY_MIN + 30):
            groups.setdefault(e.when, []).append(e)
    if not groups:
        return
    data = get_prices()
    for when in sorted(groups):
        g = groups[when]
        for e in g:
            sent[f"report:{e.uid}"] = now.isoformat()
        rows = prices.reaction(data, when, now)
        if not rows:
            continue  # рынок закрыт / нет котировок
        top = max(e.level for e in g)
        mins = round((now - when).total_seconds() / 60)
        title = max(g, key=lambda x: x.level)
        lines = [f"📊 <b>ИТОГ: {esc(title.title)}</b>" + (f" (+ ещё {len(g) - 1})" if len(g) > 1 else ""),
                 f"🕐 событие в {fmt_time(when)}, прошло {mins} мин", ""]
        if any(e.forecast for e in g):
            lines += [f"• {esc(e.title)}: прогноз {esc(e.forecast or '—')}, пред. {esc(e.previous or '—')}"
                      for e in g if e.forecast]
            lines.append("")
        calm = True
        strong = False
        for key, name, ch, up, down, ch5, thr in sorted(rows, key=lambda r: -abs(r[2]) / r[6]):
            ratio = abs(ch) / thr
            icon = "🔴" if ratio >= 1 else ("🟠" if ratio >= 0.5 else "⚪")
            strong |= ratio >= 1
            if abs(ch5) >= thr * 0.4:
                calm = False
            lines.append(f"{icon} <b>{name}</b> {sign(ch)} · размах {sign(down)} / {sign(up)}")
        lines.append("")
        if not strong:
            lines.append("✅ <b>Реакция слабая.</b> Можно возвращать обычные плечи.")
        elif calm:
            lines.append("🟡 <b>Был сильный импульс, сейчас рынок успокаивается.</b> "
                         "Плечи возвращать постепенно, следить за продолжением тренда.")
        else:
            lines.append("⏳ <b>Волатильность сохраняется.</b> Плечи пока не возвращать.")
        lines.append("<i>Фактическое значение — в терминале: бесплатный календарь его не публикует.</i>"
                     if any(e.source == "calendar" for e in g) else "")
        send("\n".join(l for l in lines).rstrip(), silent=in_quiet_hours(now) and top < 3)


def do_weekly(state: dict, sent: dict, events: list[Event], now: datetime) -> None:
    if WEEKLY_DAY < 0:
        return
    local_now = now.astimezone(DISPLAY_TZ)
    key = f"weekly:{local_now.date()}"
    if local_now.weekday() != WEEKLY_DAY or local_now.hour < WEEKLY_HOUR or key in sent:
        return
    sent[key] = now.isoformat()
    start = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    week = sorted([e for e in events if start <= e.when.astimezone(DISPLAY_TZ) < end and e.level >= 2],
                  key=lambda x: x.when)
    lines = [f"🗓 <b>ОБЗОР НЕДЕЛИ</b> {start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m')}", ""]
    if not week:
        lines.append("Календарь на следующую неделю пока пуст — обновлю, как только появятся данные. "
                     "Команда /week покажет актуальное.")
        send("\n".join(lines))
        return
    by_day: dict = {}
    for e in week:
        by_day.setdefault(e.when.astimezone(DISPLAY_TZ).date(), []).append(e)
    danger = []
    for day, evs in by_day.items():
        crit = [e for e in evs if e.level >= 3]
        d = evs[0].when
        lines.append(f"<b>{fmt_day(d)}</b>" + ("  ⚠️ опасный день" if crit else ""))
        for e in evs:
            lines.append(f"  {impact.LEVEL_ICON[e.level]} {fmt_time(e.when)} {esc(e.title)} [{e.country}]")
        if crit:
            danger.append((d, crit))
    lines.append("")
    cb = [e for e in week if re.search(r"FOMC|Federal Funds|ECB|BOE|BOJ|Bank Rate|Policy Rate|Cash Rate|Overnight Rate|Refinancing", e.title, re.I)]
    if cb:
        lines.append("🏦 <b>Центробанки:</b> " + ", ".join(f"{esc(e.title)} ({fmt_day(e.when)})" for e in cb[:5]))
    if danger:
        lines.append("🔥 <b>Самые опасные окна:</b>")
        for d, crit in danger:
            lines.append(f"  • {fmt_day(d)}: " + ", ".join(f"{fmt_time(e.when)} {esc(e.title)}" for e in crit[:3]))
        lines += ["", "🛡 <b>План по риску:</b> на эти окна заранее снижать плечи, не держать рыночные стопы "
                      "вплотную к цене, в пятницу перед NFP/CPI — особенно осторожно с позициями на выходные."]
    else:
        lines.append("😌 Критических событий не запланировано — спокойная неделя по календарю.")
    lines += ["", "Расписание Трампа публикуется за день-два — новые пункты пришлю отдельно 🆕"]
    send("\n".join(lines))


def market_snapshot(now: datetime) -> str:
    data = get_prices()
    lines = ["💹 <b>Рынок сейчас</b>", "<i>изменение за 15 мин / 1 час</i>", ""]
    for key, name, _, _, thr, _ in prices.ASSETS:
        s = data.get(key)
        if not prices.is_fresh(s, now, 30):
            lines.append(f"⚪ {name}: нет данных / рынок закрыт")
            continue
        t, p = s[-1]
        p15 = prices.price_at(s, t - timedelta(minutes=15))
        p60 = prices.price_at(s, t - timedelta(minutes=60))
        c15 = prices.pct(p15, p) if p15 else 0
        c60 = prices.pct(p60, p) if p60 else 0
        icon = "🔴" if abs(c15) >= thr else ("🟠" if abs(c15) >= thr * 0.5 else "🟢")
        lines.append(f"{icon} <b>{name}</b> {prices.fmt_price(key, p)}  {sign(c15)} / {sign(c60)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- основной цикл
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=10)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def main() -> None:
    now = datetime.fromisoformat(NOW_OVERRIDE).astimezone(timezone.utc) if NOW_OVERRIDE \
        else datetime.now(timezone.utc)
    state = load_state()
    sent: dict = state.setdefault("sent", {})

    all_events = load_calendar() + load_trump()
    all_events = list({e.uid: e for e in all_events}.values())
    upcoming = [e for e in all_events if e.when > now - timedelta(days=1)]
    alert_events = [e for e in upcoming if e.level >= MIN_LEVEL]
    print(f"[info] {now.isoformat()} — событий впереди: {len(upcoming)}, для алертов: {len(alert_events)}")

    errors = []
    for name, fn in (
        ("commands", lambda: handle_commands(state, upcoming, now)),
        ("truth", lambda: do_truth(state, now)),
        ("alerts", lambda: do_event_alerts(sent, alert_events, now)),
        ("schedule", lambda: do_trump_schedule_updates(state, now)),
        ("prices", lambda: do_price_alerts(state, upcoming, now)),
        ("reports", lambda: do_event_reports(sent, alert_events, now)),
        ("digests", lambda: do_digests(state, sent, upcoming, now)),
        ("weekly", lambda: do_weekly(state, sent, upcoming, now)),
    ):
        try:
            fn()
        except Exception as e:  # один сбой не должен ломать остальные блоки
            errors.append(f"{name}: {e}")
            print(f"[error] {name}: {e}", file=sys.stderr)

    save_state(state, now)
    if errors:
        sys.exit(1)  # чтобы GitHub прислал письмо о проблеме


if __name__ == "__main__":
    main()
