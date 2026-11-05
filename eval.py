"""Самопроверка агента: вопросы RU/UZ + эталонный SQL. Запуск: python eval.py   (нужен интернет и ключи в .env)"""
import time
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass
import agent

CASES = [  # (вопрос, эталонный SQL -> первая ячейка первой строки, должен ли быть ответ в данных)
 ("Сколько всего сделок в реестре?", "select count(*) from deals", True),
 ("Сколько сделок было заключено в марте 2024 года?", "select count(*) from deals where month='2024-03'", True),
 ("2025-yilda nechta bitim tuzilgan?", "select count(*) from deals where year=2025", True),
 ("Какой товар (product_family) по числу сделок самый популярный за 2023 год?", "select product_family from deals where year=2023 group by 1 order by count(*) desc limit 1", True),
 ("Какой регион лидирует по сумме сделок в сумах за 2024 год?", "select region from deals where year=2024 group by 1 order by sum(amount_uzs) desc limit 1", True),
 ("Сколько сделок аннулировано в 2022 году?", "select count(*) from deals where year=2022 and status like 'Аннулирован%'", True),
 ("Какой брокер продавца провёл больше всего сделок в 2025 году? Назовите код.", "select seller_broker from deals where year=2025 group by 1 order by count(*) desc limit 1", True),
 ("Сколько экспортных сделок в 2026 году?", "select count(*) from deals where year=2026 and contract_type='Экспортный'", True),
 ("Курс доллара на 2024-05-15?", "select usd_rate from usd_rates where rate_date='2024-05-15'", True),
 ("Сколько участников торгов — продавцов?", "select count(*) from participants where role='Продавец'", True),
 ("Какая сделка №100001: товар и сумма?", "select amount from deals where deal_id=100001", True),
 ("Какая выручка у компании Nonexistent Corp в 2024?", None, False),
 ("Сколько сделок было в 2019 году?", None, False),
 ("Kim UZEX birjasining direktori?", None, False),
 ("Какая будет цена на хлопок завтра?", None, False),
]
if __name__ == "__main__":
    ok = 0
    for q, sql, has in CASES:
        r = agent.ask(q); ans = r["answer"]
        if has:
            exp = agent.fmt_val(agent.run_sql(sql)[1][0][0]); dig = lambda s: "".join(ch for ch in s if ch.isalnum()).lower()
            good = r.get("found") and dig(exp) in dig(ans) or (r.get("found") and exp.lower() in ans.lower())
        else:
            exp = "нет данных"; good = r.get("found") is False
        ok += bool(good)
        print(("PASS" if good else "FAIL"), f"{r['elapsed']:.1f}s", q, "\n    ожидалось:", exp, "\n    ответ:", ans.replace("\n", " ")[:220])
    print(f"\n{ok}/{len(CASES)}")
