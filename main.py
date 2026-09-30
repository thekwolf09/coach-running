import os, io, json, time, sqlite3, datetime, threading, requests
from concurrent.futures import ThreadPoolExecutor
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i596796").strip()
INTERVALS_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_USER = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

ai_client = genai.Client(api_key=GEMINI_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_KEY)
BASE = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}"
DB = "coach_brain.db"
POOL = ThreadPoolExecutor(max_workers=4)

def init_db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, d TEXT, txt TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    c.commit()
    c.close()

def save_note(txt):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO notes (d, txt) VALUES (?, ?)", (datetime.date.today().isoformat(), txt))
    c.commit()
    c.close()

def get_notes():
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT d, txt FROM notes ORDER BY id DESC LIMIT 20").fetchall()
    c.close()
    return [{"date": r[0], "note": r[1]} for r in reversed(rows)]

def fetch_profile():
    try:
        r = requests.get(BASE, auth=AUTH, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {"zones": d.get("icu_hr_zones"), "lthr": d.get("icu_lthr"), "weight": d.get("weight")}
    except Exception:
        pass
    return {}

def fetch_wellness():
    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE}/wellness", auth=AUTH, params={"oldest": old, "newest": today}, timeout=10)
        if r.status_code == 200:
            res = []
            for j in r.json():
                if j.get("sleepSecs") or j.get("hrv") or j.get("ctl"):
                    res.append({
                        "d": j.get("id"),
                        "sleep_h": round(j.get("sleepSecs", 0) / 3600, 1) if j.get("sleepSecs") else None,
                        "hrv": j.get("hrv"),
                        "hrv_base": j.get("hrvBaseline"),
                        "rhr": j.get("restingHR"),
                        "tsb": j.get("form"),
                        "atl": j.get("atl"),
                        "ctl": j.get("ctl")
                    })
            return res
    except Exception:
        pass
    return []

def fetch_activities():
    old = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE}/activities", auth=AUTH, params={"oldest": old}, timeout=10)
        if r.status_code == 200:
            res = []
            for a in r.json()[:60]:
                spd = a.get("average_speed", 0)
                pace = f"{int((1000/spd)//60)}'{int((1000/spd)%60):02d}\"/km" if spd > 0 else None
                res.append({
                    "id": a.get("id"),
                    "d": a.get("start_date_local", "")[:10],
                    "nom": a.get("name"),
                    "km": round(a.get("distance", 0) / 1000, 2),
                    "min": round(a.get("moving_time", 0) / 60, 1),
                    "pace": pace,
                    "hr": a.get("average_heartrate"),
                    "load": a.get("icu_training_load")
                })
            return res
    except Exception:
        pass
    return []

def fetch_events():
    today = datetime.date.today().isoformat()
    fut = (datetime.date.today() + datetime.timedelta(days=91)).isoformat()
    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": today, "newest": fut}, timeout=8)
        if r.status_code == 200:
            return [{"id": e.get("id"), "d": e.get("start_date_local", "")[:10], "nom": e.get("name"), "desc": e.get("description")} for e in r.json()]
    except Exception:
        pass
    return []

def get_all_data():
    f1 = POOL.submit(fetch_profile)
    f2 = POOL.submit(fetch_wellness)
    f3 = POOL.submit(fetch_activities)
    f4 = POOL.submit(fetch_events)
    return f1.result(), f2.result(), f3.result(), f4.result()

def planifier_seance(date_str: str, titre: str, description: str = "") -> str:
    """Planifie une séance d'entraînement course à pied sur Intervals.icu.
    Args:
        date_str: Date de la séance au format AAAA-MM-JJ (ex: '2026-09-30').
        titre: Titre court de la séance (ex: 'Footing EF 25 min').
        description: Consignes d'allures, zones cardiaques et durée.
    """
    headers = {"Content-Type": "application/json"}
    date_val = f"{date_str}T18:00:00" if "T" not in date_str else date_str
    payload = {
        "category": "WORKOUT",
        "type": "Run",
        "name": titre,
        "description": description,
        "start_date_local": date_val
    }
    try:
        r = requests.post(f"{BASE}/events", auth=AUTH, headers=headers, json=payload, timeout=8)
        if r.status_code in (200, 201):
            return f"Séance '{titre}' ajoutée sur Intervals.icu pour le {date_str}."
        return f"Statut Intervals.icu : {r.status_code}"
    except Exception as err:
        return f"Erreur Intervals.icu : {err}"

