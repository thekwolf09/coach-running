import os
import json
import time
import datetime
import requests
from requests.auth import HTTPBasicAuth
from google import genai
from telegram import Update
from telegram.constants import ChatAction
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
            "profil": "Coureur préparant un 10 km sous les 40 min. Pratique régulière, renforcement musculaire intégré.",
            "notes_historique": []
        }
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"profil": "Coureur 10 km sous 40 min", "notes_historique": []}

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
    dans_7_jours = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
    url = f"{BASE_URL}/events"
    try:
        r = requests.get(url, auth=AUTH, params={"oldest": today, "newest": dans_7_jours}, timeout=10)
        if r.status_code == 200:
            events = r.json()
            if isinstance(events, list):
                return [{"date": e.get("start_date_local", "")[:10], "nom": e.get("name"), "description": e.get("description")} for e in events]
    except Exception as e:
        print(f"Erreur events: {e}")
    return []

def appel_gemini_robuste(prompt):
    modeles = ["gemini-3.8-flash", "gemini-2.5-flash"]
    for model_name in modeles:
        for attempt in range(4):
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                err_str = str(e)
                # En cas de 503 (surcharge), on patiente de plus en plus longtemps
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    wait_time = (attempt + 1) * 2  # 2s, 4s, 6s...
                    time.sleep(wait_time)
                    continue
                # Si erreur autre, on tente directement le modèle suivant
                break
    return "Le service d'analyse est actuellement saturé côté Google. Merci de réessayer dans 30 secondes."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    # Affiche le statut "en train d'écrire..." sur Telegram
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    message = update.message.text
    memoire = charger_memoire()
    
    profil = get_profil_athlete()
    activites = get_activites_enrichies(limit=30)
    sante = get_wellness_complet(jours=21)
    planifiees = get_seances_planifiees()

    prompt = f"""Tu es l'entraîneur d'athlétisme personnel de ce coureur.
Tu as accès à l'ensemble de sa télémétrie sportive et physiologique Intervals.icu.
Approche : Rigoureuse, basée sur les données réelles (modèle 80/20 polarisé, seuils LT1/LT2, découplage aérobie, charge Bannister/Coggan). Pas de blabla superficiel.

PROFIL ATHLÈTE & ZONES :
{json.dumps(profil, ensure_ascii=False)}

MÉMOIRE & HISTORIQUE CONTINU :
{memoire['profil']}
Derniers faits marquants enregistrés : {json.dumps(memoire['notes_historique'][-10:], ensure_ascii=False)}

PHYSIOLOGIE & SANTÉ (3 dernières semaines : Sommeil, VFC rMSSD vs Baseline, FC repos, ATL, CTL, TSB, Courbatures) :
{json.dumps(sante, ensure_ascii=False, indent=2)}

SÉANCES DES 4 DERNIERS MOIS (Allures, FC moy/max, Découplage %, Cadence, Temps par zone FC, RPE) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

SÉANCES ACTUELLEMENT PLANIFIÉES (7 prochains jours) :
{json.dumps(planifiees, ensure_ascii=False, indent=2)}

MESSAGE DE L'ATHLÈTE :
"{message}"

Directives :
1. Croise systématiquement les données (sommeil/VFC avec FC de séance, TSB/ATL avec charge des séances, allure vs zone cible).
2. Donne un avis direct, franc, précis et chiffré.
3. Si l'athlète te partage une information durable (douleur, ressenti d'effort, événement, modification d'objectif), inscris-la en fin de réponse sous la forme :
[MEMOIRE] note précise à enregistrer
"""
    texte = appel_gemini_robuste(prompt)

    if "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        memoire["notes_historique"].append({"date": datetime.date.today().isoformat(), "note": note})
        sauvegarder_memoire(memoire)
        await update.message.reply_text(reponse_user)
    else:
        await update.message.reply_text(texte)

if __name__ == "__main__":
    print("Démarrage du bot coach avec télémétrie complète et gestion robuste des erreurs...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
