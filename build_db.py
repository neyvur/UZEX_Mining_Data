"""Собирает единую SQLite-базу из xlsx-файлов UZEX. Запуск: python build_db.py <папка_с_xlsx> <выход.db>"""
import sys, sqlite3, re, os
import pandas as pd, numpy as np

src = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads"
out = sys.argv[2] if len(sys.argv) > 2 else "uzex.db"
rd = lambda n: pd.read_excel(os.path.join(src, n), sheet_name=0, dtype=object)

def num(s):
    if s.dtype == object:
        s = s.map(lambda v: re.sub(r"[\s\u00a0]", "", v).replace(",", ".") if isinstance(v, str) else v)
    return pd.to_numeric(s, errors="coerce")

def date(s):
    def p(v):
        if v is None or (isinstance(v, float) and np.isnan(v)): return None
        if isinstance(v, str):
            v = v.strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}", v): return v[:10]
            m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", v)
            if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            return None
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    return s.map(p)

parts = []
for y in range(2022, 2027):
    d = rd(f"{y}.xlsx")
    d = d.rename(columns={"Сумма сделки (сум)": "Сумма сделки", "Стартовая цена (сум)": "Стартовая цена"})
    std = [c for c in d.columns if "стандарт" in c.lower()]
    norm = [c for c in d.columns if "(норм.)" in c]
    flags = [c for c in d.columns if c in ("Флаг проверки", "Флаги проверки")]
    o = pd.DataFrame({
        "deal_id": num(d["Номер сделки"]).astype("int64"),
        "deal_date": date(d["Дата сделки"]),
        "deal_time": d["Время сделки"].astype(str).str[:8],
        "trade_type": d["Тип торгов"],
        "quantity": num(d["Количество"]),
        "amount": num(d["Сумма сделки"]),
        "start_price": num(d["Стартовая цена"]),
        "contract_id": num(d["Номер контракта"]).astype("int64"),
        "product": d["Наименование товара"],
        "product_std": d[std[0]] if std else (d[norm[0]] if norm else d["Наименование товара"]),
        "lot": num(d["Лот"]),
        "unit": d["Единица измерения"],
        "region": d["Регион"],
        "contract_type": d["Тип контракта"],
        "contract_form": d["Форма контракта"],
        "status": d["Состояние сделки"],
        "seller_inn": d["ИНН продавца"].astype(str),
        "buyer_inn": d["ИНН покупателя"].astype(str),
        "seller_broker": d["Брокер продавца"],
        "buyer_broker": d["Брокер покупателя"],
        "pay_date": date(d["Дата оплаты"]),
        "delivery_date": date(d["Дата поставки"]),
        "delivered_qty": num(d["Поставлено"]),
        "check_flags": d[flags[0]] if flags else None,
    })
    parts.append(o); print(y, len(o), flush=True)
deals = pd.concat(parts, ignore_index=True)
deals["year"] = deals.deal_date.str[:4].astype(int)
deals["month"] = deals.deal_date.str[:7]
deals["product_std"] = deals.product_std.fillna(deals["product"])

# --- справочники
con_raw = rd("Contracts.xlsx")
ren = {"Номер контракта":"contract_id","Наименование продукта":"product_name","Марка товара":"brand","Размер товара":"size",
 "Сорт товара":"grade","Тип товара":"kind","Класс товара":"class","Код ТН ВЭД":"hs_code","Группа товара":"product_group",
 "Категория товара":"category","Отрасль промышленности":"industry","Единица измерения":"unit","Лот":"lot","Валюта":"currency",
 "Базисная цена":"base_price","Сумма залога":"deposit","Базис поставки":"delivery_basis","Регион":"region","Адрес склада":"warehouse_address",
 "Тип торгов":"trade_type","Тип контракта":"contract_type","Форма контракта":"contract_form","Срок оплаты":"payment_days",
 "Срок поставки":"delivery_days","Упаковка":"packaging","ИНН продавца":"seller_inn","Дата регистрации":"registered_date","Действует до":"valid_until"}
contracts = con_raw.rename(columns=ren)
contracts["contract_id"] = num(contracts.contract_id).astype("int64")
for c in ["registered_date", "valid_until"]: contracts[c] = date(contracts[c])
contracts["base_price"] = num(contracts.base_price)
contracts = contracts.replace({"nan": None}).drop_duplicates("contract_id")
contracts["seller_inn"] = contracts.seller_inn.astype(str)

