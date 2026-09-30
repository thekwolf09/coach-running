csjimport os
import io
import json
import time
import sqlite3
import datetime
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i596796").strip()
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_API_KEY)
BASE_URL = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"
DB_FILE = "coach_brain.db"
EXECUTOR = ThreadPoolExecutor(max_workers=4)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS memory_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, category TEXT, content TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS processed_activities (activity_id TEXT PRIMARY KEY, processed_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS processed_wellness (wellness_date TEXT PRIMARY KEY, processed_at TEXT)")
    conn.commit()
    conn.close()

def save_memory_note(note_text: str, category: str = "general"):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO memory_notes (date, category, content) VALUES (?, ?, ?)", (today, category, note_text))
    conn.commit()
    conn.close()

def get_recent_memory_notes(limit=25):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT date, content FROM memory_notes ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "note": r[1]} for r in reversed(rows)]

def is_activity_processed(act_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_activities WHERE activity_id = ?", (str(act_id),))
    res = c.fetchone() is not None
    conn.close()
    return res

def mark_activity_processed(act_id: str):
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO processed_activities (activity_id, processed_at) VALUES (?, ?)", (str(act_id), now))
    conn.commit()
    conn.close()

def is_wellness_processed(date_str: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_wellness WHERE wellness_date = ?", (str(date_str),))
    res = c.fetchone() is not None
    conn.close()
    return res

def mark_wellness_processed(date_str: str):
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO processed_wellness (wellness_date, processed_at) VALUES (?, ?)", (str(date_str), now))
    conn.commit()
    conn.close()

def fetch_profil_athlete():
    try:
        r = requests.get(BASE_URL, auth=AUTH, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {
                "zones_fc": d.get("icu_hr_zones"),
                "fc_max": d.get("icu_resting_hr"),
                "seuil_lthr": d.get("icu_lthr"),
                "poids": d.get("weight")
            }
    except Exception:
        pass
    return {}

def fetch_wellness_complet():
    today = datetime.date.today().isoformat()
    oldest = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE_URL}/wellness", auth=AUTH, params={"oldest": oldest, "newest": today}, timeout=10)
        if r.status_code == 200:
            wellness = r.json()
            if isinstance(wellness, list):
                res = []
                for j in wellness:
                    if j.get("sleepSecs") or j.get("hrv") or j.get("ctl"):
                        res.append({
                            "date": j.get("id"),
                            "sommeil_h": round(j.get("sleepSecs", 0) / 3600, 1) if j.get("sleepSecs") else None,
                            "score_sommeil": j.get("sleepScore"),
                            "vfc": j.get("hrv"),
                            "vfc_base": j.get("hrvBaseline"),
                            "fc_repos": j.get("restingHR"),
                            "tsb": j.get("form"),
                            "atl": j.get("atl"),
                            "ctl": j.get("ctl")
                        })
                return res
    except Exception:
        pass
    return []

def fetch_activites_enrichies():
    oldest = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(f"{BASE_URL}/activities", auth=AUTH, params={"oldest": oldest}, timeout=10)
        if r.status_code == 200:
            activites = r.json()
            if isinstance(activites, list):
                res = []
                for act in activites[:70]:
                    v_ms = act.get("average_speed", 0)
                    allure = f"{int((1000/v_ms)//60)}'{int((1000/v_ms)%60):02d}\"/km" if v_ms and v_ms > 0 else None
                    res.append({
                        "id": act.get("id"),
                        "date": act.get("start_date_local", "")[:10],
                        "nom": act.get("name"),
                        "km": round(act.get("distance", 0) / 1000, 2),
                        "duree_min": round(act.get("moving_time", 0) / 60, 1),
                        "allure": allure,
                        "fc_moy": act.get("average_heartrate"),
                        "fc_max": act.get("max_heartrate"),
                        "charge": act.get("icu_training_load"),
                        "decouplage": act.get("icu_decoupling")
                    })
                return res
    except Exception:
        pass
    return []

def fetch_seances_planifiees():
    today = datetime.date.today().isoformat()
    dans_13_semaines = (datetime.date.today() + datetime.timedelta(days=91)).isoformat()
    try:
        r = requests.get(f"{BASE_URL}/events", auth=AUTH, params={"oldest": today, "newest": dans_13_semaines}, timeout=8)
        if r.status_code == 200:
            events = r.json()
            if isinstance(events, list):
                return [{
                    "id": e.get("id"),
                    "date": e.get("start_date_local", "")[:10],
                    "nom": e.get("name"),
                    "description": e.get("description")
                } for e in events]
    except Exception:
        pass
    return []

def get_toutes_les_donnees():
    f_prof = EXECUTOR.submit(fetch_profil_athlete)
    f_well = EXECUTOR.submit(fetch_wellness_complet)
    f_acts = EXECUTOR.submit(fetch_activites_enrichies)
    f_plan = EXECUTOR.submit(fetch_seances_planifiees)
    return f_prof.result(), f_well.result(), f_acts.result(), f_plan.result()

def modifier_ou_creer_seance(date_str: str, titre: str, description_workout: str, event_id: int = None) -> str:
    headers = {"Content-Type": "application/json"}
    payload = {
        "category": "WORKOUT",
        "type": "Run",
        "name": titre,
        "description": description_workout,
        "start_date_local": f"{date_str}T08:00:00"
    }
    try:
        if event_id:
            r = requests.put(f"{BASE_URL}/events/{event_id}", auth=AUTH, headers=headers, json=payload, timeout=8)
        else:
            r = requests.post(f"{BASE_URL}/events", auth=AUTH, headers=headers, json=payload, timeout=8)
        if r.status_code in (200, 201):
            return f"Séance '{titre}' enregistrée sur Intervals.icu pour le {date_str}."
        return f"Erreur Intervals.icu ({r.status_code})"
    except Exception as e:
        return f"Erreur : {e}"

def generer_analyse(prompt_parts, tools=None):
    models = ["gemini-2.5-flash", "gemini-3.8-flash"]
    for model_name in models:
        try:
            cfg = types.GenerateContentConfig(tools=tools, temperature=0.3) if tools else types.GenerateContentConfig(temperature=0.3)
            resp = ai_client.models.generate_content(model=model_name, contents=prompt_parts, config=cfg)
            if tools and resp.function_calls:
                call = resp.function_calls[0]
                tool_res = modifier_ou_creer_seance(**call.args)
                hist = list(prompt_parts) if isinstance(prompt_parts, list) else [prompt_parts]
                hist.append(resp.candidates[0].content)
                part_resp = types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": tool_res})
                hist.append(types.Content(role="user", parts=[part_resp]))
                suivi = ai_client.models.generate_content(model=model_name, contents=hist)
                return suivi.text
            if resp and resp.text:
                return resp.text
        except Exception:
            time.sleep(1)
            continue
    return "Service Google temporairement indisponible."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    return f"""Tu es l'entraîneur personnel d'athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif : 5 km sub-20 (Course du Lake Boga le 13/12/2026, cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC, Découplage, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES :
1. Analyse croisée : 6 mois passés + nuit/forme du jour + 13 semaines à venir.
2. FORMAT HTML TELEGRAM : <b>Texte en gras</b> pour les titres et allures, lignes vides entre chaque point, émojis sobres (📊, 🫀, 🎯). Pas de dièses (#) ni d'astérisques (**).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        profil, sante, activites, planifiees = get_toutes_les_donnees()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f'{contexte}\n\nMESSAGE ATHLÈTE :\n"{update.message.text}"'
        texte = generer_analyse(prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = get_toutes_les_donnees()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]
        texte = generer_analyse(prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur vocale : {e}")

def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees()
            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                if not is_activity_processed(act_id):
                    mark_activity_processed(act_id)
                    contexte = construire_contexte_global(profil, sante, activites, planifiees)
                    prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."
                    texte = generer_analyse(prompt)
                    requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_h")
                if date_nuit and sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                    mark_wellness_processed(date_nuit)
                    contexte = construire_contexte_global(profil, sante, activites, planifiees)
                    prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."
                    texte = generer_analyse(prompt)
                    requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass
        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach...")
    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
ensure_ascii=False

SÉANCES SUR 6 MOIS (Allures, FC, Découplage, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES :
1. Analyse croisée : 6 mois passés + nuit/forme du jour + 13 semaines à venir.
2. FORMAT HTML TELEGRAM : <b>Texte en gras</b> pour les titres et allures, lignes vides entre chaque point, émojis sobres (📊, 🫀, 🎯). Pas de dièses (#) ni d'astérisques (**).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        profil, sante, activites, planifiees = get_toutes_les_donnees()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f'{contexte}\n\nMESSAGE ATHLÈTE :\n"{update.message.text}"'
        texte = generer_analyse(prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = get_toutes_les_donnees()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]
        texte = generer_analyse(prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur vocale : {e}")

def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees()
            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                if not is_activity_processed(act_id):
                    mark_activity_processed(act_id)
                    contexte = construire_contexte_global(profil, sante, activites, planifiees)
                    prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."
                    texte = generer_analyse(prompt)
                    requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                if date_nuit and sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                    mark_wellness_processed(date_nuit)
                    contexte = construire_contexte_global(profil, sante, activites, planifiees)
                    prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."
                    texte = generer_analyse(prompt)
                    requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)
        except Exception:
            pass
        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach...")
    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
urn resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC moy/max, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides entre chaque section.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM NON-BLOQUANTS ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        profil, sante, activites, planifiees = get_toutes_les_donnees_sync()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f'{contexte}\n\nMESSAGE ATHLÈTE :\n"{update.message.text}"'
        texte = generer_analyse_sync(prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur technique : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = get_toutes_les_donnees_sync()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]
        texte = generer_analyse_sync(prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur vocale : {e}")

# --- SURVEILLANCE ARRIÈRE-PLAN ---
def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees_sync()

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

        except Exception as e:
            print(f"Erreur surveillance worker: {e}")

        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach avec architecture robuste...")

    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
odels.generate_content(
                        model=model,
                        contents=[
                            *parts_suivi,
                            resp.candidates[0].content,
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_function_response(
                                        name="modifier_ou_creer_seance",
                                        response={"result": res_tool}
                                    )
                                ]
                            )
                        ]
                    )
                    return suivi.text

                if resp and resp.text:
                    return resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC moy/max, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides entre chaque section.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM NON-BLOQUANTS ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        profil, sante, activites, planifiees = await asyncio.to_thread(get_toutes_les_donnees_sync)
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f'{contexte}\n\nMESSAGE ATHLÈTE :\n"{update.message.text}"'
        texte = await asyncio.to_thread(generer_analyse_sync, prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur technique : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = await asyncio.to_thread(get_toutes_les_donnees_sync)
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]
        texte = await asyncio.to_thread(generer_analyse_sync, prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur vocale : {e}")

# --- SURVEILLANCE ARRIÈRE-PLAN ---
def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees_sync()

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

        except Exception as e:
            print(f"Erreur surveillance worker: {e}")

        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach avec architecture robuste...")

    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
                    response={"result": res_tool}
                                    )
                                ]
                            )
                        ]
                    )
                    return suivi.text

                if resp and resp.text:
                    return resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC moy/max, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides entre chaque section.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        profil, sante, activites, planifiees = get_toutes_les_donnees_sync()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f"{contexte}\n\nMESSAGE ATHLÈTE :\n\"{update.message.text}\""
        
        texte = generer_analyse_sync(prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur technique : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = get_toutes_les_donnees_sync()
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]

        texte = generer_analyse_sync(prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️️ Erreur vocale : {e}")

# --- SURVEILLANCE ARRIÈRE-PLAN ---
def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees_sync()

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."""
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."""
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

        except Exception as e:
            print(f"Erreur surveillance worker: {e}")

        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach avec syntaxe corrigée...")
    
    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()


            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC moy/max, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides entre chaque section.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM TOTALEMENT NON-BLOQUANTS ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        # Exécution dans un thread séparé pour ne PAS figer Telegram
        profil, sante, activites, planifiees = await asyncio.to_thread(get_toutes_les_donnees_sync)
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        prompt = f"{contexte}\n\nMESSAGE ATHLÈTE :\n\"{update.message.text}\""
        
        texte = await asyncio.to_thread(generer_analyse_sync, prompt, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur technique : {e}")

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        audio_buffer = io.BytesIO()
        await file.download_to_memory(audio_buffer)
        audio_bytes = audio_buffer.getvalue()

        profil, sante, activites, planifiees = await asyncio.to_thread(get_toutes_les_donnees_sync)
        contexte = construire_contexte_global(profil, sante, activites, planifiees)
        
        prompt_parts = [
            contexte,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
        ]

        texte = await asyncio.to_thread(generer_analyse_sync, prompt_parts, [modifier_ou_creer_seance])
        await envoyer_reponse(update.effective_chat.id, texte, context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur vocale : {e}")

# --- SURVEILLANCE ARRIÈRE-PLAN ---
def background_surveillance_worker():
    time.sleep(30)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees_sync()

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."""
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."""
                        texte = generer_analyse_sync(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

        except Exception as e:
            print(f"Erreur surveillance worker: {e}")

        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach avec boucle asynchrone sécurisée...")
    
    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
rom_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                        ]
                    )
                    return suivi.text

                if resp and resp.text:
                    return resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC moy/max, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides entre chaque section.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    profil, sante, activites, planifiees = get_toutes_les_donnees()
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE ATHLÈTE :\n\"{update.message.text}\""
    
    texte = generer_analyse(prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    profil, sante, activites, planifiees = get_toutes_les_donnees()
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
    ]

    texte = generer_analyse(prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- THREAD DE FOND PROACTIF (SURVEILLANCE TOUTES LES 15 MIN) ---
def background_surveillance_worker():
    time.sleep(20)
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    while True:
        try:
            profil, sante, activites, planifiees = get_toutes_les_donnees()

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébriefe cette séance."""
                        texte = generer_analyse(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."""
                        texte = generer_analyse(prompt)
                        requests.post(send_url, json={"chat_id": TELEGRAM_USER_ID, "text": f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", "parse_mode": "HTML"}, timeout=10)

        except Exception as e:
            print(f"Erreur surveillance worker: {e}")

        time.sleep(900)

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach avec architecture robuste...")
    
    # Lancement du worker d'arrière-plan en thread démon indépendant
    t = threading.Thread(target=background_surveillance_worker, daemon=True)
    t.start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
arts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                        ]
                    )
                    return suivi.text

                if resp and resp.text:
                    return resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(1.5)
                    continue
                break
    return "Service temporairement indisponible côté Google."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur personnel en athlétisme de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False)}

PHYSIOLOGIE SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False)}

SÉANCES SUR 6 MOIS (Allures, FC, Découplage %, Cadence, Charge) :
{json.dumps(activites, ensure_ascii=False)}

PLAN SUR 13 SEMAINES (Cycle complet du calendrier) :
{json.dumps(planifiees, ensure_ascii=False)}

DIRECTIVES DE RÉPONSE :
1. Analyse croisée : recul sur les 6 mois passés + forme et sommeil du jour + plan sur 13 semaines.
2. FORMAT HTML TELEGRAM OBLIGATOIRE :
   - Pas de #, pas de **.
   - Utilise <b>Texte en gras</b> pour les titres, allures et métriques clés.
   - Utilise <i>Texte en italique</i> si besoin.
   - Aère avec des lignes vides.
   - Émojis sobres (📊, 🫀, 🎯).
3. Si un fait durable est mentionné, écris en fin de message :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE ATHLÈTE :\n\"{update.message.text}\""
    
    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
    ]

    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- SURVEILLANCE ARRIÈRE-PLAN ---
async def background_surveillance(bot):
    await asyncio.sleep(20)
    loop = asyncio.get_running_loop()
    while True:
        try:
            profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False)}\nDébrief express."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données enregistrées :\n{json.dumps(derniere_sante, ensure_ascii=False)}\nBrief matinal."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur surveillance: {e}")

        await asyncio.sleep(900)

async def demarrer_taches(app):
    asyncio.create_task(background_surveillance(app.bot))

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach complet...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(demarrer_taches).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
 tools else types.GenerateContentConfig(temperature=0.3)
                resp = ai_client.models.generate_content(
                    model=model,
                    contents=prompt_parts,
                    config=config
                )

                if tools and resp.function_calls:
                    call = resp.function_calls[0]
                    res_tool = modifier_ou_creer_seance(**call.args)
                    suivi = ai_client.models.generate_content(
                        model=model,
                        contents=[
                            prompt_parts if isinstance(prompt_parts, list) else [prompt_parts],
                            resp.candidates[0].content,
                            types.Content(role="user", parts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                        ]
                    )
                    return suivi.text

                if resp and resp.text:
                    return resp.text

            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep((attempt + 1) * 2)
                    continue
                break
    return "Service temporairement indisponible."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=30)
    
    return f"""Tu es l'entraîneur d'athlétisme personnel et expert de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU (Base persistante SQLite) :
{json.dumps(memoire, ensure_ascii=False, indent=2)}

PHYSIOLOGIE & SANTÉ SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB, Courbatures, Fatigue) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 6 DERNIERS MOIS (jusqu'à 100 séances : Allures, FC moy/max, Découplage %, Cadence, Zones FC, RPE, D+) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

PLAN PRÉVISIONNEL SUR 13 SEMAINES (Cycle complet dans le calendrier) :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

DIRECTIVES DE COACHING :
1. Analyse macro & micro : Tu as le recul sur tout le cycle (reprise, charge passée, évolution du CTL) et sur les 13 semaines à venir. Croise la périodisation globale avec la fraîcheur du moment (sommeil/VFC).
2. Syntaxe Workout : Si tu proposes ou programmes une séance via l'outil `modifier_ou_creer_seance`, respecte la syntaxe officielle d'Intervals.icu (ex: - 15m 65-75% HR \\n - 5x 1000m 3:55-4:00/km recovery 2m \\n - 10m Z1).
3. MISE EN FORME HTML TELEGRAM OBLIGATOIRE :
   - N'utilise AUCUN dièse (#) ni astérisques bruts (**). Utilise exclusivement les balises HTML compatibles :
     • <b>Texte en gras</b> pour les titres, allures et métriques clés.
     • <i>Texte en italique</i> pour les explications physiologiques.
   - Aère très généreusement ta réponse avec des lignes vides entre chaque point.
   - Utilise des émojis discrets en tête de section (📊 pour l'état des lieux, 🫀 pour la physiologie/récupération, 🎯 pour les conseils/prescriptions).
4. Si l'athlète te communique une donnée clé pérenne (douleur, ressenti d'effort anormal, contrainte), écris en toute fin de message :
[MEMOIRE] information exacte à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    
    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Analyse ses propos avec rigueur et réponds-lui selon sa physiologie complète."
    ]

    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- SURVEILLANCE ARRIÈRE-PLAN ---
async def background_surveillance(bot):
    await asyncio.sleep(30)
    loop = asyncio.get_running_loop()
    while True:
        try:
            profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False, indent=2)}\nFais un débrief immédiat et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données physiologiques enregistrées par Garmin :\n{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}\nFais un brief matinal direct et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur boucle surveillance: {e}")

        await asyncio.sleep(900)

