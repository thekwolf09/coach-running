import os
import io
import re
import json
import time
import sqlite3
import datetime
import asyncio
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i596796").strip()
INTERVALS_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_USER = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

MODEL_NAME = "gemini-3.8-flash"
ai_client = genai.Client(api_key=GEMINI_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_KEY)
BASE = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}"
DB = "coach_brain.db"
POOL = ThreadPoolExecutor(max_workers=4)

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, txt TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def save_note(txt: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO notes (d, txt) VALUES (?, ?)", (datetime.date.today().isoformat(), txt))
    conn.commit()
    conn.close()

def get_notes():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT d, txt FROM notes ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "note": r[1]} for r in reversed(rows)]

def fetch_profile():
    try:
        r = requests.get(BASE, auth=AUTH, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {
                "zones": d.get("icu_hr_zones"),
                "lthr": d.get("icu_lthr"),
                "weight": d.get("weight")
            }
    except Exception as err:
        logging.error(f"Erreur fetch_profile: {err}")
    return {}

def fetch_wellness():
    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE}/wellness", auth=AUTH, params={"oldest": old, "newest": today}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            # Tri antéchronologique explicite : le plus récent en premier
            raw.sort(key=lambda x: str(x.get("id", "")), reverse=True)
            res = []
            for j in raw:
                s_sec = j.get("sleepSecs") or 0
                if s_sec or j.get("hrv") or j.get("ctl"):
                    res.append({
                        "d": j.get("id"),
                        "sleep_h": round(s_sec / 3600, 1) if s_sec else None,
                        "hrv": j.get("hrv"),
                        "hrv_base": j.get("hrvBaseline"),
                        "rhr": j.get("restingHR"),
                        "tsb": j.get("form"),
                        "atl": j.get("atl"),
                        "ctl": j.get("ctl")
                    })
            return res
    except Exception as err:
        logging.error(f"Erreur fetch_wellness: {err}")
    return []

def fetch_activities():
    old = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE}/activities", auth=AUTH, params={"oldest": old}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            # Tri antéchronologique explicite avant de tronquer
            raw.sort(key=lambda x: str(x.get("start_date_local", "")), reverse=True)
            res = []
            for a in raw[:70]:
                spd = a.get("average_speed") or 0
                dist = a.get("distance") or 0
                mtime = a.get("moving_time") or 0
                pace = f"{int((1000/spd)//60)}'{int((1000/spd)%60):02d}\"/km" if spd > 0 else None
                res.append({
                    "id": a.get("id"),
                    "d": str(a.get("start_date_local", ""))[:10],
                    "nom": a.get("name"),
                    "km": round(dist / 1000, 2),
                    "min": round(mtime / 60, 1),
                    "pace": pace,
                    "hr": a.get("average_heartrate"),
                    "load": a.get("icu_training_load")
                })
            return res
    except Exception as err:
        logging.error(f"Erreur fetch_activities: {err}")
    return []

def fetch_events():
    today = datetime.date.today().isoformat()
    fut = (datetime.date.today() + datetime.timedelta(days=91)).isoformat()
    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": today, "newest": fut}, timeout=8)
        if r.status_code == 200:
            raw = r.json()
            raw.sort(key=lambda x: str(x.get("start_date_local", "")))
            return [{
                "id": e.get("id"),
                "d": str(e.get("start_date_local", ""))[:10],
                "nom": e.get("name"),
                "desc": e.get("description")
            } for e in raw]
    except Exception as err:
        logging.error(f"Erreur fetch_events: {err}")
    return []

def get_all_data():
    f1 = POOL.submit(fetch_profile)
    f2 = POOL.submit(fetch_wellness)
    f3 = POOL.submit(fetch_activities)
    f4 = POOL.submit(fetch_events)
    return f1.result(), f2.result(), f3.result(), f4.result()