part = rd("Partacipants.xlsx").rename(columns={"ИНН":"inn","Наименование":"name","Регион":"region","Вид деятельности":"activity","Роль":"role","Плательщик НДС":"vat_payer"})
part["inn"] = part.inn.astype(str)
brk = rd("Broker.xlsx").rename(columns={"Код брокера":"broker_code","Наименование":"name","ИНН":"inn","Регион":"region"})
brk["inn"] = brk.inn.astype(str)
rates = rd("USD-Rates.xlsx").rename(columns={"Дата":"rate_date","Курс":"usd_rate"})
rates["rate_date"] = date(rates.rate_date); rates["usd_rate"] = num(rates.usd_rate)

# --- обогащение сделок
deals = deals.merge(rates, left_on="deal_date", right_on="rate_date", how="left").drop(columns="rate_date")
cinfo = contracts[["contract_id","currency","product_name","category","industry","hs_code","product_group"]]
deals = deals.merge(cinfo, on="contract_id", how="left")
deals["currency"] = deals.currency.map({"Узбекский сум":"UZS","Доллар США":"USD"})
deals["currency"] = deals.currency.fillna(pd.Series(np.where(deals.contract_type=="Экспортный","USD","UZS"), index=deals.index))
deals["amount_uzs"] = np.where(deals.currency=="USD", deals.amount*deals.usd_rate, deals.amount)
deals["amount_usd"] = np.where(deals.currency=="USD", deals.amount, deals.amount/deals.usd_rate)
deals["volume"] = deals.quantity * deals.lot
deals["unit_price"] = deals.amount / deals.volume.replace(0, np.nan)
deals["product_key"] = (deals.product_std.str.lower().str.replace("ё", "е", regex=False)
                        .str.replace("-", " ", regex=False).str.replace(r"\s+", " ", regex=True).str.strip())


HS_FAMILY = {520100:"Хлопок-сырец",252329:"Портландцемент",100199:"Пшеница",271019:"Дизельное топливо",110100:"Мука пшеничная",271012:"Бензин",
 390120:"Полиэтилен",271112:"Сжиженный газ",721420:"Арматура",170199:"Сахар",151229:"Хлопковое масло",310210:"Карбамид",390210:"Полипропилен",
 310230:"Аммиачная селитра",230610:"Шрот хлопковый",271320:"Битум",390410:"Поливинилхлорид",690410:"Кирпич",280700:"Серная кислота",
 151219:"Подсолнечное масло",740311:"Катод медный",440711:"Пиломатериал",100630:"Рис",700529:"Стекло флоат",720853:"Лист стальной горячекатаный",
 520512:"Пряжа хлопчатобумажная",230990:"Комбикорм",250100:"Соль пищевая",252020:"Гипс",721710:"Проволока арматурная",760421:"Алюминиевый профиль",
 281511:"Сода каустическая",854449:"Кабель силовой",320810:"Эмаль",252210:"Известь",280430:"Азот",220190:"Питьевая вода",848210:"Подшипник",
 391721:"Трубы полиэтиленовые",280421:"Аргон"}
deals["product_family"] = pd.to_numeric(deals.hs_code, errors="coerce").map(HS_FAMILY)
deals.loc[deals.product_key.str.contains("мазут", na=False), "product_family"] = "Мазут"
deals["product_family"] = deals.product_family.fillna(deals.product_name)

con = sqlite3.connect(out)
for name, df in [("deals", deals), ("contracts", contracts), ("participants", part), ("brokers", brk), ("usd_rates", rates)]:
    df.to_sql(name, con, if_exists="replace", index=False)
for ix in ["deals(deal_id)","deals(deal_date)","deals(year)","deals(month)","deals(contract_id)","deals(seller_inn)","deals(buyer_inn)",
           "deals(seller_broker)","deals(buyer_broker)","deals(region)","deals(status)","deals(product_std)","deals(product_family)","deals(product_key)",
           "contracts(contract_id)","participants(inn)","brokers(broker_code)","usd_rates(rate_date)"]:
    con.execute(f"CREATE INDEX IF NOT EXISTS ix_{ix.replace('(','_').replace(')','')} ON {ix}")
con.commit(); con.execute("VACUUM"); con.close()
print("OK", len(deals))