async def post_init_corrigee(application):
    # Démarrage de la boucle de fond dans le lifecycle officiel
    asyncio.create_task(background_surveillance(application.bot))

if __name__ == "__main__":
    init_db()
    print("Démarrage du coach complet sans crash...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init_corrigee).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
erate_content(
                model="gemini-3.8-flash",
                contents=prompt_parts,
                config=config
            )

            if tools and resp.function_calls:
                call = resp.function_calls[0]
                res_tool = modifier_ou_creer_seance(**call.args)
                suivi = ai_client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=[
                        prompt_parts if isinstance(prompt_parts, list) else [prompt_parts],
                        resp.candidates[0].content,
                        types.Content(role="user", parts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                    ]
                )
                return suivi.text

            if resp and resp.text:
                return resp.text

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                time.sleep((attempt + 1) * 2)
                continue
            return f"Erreur technique : {e}"

    return "Service Google temporairement surchargé. Réessaie dans un instant."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur d'athlétisme personnel et expert de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False, indent=2)}

PHYSIOLOGIE & SANTÉ SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 6 DERNIERS MOIS (jusqu'à 100 séances : Allures, FC, Découplage, Cadence, Zones FC) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

PLAN PRÉVISIONNEL SUR 13 SEMAINES :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

DIRECTIVES DE COACHING :
1. Analyse chirurgicale et globale (croise le macro-cycle et l'état de fraîcheur du jour).
2. Syntaxe Workout si modification : syntaxe Intervals.icu standard.
3. FORMAT HTML TELEGRAM : <b>gras</b> pour titres/allures/chiffres clés, pas de dièses (#) ni d'astérisques bruts (**). Aère avec des lignes vides. Émojis sobres (📊, 🫀, 🎯).
4. Si l'athlète partage un fait pérenne (douleur, ressenti clé), finis par :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    
    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Analyse ses propos et réponds-lui selon sa physiologie complète."
    ]

    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- SURVEILLANCE ARRIÈRE-PLAN ---
async def background_surveillance(bot):
    await asyncio.sleep(30)
    loop = asyncio.get_running_loop()
    while True:
        try:
            profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False, indent=2)}\nFais un débrief immédiat et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}).\n{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}\nFais un brief matinal direct et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur boucle surveillance: {e}")

        await asyncio.sleep(900)