def planifier_seance(date_str: str = "", titre: str = "", description: str = "", heure: str = "18:00") -> str:
    """Planifie une séance d'entraînement de course à pied sur Intervals.icu.
    Args:
        date_str: Date de la séance au format AAAA-MM-JJ (ex: '2026-09-30').
        titre: Titre court de la séance.
        description: Consignes d'allures, intensités et zones cibles.
        heure: Heure de la séance au format HH:MM (défaut '18:00').
    """
    today_iso = datetime.date.today().isoformat()
    target_date = today_iso if any(k in str(date_str).lower() for k in ["today", "aujourd", "soir", "ce soir"]) else str(date_str).split("T")[0]
    clean_hour = heure if ":" in heure else "18:00"

    payload = {
        "category": "WORKOUT",
        "type": "Run",
        "name": titre or "Séance Course",
        "description": description or f"Séance : {titre}",
        "start_date_local": f"{target_date}T{clean_hour}:00"
    }
    try:
        r = requests.post(f"{BASE}/events", auth=AUTH, headers={"Content-Type": "application/json"}, json=payload, timeout=8)
        if r.status_code in (200, 201):
            return f"Séance '{payload['name']}' planifiée le {target_date} à {clean_hour} sur Intervals.icu."
        return f"Erreur Intervals.icu ({r.status_code}) : {r.text}"
    except Exception as err:
        return f"Erreur de connexion Intervals : {err}"

def supprimer_seance(date_str: str = "", titre: str = "") -> str:
    """Supprime une séance planifiée sur Intervals.icu.
    Args:
        date_str: Date de la séance au format AAAA-MM-JJ (ex: '2026-09-30' ou 'today').
        titre: Titre ou mot clé de la séance à supprimer.
    """
    today_iso = datetime.date.today().isoformat()
    target_date = today_iso if any(k in str(date_str).lower() for k in ["today", "aujourd", "ce jour", "soir"]) else str(date_str).split("T")[0]

    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": target_date, "newest": target_date}, timeout=8)
        if r.status_code != 200:
            return f"Impossible d'accéder au calendrier Intervals.icu ({r.status_code})."

        events = [e for e in r.json() if e.get("category") == "WORKOUT"]
        if not events:
            return f"Aucune séance trouvée sur Intervals.icu le {target_date}."

        target_event = None
        t_clean = (titre or "").strip().lower()
        if t_clean:
            for ev in events:
                if t_clean in ev.get("name", "").lower():
                    target_event = ev
                    break

        if not target_event:
            if len(events) == 1:
                target_event = events[0]
            else:
                noms = ", ".join([f"'{e.get('name')}'" for e in events])
                return f"Plusieurs séances existent le {target_date} ({noms}). Précise laquelle supprimer."

        ev_id = target_event.get("id")
        ev_nom = target_event.get("name", "Séance")
        del_r = requests.delete(f"{BASE}/events/{ev_id}", auth=AUTH, timeout=8)
        if del_r.status_code in (200, 204):
            return f"La séance '{ev_nom}' du {target_date} a bien été supprimée de ton calendrier."
        return f"Erreur suppression Intervals ({del_r.status_code})."
    except Exception as err:
        return f"Erreur de connexion Intervals : {err}"

def generate_ai(prompt_parts, user_msg_raw=""):
    tools_map = {
        "planifier_seance": planifier_seance,
        "supprimer_seance": supprimer_seance
    }
    tool_executed_msg = None

    # Configuration avec désactivation de l'Automatic Function Calling interne
    cfg = types.GenerateContentConfig(
        tools=[planifier_seance, supprimer_seance],
        temperature=0.3,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )

    # 1. Détection et exécution de l'outil (une seule fois)
    for _ in range(3):
        try:
            r = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt_parts, config=cfg)
            if r.function_calls:
                call = r.function_calls[0]
                fn = tools_map.get(call.name)
                args = dict(call.args) if call.args else {}
                tool_executed_msg = fn(**args) if fn else "Action inconnue"
                break
            if r and r.text:
                return r.text
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                time.sleep(3)
                continue
            logging.error(f"Erreur generate_ai: {e}")
            time.sleep(1.5)

    # 2. Confirmation formatée pour Telegram si un outil a tourné
    if tool_executed_msg:
        conf_prompt = (
            f"Action effectuée : {tool_executed_msg}.\n"
            f"Demande initiale : '{user_msg_raw}'.\n"
            f"Confirme à l'athlète en HTML Telegram avec un ton direct et bienveillant."
        )
        try:
            r_conf = ai_client.models.generate_content(model=MODEL_NAME, contents=conf_prompt)
            return r_conf.text
        except Exception:
            return tool_executed_msg

    return "⚠️ Service momentanément indisponible, réessaie dans un instant."

