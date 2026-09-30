import os
import io
import json
import time
import sqlite3
import datetime
import requests
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

# --- PERSISTANCE SQLITE ---
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
        conn.commit()

def save_memory_note(note_text: str, category: str = "general"):
    today = datetime.date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO memory_notes (date, category, content) VALUES (?, ?, ?)", (today, category, note_text))
        conn.commit()

def get_recent_memory_notes(limit=15):
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

# --- API INTERVALS.ICU ---
def get_profil_athlete():
    try:
        r = requests.get(BASE_URL, auth=AUTH, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {
                "zones_fc": d.get("icu_hr_zones"),
                "fc_max": d.get("icu_resting_hr"),
                "seuil_lthr": d.get("icu_lthr")
            }
    except Exception as e:
        print(f"Erreur profil: {e}")
    return {}

def get_activites_enrichies(limit=25):
    url = f"{BASE_URL}/activities"
    oldest = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest}, timeout=15)
        if r.status_code == 200:
            acts = r.json()
            if not isinstance(acts, list):
                return []
            acts_sorted = sorted(acts, key=lambda x: x.get('start_date_local', ''), reverse=True)
            res = []
            for act in acts_sorted[:limit]:
                v_ms = act.get("average_speed", 0)
                allure = f"{int((1000/v_ms)//60)}'{int((1000/v_ms)%60):02d}\"/km" if v_ms and v_ms > 0 else None
                res.append({
                    "id": act.get("id"),
                    "date": act.get("start_date_local", "")[:16],
                    "nom": act.get("name"),
                    "type": act.get("type"),
                    "distance_km": round(act.get("distance", 0) / 1000, 2),
                    "duree_min": round(act.get("moving_time", 0) / 60, 1),
                    "allure_moy": allure,
                    "fc_moy": act.get("average_heartrate"),
                    "fc_max": act.get("max_heartrate"),
                    "cadence": act.get("average_cadence"),
                    "charge_icu": act.get("icu_training_load"),
                    "decouplage_pct": act.get("icu_decoupling")
                })
            return res
    except Exception as e:
        print(f"Erreur activities: {e}")
    return []

def get_wellness_complet(jours=14):
    today = datetime.date.today().isoformat()
    oldest = (datetime.date.today() - datetime.timedelta(days=jours)).isoformat()
    url = f"{BASE_URL}/wellness"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest, "newest": today}, timeout=15)
        if r.status_code == 200:
            well = r.json()
            if isinstance(well, list):
                well_sorted = sorted(well, key=lambda x: x.get('id', ''), reverse=True)
                return [{
                    "date": j.get("id"),
                    "sommeil_h": round(j.get("sleepSecs", 0) / 3600, 1) if j.get("sleepSecs") else None,
                    "score_sommeil": j.get("sleepScore"),
                    "vfc_rmssd": j.get("hrv"),
                    "vfc_baseline": j.get("hrvBaseline"),
                    "fc_repos": j.get("restingHR"),
                    "forme_tsb": j.get("form"),
                    "fatigue_atl": j.get("atl"),
                    "condition_ctl": j.get("ctl"),
                    "stress": j.get("stress"),
                    "courbatures": j.get("soreness")
                } for j in well_sorted]
    except Exception as e:
        print(f"Erreur wellness: {e}")
    return []

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
            return f"Séance '{titre}' enregistrée sur Intervals.icu pour le {date_str}."
        return f"Erreur Intervals.icu ({r.status_code}) : {r.text}"
    except Exception as e:
        return f"Erreur d'enregistrement : {e}"

# --- MOTEUR GEMINI ROBUSTE ---
def generer_analyse(prompt_parts, tools=None):
    models = ["gemini-3.8-flash", "gemini-2.5-flash"]
    config = types.GenerateContentConfig(tools=tools, temperature=0.3) if tools else types.GenerateContentConfig(temperature=0.3)
    for model in models:
        for attempt in range(3):
            try:
                resp = ai_client.models.generate_content(model=model, contents=prompt_parts, config=config)
                if tools and resp.function_calls:
                    call = resp.function_calls[0]
                    res = modifier_ou_creer_seance(**call.args)
                    suivi = ai_client.models.generate_content(
                        model=model,
                        contents=[
                            prompt_parts if isinstance(prompt_parts, list) else [prompt_parts],
                            resp.candidates[0].content,
                            types.Content(role="user", parts=[types.Part.from_function_response(name="modifier_ou_creer_seance", response={"result": res})])
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

def construire_contexte_global():
    memoire = get_recent_memory_notes(limit=15)
    sante = get_wellness_complet(jours=14)
    activites = get_activites_enrichies(limit=20)
    profil = get_profil_athlete()
    
    return f"""Tu es l'entraîneur d'athlétisme personnel expert de ce coureur (objectif 5 km sub-20 / allures 3'59/km).
Date actuelle : {datetime.date.today().isoformat()}.

PROFIL ATHLÈTE & SEUILS :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE DURABLE (Base SQLite) :
{json.dumps(memoire, ensure_ascii=False, indent=2)}

PHYSIOLOGIE & SANTÉ (14 derniers jours : Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES RÉCENTES D'ENTRAÎNEMENT (Allures, FC, Découplage, Cadence) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

RÈGLES STRICTES DE MISE EN FORME :
- Format HTML Telegram UNIQUEMENT (<b>gras</b> pour titres/chiffres, <i>italique</i> si besoin).
- Aéré, percutant, pas de pavé indigeste.
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
