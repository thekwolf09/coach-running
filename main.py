import os
import json
import datetime
import requests
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID")
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0"))

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

def get_dernieres_activites(limit=3):
    url = f"https://intervals.icu/api/v1/athlete/{INTERVALS_ATHLETE_ID}/activities"
    headers = {"Authorization": f"Bearer {INTERVALS_API_KEY}"}
    oldest = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    try:
        r = requests.get(url, headers=headers, params={"oldest": oldest})
        if r.status_code == 200:
            activites = r.json()
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
    except Exception as e:
        print(f"Erreur API Intervals: {e}")
    return []

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
1. Réponds de façon précise et physiologique.
2. Si une nouvelle information clé apparaît (blessure, sensation, chrono test), ajoute à la fin de ta réponse une ligne sous la forme :
[MEMOIRE] information à retenir
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-3.1-pro-preview",
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
