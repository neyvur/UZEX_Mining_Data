"""Telegram-бот + веб-интерфейс UZEX Data Agent. Запуск: python bot.py"""
import os, sys, json, time, html, logging, threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception: pass
import agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
pool = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "6")))
sess = requests.Session()

def tg(method, **kw):
    for attempt in range(3):
        try:
            r = sess.post(f"{API}/{method}", json=kw, timeout=kw.get("timeout", 10) + 15)
            d = r.json()
            if d.get("ok"): return d["result"]
            if d.get("error_code") == 429: time.sleep(d.get("parameters", {}).get("retry_after", 2)); continue
            log.warning("tg %s: %s", method, d); return None
        except Exception as e:
            log.warning("tg %s error: %s", method, e); time.sleep(1)
    return None

def render(res):
    """HTML-текст ответа + раскрывающийся блок с основанием (SQL)."""
    text = html.escape(res["answer"])
    if res.get("sql"):
        sql = html.escape(res["sql"][-1].strip())
        basis = agent.MSG[agent.detect_lang(res["answer"] or "ru")]["basis"]
        extra = f"\n\n<blockquote expandable>📎 {basis} (SQL, {len(res['sql'])} запрос.):\n{sql[:1500]}</blockquote>"
        if len(text) + len(extra) < 3900: text += extra
    return text[:4000]

def typing_loop(chat_id, stop):
    while not stop.is_set():
        tg("sendChatAction", chat_id=chat_id, action="typing", timeout=5); stop.wait(4)

def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=agent.MSG["ru"]["start"] + "\n\n" + agent.MSG["uz"]["start"]
        )
        return

    if text.startswith("/diag"):
        tg("sendMessage", chat_id=chat_id, text=diag())
        return

    # Показываем пользователю, что бот начал работу
    progress = tg(
        "sendMessage",
        chat_id=chat_id,
        text="⏳ Анализирую ваш запрос...\nПроверяю данные UZEX. Пожалуйста, подождите."
    )

    stop = threading.Event()
    threading.Thread(
        target=typing_loop,
        args=(chat_id, stop),
        daemon=True
    ).start()

    try:
        res = agent.ask(text)

        log.info(
            "Q=%r -> %.1fs steps=%s model=%s found=%s",
            text[:120],
            res["elapsed"],
            res["steps"],
            res.get("model"),
            res.get("found")
        )

    except Exception:
        log.exception("ask failed")
        res = {
            "answer": agent.MSG[agent.detect_lang(text)]["fail"],
            "sql": []
        }

    finally:
        stop.set()

    # Превращаем сообщение загрузки в готовый ответ
    if progress:
        edited = tg(
            "editMessageText",
            chat_id=chat_id,
            message_id=progress["message_id"],
            text=render(res),
            parse_mode="HTML"
        )

        if edited is None:
            tg(
                "deleteMessage",
                chat_id=chat_id,
                message_id=progress["message_id"]
            )
            tg(
                "sendMessage",
                chat_id=chat_id,
                text=res["answer"][:4000],
                reply_to_message_id=msg.get("message_id")
            )
    else:
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=render(res),
            parse_mode="HTML",
            reply_to_message_id=msg.get("message_id")
        )

def diag():
    lines = []
    for name, model, fn in agent.providers()[:6]:
        t = time.time()
        try:
            fn('Верни JSON {"ok":true}', [{"role": "user", "content": "ping"}], 20, model); lines.append(f"OK   {name}:{model} {time.time()-t:.1f}s")
        except Exception as e: lines.append(f"FAIL {name}:{model} {str(e)[:120]}")
    c = agent.catalog(); lines.append(f"DB: {c.date_range[2]} сделок, {c.date_range[0]} … {c.date_range[1]}")
    return "\n".join(lines)

def poll():
    me = tg("getMe"); log.info("Бот: %s", me and me.get("username"))
    tg("deleteWebhook", drop_pending_updates=False)
    offset = 0
    while True:
        ups = tg("getUpdates", offset=offset, timeout=40, allowed_updates=["message"])
        if ups is None: time.sleep(3); continue
        for u in ups:
            offset = u["update_id"] + 1
            if "message" in u: pool.submit(handle, u["message"])

# ------------------------------------------------------------------ веб-интерфейс (запасной канал)
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>UZEX Data Agent</title>
<style>body{font-family:system-ui;max-width:760px;margin:30px auto;padding:0 14px}textarea{width:100%;height:80px;font-size:16px;padding:8px}button{font-size:16px;padding:8px 18px;margin-top:8px}
pre{background:#f4f4f4;padding:10px;white-space:pre-wrap;overflow:auto}#a{white-space:pre-wrap;font-size:18px;margin-top:18px}</style>
<h2>UZEX Data Agent</h2><p>Вопросы о сделках биржи (RU / UZ). Если в данных ответа нет — агент так и скажет.</p>
<textarea id=q placeholder="Savol / Вопрос…"></textarea><br><button onclick=go()>Спросить / So‘rash</button><div id=a></div><details><summary>SQL</summary><pre id=s></pre></details>
<script>async function go(){a.textContent='…';s.textContent='';let r=await fetch('/ask',{method:'POST',body:JSON.stringify({q:q.value})});let d=await r.json();a.textContent=d.answer;s.textContent=(d.sql||[]).join('\\n\\n')}</script>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/health"): self._send(200, "ok", "text/plain")
        else: self._send(200, PAGE, "text/html")
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0)); q = json.loads(self.rfile.read(n) or b"{}").get("q", "").strip()
            res = agent.ask(q) if q else {"answer": "", "sql": []}
            self._send(200, json.dumps({"answer": res["answer"], "sql": res.get("sql", []), "found": res.get("found")}, ensure_ascii=False))
        except Exception as e: self._send(500, json.dumps({"answer": str(e)}))

def serve_web(port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), H); log.info("web on :%s", port); srv.serve_forever()

if __name__ == "__main__":
    if not os.path.exists(agent.DB_PATH) and os.path.exists(agent.DB_PATH + ".gz"):
        import gzip, shutil
        log.info("Распаковываю %s.gz …", agent.DB_PATH)
        with gzip.open(agent.DB_PATH + ".gz", "rb") as f, open(agent.DB_PATH, "wb") as o: shutil.copyfileobj(f, o)
    if not os.path.exists(agent.DB_PATH):
        log.error("Нет базы %s — соберите: python build_db.py <папка_xlsx> uzex.db (или см. README)", agent.DB_PATH); sys.exit(1)
    agent.catalog()
    port = int(os.getenv("PORT", "0"))
    if port: threading.Thread(target=serve_web, args=(port,), daemon=True).start()
    if TOKEN: poll()
    elif port: threading.Event().wait()
    else: log.error("Задайте TELEGRAM_TOKEN"); sys.exit(1)
