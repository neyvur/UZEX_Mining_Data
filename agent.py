"""UZEX data agent: вопрос (RU/UZ) -> LLM пишет SQL -> выполняем на реальных данных -> ответ только из результатов."""
import os, re, json, time, sqlite3, threading, logging
import requests

log = logging.getLogger("agent")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uzex.db"))
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET", "75"))   # жёсткий лимит комиссии 90 сек
MAX_STEPS = int(os.getenv("MAX_STEPS", "6"))

# ----------------------------------------------------------------------------- БД
_like_cache = {}
def _u_like(pat, s, esc=None):
    if pat is None or s is None: return None
    key = (pat, esc)
    rx = _like_cache.get(key)
    if rx is None:
        out, i = [], 0
        while i < len(pat):
            ch = pat[i]
            if esc and ch == esc and i + 1 < len(pat):
                i += 1; out.append(re.escape(pat[i]))
            elif ch == "%": out.append(".*")
            elif ch == "_": out.append(".")
            else: out.append(re.escape(ch))
            i += 1
        rx = _like_cache[key] = re.compile("^" + "".join(out) + "$", re.I | re.S)
    return 1 if rx.match(str(s)) else 0

def _u_lower(s): return None if s is None else str(s).lower().replace("ё", "е")
def _u_upper(s): return None if s is None else str(s).upper()

_local = threading.local()
def db():
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        con.create_function("like", 2, _u_like, deterministic=True)
        con.create_function("like", 3, _u_like, deterministic=True)
        con.create_function("lower", 1, _u_lower, deterministic=True)
        con.create_function("upper", 1, _u_upper, deterministic=True)
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA cache_size=-200000")
        _local.con = con
    return con

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex|load_extension|truncate)\b", re.I)

def run_sql(sql, max_rows=200, timeout=25):
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.I): raise ValueError("Разрешены только SELECT-запросы")
    if ";" in sql: raise ValueError("Только один запрос")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)): raise ValueError("Запрещённая операция")
    con = db(); t0 = time.time()
    con.set_progress_handler(lambda: 1 if time.time() - t0 > timeout else 0, 20000)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    finally:
        con.set_progress_handler(None, 0)
    truncated = len(rows) > max_rows
    return cols, rows[:max_rows], truncated

def fmt_val(v):
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else f"{v:.6f}".rstrip("0").rstrip(".")
    return "NULL" if v is None else str(v)

def fmt_result(cols, rows, truncated, limit_chars=7000, limit_rows=60):
    if not rows: return "(0 строк)"
    lines = [" | ".join(cols)]
    for r in rows[:limit_rows]: lines.append(" | ".join(fmt_val(v) for v in r))
    txt = "\n".join(lines)
    if len(rows) > limit_rows or truncated: txt += f"\n... (показаны первые {min(len(rows), limit_rows)} строк; результат обрезан)"
    return txt[:limit_chars]

# ----------------------------------------------------------------------------- поиск имён
def _norm(s): return re.sub(r"\s+", " ", re.sub(r"[\"'«»“”‘’`ʻʼ‘]", "", str(s).lower().replace("ё", "е").replace("-", " "))).strip()
def _toks(s): return [t for t in re.split(r"[^\w]+", _norm(s)) if t]

