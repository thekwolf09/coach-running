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
POOL = ThreadPoolExecutor(max_workers=5)

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, txt TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, txt TEXT, d TEXT)")
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

def save_chat_msg(role: str, text: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (role, txt, d) VALUES (?, ?, ?)", (role, text, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_chat_history(limit: int = 6):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT role, txt FROM chat_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "text": r[1]} for r in reversed(rows)]

def fetch_profile():
    try:
        r = requests.get(BASE, auth=AUTH, timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {
                "zones": d.get("icu_hr_zones"),
                "lthr": d.get("icu_lthr"),
                "weight": d.get("weight"),
                "city": d.get("city"),
                "country": d.get("country"),
                "timezone": d.get("timezone")
            }
    except Exception as err:
        logging.error(f"Erreur fetch_profile: {err}")
    return {}

def fetch_weather(city_name: str = ""):
    target_city = city_name.strip() or "Melbourne"
    try:
        geo_r = requests.get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={target_city}&count=1&language=fr&format=json",
            timeout=6
        )
        if geo_r.status_code == 200 and geo_r.json().get("results"):
            geo = geo_r.json()["results"][0]
            lat, lon = geo["latitude"], geo["longitude"]
            nom_ville = geo.get("name", target_city)

            w_url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,apparent_temperature,precipitation,wind_speed_10m"
                f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                f"&timezone=auto"
            )
            w_r = requests.get(w_url, timeout=6)
            if w_r.status_code == 200:
                data = w_r.json()
                cur = data.get("current", {})
                daily = data.get("daily", {})
                return {
                    "ville": nom_ville,
                    "temp_actuelle": f"{cur.get('temperature_2m')}°C",
                    "ressenti": f"{cur.get('apparent_temperature')}°C",
                    "vent": f"{cur.get('wind_speed_10m')} km/h",
                    "pluie_actuelle": f"{cur.get('precipitation')} mm",
                    "temp_max_jour": f"{daily.get('temperature_2m_max', ['?'])[0]}°C",
                    "temp_min_jour": f"{daily.get('temperature_2m_min', ['?'])[0]}°C",
                    "probabilite_pluie": f"{daily.get('precipitation_probability_max', ['0'])[0]}%"
                }
    except Exception as err:
        logging.error(f"Erreur fetch_weather: {err}")
    return {"info": "Météo non disponible"}

