#!/usr/bin/env python3
"""
Market Event Alert Bot — предупреждает в Telegram о важных событиях
(макростатистика, центробанки, OPEC, выступления Трампа) заранее,
чтобы риск-менеджер успел срезать плечи.

Запускается по расписанию (GitHub Actions каждые 10 минут) — каждый запуск
проверяет календарь, отправляет то, что пора, и сохраняет state.json,
чтобы не слать дубли.
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

# ---------------------------------------------------------------- настройки
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")          # опционально: AI-комментарий
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "Europe/London"))  # в каком поясе показывать время
LEAD_MINUTES = int(os.getenv("LEAD_MINUTES", "60"))              # основное предупреждение
FINAL_MINUTES = int(os.getenv("FINAL_MINUTES", "15"))            # финальное напоминание (0 = выкл)
MIN_LEVEL = int(os.getenv("MIN_LEVEL", "2"))                     # 1=средний, 2=высокий, 3=критический
FINAL_MIN_LEVEL = int(os.getenv("FINAL_MIN_LEVEL", "3"))         # для каких уровней слать финальное
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "7"))                 # утренний дайджест (по DISPLAY_TZ), -1 = выкл
COUNTRIES = set(os.getenv("COUNTRIES", "USD,EUR,GBP,JPY,CNY,CAD,AUD,CHF,All").split(","))
DRY_RUN = os.getenv("DRY_RUN") == "1"

STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))
FIXTURES = os.getenv("FIXTURES_DIR")  # для тестов: читать JSON из папки вместо сети
NOW_OVERRIDE = os.getenv("NOW")       # для тестов: ISO-время "сейчас"

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
HEADERS = {"User-Agent": "market-alert-bot/1.0 (risk management alerts)"}


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
def fetch_json(url: str):
    if FIXTURES:
        p = Path(FIXTURES) / url.rsplit("/", 1)[-1]
        return json.loads(p.read_text()) if p.exists() else []
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def load_calendar() -> list[Event]:
    events = []
    for url in FF_URLS:
        try:
            data = fetch_json(url)
        except Exception as e:  # nextweek часто 404 до выходных — это нормально
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


def load_trump() -> list[Event]:
    try:
        data = fetch_json(TRUMP_URL)
    except Exception as e:
        print(f"[warn] trump schedule: {e}", file=sys.stderr)
        return []
    events = []
    for item in data:
        if not item.get("time") or not item.get("date"):
            continue  # без точного времени алерт за час не поставить — попадёт только в дайджест
        details = (item.get("details") or "").strip()
        cls = impact.classify_trump(details, item.get("coverage"))
        if not cls:
            continue
        tz, time_note = ET, "ET"
        m = re.search(r"\[(\w{3,4}) Local\]", details)
        if m:
            tz_name = LOCAL_TAGS.get(m.group(1).upper())
            if tz_name:
                tz, time_note = ZoneInfo(tz_name), f"{m.group(1)} local"
            else:
                time_note = f"⚠️ время указано по местному ({m.group(1)}), пересчёт не точный"
        try:
            naive = datetime.fromisoformat(f"{item['date']}T{item['time']}")
        except Exception:
            continue
        when = naive.replace(tzinfo=tz).astimezone(timezone.utc)
        level, label, note = cls
        clean = re.sub(r"\s*\[\w+ Local\]", "", details)
        events.append(Event(
            when=when, source="trump", title=clean, country="USA", level=level,
            label=f"Трамп: {label}", note=note, assets=impact.TRUMP_ASSETS,
            location=item.get("location") or "", time_note=time_note,
        ))
    return events


def load_trump_untimed(today_et: str) -> list[str]:
    """События Трампа на сегодня без точного времени (TBD) — для дайджеста."""
    try:
        data = fetch_json(TRUMP_URL)
    except Exception:
        return []
    out = []
    for item in data:
        if item.get("date") == today_et and not item.get("time"):
            d = (item.get("details") or "").strip()
            if impact.classify_trump(d.replace("TBD:", ""), item.get("coverage")):
                out.append(re.sub(r"^TBD:\s*|\s*\[\w+ Local\]", "", d))
    return out


# ---------------------------------------------------------------- AI-комментарий
def ai_comment(group: list[Event]) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    desc = "\n".join(
        f"- {e.label}: {e.title} ({e.country}); прогноз {e.forecast or 'н/д'}, пред. {e.previous or 'н/д'}"
        for e in group
    )
    prompt = (
        "Ты помощник риск-менеджера проп-фирмы (крипта, forex, индексы, сырьё). "
        f"Через ~час выходят события:\n{desc}\n\n"
        "Дай 2–3 коротких пункта на русском: какие инструменты затронет сильнее всего, "
        "в какую сторону сценарии сюрприза (выше/ниже прогноза), на что обратить внимание. "
        "Без воды, до 60 слов, без markdown-заголовков."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:
        print(f"[warn] AI comment failed: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------- форматирование
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(DISPLAY_TZ).strftime("%H:%M")


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
        if e.time_note and e.time_note != "ET":
            lines.append(f"   {esc(e.time_note)}")
    if not final:
        main = max(group, key=lambda x: x.level)
        assets = "; ".join(dict.fromkeys(e.assets for e in group))
        lines += [
            "",
            f"<b>Импакт: {impact.LEVEL_NAME[top]}</b>",
            f"💡 {esc(main.note)}",
            f"📊 Затронет: {esc(assets)}",
            f"🛡 <b>Действие:</b> {esc(impact.risk_advice(top))}",
        ]
        comment = ai_comment(group)
        if comment:
            lines += ["", f"🤖 <i>{esc(comment)}</i>"]
    else:
        lines += ["", f"🛡 {esc(impact.risk_advice(top))}"]
    return "\n".join(lines)


def format_digest(events: list[Event], untimed: list[str], day: datetime, title: str) -> str:
    lines = [f"📅 <b>{title}</b> — {day.strftime('%a %d.%m')}", ""]
    if not events and not untimed:
        lines.append("Важных событий не запланировано. Спокойный день 🙂")
    for e in sorted(events, key=lambda x: x.when):
        extra = f" · прогноз {esc(e.forecast)}" if e.forecast else ""
        lines.append(f"{impact.LEVEL_ICON[e.level]} <b>{fmt_time(e.when)}</b> {esc(e.title)} [{e.country}]{extra}")
    if untimed:
        lines += ["", "🇺🇸 <b>Трамп, время не объявлено (TBD):</b>"]
        lines += [f"• {esc(t)}" for t in untimed]
    lines += ["", f"Время: {DISPLAY_TZ.key}. Предупреждение придёт за {LEAD_MINUTES} мин до каждого события."]
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
def send(text: str) -> None:
    if DRY_RUN or not TG_TOKEN:
        plain = re.sub(r"</?[bi]>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        print("=" * 60 + "\n" + plain + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        print(f"[error] telegram: {r.status_code} {r.text}", file=sys.stderr)
        r.raise_for_status()


def handle_commands(state: dict, events: list[Event], now: datetime) -> None:
    """Обрабатывает /today, /week, /next, отправленные боту (ответ с задержкой до 10 мин)."""
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
        cmd = (msg.get("text") or "").split("@")[0].strip().lower()
        local_now = now.astimezone(DISPLAY_TZ)
        if cmd == "/today":
            day = [e for e in events if e.when.astimezone(DISPLAY_TZ).date() == local_now.date()]
            send(format_digest(day, [], local_now, "События сегодня"))
        elif cmd == "/week":
            end = now + timedelta(days=7)
            wk = [e for e in events if now <= e.when <= end and e.level >= 2]
            lines = ["📆 <b>Важные события на 7 дней</b>", ""]
            for e in sorted(wk, key=lambda x: x.when):
                lines.append(f"{impact.LEVEL_ICON[e.level]} {e.when.astimezone(DISPLAY_TZ).strftime('%a %d.%m %H:%M')} "
                             f"{esc(e.title)} [{e.country}]")
            send("\n".join(lines) if wk else "На неделю важных событий не найдено.")
        elif cmd in ("/next", "/start", "/help"):
            nxt = sorted([e for e in events if e.when > now], key=lambda x: x.when)[:5]
            lines = ["🤖 Бот на страже. Команды: /today /week /next", "", "<b>Ближайшие события:</b>"]
            lines += [f"{impact.LEVEL_ICON[e.level]} {e.when.astimezone(DISPLAY_TZ).strftime('%a %H:%M')} {esc(e.title)}"
                      for e in nxt]
            send("\n".join(lines))


# ---------------------------------------------------------------- основной цикл
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=10)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def main() -> None:
    now = datetime.fromisoformat(NOW_OVERRIDE).astimezone(timezone.utc) if NOW_OVERRIDE \
        else datetime.now(timezone.utc)
    state = load_state()
    sent: dict = state.setdefault("sent", {})

    all_events = load_calendar() + load_trump()
    # убираем дубли (FF thisweek/nextweek пересекаются)
    all_events = list({e.uid: e for e in all_events}.values())
    events = [e for e in all_events if e.level >= MIN_LEVEL]
    print(f"[info] {now.isoformat()} — загружено {len(all_events)} событий, из них >= уровня {MIN_LEVEL}: {len(events)}")

    handle_commands(state, all_events, now)

    # --- утренний дайджест
    local_now = now.astimezone(DISPLAY_TZ)
    dkey = f"digest:{local_now.date().isoformat()}"
    if DIGEST_HOUR >= 0 and local_now.hour >= DIGEST_HOUR and dkey not in sent:
        today = [e for e in all_events if e.when.astimezone(DISPLAY_TZ).date() == local_now.date()
                 and e.level >= max(1, MIN_LEVEL - 1)]
        untimed = load_trump_untimed(now.astimezone(ET).date().isoformat())
        send(format_digest(today, untimed, local_now, "План на день"))
        sent[dkey] = now.isoformat()

    # --- алерты: группируем события, выходящие в одну минуту
    def due(kind: str, minutes: int, min_level: int) -> list[Event]:
        return [e for e in events
                if e.level >= min_level and f"{kind}:{e.uid}" not in sent
                and timedelta(0) < (e.when - now) <= timedelta(minutes=minutes)]

    for kind, minutes, min_level in (("lead", LEAD_MINUTES, MIN_LEVEL),
                                     ("final", FINAL_MINUTES, FINAL_MIN_LEVEL)):
        if minutes <= 0:
            continue
        batch = due(kind, minutes, min_level)
        groups: dict[datetime, list[Event]] = {}
        for e in batch:
            groups.setdefault(e.when, []).append(e)
        for when in sorted(groups):
            g = groups[when]
            # если основной алерт не успели отправить, а уже пора финальный — шлём один полный
            if kind == "final" and any(f"lead:{e.uid}" not in sent for e in g):
                continue
            send(format_alert(g, now, final=(kind == "final")))
            for e in g:
                sent[f"{kind}:{e.uid}"] = now.isoformat()
            # если полный алерт отправлен уже внутри окна финального — финальный не нужен
            if kind == "lead" and (when - now) <= timedelta(minutes=FINAL_MINUTES):
                for e in g:
                    sent[f"final:{e.uid}"] = now.isoformat()

    save_state(state, now)


if __name__ == "__main__":
    main()