class Catalog:
    def __init__(self):
        con = db()
        self.products = []
        for key, name, n, hs in con.execute("select product_key, min(product_std), count(*), min(product_family) from deals group by product_key"):
            self.products.append((key, name, n, hs, [t[:5] for t in _toks(key)]))
        self.entities = []
        for inn, name, reg, act, role, vat in con.execute("select inn,name,region,activity,role,vat_payer from participants"):
            self.entities.append((_norm(name), f"участник | ИНН {inn} | {name} | {reg} | {act} | {role}"))
        for code, name, inn, reg in con.execute("select broker_code,name,inn,region from brokers"):
            self.entities.append((_norm(name), f"брокер | код {code} | ИНН {inn} | {name} | {reg}"))
        self.meta = {}
        for col, tbl in [("region","deals"),("trade_type","deals"),("status","deals"),("unit","deals"),("contract_form","deals"),
                         ("contract_type","deals"),("category","deals"),("industry","deals"),("product_group","deals"),("currency","deals"),("product_family","deals")]:
            self.meta[col] = [r[0] for r in con.execute(f"select distinct {col} from {tbl} where {col} is not null order by 1")]
        self.meta["activity"] = [r[0] for r in con.execute("select distinct activity from participants order by 1")]
        self.date_range = con.execute("select min(deal_date), max(deal_date), count(*) from deals").fetchone()

    def search_products(self, queries, limit=15):
        qs = queries if isinstance(queries, list) else [queries]
        res = {}
        for q in qs:
            qt = [t[:5] for t in _toks(q)]
            if not qt: continue
            for key, name, n, hs, pt in self.products:
                hit = sum(1 for t in qt if any(p.startswith(t) or t.startswith(p) for p in pt))
                if hit == len(qt) or (len(qt) > 2 and hit >= len(qt) - 1):
                    res[key] = (hit, n, name, hs)
        top = sorted(res.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:limit]
        if not top: return "(ничего не найдено; попробуйте другое слово, синонимы или русское название)"
        return "\n".join(f"product_key='{k}' | пример названия: {v[2]} | сделок: {v[1]} | product_family: {v[3]}" for k, v in top)

    def search_entities(self, queries, limit=15):
        qs = queries if isinstance(queries, list) else [queries]
        out = []
        for q in qs:
            nq = _norm(q)
            if not nq: continue
            for key, line in self.entities:
                if nq in key and line not in out: out.append(line)
        if not out: return "(ничего не найдено)"
        return "\n".join(out[:limit]) + (f"\n... всего найдено {len(out)}" if len(out) > limit else "")

_catalog = None
def catalog():
    global _catalog
    if _catalog is None: _catalog = Catalog()
    return _catalog

# ----------------------------------------------------------------------------- LLM
class LLMError(Exception): pass

def _post(url, headers, payload, timeout):
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200: raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()

def call_gemini(system, messages, timeout, model):
    key = os.getenv("GEMINI_API_KEY")
    if not key: raise LLMError("GEMINI_API_KEY не задан")
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in messages]
    payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
               "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 2048}}
    if "2.5" in model: payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": int(os.getenv("GEMINI_THINKING", "512"))}
    d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
              {"x-goog-api-key": key, "Content-Type": "application/json"}, payload, timeout)
    try: return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    except Exception: raise LLMError(f"Пустой ответ Gemini: {str(d)[:300]}")

def call_groq(system, messages, timeout, model):
    key = os.getenv("GROQ_API_KEY")
    if not key: raise LLMError("GROQ_API_KEY не задан")
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in messages]
    payload = {"model": model, "messages": msgs, "temperature": 0, "max_tokens": 2048, "response_format": {"type": "json_object"}}
    d = _post("https://api.groq.com/openai/v1/chat/completions", {"Authorization": f"Bearer {key}"}, payload, timeout)
    try: return d["choices"][0]["message"]["content"]
    except Exception: raise LLMError(f"Пустой ответ Groq: {str(d)[:300]}")

def providers():
    g = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
    q = [m.strip() for m in os.getenv("GROQ_MODELS", "llama-3.3-70b-versatile,openai/gpt-oss-120b,llama-3.1-8b-instant").split(",") if m.strip()]
    order = os.getenv("PROVIDER_ORDER", "gemini,groq").split(",")
    out = []
    for o in order:
        if o.strip() == "gemini": out += [("gemini", m, call_gemini) for m in g]
        if o.strip() == "groq": out += [("groq", m, call_groq) for m in q]
    return out

_bad = {}   # (provider, model) -> время, до которого не использовать
def llm(system, messages, deadline):
    errs = []
    for name, model, fn in providers():
        if _bad.get((name, model), 0) > time.time(): continue
        left = deadline - time.time()
        if left < 4: break
        try:
            return fn(system, messages, min(left - 1, 40), model), f"{name}:{model}"
        except Exception as e:
            msg = str(e); errs.append(f"{name}:{model}: {msg[:160]}")
            log.warning("LLM fail %s:%s %s", name, model, msg[:200])
            if "HTTP 404" in msg or "HTTP 400" in msg and "model" in msg.lower(): _bad[(name, model)] = time.time() + 3600
            elif "HTTP 429" in msg: _bad[(name, model)] = time.time() + 20
    raise LLMError(" ; ".join(errs) or "нет доступного LLM")