async def demarrer_taches(app):
    asyncio.create_task(background_surveillance(app.bot))

if __name__ == "__main__":
    init_db()
    print("Démarrage du bot coach...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(demarrer_taches).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    # run_polling gère la boucle de réception Telegram de façon 100% native
    app.run_polling(drop_pending_updates=True)
erate_content(
                model="gemini-3.8-flash",
                contents=prompt_parts,
                config=config
            )

            if tools and resp.function_calls:
                call = resp.function_calls[0]
                res_tool = modifier_ou_creer_seance(**call.args)
                suivi = ai_client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=[
                        prompt_parts if isinstance(prompt_parts, list) else [prompt_parts],
                        resp.candidates[0].content,
                        types.Content(role="user", parts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                    ]
                )
                return suivi.text

            if resp and resp.text:
                return resp.text

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                time.sleep((attempt + 1) * 2)
                continue
            return f"Erreur technique : {e}"

    return "Service Google temporairement surchargé. Réessaie dans un instant."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur d'athlétisme personnel et expert de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False, indent=2)}

PHYSIOLOGIE & SANTÉ SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 6 DERNIERS MOIS (jusqu'à 100 séances : Allures, FC, Découplage, Cadence, Zones FC) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

PLAN PRÉVISIONNEL SUR 13 SEMAINES :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

DIRECTIVES DE COACHING :
1. Analyse chirurgicale et globale (croise le macro-cycle et l'état de fraîcheur du jour).
2. Syntaxe Workout si modification : syntaxe Intervals.icu standard.
3. FORMAT HTML TELEGRAM : <b>gras</b> pour titres/allures/chiffres clés, pas de dièses (#) ni d'astérisques bruts (**). Aère avec des lignes vides. Émojis sobres (📊, 🫀, 🎯).
4. Si l'athlète partage un fait pérenne (douleur, ressenti clé), finis par :
[MEMOIRE] note à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    
    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Analyse ses propos et réponds-lui selon sa physiologie complète."
    ]

    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- SURVEILLANCE ARRIÈRE-PLAN ---
async def background_surveillance(bot):
    await asyncio.sleep(30)
    loop = asyncio.get_running_loop()
    while True:
        try:
            profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)

            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False, indent=2)}\nFais un débrief immédiat et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}).\n{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}\nFais un brief matinal direct et concis."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur boucle surveillance: {e}")

        await asyncio.sleep(900)

