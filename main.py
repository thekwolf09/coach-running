import os
import json
import datetime
import requests
from requests.auth import HTTPBasicAuth
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "0").strip()
if not INTERVALS_ATHLETE_ID:
    INTERVALS_ATHLETE_ID = "0"

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

def get_dernieres_activites(limit=5):
    # L'ID '0' est le raccourci officiel Intervals.icu pour l'athlète lié à la clé
    athlete_id = INTERVALS_ATHLETE_ID if INTERVALS_ATHLETE_ID != "0" else "0"
    url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities"
    
    # Authentification HTTP Basic : username='API_KEY', password=ta_cle
    auth = HTTPBasicAuth("API_KEY", INTERVALS_API_KEY)
    
    # Recherche large sur les 90 derniers jours sans date de fin stricte
    oldest = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    params = {"oldest": oldest}
    
    try:
        r = requests.get(url, auth=auth, params=params, timeout=10)
        if r.status_code == 200:
            activites = r.json()
            if not isinstance(activites, list):
                return []
            activites_recentes = sorted(activites, key=lambda x: x.get('start_date_local', ''), reverse=True)
            resume = []
            for act in activites_recentes[:limit]:
                resume.append({
                    "date": act.get("start_date_local"),
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
            print(f"Erreur API Intervals : status {r.status_code} - {r.text}")
            return [{"erreur_intervals": f"Code HTTP {r.status_code}: {r.text}"}]
    except Exception as e:
        print(f"Erreur connexion Intervals: {e}")
        return [{"erreur_connexion": str(e)}]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    message = update.message.text
    memoire = charger_memoire()
    activites = get_dernieres_activites()

    prompt = f"""Tu es un entraîneur d'athlétisme expert en course de fond (approche polarisée 80/20, gestion fine du seuil LT2 et de la charge).
Analyse les retours de l'athlète avec rigueur, franchise et précision chiffrée. Pas de blabla inutile.

PROFIL :
{memoire['profil']}
Dernières notes clés : {json.dumps(memoire['notes_historique'][-5:], ensure_ascii=False)}

DERNIÈRES SÉANCES DÉTECTÉES SUR INTERVALS.ICU :
{json.dumps(activites, ensure_ascii=False, indent=2)}

MESSAGE DE L'ATHLÈTE :
"{message}"

Consignes :
1. Si des activités sont présentes dans le JSON, analyse-les précisément.
2. Si une erreur technique apparaît dans le JSON (ex: 'erreur_intervals'), signale-la clairement.
3. Si une nouvelle information clé apparaît (blessure, sensation, chrono test), ajoute à la fin de ta réponse une ligne sous la forme :
[MEMOIRE] information à retenir
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        )
        texte = response.text
        if "[MEMOIRE]" in texte:
            parts = texte.split("[MEMOIRE]")
            reponse_user = parts[0].strip()
            note = parts[1].strip().split("\n")[0]
            memoire["notes_historique"].append({"date": datetime.date.today().isoformat(), "note": note})
            sauvegarder_memoire(memoire)
            await update.message.reply_text(reponse_user)
        else:
            await update.message.reply_text(texte)
    except Exception as e:
        await update.message.reply_text(f"Erreur d'analyse : {e}")

if __name__ == "__main__":
    print("Démarrage du bot coach...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