def parse_json(txt):
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try: return json.loads(txt)
    except Exception: pass
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        try: return json.loads(txt[i:j + 1])
        except Exception: pass
    raise ValueError("не JSON")

# ----------------------------------------------------------------------------- промпт
def build_system():
    c = catalog(); m = c.meta
    lo, hi, n = c.date_range
    return f"""Ты — аналитический агент биржи UZEX. Отвечаешь на вопросы о биржевых сделках СТРОГО по данным в базе SQLite. Вопросы приходят на русском или узбекском (латиница или кириллица). Отвечай на языке вопроса.

ЖЁСТКИЕ ПРАВИЛА
1. Любое число, название, код, дату в ответе берёшь ТОЛЬКО из результатов SQL-запросов (RESULT). Ничего не выдумываешь, не прикидываешь «на глаз», не используешь знания из головы о бирже, компаниях, ценах.
2. Если данных для ответа нет (пустой результат, нет такого поля/товара/компании/периода, вопрос не про данные биржи) — ответ: found=false и честное «В данных ответа нет» + коротко почему. Не угадывай.
3. Прежде чем сказать «нет данных», проверь: правильно ли написано название (используй search_products / search_entities), не слишком ли узкий фильтр.
4. Данные покрывают сделки с {lo} по {hi} (всего {n} записей). Вопросы о датах вне этого периода — «нет данных».
5. Если вопрос неоднозначен (например, не указан статус), по умолчанию считай ВСЕ записи реестра и явно напиши это допущение в ответе одной фразой. «Исполненные/успешные» = status LIKE 'Исполнен%' (включая частично); «полностью исполненные» = status='Исполнен'.
6. Для вычислений используй SQL (SUM/COUNT/AVG/ORDER BY…), а не свою арифметику. Для «топ N» бери LIMIT N и при равенстве учитывай все. Числа в ответе пиши точно как в результате (допустимы пробелы как разделители тысяч); не округляй, если не просят — иначе скажи, что округлил.
7. Ответ короткий и по делу: сначала прямой ответ (число/название/код), затем 1 строка с пояснением основы (какая таблица, фильтр, период, валюта/единица).

ФОРМАТ ТВОЕГО ВЫВОДА — всегда один JSON-объект, без текста вокруг:
 {{"action":"sql","sql":"SELECT ..."}}                  — выполнить запрос (только SELECT/WITH, один запрос)
 {{"action":"search_products","queries":["хлопок","paxta"]}}   — найти варианты названий товара (ищи по-русски; переводи с узбекского/английского)
 {{"action":"search_entities","queries":["Baba Group"]}}       — найти компанию-участника или брокера по названию -> ИНН/код
 {{"action":"final","found":true,"answer":"текст ответа"}}   — финальный ответ (found=false если данных нет)
 {{"action":"smalltalk","answer":"..."}}                       — только для приветствий/вопросов «что ты умеешь»
После каждого шага получишь RESULT. Можно делать до {MAX_STEPS-1} шагов; будь экономным.

БАЗА (SQLite). Таблицы:
deals — 1 строка = 1 сделка (реестр spot.uzex.uz)
  deal_id (номер сделки, int), deal_date (текст 'YYYY-MM-DD'), deal_time ('HH:MM:SS'), year (int), month ('YYYY-MM'),
  trade_type (тип торгов), quantity (количество, как в реестре, в лотах), lot (размер лота в единицах unit), volume (= quantity*lot, физический объём в unit),
  unit (единица измерения), amount (сумма сделки В ВАЛЮТЕ контракта), currency ('UZS' или 'USD'), amount_uzs (сумма в сумах: для USD пересчитана по курсу дня), amount_usd (сумма в долларах),
  usd_rate (курс сум за 1 USD на дату сделки), start_price (стартовая цена за единицу, в валюте контракта), unit_price (= amount/volume),
  contract_id (номер контракта -> contracts), product (название в реестре, регистр/написание «грязные»), product_std (стандартизованное название), product_key (product_std в нижнем регистре, '-'→' '; детальный вариант написания, ~430 значений), product_family (УКРУПНЁННЫЙ товар, 41 значение: Хлопок-сырец, Пшеница, Бензин, Портландцемент… — ИСПОЛЬЗУЙ для вопросов «какой товар / по товарам / по хлопку»; для конкретной марки/сорта — product_key LIKE),
  product_name/category/industry/hs_code/product_group (из карточки контракта; hs_code = код ТН ВЭД, надёжный признак товара),
  region (регион, латиницей по-узбекски), contract_type (Внутренний/Экспортный), contract_form (Спот/Форвард/Фьючерс), status (состояние сделки),
  seller_inn, buyer_inn, seller_broker, buyer_broker (код брокера BKxxx), pay_date, delivery_date (ISO-текст или NULL), delivered_qty (поставлено, того же масштаба, что quantity),
  check_flags (служебные пометки качества данных, обычно игнорируй)
contracts — карточки контрактов: contract_id, product_name, brand, size, grade, kind, class, hs_code, product_group, category, industry, unit, lot, currency, base_price, deposit, delivery_basis, region (по-русски), warehouse_address, trade_type, contract_type, contract_form, payment_days, delivery_days, packaging, seller_inn, registered_date, valid_until
participants — inn, name, region, activity (вид деятельности), role (Покупатель/Продавец), vat_payer (Да/Нет)
brokers — broker_code, name, inn, region
usd_rates — rate_date, usd_rate
Связи: deals.seller_inn/buyer_inn -> participants.inn; deals.seller_broker/buyer_broker -> brokers.broker_code; deals.contract_id -> contracts.contract_id.

ЗНАЧЕНИЯ СПРАВОЧНИКОВ (точные, используй именно их):
 region в deals: {m['region']}
  (Ташкент город=Toshkent shahri; Ташкентская обл.=Toshkent viloyati; Самарканд=Samarqand; Бухара=Buxoro; Фергана=Farg‘ona; Наманган=Namangan; Андижан=Andijon; Кашкадарья=Qashqadaryo; Сурхандарья=Surxondaryo; Хорезм=Xorazm; Навои=Navoiy; Джизак=Jizzax; Сырдарья=Sirdaryo; Каракалпакстан=Qoraqalpog‘iston Respublikasi. Апостроф в Farg‘ona и Qoraqalpog‘iston — символ ‘ (U+2018): ищи через LIKE '%arg%ona%' / '%Qoraqalpog%'.)
 trade_type: {m['trade_type']}  (Английский=English auction/ingliz auksioni, Двусторонний=ikki tomonlama, Обратный=teskari)
 status: {m['status']}
 contract_form: {m['contract_form']}  contract_type: {m['contract_type']}  unit: {m['unit']}
 category: {m['category']}
 industry: {m['industry']}
 product_group: {m['product_group']}   activity участников: {m['activity']}
 product_family (все значения): {m['product_family']}

ПОДСКАЗКИ ПО SQL (SQLite)
 • LIKE и lower() работают без учёта регистра и для кириллицы. Товар: для укрупнённого — product_family = 'Хлопок-сырец'; для конкретного сорта/марки — product_key LIKE '%цем ii/а%' (сначала search_products, чтобы увидеть реальные варианты написания). В ответе называй, по какому признаку сгруппировано.
 • Даты — текст ISO: deal_date BETWEEN '2024-01-01' AND '2024-12-31', либо year=2024, month='2024-03'. День недели: strftime('%w', deal_date).
 • Суммы по разным валютам складывай через amount_uzs (или amount_usd). Сумма для USD-контрактов в amount — доллары, не сумы. Всегда указывай валюту в ответе.
 • Цена за единицу: unit_price (в валюте контракта). Средневзвешенная = SUM(amount)/SUM(volume) в рамках одной единицы измерения и одной валюты; не смешивай тонны и килограммы.
 • Медианы/процентили: ORDER BY + LIMIT 1 OFFSET (n-1)/2.
 • Имена компаний в базе — вымышленные, на латинице («"Baba Group" MChJ»); ищи через search_entities, потом фильтруй по ИНН.
 • Покупатель/продавец: seller_inn/buyer_inn; брокеры: seller_broker/buyer_broker.
 • Смысл статусов (из README биржи): 'Аннулирован(Не оплачен)' — не оплачен в срок, сделка НЕ состоялась. 'Исполнен' — оплачена и поставлена полностью; 'Исполнен(Частично)' — поставлена часть (сколько лотов — delivered_qty); 'Оплачен' — оплачена, ещё не поставлена; 'Ждёт оплату' — спот подписан, срок оплаты не истёк; 'Заключен' — форвард/фьючерс подписан, срок оплаты не истёк; 'Не подписан' — договор не подписан; 'Новый' — заключена 2026-10-30 (последний день выгрузки). Состояния даны на конец дня 2026-10-30. Пустые pay_date/delivery_date/delivered_qty = этого ещё не было (или сделка аннулирована).
 • quantity = число лотов; объём в единицах unit = quantity*lot (колонка volume). delivered_qty тоже в лотах. Цена за единицу = amount/volume.
 • Сроки: срок оплаты (contracts.payment_days) — календарные дни от deal_date; срок поставки (contracts.delivery_days) — дни от pay_date. Реальная задержка = julianday(pay_date)-julianday(deal_date) и т.п.
 • Карточку контракта заполняет продавец, поэтому один товар может быть записан по-разному (регистр, «1 сорт»/«1-сорта») — для вопросов про товар используй product_family или search_products.
 • Возвращай в SELECT человекочитаемые поля (название, код/ИНН), а не только числа.
"""