async def main():
    init_db()
    print("Démarrage du bot autonome...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))

    async with app:
        await app.start()
        await app.updater.start_polling()
        # Lancement de la boucle de fond dans le runtime asyncio stabilisé
        asyncio.create_task(background_surveillance(app.bot))
        # Maintien en vie du conteneur sans bloquer
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
ur Intervals.icu ({r.status_code}) : {r.text}"
    except Exception as e:
        return f"Erreur lors de la programmation de la séance : {e}"

# --- MOTEUR GEMINI ---
def generer_analyse(prompt_parts, tools=None):
    for attempt in range(4):
        try:
            config = types.GenerateContentConfig(tools=tools, temperature=0.3) if tools else types.GenerateContentConfig(temperature=0.3)
            resp = ai_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt_parts,
                config=config
            )

            if tools and resp.function_calls:
                call = resp.function_calls[0]
                res_tool = modifier_ou_creer_seance(**call.args)
                suivi = ai_client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=[
                        prompt_parts if isinstance(prompt_parts, list) else [prompt_parts],
                        resp.candidates[0].content,
                        types.Content(role="user", parts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res_tool})])
                    ]
                )
                return suivi.text

            if resp and resp.text:
                return resp.text

        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str:
                time.sleep((attempt + 1) * 2)
                continue
            return f"Erreur technique de l'assistant : {e}"

    return "Service Google temporairement surchargé. Merci de réessayer dans quelques instants."

