import os
import json
import time
import datetime
import requests
from requests.auth import HTTPBasicAuth
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "0").strip() or "0"
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
MEMORY_FILE = "coach_memory.json"

def charger_memoire():
    if not os.path.exists(MEMORY_FILE):
        return {
            "profil": "Coureur visant un 10 km sous les 40 min. Entraînement sérieux, régulier, avec renforcement musculaire.",
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

def get_dernieres_activites(limit=30):
    url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}/activities"
    auth = HTTPBasicAuth("API_KEY", INTERVALS_API_KEY)
    
    # Historique élargi aux 120 derniers jours (4 mois)
    oldest = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
    params = {"oldest": oldest}
    
    try:
        r = requests.get(url, auth=auth, params=params, timeout=15)
        if r.status_code == 200:
            activites = r.json()
            if not isinstance(activites, list):
                return []
            activites_recentes = sorted(activites, key=lambda x: x.get('start_date_local', ''), reverse=True)
            resume = []
            for act in activites_recentes[:limit]:
                resume.append({
                    "date": act.get("start_date_local", "")[:10],
                    "nom": act.get("name"),
                    "type": act.get("type"),
                    "distance_km": round(act.get("distance", 0) / 1000, 2),
                    "duree_min": round(act.get("moving_time", 0) / 60, 1),
                    "fc_moyenne": act.get("average_heartrate"),
                    "charge_icu": act.get("icu_training_load"),
                    "decouplage_cardiaque": act.get("icu_decoupling")
                })
            return resume
        else:
            return [{"erreur_intervals": f"Code HTTP {r.status_code}"}]
    except Exception as e:
        return [{"erreur_connexion": str(e)}]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    message = update.message.text
    memoire = charger_memoire()
    # Récupère jusqu'aux 30 dernières séances
    activites = get_dernieres_activites(limit=30)

    prompt = f"""Tu es un entraîneur d'athlétisme expert en course de fond (approche polarisée 80/20, gestion fine du seuil LT2 et de la charge).
Analyse les retours de l'athlète avec rigueur, franchise et précision chiffrée. Pas de blabla inutile.

PROFIL DU COUREUR :
{memoire['profil']}
Dernières notes clés conservées en mémoire : {json.dumps(memoire['notes_historique'][-10:], ensure_ascii=False)}

HISTORIQUE DES SÉANCES (jusqu'à 30 séances sur 4 mois) :
{json.dumps(activites, ensure_ascii=False, indent=2)}

MESSAGE DE L'ATHLÈTE :
"{message}"

Consignes :
1. Analyse les tendances de fond sur les semaines et mois disponibles (progression du volume, allure en endurance, dérives cardiaques, charge cumulée).
2. Si une nouvelle information clé apparaît (blessure, ressenti marquant, nouveau test chrono), ajoute à la fin de ta réponse une ligne sous la forme :
[MEMOIRE] information à retenir
"""
    # Gestion automatique des erreurs 503 avec 2 tentatives
    texte = None
    for attempt in range(2):
        try:
            response = ai_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            texte = response.text
            break
        except Exception as e:
            if "503" in str(e) and attempt == 0:
                time.sleep(2)
                continue
            texte = f"Erreur d'analyse : {e}"

    if texte and "[MEMOIRE]" in texte:
        parts = texte.split("[MEMOIRE]")
        reponse_user = parts[0].strip()
        note = parts[1].strip().split("\n")[0]
        memoire["notes_historique"].append({"date": datetime.date.today().isoformat(), "note": note})
        sauvegarder_memoire(memoire)
        await update.message.reply_text(reponse_user)
    elif texte:
        await update.message.reply_text(texte)

if __name__ == "__main__":
    print("Démarrage du bot coach...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