NUM_RX = re.compile(r"\d[\d\s\u00a0.,]*\d|\d")
def _digits(s): return re.sub(r"\D", "", s)

def unsupported_numbers(answer, evidence, question):
    """Числа (>=5 значащих цифр) в ответе, которых нет ни в результатах запросов, ни в вопросе."""
    ev = _digits_blob(evidence + " " + question)
    bad = []
    for m in NUM_RX.finditer(answer):
        d = _digits(m.group())
        if len(d.lstrip("0")) >= 5 and d not in ev and d.lstrip("0") not in ev:
            bad.append(m.group().strip())
    return bad

def _digits_blob(text):
    # все числовые токены из текста, цифры слитно (для сравнения «1 234 567» и «1234567»); дроби с запятой/точкой тоже
    toks = set()
    for m in NUM_RX.finditer(text):
        d = _digits(m.group()); toks.add(d); toks.add(d.lstrip("0"))
        # округлённые варианты: целая часть дроби
        mm = re.split(r"[.,]", m.group().replace(" ", ""))
        if len(mm) == 2: toks.add(_digits(mm[0]))
    return _BlobSet(toks, text)

class _BlobSet:
    def __init__(self, toks, text): self.toks, self.text = toks, _digits(text)
    def __contains__(self, d): return d in self.toks