def construire_contexte_global(profil, sante, activites, planifiees):
    memoire = get_recent_memory_notes(limit=25)
    
    return f"""Tu es l'entraîneur d'athlétisme personnel et expert de ce coureur.
Date du jour : {datetime.date.today().isoformat()}.
Objectif prioritaire : 5 km sub-20 (Course du Lake Boga le 13/12/2026, allure cible 3'59/km).

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU (Base persistante SQLite) :
{json.dumps(memoire, ensure_ascii=False, indent=2)}

PHYSIOLOGIE & SANTÉ SUR 6 MOIS (Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB, Courbatures, Fatigue) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 6 DERNIERS MOIS (jusqu'à 100 séances : Allures, FC moy/max, Découplage %, Cadence, Zones FC, RPE, D+) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

PLAN PRÉVISIONNEL SUR 13 SEMAINES (Cycle complet dans le calendrier) :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

DIRECTIVES STRICTES DE COACHING :
1. Analyse macro & micro : Tu as le recul sur tout le cycle (reprise, charge passée, évolution du CTL) et sur les 13 semaines à venir. Croise la périodisation globale avec la fraîcheur du moment (sommeil/VFC).
2. Syntaxe Workout : Si tu proposes ou programmes une séance via l'outil `modifier_ou_creer_seance`, respecte la syntaxe officielle d'Intervals.icu (ex: - 15m 65-75% HR \\n - 5x 1000m 3:55-4:00/km recovery 2m \\n - 10m Z1).
3. MISE EN FORME HTML TELEGRAM OBLIGATOIRE :
   - N'utilise AUCUN dièse (#) ni astérisques bruts (**). Utilise exclusivement les balises HTML compatibles :
     • <b>Texte en gras</b> pour les titres, allures et métriques clés.
     • <i>Texte en italique</i> pour les explications physiologiques.
   - Aère très généreusement ta réponse avec des lignes vides entre chaque point.
   - Utilise des émojis discrets en tête de section (📊 pour l'état des lieux, 🫀 pour la physiologie/récupération, 🎯 pour les conseils/prescriptions).
4. Si l'athlète te communique une donnée clé pérenne (douleur, ressenti d'effort anormal, contrainte), écris en toute fin de message :
[MEMOIRE] information exacte à enregistrer
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    
    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    loop = asyncio.get_running_loop()
    profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)
    contexte = construire_contexte_global(profil, sante, activites, planifiees)
    
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Analyse ses propos avec rigueur et réponds-lui selon sa physiologie complète."
    ]

    texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt_parts, [modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- PROACTIVITÉ (SURVEILLANCE TOUTES LES 15 MIN) ---
async def background_surveillance(bot):
    await asyncio.sleep(25)
    loop = asyncio.get_running_loop()
    while True:
        try:
            profil, sante, activites, planifiees = await loop.run_in_executor(EXECUTOR, get_toutes_les_donnees)

            # 1. Détection de nouvelle séance terminée
            if activites:
                derniere = activites[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    if cursor.fetchone()[0] == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : L'athlète vient d'enregistrer une NOUVELLE séance sportive !\n{json.dumps(derniere, ensure_ascii=False, indent=2)}\nFais un débrief immédiat, chaleureux mais rigoureux de cette séance."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            # 2. Détection du réveil et de la nuit
            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_heures")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    if cursor.fetchone()[0] == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global(profil, sante, activites, planifiees)
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données physiologiques enregistrées par Garmin :\n{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}\nFais un brief matinal percutant et aéré."""
                        texte = await loop.run_in_executor(EXECUTOR, generer_analyse, prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur boucle surveillance: {e}")

        await asyncio.sleep(900)

async def post_init(application):
    asyncio.create_task(background_surveillance(application.bot))

# --- LANCEMENT APPLICATION ---
if __name__ == "__main__":
    init_db()
    print("Démarrage du coach avec cycle macro (6 mois d'historique & 13 semaines de plan)...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
sure_ascii=False, indent=2)}

