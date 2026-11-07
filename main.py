"""Штаб: сервер мини-приложения и Telegram-бота.

Один процесс: API для мини-приложения (FastAPI), Telegram-бот (aiogram, long polling),
база SQLite, агенты через GigaChat, распознавание голоса через Vosk (офлайн, на сервере).
Настройки читаются из файла .env (см. install.sh).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger("shtab")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = Path(__file__).resolve().parent.parent


def _env(name, default=""):
    return os.environ.get(name, default).strip()


BOT_TOKEN = _env("BOT_TOKEN")
OWNER_ID = _env("OWNER_ID")
GIGA_KEY = _env("GIGA_KEY")
GIGA_SCOPE = _env("GIGA_SCOPE", "GIGACHAT_API_PERS")
GIGA_MODEL = _env("GIGA_MODEL", "GigaChat-2-Pro")
GIGA_CA = _env("GIGA_CA", str(BASE / "russian_trusted_root_ca.pem"))
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")
DB_PATH = _env("DB_PATH", str(BASE / "data" / "shtab.db"))
VOSK_MODEL = _env("VOSK_MODEL", str(BASE / "vosk-model"))
WEB_DIR = BASE / "web"

REC = ("contact", "partner", "log")              # записи базы, которые могут быть общими
PERSONAL = ("task", "plan", "settings", "chat")  # личное, видит только автор
KINDS = REC + PERSONAL
SHARE = {1: ("partner",), 2: (), 3: REC}
NICHES = ["Редкие товары из-за границы", "Ремонт и интерьер", "Медицина", "Авто", "Юристы и документы",
          "Недвижимость", "Красота и здоровье", "Образование", "Другое"]

# ---------------------------------------------------------------- база данных
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL")
_db.executescript("""
CREATE TABLE IF NOT EXISTS items(author TEXT, id TEXT, kind TEXT, data TEXT, updated REAL, PRIMARY KEY(author, id));
CREATE TABLE IF NOT EXISTS members(tg_id TEXT PRIMARY KEY, name TEXT, joined REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
""")
_lock = threading.Lock()


def q(sql, args=(), one=False):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchall()
        _db.commit()
    return (rows[0] if rows else None) if one else rows


def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return json.loads(r[0]) if r else default


def kv_set(k, v):
    q("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def mode():
    m = kv_get("mode", 1)
    return m if m in (1, 2, 3) else 1


def is_member(uid):
    return uid == OWNER_ID or q("SELECT 1 FROM members WHERE tg_id=?", (uid,), one=True) is not None


def add_member(uid, name):
    q("INSERT INTO members(tg_id, name, joined) VALUES(?, ?, ?) ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
      (uid, name, time.time()))


def members():
    rows = q("SELECT tg_id, name FROM members ORDER BY joined")
    out = [{"id": r[0], "name": r[1] or "Друг"} for r in rows]
    if OWNER_ID and not any(m["id"] == OWNER_ID for m in out):
        out.insert(0, {"id": OWNER_ID, "name": "Владелец"})
    return out


def visible_items(uid):
    owner = uid == OWNER_ID
    rows = q("SELECT author, id, kind, data FROM items")
    shared = SHARE[mode()]
    out = []
    for author, iid, kind, data in rows:
        if author == uid or (kind in REC and (owner or kind in shared)):
            out.append({"author": author, "id": iid, "data": json.loads(data)})
    return out


def save_item(author, iid, data):
    kind = data.get("kind")
    q("INSERT INTO items(author, id, kind, data, updated) VALUES(?, ?, ?, ?, ?) "
      "ON CONFLICT(author, id) DO UPDATE SET kind=excluded.kind, data=excluded.data, updated=excluded.updated",
      (author, iid, kind, json.dumps(data, ensure_ascii=False), time.time()))


def delete_item(author, iid):
    q("DELETE FROM items WHERE author=? AND id=?", (author, iid))


def next_code(author):
    mx = 0
    for (data,) in q("SELECT data FROM items WHERE author=? AND kind='contact'", (author,)):
        m = re.search(r"(\d+)", json.loads(data).get("code", ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def new_id(kind):
    return f"{kind}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- GigaChat
_giga = {"token": None, "exp": 0}


def _verify():
    return GIGA_CA if os.path.exists(GIGA_CA) else False


async def giga_token():
    if _giga["token"] and time.time() < _giga["exp"] - 60:
        return _giga["token"]
    async with httpx.AsyncClient(verify=_verify(), timeout=30) as c:
        r = await c.post("https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                         headers={"Authorization": f"Basic {GIGA_KEY}", "RqUID": str(uuid.uuid4()),
                                  "Accept": "application/json",
                                  "Content-Type": "application/x-www-form-urlencoded"},
                         content=f"scope={GIGA_SCOPE}")
        r.raise_for_status()
        j = r.json()
    _giga["token"] = j["access_token"]
    _giga["exp"] = j.get("expires_at", 0) / 1000 if j.get("expires_at") else time.time() + 1500
    return _giga["token"]


async def giga_chat(messages, temperature=0.4):
    tok = await giga_token()
    async with httpx.AsyncClient(verify=_verify(), timeout=120) as c:
        r = await c.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                         json={"model": GIGA_MODEL, "messages": messages, "temperature": temperature})
        if r.status_code == 401:
            _giga["token"] = None
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def parse_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        return json.loads(text[a:b + 1])
    raise ValueError("no json")


# ---------------------------------------------------------------- Секретарь
SECRETARY = """Ты — Секретарь штаба будущего консьерж-сервиса. Основатель прислал заметку (иногда это расшифровка голосового — возможны ошибки распознавания, исправляй по смыслу). Разложи её на записи базы.
Правила:
— Реальные имена людей НЕ записывай: заменяй ролью («владелец автосервиса»). Названия компаний и мастеров записывать можно.
— Если знакомый уже есть в списке ниже (по коду или описанию) — используй его код. Нового знакомого добавь в contacts с ref "new1", "new2" и ссылайся на этот ref в logs.
— Ниша — строго одна из: {niches}.
— result в logs: "wait" (ждём), "good" (доволен), "bad" (не доволен). amount — примерная сумма заказа в рублях числом или null.
— Не выдумывай того, чего нет в заметке. Если записывать нечего — верни пустые массивы и объясни в reply.
Существующие знакомые:
{contacts}
Существующие исполнители: {partners}

Ответь только JSON без пояснений:
{{"contacts":[{{"ref":"new1","role":"","needs":"","spend":"","niche":""}}],"partners":[{{"name":"","niche":"","why":"","who":""}}],"logs":[{{"contact":"К-001 или new1","task":"","partner":"","result":"wait","amount":null}}],"tasks":[{{"text":""}}],"reply":"одна-две фразы: что записано и что стоит уточнить"}}

Заметка:
{text}"""


async def run_secretary(uid, text):
    mine = [json.loads(d) for (d,) in q("SELECT data FROM items WHERE author=? AND kind='contact'", (uid,))]
    partners = [json.loads(d).get("name", "") for (d,) in q("SELECT data FROM items WHERE kind='partner'")]
    prompt = SECRETARY.format(
        niches="; ".join(NICHES),
        contacts="\n".join(f'{c.get("code")}: {c.get("role", "")} ({c.get("needs", "")})' for c in mine) or "нет",
        partners=", ".join(p for p in partners if p) or "нет",
        text=text[:6000])
    raw = await giga_chat([{"role": "user", "content": prompt}], temperature=0.1)
    r = parse_json(raw)
    now = time.time() * 1000
    ids, refs = [], {}
    code = next_code(uid)
    niche = lambda v: v if v in NICHES else "Другое"
    for c in r.get("contacts") or []:
        if not isinstance(c, dict) or not c.get("role"):
            continue
        cd = f"К-{code:03d}"
        code += 1
        refs[c.get("ref") or cd] = cd
        iid = new_id("contact")
        save_item(uid, iid, {"kind": "contact", "code": cd, "role": str(c["role"]), "needs": str(c.get("needs") or ""),
                             "spend": str(c.get("spend") or ""), "niche": niche(c.get("niche")), "created": now})
        ids.append(iid)
    for p in r.get("partners") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        iid = new_id("partner")
        save_item(uid, iid, {"kind": "partner", "name": str(p["name"]), "niche": niche(p.get("niche")), "status": "found",
                             "why": str(p.get("why") or ""), "who": str(refs.get(p.get("who"), p.get("who") or "")),
                             "contact": "", "commission": "none", "created": now})
        ids.append(iid)
    for l in r.get("logs") or []:
        if not isinstance(l, dict) or not l.get("task"):
            continue
        try:
            amt = int(float(l.get("amount"))) if l.get("amount") not in (None, "") else 0
        except Exception:
            amt = 0
        iid = new_id("log")
        save_item(uid, iid, {"kind": "log", "date": time.strftime("%Y-%m-%d"),
                             "contact": str(refs.get(l.get("contact"), l.get("contact") or "")),
                             "task": str(l["task"]), "partner": str(l.get("partner") or ""),
                             "result": l.get("result") if l.get("result") in ("wait", "good", "bad") else "wait",
                             "amount": str(amt) if amt > 0 else "", "note": "", "created": now})
        ids.append(iid)
    for t in r.get("tasks") or []:
        if not isinstance(t, dict) or not t.get("text"):
            continue
        iid = new_id("task")
        save_item(uid, iid, {"kind": "task", "text": str(t["text"])[:200], "done": False, "from": "Секретарь", "created": now})
        ids.append(iid)
    return ids, str(r.get("reply") or "")


# ---------------------------------------------------------------- голос (Vosk, офлайн)
_vosk = {"model": None}


def transcribe(ogg_path):
    from vosk import KaldiRecognizer, Model
    if _vosk["model"] is None:
        _vosk["model"] = Model(VOSK_MODEL)
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ogg_path, "-ar", "16000", "-ac", "1",
                        "-f", "s16le", wav.name], check=True)
        rec = KaldiRecognizer(_vosk["model"], 16000)
        parts = []
        with open(wav.name, "rb") as f:
            while chunk := f.read(8000):
                if rec.AcceptWaveform(chunk):
                    parts.append(json.loads(rec.Result()).get("text", ""))
        parts.append(json.loads(rec.FinalResult()).get("text", ""))
    return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------- проверка подписи Telegram
def check_init_data(init_data):
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received or not BOT_TOKEN:
        raise HTTPException(401, "Откройте штаб через бота в Telegram")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Неверная подпись Telegram")
    if time.time() - int(pairs.get("auth_date", "0") or 0) > 7 * 86400:
        raise HTTPException(401, "Сессия устарела — откройте штаб заново")
    user = json.loads(pairs.get("user", "{}"))
    uid = str(user.get("id", ""))
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Друг"
    if not uid:
        raise HTTPException(401, "Нет пользователя")
    if not is_member(uid):
        raise HTTPException(403, "Вас пока нет в штабе. Попросите владельца прислать приглашение.")
    add_member(uid, name)
    return uid


# ---------------------------------------------------------------- API
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


async def body_of(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    if not isinstance(b, dict):
        b = {}
    return b


def target(uid, b):
    author = str(b.get("author") or uid)
    if author != uid and uid != OWNER_ID:
        raise HTTPException(403, "Чужие записи может менять только владелец")
    iid = str(b.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_\-:.]{1,80}", iid):
        raise HTTPException(400, "Неверный id")
    return author, iid


@app.post("/api/state")
async def api_state(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    return {"me": uid, "owner": uid == OWNER_ID, "mode": mode(), "members": members(), "items": visible_items(uid)}


@app.post("/api/put")
async def api_put(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    b = await body_of(request)
    author, iid = target(uid, b)
    data = b.get("data")
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise HTTPException(400, "Неверная запись")
    if author != uid and data["kind"] not in REC:
        raise HTTPException(403, "Личные записи меняет только автор")
    if len(json.dumps(data, ensure_ascii=False)) > 60000:
        raise HTTPException(413, "Запись слишком большая")
    save_item(author, iid, data)
    return {"ok": True}


@app.post("/api/del")
async def api_del(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    author, iid = target(uid, await body_of(request))
    delete_item(author, iid)
    return {"ok": True}


@app.post("/api/mode")
async def api_mode(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    if uid != OWNER_ID:
        raise HTTPException(403, "Режим меняет только владелец")
    m = (await body_of(request)).get("mode")
    if m not in (1, 2, 3):
        raise HTTPException(400, "Режим 1, 2 или 3")
    kv_set("mode", m)
    return {"ok": True}


@app.post("/api/ask")
async def api_ask(request: Request):
    check_init_data(request.headers.get("X-Init-Data"))
    msgs = (await body_of(request)).get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise HTTPException(400, "Нет сообщений")
    clean = []
    for m in msgs[-24:]:
        if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and m.get("content"):
            clean.append({"role": m["role"], "content": str(m["content"])[:20000]})
    try:
        return {"text": await giga_chat(clean)}
    except Exception as e:
        log.warning("giga ask failed: %s", e)
        raise HTTPException(502, "Агент сейчас недоступен. Попробуйте через минуту.")


@app.post("/api/inbox")
async def api_inbox(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    text = str((await body_of(request)).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустая заметка")
    try:
        ids, reply = await run_secretary(uid, text)
    except Exception as e:
        log.warning("secretary failed: %s", e)
        raise HTTPException(502, "Не получилось разобрать заметку. Попробуйте сформулировать проще.")
    return {"ids": ids, "reply": reply}


@app.post("/api/undo")
async def api_undo(request: Request):
    uid = check_init_data(request.headers.get("X-Init-Data"))
    for iid in (await body_of(request)).get("ids") or []:
        delete_item(uid, str(iid))
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(HTTPException)
async def http_err(_, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- Telegram-бот
def start_bot():
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                               Message, WebAppInfo)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    def open_kb():
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть штаб", web_app=WebAppInfo(url=PUBLIC_URL))]])

    def name_of(u):
        return " ".join(x for x in (u.first_name, u.last_name) if x) or u.username or "Друг"

    @dp.message(CommandStart())
    async def on_start(m: Message, command: CommandObject):
        uid = str(m.from_user.id)
        code = (command.args or "").strip()
        if code and not is_member(uid):
            invites = kv_get("invites", {})
            if invites.pop(code, None):
                kv_set("invites", invites)
                add_member(uid, name_of(m.from_user))
                if OWNER_ID:
                    try:
                        await bot.send_message(int(OWNER_ID), f"{name_of(m.from_user)} вошёл в штаб по приглашению.")
                    except Exception:
                        pass
        if not is_member(uid):
            await m.answer("Это закрытый штаб. Чтобы войти, нужна ссылка-приглашение от владельца.")
            return
        add_member(uid, name_of(m.from_user))
        await m.answer("Штаб открыт.\n\nОтправляйте сюда текст или голосовое — Секретарь сам разложит заметку по базе. "
                       "Всё остальное — в приложении штаба.", reply_markup=open_kb())

    @dp.message(Command("invite"))
    async def on_invite(m: Message):
        if str(m.from_user.id) != OWNER_ID:
            return
        code = secrets.token_urlsafe(8)
        invites = kv_get("invites", {})
        invites[code] = time.time()
        kv_set("invites", invites)
        me = await bot.get_me()
        await m.answer(f"Одноразовое приглашение в штаб:\nhttps://t.me/{me.username}?start={code}\n\n"
                       "Отправьте эту ссылку другу. После входа она перестанет работать.")

    @dp.message(Command("members"))
    async def on_members(m: Message):
        if str(m.from_user.id) != OWNER_ID:
            return
        rows = [f"{x['name']} — id {x['id']}" for x in members() if x["id"] != OWNER_ID]
        await m.answer("Участники штаба:\n" + ("\n".join(rows) if rows else "пока только вы") +
                       "\n\nУбрать участника: /remove id")

    @dp.message(Command("remove"))
    async def on_remove(m: Message, command: CommandObject):
        if str(m.from_user.id) != OWNER_ID:
            return
        rid = (command.args or "").strip()
        if not rid or rid == OWNER_ID:
            await m.answer("Напишите: /remove id участника (его видно в /members).")
            return
        q("DELETE FROM members WHERE tg_id=?", (rid,))
        await m.answer("Участник убран. Его записи остались в базе, войти он больше не сможет.")

    async def handle_note(m: Message, text: str):
        uid = str(m.from_user.id)
        try:
            ids, reply = await run_secretary(uid, text)
        except Exception as e:
            log.warning("bot secretary failed: %s", e)
            await m.answer("Не получилось разобрать заметку. Попробуйте ещё раз или сформулируйте проще.")
            return
        kv_set(f"undo:{uid}", ids)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить запись", callback_data="undo")],
                                                   [InlineKeyboardButton(text="Открыть штаб", web_app=WebAppInfo(url=PUBLIC_URL))]]) if ids else open_kb()
        await m.answer((reply or "Готово.") + (f"\n\nДобавлено записей: {len(ids)}." if ids else ""), reply_markup=kb)

    @dp.callback_query(F.data == "undo")
    async def on_undo(c: CallbackQuery):
        uid = str(c.from_user.id)
        for iid in kv_get(f"undo:{uid}", []):
            delete_item(uid, iid)
        kv_set(f"undo:{uid}", [])
        await c.answer("Запись отменена")
        await c.message.edit_reply_markup(reply_markup=open_kb())

    @dp.message(F.voice)
    async def on_voice(m: Message):
        if not is_member(str(m.from_user.id)):
            return
        if m.voice.duration > 180:
            await m.answer("Голосовое длиннее 3 минут — разбейте, пожалуйста, на части.")
            return
        await m.answer("Слушаю…")
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            await bot.download(m.voice, destination=f.name)
            try:
                text = await asyncio.to_thread(transcribe, f.name)
            except Exception as e:
                log.warning("voice failed: %s", e)
                await m.answer("Не получилось распознать голосовое. Попробуйте ещё раз или напишите текстом.")
                return
        if not text:
            await m.answer("Не расслышал. Попробуйте говорить чуть медленнее.")
            return
        await m.answer(f"Распознал: «{text}»")
        await handle_note(m, text)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(m: Message):
        if not is_member(str(m.from_user.id)):
            await m.answer("Это закрытый штаб. Чтобы войти, нужна ссылка-приглашение от владельца.")
            return
        await handle_note(m, m.text)

    async def backups():
        from aiogram.types import FSInputFile
        while True:
            await asyncio.sleep(24 * 3600)
            if not OWNER_ID:
                continue
            try:
                path = Path(DB_PATH).with_name("backup.db")
                with _lock:
                    dst = sqlite3.connect(path)
                    _db.backup(dst)
                    dst.close()
                await bot.send_document(int(OWNER_ID), FSInputFile(path, filename=f"shtab-{time.strftime('%Y-%m-%d')}.db"),
                                        caption="Ежедневная резервная копия базы штаба.", disable_notification=True)
            except Exception as e:
                log.warning("backup failed: %s", e)

    async def run():
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Штаб", web_app=WebAppInfo(url=PUBLIC_URL)))
        except Exception as e:
            log.warning("menu button: %s", e)
        asyncio.create_task(backups())
        await dp.start_polling(bot, handle_signals=False)

    return run


@app.on_event("startup")
async def _startup():
    if BOT_TOKEN and PUBLIC_URL and os.environ.get("SHTAB_NO_BOT") != "1":
        asyncio.create_task(start_bot()())
    else:
        log.info("bot disabled")