# ----------------------------------------------------------------------------- локализация коротких сообщений
def detect_lang(q):
    ql = q.lower()
    if re.search(r"[қўғҳ]", ql): return "uz"
    if re.search(r"[а-яё]", ql): return "ru"
    if re.search(r"\b(qancha|nechta|nima|qaysi|necha|savdo|bitim|bitimlar|shartnoma|narx|viloyat|eng|yil|oy|kim|qachon|uchun|va|bo['‘’ʻ]?ldi|mahsulot)\b|['‘’ʻ]", ql): return "uz"
    return "ru"

MSG = {
    "ru": {
        "nodata": "В реестре нет данных для ответа.",
        "fail": "Не удалось обработать запрос. Попробуйте ещё раз через минуту.",
        "timeout": "Запрос занял слишком много времени. Попробуйте задать вопрос короче.",
        "basis": "Основа ответа",

        "start": (
            "👋 Здравствуйте! Я — UZEX Navigator.\n\n"
            "Анализирую сделки UZEX за 2022 — октябрь 2026 "
            "на русском и узбекском языках. Только реальные данные, без догадок.\n\n"
            "📌 Примеры вопросов:\n"
            "• Сколько сделок было в 2025 году?\n"
            "• Топ-5 брокеров по сумме сделок.\n"
            "• Какой товар продавался чаще всего?"
        )
    },

    "uz": {
        "nodata": "Reyestrda javob uchun ma’lumot topilmadi.",
        "fail": "So‘rovni bajarib bo‘lmadi. Bir daqiqadan so‘ng qayta urinib ko‘ring.",
        "timeout": "So‘rov juda ko‘p vaqt oldi. Savolni qisqaroq yozib ko‘ring.",
        "basis": "Javob asosi",

        "start": (
            "👋 Assalomu alaykum! Men — UZEX Navigator.\n\n"
            "UZEX bitimlarini 2022 — 2026-yil oktabrgacha "
            "rus va o‘zbek tillarida tahlil qilaman. Faqat aniq ma’lumotlar, taxminlarsiz.\n\n"
            "📌 Masalan:\n"
            "• 2025-yilda nechta bitim tuzilgan?\n"
            "• Bitimlar summasi bo‘yicha eng yirik 5 broker.\n"
            "• Qaysi mahsulot eng ko‘p sotilgan?"
        )
    }
}