RÈGLES STRICTES DE MISE EN FORME :
- Format HTML Telegram UNIQUEMENT (<b>gras</b> pour titres/chiffres, <i>italique</i> si besoin).
- Aéré, percutant, pas de pavé indigeste. Laisse des lignes vides entre chaque point.
- Émojis sobres (📊, 🫀, 🎯).
- Si une information durable est donnée (douleur, ressenti clé, imprévu), finis par :
[MEMOIRE] note à conserver
"""

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- HANDLERS TELEGRAM ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    contexte = construire_contexte_global()
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    texte = generer_analyse(prompt, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    contexte = construire_contexte_global()
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Analyse ses propos et réponds-lui avec précision."
    ]

    texte = generer_analyse(prompt_parts, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

# --- BOUCLE DE FOND ASYNCHRONE (PROACTIVITÉ SANS JOBQUEUE) ---
async def background_surveillance(bot):
    await asyncio.sleep(30)  # Pause initiale après démarrage
    while True:
        try:
            # 1. Vérification nouvelle séance
            acts = get_activites_enrichies(limit=2)
            if acts:
                derniere = acts[0]
                act_id = str(derniere.get("id"))
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_activities")
                    count = cursor.fetchone()[0]
                    if count == 0:
                        mark_activity_processed(act_id)
                    elif not is_activity_processed(act_id):
                        mark_activity_processed(act_id)
                        contexte = construire_contexte_global()
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Nouvelle séance détectée !\n{json.dumps(derniere, ensure_ascii=False, indent=2)}\nFais un débrief concis et précis."""
                        texte = generer_analyse(prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

            # 2. Vérification nouvelle nuit Garmin
            sante = get_wellness_complet(jours=2)
            if sante:
                derniere_sante = sante[0]
                date_nuit = derniere_sante.get("date")
                sommeil = derniere_sante.get("sommeil_h")
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM processed_wellness")
                    count = cursor.fetchone()[0]
                    if count == 0:
                        if date_nuit:
                            mark_wellness_processed(date_nuit)
                    elif sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
                        mark_wellness_processed(date_nuit)
                        contexte = construire_contexte_global()
                        prompt = f"""{contexte}\n\nÉVÉNEMENT PROACTIF : Réveil ({date_nuit}). Données physiologiques reçues :\n{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}\nFais un brief matinal direct et concis."""
                        texte = generer_analyse(prompt)
                        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}", parse_mode=ParseMode.HTML)

        except Exception as e:
            print(f"Erreur boucle surveillance: {e}")

        await asyncio.sleep(900)  # Vérification toutes les 15 minutes

async def post_init(application):
    asyncio.create_task(background_surveillance(application.bot))

