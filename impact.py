"""
Правила оценки импакта событий на рынки.

Каждое правило: регулярка по названию события -> уровень, какие рынки задевает,
и короткая подсказка для риск-менеджера. Правила проверяются сверху вниз,
срабатывает первое совпавшее. Редактируйте под свою книгу.
"""
import re

# Уровни: 3 = критический, 2 = высокий, 1 = средний
LEVEL_ICON = {3: "🔴🔴", 2: "🔴", 1: "🟠"}
LEVEL_NAME = {3: "КРИТИЧЕСКИЙ", 2: "ВЫСОКИЙ", 1: "СРЕДНИЙ"}

# Активы, на которые обычно влияет макро конкретной валюты/региона
CCY_ASSETS = {
    "USD": "DXY, EUR/USD, USD/JPY, золото, S&P 500 / Nasdaq, BTC/ETH",
    "EUR": "EUR/USD, EUR/GBP, EUR/JPY, DAX / Euro Stoxx",
    "GBP": "GBP/USD, EUR/GBP, FTSE 100",
    "JPY": "USD/JPY, кроссы JPY, Nikkei",
    "CNY": "USD/CNH, AUD/USD, медь, железная руда, Hang Seng",
    "CAD": "USD/CAD, нефть",
    "AUD": "AUD/USD, медь, железная руда",
    "NZD": "NZD/USD",
    "CHF": "USD/CHF, EUR/CHF, золото",
    "All": "Нефть (Brent/WTI), CAD, NOK, энергетический сектор",
}

RULES = [
    # --- Центробанки ---
    (r"FOMC Statement|Federal Funds Rate|FOMC Press Conference", 3,
     "Решение ФРС по ставке",
     "Самое волатильное событие месяца для USD, индексов и крипты. Спреды расширяются, возможны гэпы и проскальзывания. Плечи — к минимуму за 30–60 мин."),
    (r"FOMC (Meeting )?Minutes", 2, "Протокол ФРС",
     "Движение обычно меньше, чем на решении, но резкий импульс в первые минуты возможен."),
    (r"Fed Chair|Powell|Warsh", 2, "Выступление главы ФРС",
     "Любой намёк на траекторию ставок двигает USD, облигации, индексы и BTC. Особенно опасна Q&A-часть."),
    (r"Main Refinancing Rate|ECB Press Conference|Monetary Policy Statement.*EUR", 3,
     "Решение ЕЦБ", "Основной драйвер EUR. Пресс-конференция через 45 мин после решения — вторая волна."),
    (r"Official Bank Rate|MPC Official Bank Rate|BOE", 3, "Решение Банка Англии",
     "Сильные движения GBP, особенно при расколе голосов MPC."),
    (r"BOJ Policy Rate|BOJ Press Conference|Monetary Policy Statement.*JPY", 3,
     "Решение Банка Японии", "Риск резких движений JPY и carry-trade разворотов, задевает глобальные индексы и крипту."),
    (r"Cash Rate|Overnight Rate|Policy Rate|Interest Rate", 2, "Решение центробанка",
     "Ожидайте всплеск волатильности в валюте страны."),
    # --- Ключевая макростатистика США ---
    (r"Non-Farm Employment Change|Nonfarm Payrolls|Unemployment Rate", 3,
     "Рынок труда (NFP)", "Одно из самых волатильных событий: USD, золото, индексы и BTC часто дают 1–2% за минуты."),
    (r"\bCPI\b|Consumer Price Index", 3, "Инфляция (CPI)",
     "Определяет ожидания по ставке ФРС. Сильный сюрприз = резкое движение по всем рисковым активам."),
    (r"Core PCE|PCE Price Index", 2, "Инфляция PCE",
     "Предпочитаемый ФРС индикатор инфляции."),
    (r"\bPPI\b", 2, "Цены производителей (PPI)", "Опережающий индикатор инфляции."),
    (r"Advance GDP|Prelim GDP|GDP q/q|GDP", 2, "ВВП", "Сюрприз по росту экономики двигает валюту и индексы."),
    (r"Retail Sales", 2, "Розничные продажи", "Ключевой индикатор потребления."),
    (r"ISM (Manufacturing|Services)|PMI", 1, "Индексы деловой активности (PMI/ISM)",
     "Умеренная волатильность, сильнее при пересечении отметки 50."),
    (r"JOLTS|ADP|Unemployment Claims", 1, "Данные по рынку труда", "Средний импакт, задаёт настрой перед NFP."),
    # --- Сырьё ---
    (r"OPEC|JMMC", 2, "Встреча OPEC+", "Решения по добыче двигают нефть на 3–8%, тянут CAD/NOK и энергетику."),
    (r"Crude Oil Inventories", 1, "Запасы нефти EIA", "Краткосрочная волатильность нефти."),
    # --- Политика / прочее ---
    (r"Election|Referendum|Vote", 2, "Выборы / голосование", "Возможны гэпы на открытии и резкие движения валюты."),
    (r"Speaks|Testifies|Speech", 1, "Выступление официального лица", "Риск неожиданных заявлений."),
]