# ----------------------------------------------------------------------------- агент
def ask(question, history=None):
    """Возвращает dict(answer, found, sql, steps, model, elapsed)."""
    t0 = time.time(); deadline = t0 + TOTAL_BUDGET
    lang = detect_lang(question); T = MSG[lang]
    system = build_system()
    msgs = [{"role": "user", "content": f"ВОПРОС: {question}"}]
    evidence, sqls, steps, model_used = "", [], 0, None
    retried_guard = 0
    try:
        while steps < MAX_STEPS:
            steps += 1
            if steps == MAX_STEPS: msgs.append({"role": "user", "content": "Это последний шаг: дай action=final по тому, что уже получено (если данных недостаточно — found=false)."})
            raw, model_used = llm(system, msgs, deadline)
            try: obj = parse_json(raw)
            except Exception:
                msgs += [{"role": "assistant", "content": raw[:1500]}, {"role": "user", "content": "Ответ должен быть ОДНИМ JSON-объектом по формату. Повтори."}]
                continue
            act = obj.get("action")
            msgs.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})
            if act == "sql":
                sql = str(obj.get("sql", ""))
                try:
                    cols, rows, trunc = run_sql(sql); res = fmt_result(cols, rows, trunc); sqls.append(sql)
                    evidence += "\n" + res
                    note = "" if rows else "\nПустой результат: проверь фильтры/написание (search_products/search_entities) прежде чем сказать, что данных нет."
                    msgs.append({"role": "user", "content": f"RESULT ({len(rows)} строк):\n{res}{note}"})
                except Exception as e:
                    msgs.append({"role": "user", "content": f"SQL ERROR: {e}. Исправь запрос."})
            elif act == "search_products":
                res = catalog().search_products(obj.get("queries") or obj.get("query") or []); evidence += "\n" + res
                msgs.append({"role": "user", "content": f"RESULT:\n{res}"})
            elif act == "search_entities":
                res = catalog().search_entities(obj.get("queries") or obj.get("query") or []); evidence += "\n" + res
                msgs.append({"role": "user", "content": f"RESULT:\n{res}"})
            elif act == "smalltalk" and not sqls:
                return dict(answer=str(obj.get("answer") or T["start"]), found=None, sql=[], steps=steps, model=model_used, elapsed=time.time() - t0)
            elif act in ("final", "smalltalk"):
                found = obj.get("found", True); ans = str(obj.get("answer", "")).strip()
                if found and not sqls:
                    msgs.append({"role": "user", "content": "Ты не выполнял SQL. Ответ без данных запрещён: выполни запрос или поставь found=false."}); continue
                bad = unsupported_numbers(ans, evidence, question) if found else []
                if bad and retried_guard < 2:
                    retried_guard += 1
                    msgs.append({"role": "user", "content": f"Проверка: числа {bad[:5]} из ответа не найдены в результатах запросов. Используй только числа из RESULT (точно как есть) или выполни недостающий запрос, затем снова action=final."}); continue
                if bad: found, ans = False, T["nodata"]
                if not found and not ans: ans = T["nodata"]
                return dict(answer=ans, found=bool(found), sql=sqls, steps=steps, model=model_used, elapsed=time.time() - t0)
            else:
                msgs.append({"role": "user", "content": "Неизвестный action. Используй sql / search_products / search_entities / final."})
            if time.time() > deadline - 3: break
        return dict(answer=T["timeout"], found=None, sql=sqls, steps=steps, model=model_used, elapsed=time.time() - t0, error="budget")
    except LLMError as e:
        log.error("LLM error: %s", e)
        return dict(answer=T["fail"], found=None, sql=sqls, steps=steps, model=model_used, elapsed=time.time() - t0, error=str(e))