# --- LANCEMENT DE L'APPLICATION ---
if __name__ == "__main__":
    init_db()
    print("Démarrage du bot autonome...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))
    app.run_polling()
ENTRAÎNEMENT (Allures, FC, Découplage, Cadence) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

RÈGLES STRICTES DE MISE EN FORME :
- Format HTML Telegram UNIQUEMENT (<b>gras</b> pour titres/chiffres, <i>italique</i> si besoin).
- Aéré, percutant, pas de pavé indigeste. Laisse des lignes vides entre chaque point.
- Émojis sobres (📊, 🫀, 🎯).
- Si une information durable est donnée (douleur, ressenti clé, imprévu), finis par :
[MEMOIRE] note à conserver
"""

# --- GESTION DES MESSAGES UTILISATEUR ---
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    contexte = construire_contexte_global()
    prompt = f"{contexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    texte = generer_analyse(prompt, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    contexte = construire_contexte_global()
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Écoute son ressenti, analyse ses propos et réponds-lui avec précision."
    ]

    texte = generer_analyse(prompt_parts, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- PROACTIVITÉ (TÂCHES DE FOND TOUTES LES 15 MIN) ---
async def job_debrief_post_seance(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie toutes les 15 minutes si une nouvelle séance Garmin a été injectée sur Intervals.icu."""
    acts = get_activites_enrichies(limit=2)
    if not acts:
        return
    derniere = acts[0]
    act_id = str(derniere.get("id"))

    # Initialisation au tout premier lancement pour ne pas débriefer l'historique ancien
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_activities")
        count = cursor.fetchone()[0]
        if count == 0:
            mark_activity_processed(act_id)
            return

    if not is_activity_processed(act_id):
        mark_activity_processed(act_id)
        contexte = construire_contexte_global()
        prompt = f"""{contexte}

ÉVÉNEMENT PROACTIF : L'athlète vient d'enregistrer une NOUVELLE séance sportive !
Détails de la séance :
{json.dumps(derniere, ensure_ascii=False, indent=2)}

Mission : Fais un débrief immédiat, chaleureux mais rigoureux de cette séance (analyse de l'allure, régularité cardiaque, découplage, respect des zones). Donne les consignes de récupération immédiate."""
        
        texte = generer_analyse(prompt)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

async def job_check_nouveau_sommeil(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie si les données de sommeil de la dernière nuit viennent d'arriver depuis Garmin."""
    sante = get_wellness_complet(jours=2)
    if not sante:
        return
    
    derniere_sante = sante[0]
    date_nuit = derniere_sante.get("date")
    sommeil = derniere_sante.get("sommeil_h")

    # Initialisation au tout premier lancement
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_wellness")
        count = cursor.fetchone()[0]
        if count == 0:
            if date_nuit:
                mark_wellness_processed(date_nuit)
            return

    # Si Garmin a bien renseigné le sommeil et qu'il n'a pas encore été analysé
    if sommeil and sommeil > 0 and not is_wellness_processed(date_nuit):
        mark_wellness_processed(date_nuit)
        
        contexte = construire_contexte_global()
        prompt = f"""{contexte}

ÉVÉNEMENT PROACTIF : Les données de sommeil et de VFC de la nuit ({date_nuit}) viennent d'arriver depuis Garmin !
Détails physiologiques du réveil :
{json.dumps(derniere_sante, ensure_ascii=False, indent=2)}

Mission : Fais un brief matinal percutant et aéré (durée/score sommeil, VFC vs baseline habituelle, forme TSB).
Valide la séance prévue aujourd'hui ou préconise un ajustement immédiat selon sa récupération réelle."""

        texte = generer_analyse(prompt)
        await context.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=f"☀️ <b>Réveil détecté · Métriques physiologiques</b>\n\n{texte}",
            parse_mode=ParseMode.HTML
        )

# --- LANCEMENT DE L'APPLICATION ---
if __name__ == "__main__":
    init_db()
    print("Démarrage du coach augmenté (Mémoire SQLite, Vocal natif & Détection proactive)...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers messages texte et audio vocal
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))

    # Tâches planifiées proactives (boucles de fond toutes les 15 minutes)
    job_queue = app.job_queue
    if job_queue:
        # Surveillance de nouvelle séance de course
        job_queue.run_repeating(job_debrief_post_seance, interval=900, first=60)
        # Surveillance de nouvelle nuit Garmin
        job_queue.run_repeating(job_check_nouveau_sommeil, interval=900, first=90)

    app.run_polling()
ntexte}\n\nMESSAGE DE L'ATHLÈTE :\n\"{update.message.text}\""
    texte = generer_analyse(prompt, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def handle_message_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    audio_buffer = io.BytesIO()
    await file.download_to_memory(audio_buffer)
    audio_bytes = audio_buffer.getvalue()

    contexte = construire_contexte_global()
    prompt_parts = [
        contexte,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
        "Voici le message vocal de l'athlète. Écoute son ressenti, analyse ses propos et réponds-lui avec précision."
    ]

    texte = generer_analyse(prompt_parts, tools=[modifier_ou_creer_seance])
    await envoyer_reponse(update.effective_chat.id, texte, context.bot)

async def envoyer_reponse(chat_id, texte, bot):
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        save_memory_note(note)
    else:
        reponse_user = texte

    try:
        await bot.send_message(chat_id=chat_id, text=reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=reponse_user)

# --- PROACTIVITÉ (CRON & TÂCHES DE FOND) ---
async def job_debrief_post_seance(context: ContextTypes.DEFAULT_TYPE):
    """Vérifie toutes les 15 minutes si une nouvelle séance Garmin a été injectée sur Intervals.icu."""
    acts = get_activites_enrichies(limit=2)
    if not acts:
        return
    derniere = acts[0]
    act_id = str(derniere.get("id"))

    # Initialisation au premier lancement pour ne pas débriefer le passé
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM processed_activities")
        count = cursor.fetchone()[0]
        if count == 0:
            mark_activity_processed(act_id)
            return

    if not is_activity_processed(act_id):
        mark_activity_processed(act_id)
        contexte = construire_contexte_global()
        prompt = f"""{contexte}

ÉVÉNEMENT PROACTIF : L'athlète vient d'enregistrer une NOUVELLE séance sportive !
Détails de la séance :
{json.dumps(derniere, ensure_ascii=False, indent=2)}

Mission : Fais un débrief immédiat, chaleureux mais rigoureux de cette séance (analyse de l'allure, régularité cardiaque, découplage, respect des zones). Donne les consignes de récupération immédiate."""
        
        texte = generer_analyse(prompt)
        await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"🏁 <b>Nouvelle séance détectée !</b>\n\n{texte}", parse_mode=ParseMode.HTML)

