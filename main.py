import os
import json
import time
import datetime
import requests
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "0").strip() or "0"
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_API_KEY)
BASE_URL = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}"

MEMORY_FILE = "coach_memory.json"

def charger_memoire():
    if not os.path.exists(MEMORY_FILE):
        return {
            "profil": "Coureur préparant un 5 km sub-20 (Course du Lake Boga le 13/12/2026). Allure cible 3'59/km.",
            "notes_historique": []
        }
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"profil": "Coureur 5 km sub-20 (Lake Boga)", "notes_historique": []}

def sauvegarder_memoire(data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_profil_athlete():
    try:
        r = requests.get(BASE_URL, auth=AUTH, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "zones_fc": data.get("icu_hr_zones"),
                "fc_max_enregistree": data.get("icu_resting_hr"),
                "seuil_lactique_fc": data.get("icu_lthr"),
                "poids_reference": data.get("weight")
            }
    except Exception as e:
        print(f"Erreur profil: {e}")
    return {}

def get_activites_enrichies(limit=30):
    url = f"{BASE_URL}/activities"
    oldest = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest}, timeout=15)
        if r.status_code == 200:
            activites = r.json()
            if not isinstance(activites, list):
                return []
            activites_recentes = sorted(activites, key=lambda x: x.get('start_date_local', ''), reverse=True)
            resume = []
            for act in activites_recentes[:limit]:
                vitesse_ms = act.get("average_speed", 0)
                allure_str = None
                if vitesse_ms and vitesse_ms > 0:
                    sec_per_km = 1000 / vitesse_ms
                    allure_str = f"{int(sec_per_km // 60)}'{int(sec_per_km % 60):02d}\"/km"

                resume.append({
                    "date": act.get("start_date_local", "")[:10],
                    "nom": act.get("name"),
                    "type": act.get("type"),
                    "distance_km": round(act.get("distance", 0) / 1000, 2),
                    "duree_min": round(act.get("moving_time", 0) / 60, 1),
                    "allure_moyenne": allure_str,
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
            return resume
    except Exception as e:
        print(f"Erreur activites: {e}")
    return []

def get_wellness_complet(jours=21):
    today = datetime.date.today().isoformat()
    oldest = (datetime.date.today() - datetime.timedelta(days=jours)).isoformat()
    url = f"{BASE_URL}/wellness"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": oldest, "newest": today}, timeout=15)
        if r.status_code == 200:
            wellness = r.json()
            if not isinstance(wellness, list):
                return []
            wellness_recents = sorted(wellness, key=lambda x: x.get('id', ''), reverse=True)
            resume_sante = []
            for j in wellness_recents:
                resume_sante.append({
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
                })
            return resume_sante
    except Exception as e:
        print(f"Erreur wellness: {e}")
    return []

def get_seances_planifiees():
    today = datetime.date.today().isoformat()
    dans_14_jours = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
    url = f"{BASE_URL}/events"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": today, "newest": dans_14_jours}, timeout=10)
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

# Outil d'écriture Intervals.icu : création ou mise à jour de séance
def modifier_ou_creer_seance(date_str: str, titre: str, description_workout: str, event_id: int = None) -> str:
    """Modifie une séance existante ou en programme une nouvelle sur Intervals.icu."""
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
        else:
            return f"Erreur Intervals.icu ({r.status_code}) : {r.text}"
    except Exception as e:
        return f"Erreur lors de la mise à jour de la séance : {e}"

def appel_gemini_avec_outils(prompt):
    modeles = ["gemini-3.8-flash", "gemini-2.5-flash"]
    for model_name in modeles:
        for attempt in range(3):
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[modifier_ou_creer_seance],
                        temperature=0.3
                    )
                )

                # Si l'IA déclenche l'outil de modification de séance
                if response.function_calls:
                    tool_call = response.function_calls[0]
                    args = tool_call.args
                    resultat_tool = modifier_ou_creer_seance(
                        date_str=args.get("date_str"),
                        titre=args.get("titre"),
                        description_workout=args.get("description_workout"),
                        event_id=args.get("event_id")
                    )
                    # Relance avec le résultat de l'outil pour obtenir la confirmation finale
                    suivi = ai_client.models.generate_content(
                        model=model_name,
                        contents=[
                            prompt,
                            response.candidates[0].content,
                            types.Content(
                                role="user",
                                parts=[types.Part.from_function_response(
                                    name="modifier_ou_creer_seance",
                                    response={"resultat": resultat_tool}
                                )]
                            )
                        ]
                    )
                    return suivi.text

                if response and response.text:
                    return response.text

            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    time.sleep((attempt + 1) * 2)
                    continue
                break
    return "Service temporairement indisponible côté Google. Réessaie dans un instant."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    message = update.message.text
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