def fetch_wellness():
    today = datetime.date.today()
    d90 = (today - datetime.timedelta(days=90)).isoformat()
    today_iso = today.isoformat()
    try:
        r = requests.get(f"{BASE}/wellness", auth=AUTH, params={"oldest": d90, "newest": today_iso}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
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
    today = datetime.date.today()
    d180 = (today - datetime.timedelta(days=180)).isoformat()
    d90 = (today - datetime.timedelta(days=90)).isoformat()
    try:
        r = requests.get(f"{BASE}/activities", auth=AUTH, params={"oldest": d180}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            raw.sort(key=lambda x: str(x.get("start_date_local", "")), reverse=True)
            
            raw_3months = []
            older_acts = []

            for a in raw:
                d_str = str(a.get("start_date_local", ""))[:10]
                spd = a.get("average_speed") or 0
                dist = a.get("distance") or 0
                mtime = a.get("moving_time") or 0
                pace = f"{int((1000/spd)//60)}'{int((1000/spd)%60):02d}\"/km" if spd > 0 else None
                item = {
                    "id": a.get("id"),
                    "d": d_str,
                    "nom": a.get("name"),
                    "km": round(dist / 1000, 2),
                    "min": round(mtime / 60, 1),
                    "pace": pace,
                    "hr": a.get("average_heartrate"),
                    "load": a.get("icu_training_load")
                }
                if d_str >= d90:
                    raw_3months.append(item)
                else:
                    older_acts.append(item)

            weeks = {}
            for a in older_acts:
                dt = datetime.datetime.strptime(a["d"], "%Y-%m-%d").date()
                w_key = f"{dt.year}-S{dt.isocalendar()[1]:02d}"
                if w_key not in weeks:
                    weeks[w_key] = {"km": 0.0, "min": 0.0, "load": 0, "count": 0}
                weeks[w_key]["km"] += a["km"]
                weeks[w_key]["min"] += a["min"]
                weeks[w_key]["load"] += (a["load"] or 0)
                weeks[w_key]["count"] += 1

            weekly_summary = [
                {
                    "semaine": k,
                    "seances": v["count"],
                    "km_total": round(v["km"], 1),
                    "temps_h": round(v["min"] / 60, 1),
                    "charge_totale": v["load"]
                }
                for k, v in sorted(weeks.items(), reverse=True)
            ]

            return {
                "brut_3_mois": raw_3months,
                "agregat_semaines_3_a_6_mois": weekly_summary
            }
    except Exception as err:
        logging.error(f"Erreur fetch_activities: {err}")
    return {"brut_3_mois": [], "agregat_semaines_3_a_6_mois": []}

def fetch_events():
    today = datetime.date.today()
    past_180 = (today - datetime.timedelta(days=180)).isoformat()
    fut_90 = (today + datetime.timedelta(days=90)).isoformat()
    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": past_180, "newest": fut_90}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            raw.sort(key=lambda x: str(x.get("start_date_local", "")))
            return [{
                "id": e.get("id"),
                "d": str(e.get("start_date_local", ""))[:10],
                "nom": e.get("name"),
                "desc": e.get("description"),
                "category": e.get("category")
            } for e in raw if e.get("category") == "WORKOUT"]
    except Exception as err:
        logging.error(f"Erreur fetch_events: {err}")
    return []

def get_all_data():
    prof = fetch_profile()
    city = prof.get("city") or "Melbourne"
    f_weather = POOL.submit(fetch_weather, city)
    f_well = POOL.submit(fetch_wellness)
    f_acts = POOL.submit(fetch_activities)
    f_evts = POOL.submit(fetch_events)
    return prof, f_weather.result(), f_well.result(), f_acts.result(), f_evts.result()

def parse_relative_date(date_str: str) -> str:
    s = str(date_str).lower().strip()
    today = datetime.date.today()
    if any(k in s for k in ["demain", "tomorrow"]):
        return (today + datetime.timedelta(days=1)).isoformat()
    if any(k in s for k in ["hier", "yesterday"]):
        return (today - datetime.timedelta(days=1)).isoformat()
    if any(k in s for k in ["today", "aujourd", "soir", "ce soir", "ce jour"]):
        return today.isoformat()
    return s.split("T")[0]

def planifier_seance(date_str: str = "", titre: str = "", description: str = "", heure: str = "18:00", **kwargs) -> str:
    """Planifie une séance d'entraînement sur Intervals.icu.
    Args:
        date_str: Date au format AAAA-MM-JJ (ex: '2026-10-01' ou 'demain').
        titre: Titre court de la séance.
        description: Consignes d'allures, blocs et zones cibles.
        heure: Heure au format HH:MM (défaut '18:00').
    """
    d_val = date_str or kwargs.get("date") or kwargs.get("start_date") or datetime.date.today().isoformat()
    t_val = titre or kwargs.get("title") or kwargs.get("name") or "Séance Course"
    desc_val = description or kwargs.get("desc") or kwargs.get("details") or f"Séance : {t_val}"
    target_date = parse_relative_date(d_val)
    clean_hour = heure if ":" in str(heure) else "18:00"

    payload = {
        "category": "WORKOUT",
        "type": "Run",
        "name": t_val,
        "description": desc_val,
        "start_date_local": f"{target_date}T{clean_hour}:00"
    }
    try:
        r = requests.post(f"{BASE}/events", auth=AUTH, headers={"Content-Type": "application/json"}, json=payload, timeout=8)
        if r.status_code in (200, 201):
            return f"Séance '{payload['name']}' planifiée le {target_date} à {clean_hour} sur Intervals.icu."
        return f"Erreur Intervals.icu ({r.status_code}) : {r.text}"
    except Exception as err:
        return f"Erreur connexion Intervals : {err}"

def supprimer_seance(date_str: str = "", titre: str = "", **kwargs) -> str:
    """Supprime une séance planifiée sur Intervals.icu.
    Args:
        date_str: Date de la séance (ex: '2026-09-30', 'today' ou 'demain').
        titre: Titre ou mot clé de la séance (optionnel).
    """
    d_val = date_str or kwargs.get("date") or kwargs.get("target_date") or datetime.date.today().isoformat()
    target_date = parse_relative_date(d_val)

    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": target_date, "newest": target_date}, timeout=8)
        if r.status_code != 200:
            return f"Impossible d'accéder au calendrier Intervals.icu ({r.status_code})."

        events = [e for e in r.json() if e.get("category") == "WORKOUT"]
        if not events:
            return f"Aucune séance trouvée sur Intervals.icu le {target_date}."

        target_event = None
        t_clean = (titre or kwargs.get("name") or kwargs.get("title") or "").strip().lower()
        if t_clean:
            for ev in events:
                if t_clean in str(ev.get("name", "")).lower():
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
        return f"Erreur connexion Intervals : {err}"

def deplacer_seance(date_origine: str = "", date_cible: str = "", titre: str = "", heure: str = "18:00", **kwargs) -> str:
    """Déplace une séance existante vers une autre date sur Intervals.icu.
    Args:
        date_origine: Date actuelle de la séance (ex: 'today' ou '2026-09-30').
        date_cible: Nouvelle date souhaitée (ex: 'demain' ou '2026-10-01').
        titre: Titre ou mot clé de la séance à déplacer (optionnel).
        heure: Heure cible au format HH:MM (défaut '18:00').
    """
    d_orig = parse_relative_date(date_origine or kwargs.get("date_source") or datetime.date.today().isoformat())
    d_dest = parse_relative_date(date_cible or kwargs.get("target_date") or kwargs.get("date_destination") or "")
    clean_hour = heure if ":" in str(heure) else "18:00"

    if not d_dest:
        return "Précise vers quelle date déplacer la séance."

    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": d_orig, "newest": d_orig}, timeout=8)
        if r.status_code != 200:
            return f"Erreur lecture calendrier ({r.status_code})."

        events = [e for e in r.json() if e.get("category") == "WORKOUT"]
        if not events:
            return f"Aucune séance trouvée à déplacer le {d_orig}."

        target_event = None
        t_clean = (titre or kwargs.get("name") or kwargs.get("title") or "").strip().lower()
        if t_clean:
            for ev in events:
                if t_clean in str(ev.get("name", "")).lower():
                    target_event = ev
                    break

        if not target_event:
            target_event = events[0]

        ev_id = target_event.get("id")
        ev_nom = target_event.get("name", "Séance")
        payload = {
            "start_date_local": f"{d_dest}T{clean_hour}:00"
        }
        put_r = requests.put(f"{BASE}/events/{ev_id}", auth=AUTH, json=payload, timeout=8)
        if put_r.status_code in (200, 204):
            return f"La séance '{ev_nom}' a été déplacée du {d_orig} au {d_dest} à {clean_hour}."
        return f"Erreur lors du déplacement sur Intervals ({put_r.status_code}) : {put_r.text}"
    except Exception as err:
        return f"Erreur connexion Intervals : {err}"

def generate_ai(prompt_parts, user_msg_raw=""):
    tools_map = {
        "planifier_seance": planifier_seance,
        "supprimer_seance": supprimer_seance,
        "deplacer_seance": deplacer_seance
    }
    dernier_bug = ""
    cfg = types.GenerateContentConfig(
        tools=[planifier_seance, supprimer_seance, deplacer_seance],
        temperature=0.3
    )

    for _ in range(3):
        try:
            r = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt_parts, config=cfg)
            if r.function_calls:
                call = r.function_calls[0]
                fn = tools_map.get(call.name)
                args = dict(call.args) if call.args else {}
                tool_res = fn(**args) if fn else "Action inconnue"
                time.sleep(1)
                conf_prompt = (
                    f"Action exécutée : {tool_res}.\n"
                    f"Demande initiale : '{user_msg_raw}'.\n"
                    f"Confirme à l'athlète en HTML Telegram avec un ton direct et bienveillant."
                )
                r_conf = ai_client.models.generate_content(model=MODEL_NAME, contents=conf_prompt)
                return r_conf.text
            if r and r.text:
                return r.text
        except Exception as e:
            dernier_bug = str(e)
            logging.error(f"Erreur generate_ai: {e}")
            if "429" in dernier_bug or "RESOURCE_EXHAUSTED" in dernier_bug:
                time.sleep(3)
                continue
            time.sleep(1.5)

    return f"⚠️ Erreur détaillée : {dernier_bug}"

def make_prompt(prof, weather, well, acts, evts, user_msg):
    mem = get_notes()
    chat_hist = get_chat_history(6)

    today_str = datetime.date.today().isoformat()
    consignes = (
        "Consignes d'entraînement et d'analyse :\n"
        "1. Contexte temporel : les listes récentes sont triées de la plus récente à la plus ancienne. "
        "Les agrégats 3-6 mois te permettent de comparer le niveau actuel avec les cycles précédents.\n"
        "2. Météo : tiens compte de la météo pour conseiller l'horaire de course ou adapter l'allure si forte chaleur ou vent.\n"
        "3. HISTORIQUE DE CONVERSATION : sers-toi des derniers messages échangés pour comprendre les pronoms "
        "('celle-ci', 'la séance', 'demain').\n"
        "4. OUTILS CALENDRIER : si l'athlète demande d'ajouter, décaler ou supprimer une séance, appelle "
        "obligatoirement planifier_seance, deplacer_seance ou supprimer_seance.\n"
        "5. FORMAT HTML TELEGRAM : utilise <b>Texte en gras</b> pour les chiffres clés et allures. "
        "Aère avec des lignes vides. Pas de Markdown (# ou **).\n"
        "6. Si un fait durable est mentionné, écris en fin de message : [MEMOIRE] note à enregistrer"
    )

    return (
        f"Tu es l'entraîneur d'athlétisme personnel de ce coureur (objectif prioritaire : 5 km sub-20, cible 3'59/km au 13/12/2026).\n"
        f"Date du jour : {today_str}.\n\n"
        f"MÉTÉO DU JOUR : {json.dumps(weather, ensure_ascii=False)}\n"
        f"PROFIL ATHLÈTE : {json.dumps(prof, ensure_ascii=False)}\n"
        f"HISTORIQUE RÉCENT CONVERSATION : {json.dumps(chat_hist, ensure_ascii=False)}\n"
        f"MÉMOIRE DURABLE (Notes clés) : {json.dumps(mem, ensure_ascii=False)}\n"
        f"SANTÉ 3 DERNIERS MOIS (brut) : {json.dumps(well, ensure_ascii=False)}\n"
        f"ACTIVITÉS 3 DERNIERS MOIS (brut) : {json.dumps(acts.get('brut_3_mois', []), ensure_ascii=False)}\n"
        f"ACTIVITÉS 3 À 6 MOIS (agrégats hebdo) : {json.dumps(acts.get('agregat_semaines_3_a_6_mois', []), ensure_ascii=False)}\n"
        f"CALENDRIER (programmes passés 6 mois + prévisionnel 3 mois) : {json.dumps(evts, ensure_ascii=False)}\n\n"
        f"{consignes}\n\n"
        f"MESSAGE DE L'ATHLÈTE :\n\"{user_msg}\""
    )

async def send_reply(cid, text, bot):
    if "[MEMOIRE]" in text:
        parts = text.split("[MEMOIRE]")
        text = parts[0].strip()
        save_note(parts[1].strip().split("\n")[0])
    
    save_chat_msg("coach", text