async def job_brief_matinal(context: ContextTypes.DEFAULT_TYPE):
    """Envoie un point métabolique chaque matin à 7h30."""
    contexte = construire_contexte_global()
    prompt = f"""{contexte}

ÉVÉNEMENT PROACTIF : C'est le réveil (brief matinal).
Mission : Analyse la nuit qui vient de se terminer (durée, score sommeil, VFC du matin vs baseline habituelle, TSB).
Valide ou alerte l'athlète sur la capacité du jour à encaisser l'entraînement prévu."""
    
    texte = generer_analyse(prompt)
    await context.bot.send_message(chat_id=TELEGRAM_USER_ID, text=f"☀️ <b>Brief Forme & Sommeil du Jour</b>\n\n{texte}", parse_mode=ParseMode.HTML)

# --- LANCEMENT APPLICATION ---
if __name__ == "__main__":
    init_db()
    print("Démarrage du coach augmenté (Mémoire SQLite, Reconnaissance Vocale & Proactivité)...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers messages texte et audio vocal
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_message_voice))

    # Tâches planifiées proactives
    job_queue = app.job_queue
    if job_queue:
        # Vérifie l'arrivée d'une nouvelle séance toutes les 15 minutes (900 secondes)
        job_queue.run_repeating(job_debrief_post_seance, interval=900, first=60)
        # Brief matinal quotidien à 07h30 (heure locale)
        job_queue.run_daily(job_brief_matinal, time=datetime.time(hour=7, minute=30))

    app.run_polling()
t
    memoire = charger_memoire()
    
    profil = get_profil_athlete()
    activites = get_activites_enrichies(limit=30)
    sante = get_wellness_complet(jours=21)
    planifiees = get_seances_planifiees()
    date_du_jour = datetime.date.today().isoformat()

    prompt = f"""Tu es l'entraîneur d'athlétisme personnel de ce coureur.
Date d'aujourd'hui : {date_du_jour}.

Tu as accès complet à sa télémétrie sportive et physiologique Intervals.icu.
Tu disposes aussi de l'outil `modifier_ou_creer_seance` pour programmer ou ajuster directement des séances dans son calendrier Intervals.icu / Garmin.

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU :
{memoire['profil']}
Derniers faits marquants : {json.dumps(memoire['notes_historique'][-10:], ensure_ascii=False)}

PHYSIOLOGIE & SANTÉ (3 semaines : Sommeil, VFC rMSSD, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 4 DERNIERS MOIS (Allures, FC, Découplage, Cadence, Zones FC) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

SÉANCES ACTUELLEMENT PLANIFIÉES DANS LE CALENDRIER :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

MESSAGE DE L'ATHLÈTE :
"{message}"

CONSIGNES DE MODIFICATION / PLANIFICATION :
- Si l'athlète te demande d'adapter, déplacer ou créer une séance (ou si tu juges sur sa physiologie qu'un allègement/reprogrammation est indispensable et qu'il le sollicite) :
  -> Appelle la fonction `modifier_ou_creer_seance`.
  -> Dans `description_workout`, utilise la syntaxe officielle d'Intervals.icu pour structurer le workout (ex:
     - 15m 65-75% HR
     - 5x 1000m 3:55-4:00/km recovery 2m 60-70% HR
     - 10m 60-70% HR
  -> Si c'est une séance existante du calendrier, fournis son `event_id`.

CONSIGNES STRICTES DE FORMATAGE (HTML OBLIGATOIRE) :
- N'utilise PAS de Markdown (*, **, #). Utilise STRICTEMENT des balises HTML supportées par Telegram :
  • <b>Texte en gras</b> pour les titres, allures et métriques clés.
  • <i>Texte en italique</i> pour les explications physiologiques.
- Rends le message AÉRÉ avec des sauts de ligne réguliers.
- Utilise des émojis discrets en tête de section (📊, 🫀, 🎯).
- Si une information durable est partagée par l'athlète, écris en toute fin :
[MEMOIRE] note précise à enregistrer
"""
    texte = appel_gemini_avec_outils(prompt)

    reponse_user = texte
    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        memoire["notes_historique"].append({"date": datetime.date.today().isoformat(), "note": note})
        sauvegarder_memoire(memoire)

    try:
        await update.message.reply_text(reponse_user, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(reponse_user)

if __name__ == "__main__":
    print("Démarrage du bot coach avec capacité de planification et synchronisation Garmin...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