# Расписание Трампа: что считаем рыночно значимым
TRUMP_RULES = [
    (r"address(es)? (to )?the nation|oval office address|state of the union|joint session", 3,
     "Обращение к нации",
     "Максимальный риск: возможны заявления по тарифам, санкциям, войне, ФРС. USD, индексы, нефть, BTC."),
    (r"press conference|press briefing|news conference|press gaggle|takes questions", 3,
     "Пресс-конференция",
     "Q&A непредсказуемы — частый источник заявлений про тарифы, ФРС, Китай, крипту."),
    (r"tariff|trade|china|xi jinping|fed|federal reserve|economy|economic|crypto|bitcoin|oil|energy|sanction|ukraine|russia|putin|iran|israel|deal",
     3, "Выступление на рыночную тему",
     "Тема напрямую связана с рынками — высокий риск резких движений."),
    (r"rally|delivers remarks|gives remarks|keynote|speech|participates in a fireside", 2,
     "Публичное выступление",
     "Трамп часто отходит от текста — возможны внезапные заявления по тарифам/ФРС."),
    (r"signs|executive order|announcement|announces", 2, "Подписание указов / анонс",
     "Указы могут вводить тарифы, санкции, регуляцию — проверьте тему."),
    (r"bilateral|meets with|meeting with (the )?(president|prime minister|chancellor)|summit|g7|g20|nato|united nations", 2,
     "Международная встреча / саммит",
     "Возможны заявления по торговле, санкциям, геополитике."),
]

TRUMP_EXCLUDE = re.compile(
    r"executive time|departs|arrives|en route|lunch|dinner|intelligence briefing|policy time|"
    r"no public events|golf|photo|church|signs? (bills?|proclamation) honoring|closed press$",
    re.I,
)

TRUMP_ASSETS = "S&P 500 / Nasdaq, DXY и мажоры, USD/CNH, нефть, золото, BTC/ETH"


def classify_calendar(title: str, country: str, ff_impact: str):
    """Возвращает (level, label, note) или None, если событие не важно."""
    for pattern, level, label, note in RULES:
        if re.search(pattern, title, re.I):
            # Понижаем уровень для второстепенных стран, если FF не дал High
            if ff_impact != "High" and level > 1:
                level -= 1
            return level, label, note
    if ff_impact == "High":
        return 2, "Важное событие", "ForexFactory помечает как высокий импакт."
    return None


def classify_trump(details: str, coverage: str | None, sensitive: bool = True):
    """sensitive=True: любое публичное событие с прессой считается важным,
    потому что Трамп часто отвечает на вопросы журналистов на любых мероприятиях."""
    text = re.sub(r"^TBD:\s*", "", details or "")
    if TRUMP_EXCLUDE.search(text):
        return None
    cov = (coverage or "").strip().lower()
    if cov == "closed press":
        return None
    for pattern, level, label, note in TRUMP_RULES:
        if re.search(pattern, text, re.I):
            return level, label, note
    press = any(w in cov for w in ("open", "pool", "spray", "credentialed", "travel"))
    if sensitive and press:
        return 2, "Публичное мероприятие с прессой", (
            "Журналисты рядом — Трамп часто отвечает на вопросы (тарифы, ФРС, Китай) "
            "даже на мероприятиях на постороннюю тему.")
    if press or not cov:
        return 1, "Публичное мероприятие", "Возможны незапланированные заявления."
    return None


def risk_advice(level: int) -> str:
    if level >= 3:
        return ("Снизить плечи до минимума / закрыть внутридневные позиции, "
                "убрать рыночные стопы рядом с ценой, проверить маржу на гэп.")
    if level == 2:
        return "Снизить плечи по затронутым инструментам, расширить стопы или уменьшить объём."
    return "Следить за позициями в затронутых инструментах."
