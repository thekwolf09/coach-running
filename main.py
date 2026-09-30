import os
import io
import json
import time
import asyncio
import sqlite3
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# --- CONFIGURATION ENVIRONNEMENT ---
INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "0").strip() or "0"
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_API_KEY)
BASE_URL = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"

DB_FILE = "coach_brain.db"
EXECUTOR = ThreadPoolExecutor(max_workers=5)

# --- BASE DE DONNÉES PERSISTANTE (SQLITE) ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                category TEXT,
                content TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_activities (
                activity_id TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_wellness (
                wellness_date TEXT PRIMARY KEY,
                processed_at TEXT
            )
        """)
        conn.commit()

def save_memory_note(note_text: str, category: str = "general"):
    today = datetime.date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO memory_notes (date, category, content) VALUES (?, ?, ?)", (today, category, note_text))
        conn.commit()

def get_recent_memory_notes(limit=25):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT date, content FROM memory_notes ORDER BY id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        return [{"date": r[0], "note": r[1]} for r in reversed(rows)]

def is_activity_processed(act_id: str) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_activities WHERE activity_id = ?", (str(act_id),))
        return cursor.fetchone() is not None

def mark_activity_processed(act_id: str):
    now = datetime.datetime.now().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO processed_activities (activity_id, processed_at) VALUES (?, ?)", (str(act_id), now))
        conn.commit()

def is_wellness_processed(date_str: str) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_wellness WHERE wellness_date = ?", (str(date_str),))
        return cursor.fetchone() is not None

def mark_wellness_processed(date_str: str):
    now = datetime.datetime.now().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO processed_wellness (wellness_date, processed_at) VALUES (?, ?)", (str(date_str), now))
        conn.commit()

# --- APPELS TÉLÉMÉTRIQUES INTERVALS.ICU EN PARALLÈLE ---
def fetch_profil_athlete():
    try:
        r = requests.get(BASE_URL, auth=AUTH, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {
                "zones_fc": d.get("icu_hr_zones"),
                "fc_max_enregistree": d.get("icu_resting_hr"),
                "seuil_lactique_lthr": d.get("icu_lthr"),
                "poids_reference": d.get("weight")
            }
    except Exception as e:
        print(f"Erreur profil: {e}")
    return {}

def fetch_wellness_complet():
    # 6 mois complets de données physiologiques (180 jours)
    today = datetime.date.today().isoformat()
    oldest = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    url = f"{BASE_URL}/wellness"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest, "newest": today}, timeout=15)
        if r.status_code == 200:
            wellness = r.json()
            if isinstance(wellness, list):
                wellness_sorted = sorted(wellness, key=lambda x: x.get('id', ''), reverse=True)
                return [{
                    "date": j.get("id"),
                    "sommeil_heures": round(j.get("sleepSecs", 0) / 3600, 1) if j.get("sleepSecs") else None,
                    "score_sommeil": j.get("sleepScore"),
                    "qualite_sommeil": j.get("sleepQuality"),
                    "vfc_rmssd": j.get("hrv"),
                    "vfc_baseline": j.get("hrvBaseline"),
                    "fc_repos": j.get("restingHR"),
                    "forme_tsb": j.get("form"),
                    "fatigue_aigue_atl": j.get("atl"),
                    "condition_physique_ctl": j.get("ctl"),
                    "stress": j.get("stress"),
                    "courbatures_soreness": j.get("soreness"),
                    "fatigue_percue": j.get("fatigue"),
                    "poids_kg": j.get("weight"),
                    "hydratation_l": j.get("water")
                } for j in wellness_sorted]
    except Exception as e:
        print(f"Erreur wellness: {e}")
    return []

def fetch_activites_enrichies():
    # 6 mois complets d'historique de séances (180 jours, jusqu'à 100 séances)
    url = f"{BASE_URL}/activities"
    oldest = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest}, timeout=20)
        if r.status_code == 200:
            activites = r.json()
            if isinstance(activites, list):
                activites_sorted = sorted(activites, key=lambda x: x.get('start_date_local', ''), reverse=True)
                res = []
                for act in activites_sorted[:100]:
                    v_ms = act.get("average_speed", 0)
                    allure = f"{int((1000/v_ms)//60)}'{int((1000/v_ms)%60):02d}\"/km" if v_ms and v_ms > 0 else None
                    res.append({
                        "id": act.get("id"),
                        "date": act.get("start_date_local", "")[:16],
                        "nom": act.get("name"),
                        "type": act.get("type"),
                        "distance_km": round(act.get("distance", 0) / 1000, 2),
                        "duree_min": round(act.get("moving_time", 0) / 60, 1),
                        "allure_moyenne": allure,
                        "fc_moyenne": act.get("average_heartrate"),
                        "fc_max": act.get("max_heartrate"),
                        "cadence_moyenne": act.get("average_cadence"),
                        "denivele_d_plus": act.get("total_elevation_gain"),
                        "charge_icu": act.get("icu_training_load"),
                        "decouplage_cardiaque_pct": act.get("icu_decoupling"),
                        "repartition_zones_fc_sec": act.get("icu_hr_zone_times"),
                        "ressenti_rpe": act.get("perceived_exertion"),
                        "commentaires": act.get("description")
                    })
                return res
    except Exception as e:
        print(f"Erreur activites: {e}")
    return []

def fetch_seances_planifiees():
    # 13 semaines de prévisionnel (91 jours)
    today = datetime.date.today().isoformat()
    dans_13_semaines = (datetime.date.today() + datetime.timedelta(days=91)).isoformat()
    url = f"{BASE_URL}/events"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": today, "newest": dans_13_semaines}, timeout=15)
        if r.status_code == 200:
            events = r.json()
            if isinstance(events, list):
                return [{
                    "id": e.get("id"),
                    "date": e.get("start_date_local", "")[:10],
                    "nom": e.get("name"),
                    "description": e.get("description")
                } for e in events]
    except Exception as e:
        print(f"Erreur events: {e}")
    return []

def get_toutes_les_donnees():
    """Récupère les 6 mois d'historique et les 13 semaines de prévisionnel en simultané."""
    f_profil = EXECUTOR.submit(fetch_profil_athlete)
    f_well = EXECUTOR.submit(fetch_wellness_complet)
    f_acts = EXECUTOR.submit(fetch_activites_enrichies)
    f_plan = EXECUTOR.submit(fetch_seances_planifiees)
    return f_profil.result(), f_well.result(), f_acts.result(), f_plan.result()

# --- PLANIFICATION / TOOL INTERVALS.ICU ---
def modifier_ou_creer_seance(date_str: str, titre: str, description_workout: str, event_id: int = None) -> str:
    """Modifie ou crée une séance planifiée dans le calendrier Intervals.icu / Garmin."""
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
            url = f"{BASE_URL}/events/{event_id}"
            r = requests.put(url, auth=AUTH, headers=headers, json=payload, timeout=10)
        else:
            url = f"{BASE_URL}/events"
            r = requests.post(url, auth=AUTH, headers=headers, json=payload, timeout=10)
        if r.status_code in (200, 201):
            return f"Séance '{titre}' enregistrée avec succès sur Intervals.icu pour le {date_str}."
        return f"Erreur Intervals.icu ({r.status_code}) : {r.text}"
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