def make_prompt(prof, well, acts, evts, user_msg):
    mem = get_notes()
    consignes = (
        "Consignes d'analyse :\n"
        "1. Analyse croisée complète : données récentes en priorité (les listes sont triées du plus récent au plus ancien).\n"
        "2. FORMAT HTML TELEGRAM : utilise <b>Texte en gras</b> pour les allures et chiffres clés. "
        "Aère avec des lignes vides. Pas de balises Markdown (# ou **).\n"
        "3. Si un fait durable est mentionné, écris en fin de message : [MEMOIRE] note à enregistrer\n"
        "4. Si l'athlète demande d'ajouter ou supprimer une séance, appelle obligatoirement planifier_seance ou supprimer_seance."
    )
    return (
        f"Tu es l'entraîneur personnel de ce coureur (objectif prioritaire : 5 km sub-20, cible 3'59/km au 13/12/2026).\n"
        f"Date du jour : {datetime.date.today().isoformat()}.\n\n"
        f"PROFIL ATHLÈTE : {json.dumps(prof, ensure_ascii=False)}\n"
        f"MÉMOIRE DURABLE : {json.dumps(mem, ensure_ascii=False)}\n"
        f"SANTÉ (du plus récent au plus ancien) : {json.dumps(well, ensure_ascii=False)}\n"
        f"SÉANCES (du plus récent au plus ancien) : {json.dumps(acts, ensure_ascii=False)}\n"
        f"CALENDRIER À VENIR : {json.dumps(evts, ensure_ascii=False)}\n\n"
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
        # Nettoyage des balises si Telegram rejette le HTML
        clean_text = re.sub(r'<[^>]+>', '', text)
        await bot.send_message(chat_id=cid, text=clean_text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TG_USER:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = update.message.text
    prof, well, acts, evts = await asyncio.to_thread(get_all_data)
    prompt = make_prompt(prof, well, acts, evts, msg)
    ans = await asyncio.to_thread(generate_ai, prompt, user_msg_raw=msg)
    await send_reply(update.effective_chat.id, ans, context.bot)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TG_USER:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    v = update.message.voice or update.message.audio
    f = await context.bot.get_file(v.file_id)
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    mime = getattr(v, "mime_type", None) or "audio/ogg"

    prof, well, acts, evts = await asyncio.to_thread(get_all_data)
    prompt = [make_prompt(prof, well, acts, evts, "Message vocal"), types.Part.from_bytes(data=buf.getvalue(), mime_type=mime)]
    ans = await asyncio.to_thread(generate_ai, prompt, user_msg_raw="Message vocal")
    await send_reply(update.effective_chat.id, ans, context.bot)

def bg_loop():
    time.sleep(20)
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    try:
        _, well_init, acts_init, _ = get_all_data()
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        for a in acts_init:
            c.execute("INSERT OR IGNORE INTO seen_acts VALUES (?)", (str(a["id"]),))
        if well_init and well_init[0].get("d"):
            c.execute("INSERT OR IGNORE INTO seen_well VALUES (?)", (str(well_init[0]["d"]),))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Erreur init bg_loop: {e}")

    while True:
        time.sleep(900)
        try:
            prof, well, acts, evts = get_all_data()
            conn = sqlite3.connect(DB)
            c = conn.cursor()

            if acts:
                # Boucle sur les activités non vues
                for a in acts:
                    act_id = str(a["id"])
                    c.execute("SELECT 1 FROM seen_acts WHERE id=?", (act_id,))
                    if not c.fetchone():
                        c.execute("INSERT OR IGNORE INTO seen_acts VALUES (?)", (act_id,))
                        conn.commit()
                        ans = generate_ai(make_prompt(prof, well, acts, evts, f"Débrief séance : {a}"))
                        requests.post(url, json={"chat_id": TG_USER, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{ans}", "parse_mode": "HTML"}, timeout=10)

            if well and well[0].get("d") and well[0].get("sleep_h"):
                last_d = well[0]["d"]
                c.execute("SELECT 1 FROM seen_well WHERE d=?", (last_d,))
                if not c.fetchone():
                    c.execute("INSERT OR IGNORE INTO seen_well VALUES (?)", (last_d,))
                    conn.commit()
                    ans = generate_ai(make_prompt(prof, well, acts, evts, f"Brief réveil : {well[0]}"))
                    requests.post(url, json={"chat_id": TG_USER, "text": f"☀️ <b>Réveil détecté</b>\n\n{ans}", "parse_mode": "HTML"}, timeout=10)

            conn.close()
        except Exception as e:
            logging.error(f"Erreur boucle bg_loop: {e}")

if __name__ == "__main__":
    init_db()
    logging.info("Bot Coach Running démarré avec succès !")
    threading.Thread(target=bg_loop, daemon=True).start()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling()
    