def generate_ai(prompt_parts, user_msg_raw=""):
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    for m in models:
        try:
            cfg = types.GenerateContentConfig(tools=[planifier_seance], temperature=0.3)
            r = ai_client.models.generate_content(model=m, contents=prompt_parts, config=cfg)
            if r.function_calls:
                call = r.function_calls[0]
                args = call.args or {}
                tool_res = planifier_seance(**args)
                conf = (
                    f"Action exécutée : {tool_res}.\n"
                    f"Demande de l'athlète : '{user_msg_raw}'.\n"
                    f"Confirme à l'athlète en HTML Telegram avec un court conseil d'allure."
                )
                return ai_client.models.generate_content(model=m, contents=conf).text
            if r and r.text:
                return r.text
        except Exception:
            try:
                r_fallback = ai_client.models.generate_content(model=m, contents=prompt_parts)
                if r_fallback and r_fallback.text:
                    return r_fallback.text
            except Exception:
                pass
            continue
    return "Service temporairement indisponible côté Google, réessaie dans un instant."

def make_prompt(prof, well, acts, evts, user_msg):
    mem = get_notes()
    consignes = (
        "Consignes :\n"
        "1. Analyse croisée complète (6 mois passés, nuit actuelle, 13 semaines à venir).\n"
        "2. FORMAT HTML TELEGRAM : utilise <b>Texte en gras</b> pour les allures et chiffres clés. Lignes vides pour aérer. Pas de dièses (#) ni d'astérisques (**).\n"
        "3. Si un fait durable est mentionné, écris en fin de message : [MEMOIRE] note à enregistrer\n"
        "4. Si l'athlète demande d'ajouter ou planifier une séance, utilise la fonction 'planifier_seance'."
    )
    return (
        f"Tu es l'entraîneur d'athlétisme personnel de ce coureur (objectif prioritaire : 5 km sub-20, cible 3'59/km au 13/12/2026).\n"
        f"Date du jour : {datetime.date.today().isoformat()}.\n\n"
        f"PROFIL ATHLÈTE : {json.dumps(prof, ensure_ascii=False)}\n"
        f"MÉMOIRE DURABLE (Base SQLite) : {json.dumps(mem, ensure_ascii=False)}\n"
        f"SANTÉ SUR 6 MOIS : {json.dumps(well, ensure_ascii=False)}\n"
        f"SÉANCES SUR 6 MOIS : {json.dumps(acts, ensure_ascii=False)}\n"
        f"PLAN SUR 13 SEMAINES : {json.dumps(evts, ensure_ascii=False)}\n\n"
        f"{consignes}\n\n"
        f"MESSAGE DE L'ATHLÈTE :\n\"{user_msg}\""
    )

async def send_reply(cid, text, bot):
    if "[MEMOIRE]" in text:
        parts = text.split("[MEMOIRE]")
        text = parts[0].strip()
        save_note(parts[1].strip().split("\n")[0])
    try:
        await bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=cid, text=text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TG_USER:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = update.message.text
    prof, well, acts, evts = get_all_data()
    prompt = make_prompt(prof, well, acts, evts, msg)
    ans = generate_ai(prompt, user_msg_raw=msg)
    await send_reply(update.effective_chat.id, ans, context.bot)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TG_USER:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    v = update.message.voice or update.message.audio
    f = await context.bot.get_file(v.file_id)
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    prof, well, acts, evts = get_all_data()
    prompt = [make_prompt(prof, well, acts, evts, "Message vocal de l'athlète"), types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/ogg")]
    ans = generate_ai(prompt, user_msg_raw="Message vocal")
    await send_reply(update.effective_chat.id, ans, context.bot)

def bg_loop():
    time.sleep(30)
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    while True:
        try:
            prof, well, acts, evts = get_all_data()
            c = sqlite3.connect(DB)
            if acts:
                last_id = str(acts[0]["id"])
                if not c.execute("SELECT 1 FROM seen_acts WHERE id=?", (last_id,)).fetchone():
                    c.execute("INSERT OR IGNORE INTO seen_acts VALUES (?)", (last_id,))
                    c.commit()
                    ans = generate_ai(make_prompt(prof, well, acts, evts, f"Nouvelle séance détectée : {acts[0]}"))
                    requests.post(url, json={"chat_id": TG_USER, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{ans}", "parse_mode": "HTML"}, timeout=10)
            if well:
                last_d = well[0].get("d")
                if last_d and well[0].get("sleep_h") and not c.execute("SELECT 1 FROM seen_well WHERE d=?", (last_d,)).fetchone():
                    c.execute("INSERT OR IGNORE INTO seen_well VALUES (?)", (last_d,))
                    c.commit()
                    ans = generate_ai(make_prompt(prof, well, acts, evts, f"Réveil enregistré : {well[0]}"))
                    requests.post(url, json={"chat_id": TG_USER, "text": f"☀️ <b>Réveil détecté</b>\n\n{ans}", "parse_mode": "HTML"}, timeout=10)
            c.close()
        except Exception:
            pass
        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Coach Running prêt et connecté !")
    threading.Thread(target=bg_loop, daemon=True).start()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling()


