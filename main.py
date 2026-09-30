"""Coach running : bot Telegram (Intervals.icu + Gemini + Open-Meteo + SQLite)."""
import os
import io
import re
import json
import time
import html
import sqlite3
import datetime
import asyncio
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "i596796").strip()
INTERVALS_KEY = os.environ.get("INTERVALS_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_USER = int(os.environ.get("TELEGRAM_USER_ID", "0").strip() or 0)

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
DEFAULT_CITY = os.environ.get("DEFAULT_CITY", "Melbourne").strip()

GOAL_DATE = datetime.date(2026, 12, 13)
GOAL_TEXT = "5 km sub-20, cible 3'59/km"

BG_INTERVAL = 900               # secondes entre deux verifications automatiques
MAX_DEBRIEFS_PER_CYCLE = 2      # limite d'appels Gemini automatiques par cycle
MAX_UNAVAILABILITY_DAYS = 14    # plage maximale supprimable en une demande
DATA_TTL = 120                  # cache des donnees Intervals (secondes)

ai_client = genai.Client(api_key=GEMINI_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_KEY)
BASE = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
DB = "coach_brain.db"
POOL = ThreadPoolExecutor(max_workers=5)
AI_LOCK = threading.Lock()

JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
JOURS_MAP = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
KEEP_CATEGORIES = {"WORKOUT", "RACE_A", "RACE_B", "RACE_C", "SICK", "INJURY", "HOLIDAY"}
ACWR_LIMIT = 1.35


class AIError(Exception):
    """Erreur Gemini avec un message deja lisible pour l'athlete."""


class DateError(ValueError):
    """Date invalide fournie a un outil."""


# --------------------------------------------------------------------------
# BASE DE DONNEES
# --------------------------------------------------------------------------
def db_exec(sql, params=(), fetch=False):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    db_exec("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, txt TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_reports (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, txt TEXT, d TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS deleted_events (id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, d TEXT, payload TEXT)")
    db_exec("DELETE FROM deleted_events WHERE d < date('now', '-30 day')")


def save_note(txt: str):
    db_exec("INSERT INTO notes (d, txt) VALUES (?, ?)", (datetime.date.today().isoformat(), txt[:500]))


def get_notes():
    rows = db_exec("SELECT d, txt FROM notes ORDER BY id DESC LIMIT 20", fetch=True)
    return [{"date": r[0], "note": r[1]} for r in reversed(rows)]


def save_chat_msg(role: str, text: str):
    db_exec("INSERT INTO chat_history (role, txt, d) VALUES (?, ?, ?)",
            (role, text[:1500], datetime.datetime.now().isoformat()))
    db_exec("DELETE FROM chat_history WHERE id NOT IN (SELECT id FROM chat_history ORDER BY id DESC LIMIT 200)")


def get_chat_history(limit: int = 6):
    rows = db_exec("SELECT role, txt FROM chat_history ORDER BY id DESC LIMIT ?", (limit,), fetch=True)
    return [{"role": r[0], "text": r[1][:500]} for r in reversed(rows)]


def is_seen(table: str, key: str) -> bool:
    col = "d" if table == "seen_well" else "id"
    return bool(db_exec(f"SELECT 1 FROM {table} WHERE {col}=?", (key,), fetch=True))


def mark_seen(table: str, key: str):
    db_exec(f"INSERT OR IGNORE INTO {table} VALUES (?)", (key,))


# --------------------------------------------------------------------------
# DONNEES INTERVALS / METEO
# --------------------------------------------------------------------------
def _pace(spd):
    if not spd or spd <= 0:
        return None
    total = round(1000 / spd)
    return f"{total // 60}'{total % 60:02d}\"/km"


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
                "timezone": d.get("timezone"),
            }
        logging.error(f"fetch_profile : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_profile : {err}")
    return {}


_WEATHER_CACHE = {}


def fetch_weather(city_name: str = ""):
    city = (city_name or "").strip() or DEFAULT_CITY
    cached = _WEATHER_CACHE.get(city)
    if cached and time.time() - cached[0] < 1800:
        return cached[1]
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "fr", "format": "json"},
            timeout=6,
        )
        results = geo.json().get("results") if geo.status_code == 200 else None
        if results:
            g = results[0]
            w = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": g["latitude"],
                    "longitude": g["longitude"],
                    "current": "temperature_2m,apparent_temperature,precipitation,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "forecast_days": 3,
                    "timezone": "auto",
                },
                timeout=6,
            )
            if w.status_code == 200:
                data = w.json()
                cur = data.get("current", {})
                daily = data.get("daily", {})
                dates = daily.get("time", [])
                tmax = daily.get("temperature_2m_max", [])
                tmin = daily.get("temperature_2m_min", [])
                pluie = daily.get("precipitation_probability_max", [])
                forecast = [
                    {
                        "date": dates[i],
                        "tmax_c": tmax[i] if i < len(tmax) else None,
                        "tmin_c": tmin[i] if i < len(tmin) else None,
                        "proba_pluie_pct": pluie[i] if i < len(pluie) else None,
                    }
                    for i in range(len(dates))
                ]
                out = {
                    "ville": g.get("name", city),
                    "maintenant": {
                        "temp_c": cur.get("temperature_2m"),
                        "ressenti_c": cur.get("apparent_temperature"),
                        "vent_kmh": cur.get("wind_speed_10m"),
                        "pluie_mm": cur.get("precipitation"),
                    },
                    "previsions_3j": forecast,
                }
                _WEATHER_CACHE[city] = (time.time(), out)
                return out
    except Exception as err:
        logging.error(f"Erreur fetch_weather : {err}")
    return {"info": "Météo non disponible"}


def fetch_wellness():
    today = datetime.date.today()
    d90 = (today - datetime.timedelta(days=90)).isoformat()
    try:
        r = requests.get(f"{BASE}/wellness", auth=AUTH,
                         params={"oldest": d90, "newest": today.isoformat()}, timeout=10)
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
                        "ctl": j.get("ctl"),
                    })
            return res
        logging.error(f"fetch_wellness : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_wellness : {err}")
    return []


def evaluate_injury_risk(wellness_list):
    if not wellness_list:
        return {"acwr": None, "alerte_surcharge": False, "alerte_vrc": False,
                "statut": "INCONNU", "details": ["Données de santé indisponibles"]}

    # ACWR : premiere ligne qui a a la fois ATL et CTL
    ref = next((w for w in wellness_list if (w.get("atl") or 0) > 0 and (w.get("ctl") or 0) > 0), None)
    acwr = round(ref["atl"] / ref["ctl"], 2) if ref else None
    alerte_surcharge = acwr is not None and acwr >= ACWR_LIMIT

    # VRC : deux dernieres nuits MESUREES sous 90 % de la reference
    pts = [w for w in wellness_list if w.get("hrv") and w.get("hrv_base")][:2]
    alerte_vrc = len(pts) == 2 and all(w["hrv"] < w["hrv_base"] * 0.90 for w in pts)

    details = []
    if alerte_surcharge:
        details.append(f"ACWR élevé à {acwr} (seuil critique : {ACWR_LIMIT})")
    if alerte_vrc:
        details.append("VRC sous la référence (moins de 90 %) sur les deux dernières mesures")

    if alerte_surcharge or alerte_vrc:
        statut = "DANGER"
    elif acwr is None and len(pts) < 2:
        statut = "INCONNU"
    else:
        statut = "OPTIMAL"

    return {"acwr": acwr, "alerte_surcharge": alerte_surcharge, "alerte_vrc": alerte_vrc,
            "statut": statut, "details": details}


def fetch_activities():
    today = datetime.date.today()
    d180 = (today - datetime.timedelta(days=180)).isoformat()
    d90 = (today - datetime.timedelta(days=90)).isoformat()
    empty = {"brut_3_mois": [], "agregat_semaines_3_a_6_mois": [], "top_performances_recentes": []}
    try:
        r = requests.get(f"{BASE}/activities", auth=AUTH, params={"oldest": d180}, timeout=10)
        if r.status_code != 200:
            logging.error(f"fetch_activities : HTTP {r.status_code}")
            return empty

        raw = r.json()
        raw.sort(key=lambda x: str(x.get("start_date_local", "")), reverse=True)

        raw_3months, older_acts, runs = [], [], []
        for a in raw:
            d_str = str(a.get("start_date_local") or "")[:10]
            if len(d_str) != 10:
                continue
            spd = a.get("average_speed") or 0
            dist = a.get("distance") or 0
            mtime = a.get("moving_time") or 0
            item = {
                "id": a.get("id"),
                "d": d_str,
                "type": a.get("type"),
                "nom": a.get("name"),
                "km": round(dist / 1000, 2),
                "min": round(mtime / 60, 1),
                "pace": _pace(spd),
                "hr": a.get("average_heartrate"),
                "load": a.get("icu_training_load"),
            }
            if a.get("type") in RUN_TYPES and dist >= 3000 and spd > 0:
                runs.append((spd, item))
            if d_str >= d90:
                raw_3months.append(item)
            else:
                older_acts.append(item)

        runs.sort(key=lambda x: x[0], reverse=True)
        top_perfs = [
            {"allure": i["pace"], "distance_km": i["km"], "date": i["d"], "nom": i["nom"]}
            for _, i in runs[:5]
        ]

        weeks = {}
        for a in older_acts:
            iso = datetime.date.fromisoformat(a["d"]).isocalendar()
            w_key = f"{iso[0]}-S{iso[1]:02d}"
            w = weeks.setdefault(w_key, {"km": 0.0, "min": 0.0, "load": 0, "count": 0})
            w["km"] += a["km"]
            w["min"] += a["min"]
            w["load"] += a["load"] or 0
            w["count"] += 1

        weekly_summary = [
            {"semaine": k, "seances": v["count"], "km_total": round(v["km"], 1),
             "temps_h": round(v["min"] / 60, 1), "charge_totale": round(v["load"])}
            for k, v in sorted(weeks.items(), reverse=True)
        ]
        return {
            "brut_3_mois": raw_3months,
            "agregat_semaines_3_a_6_mois": weekly_summary,
            "top_performances_recentes": top_perfs,
        }
    except Exception as err:
        logging.exception(f"Erreur fetch_activities : {err}")
    return empty


def fetch_events():
    today = datetime.date.today()
    past = (today - datetime.timedelta(days=28)).isoformat()
    fut = (today + datetime.timedelta(days=90)).isoformat()
    try:
        r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": past, "newest": fut}, timeout=10)
        if r.status_code == 200:
            raw = r.json()
            raw.sort(key=lambda x: str(x.get("start_date_local", "")))
            return [{
                "id": e.get("id"),
                "d": str(e.get("start_date_local") or "")[:10],
                "h": str(e.get("start_date_local") or "")[11:16],
                "nom": e.get("name"),
                "desc": (e.get("description") or "")[:400],
                "category": e.get("category"),
            } for e in raw if e.get("category") in KEEP_CATEGORIES]
        logging.error(f"fetch_events : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_events : {err}")
    return []


_DATA_CACHE = {"t": 0.0, "v": None}
_DATA_LOCK = threading.Lock()


def invalidate_cache():
    with _DATA_LOCK:
        _DATA_CACHE["t"] = 0.0


def _load_all_data():
    f_prof = POOL.submit(fetch_profile)
    f_well = POOL.submit(fetch_wellness)
    f_acts = POOL.submit(fetch_activities)
    f_evts = POOL.submit(fetch_events)
    prof = f_prof.result()
    weather = fetch_weather(prof.get("city") or DEFAULT_CITY)
    return prof, weather, f_well.result(), f_acts.result(), f_evts.result()


def get_all_data():
    with _DATA_LOCK:
        if _DATA_CACHE["v"] and time.time() - _DATA_CACHE["t"] < DATA_TTL:
            return _DATA_CACHE["v"]
        value = _load_all_data()
        if value[2] or value[3].get("brut_3_mois"):   # ne pas mettre en cache un chargement vide
            _DATA_CACHE.update(t=time.time(), v=value)
        return value


# --------------------------------------------------------------------------
# OUTILS (appeles par Gemini)
# --------------------------------------------------------------------------
def parse_relative_date(date_str):
    """Retourne une date ISO, ou None si vide. Leve DateError si invalide."""
    s = str(date_str or "").lower().strip()
    if not s:
        return None
    today = datetime.date.today()

    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(0)).isoformat()
        except ValueError:
            raise DateError(f"Date invalide : '{date_str}'. Utilise le format AAAA-MM-JJ.")

    if any(k in s for k in ("après-demain", "apres-demain", "après demain", "apres demain", "day after tomorrow")):
        return (today + datetime.timedelta(days=2)).isoformat()
    if any(k in s for k in ("demain", "tomorrow")):
        return (today + datetime.timedelta(days=1)).isoformat()
    if any(k in s for k in ("hier", "yesterday")):
        return (today - datetime.timedelta(days=1)).isoformat()
    if any(k in s for k in ("today", "aujourd", "ce soir", "ce jour", "maintenant")):
        return today.isoformat()
    for name, wd in JOURS_MAP.items():
        if re.search(rf"\b{name}\b", s):
            delta = (wd - today.weekday()) % 7 or 7
            return (today + datetime.timedelta(days=delta)).isoformat()
    raise DateError(f"Date invalide : '{date_str}'. Utilise le format AAAA-MM-JJ.")


def _hhmm(heure, default="18:00"):
    m = re.match(r"^\s*(\d{1,2})\s*(?:[:hH]\s*(\d{2})?)?\s*$", str(heure or ""))
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if hh < 24 and mm < 60:
            return f"{hh:02d}:{mm:02d}"
    return default


def _list_events(d_from, d_to, categories=("WORKOUT",)):
    r = requests.get(f"{BASE}/events", auth=AUTH, params={"oldest": d_from, "newest": d_to}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"lecture du calendrier impossible (HTTP {r.status_code})")
    return [e for e in r.json() if e.get("category") in categories]


def _noms(events):
    return ", ".join(f"'{e.get('name')}'" for e in events)


def _pick_event(events, titre, d):
    """Retourne (evenement, message_erreur). Ne devine jamais en cas d'ambiguite."""
    t = (titre or "").strip().lower()
    if t:
        matches = [e for e in events if t in str(e.get("name", "")).lower()]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"Plusieurs séances correspondent à '{titre}' le {d} ({_noms(matches)}). Précise laquelle."
        return None, f"Aucune séance nommée '{titre}' le {d}. Séances trouvées : {_noms(events)}."
    if len(events) == 1:
        return events[0], None
    return None, f"Plusieurs séances le {d} ({_noms(events)}). Précise laquelle."


def _delete_event(ev, batch):
    r = requests.delete(f"{BASE}/events/{ev['id']}", auth=AUTH, timeout=8)
    if r.status_code not in (200, 204):
        logging.error(f"Suppression refusée ({r.status_code}) : {r.text[:200]}")
        return False
    try:
        db_exec("INSERT INTO deleted_events (batch, d, payload) VALUES (?, ?, ?)",
                (batch, datetime.datetime.now().isoformat(), json.dumps(ev, ensure_ascii=False)))
    except Exception:
        logging.exception("Sauvegarde de l'événement supprimé impossible")
    return True


def planifier_seance(date_str: str, titre: str, description: str = "", heure: str = "18:00") -> str:
    """Planifie une séance de course à pied dans le calendrier Intervals.icu.

    Args:
        date_str: Date de la séance au format AAAA-MM-JJ (ex: '2026-10-02'). Doit être aujourd'hui ou dans le futur.
        titre: Titre court de la séance (ex: 'Fractionné 6x800m').
        description: Consignes détaillées : échauffement, allures cibles, récupérations, retour au calme.
        heure: Heure de début au format HH:MM (défaut '18:00').
    """
    try:
        d = parse_relative_date(date_str)
        if not d:
            return "⚠️ Précise la date de la séance."
        if d < datetime.date.today().isoformat():
            return f"⚠️ Impossible de planifier dans le passé ({d})."
        h = _hhmm(heure)
        name = (titre or "").strip() or "Séance Course"

        if any(str(e.get("name", "")).strip().lower() == name.lower() for e in _list_events(d, d)):
            return f"⚠️ La séance '{name}' existe déjà le {d}, rien n'a été créé."

        payload = {
            "category": "WORKOUT",
            "type": "Run",
            "name": name,
            "description": (description or "").strip() or f"Séance : {name}",
            "start_date_local": f"{d}T{h}:00",
        }
        r = requests.post(f"{BASE}/events", auth=AUTH, json=payload, timeout=8)
        if r.status_code in (200, 201):
            invalidate_cache()
            return f"✅ Séance '{name}' planifiée le {d} à {h}."
        logging.error(f"planifier_seance : HTTP {r.status_code} {r.text[:200]}")
        return f"⚠️ Intervals.icu a refusé la création (HTTP {r.status_code})."
    except DateError as e:
        return f"⚠️ {e}"
    except Exception as err:
        logging.exception("planifier_seance")
        return f"⚠️ Erreur de connexion Intervals : {err}"


def supprimer_seance(date_str: str, titre: str = "") -> str:
    """Supprime UNE séance planifiée dans Intervals.icu (une sauvegarde est conservée pour pouvoir l'annuler).

    Args:
        date_str: Date de la séance au format AAAA-MM-JJ.
        titre: Titre ou mot-clé de la séance. Obligatoire s'il y a plusieurs séances ce jour-là.
    """
    try:
        d = parse_relative_date(date_str) or datetime.date.today().isoformat()
        events = _list_events(d, d)
        if not events:
            return f"⚠️ Aucune séance planifiée le {d}."
        ev, err = _pick_event(events, titre, d)
        if err:
            return f"⚠️ {err}"
        if _delete_event(ev, datetime.datetime.now().isoformat()):
            invalidate_cache()
            return f"✅ Séance '{ev.get('name', 'Séance')}' du {d} supprimée."
        return "⚠️ Intervals.icu a refusé la suppression."
    except DateError as e:
        return f"⚠️ {e}"
    except Exception as err:
        logging.exception("supprimer_seance")
        return f"⚠️ Erreur de connexion Intervals : {err}"


def deplacer_seance(date_origine: str, date_cible: str, titre: str = "", heure: str = "") -> str:
    """Déplace une séance planifiée vers une autre date dans Intervals.icu.

    Args:
        date_origine: Date actuelle de la séance au format AAAA-MM-JJ.
        date_cible: Nouvelle date au format AAAA-MM-JJ.
        titre: Titre ou mot-clé de la séance. Obligatoire s'il y a plusieurs séances ce jour-là.
        heure: Nouvelle heure HH:MM. Si vide, l'heure d'origine est conservée.
    """
    try:
        d_orig = parse_relative_date(date_origine) or datetime.date.today().isoformat()
        d_dest = parse_relative_date(date_cible)
        if not d_dest:
            return "⚠️ Précise vers quelle date déplacer la séance."
        if d_dest < datetime.date.today().isoformat():
            return f"⚠️ Impossible de déplacer dans le passé ({d_dest})."

        events = _list_events(d_orig, d_orig)
        if not events:
            return f"⚠️ Aucune séance à déplacer le {d_orig}."
        ev, err = _pick_event(events, titre, d_orig)
        if err:
            return f"⚠️ {err}"

        orig_h = str(ev.get("start_date_local") or "")[11:16]
        h = _hhmm(heure, default=_hhmm(orig_h))
        r = requests.put(f"{BASE}/events/{ev['id']}", auth=AUTH,
                         json={"start_date_local": f"{d_dest}T{h}:00"}, timeout=8)
        if r.status_code in (200, 204):
            invalidate_cache()
            return f"✅ Séance '{ev.get('name', 'Séance')}' déplacée du {d_orig} au {d_dest} à {h}."
        logging.error(f"deplacer_seance : HTTP {r.status_code} {r.text[:200]}")
        return f"⚠️ Intervals.icu a refusé le déplacement (HTTP {r.status_code})."
    except DateError as e:
        return f"⚠️ {e}"
    except Exception as err:
        logging.exception("deplacer_seance")
        return f"⚠️ Erreur de connexion Intervals : {err}"


def gerer_indisponibilite(date_debut: str, date_fin: str = "", motif: str = "Repos forcé") -> str:
    """Supprime les séances planifiées sur une courte période (maladie, blessure, imprévu). Maximum 14 jours, jamais dans le passé.

    Args:
        date_debut: Premier jour d'indisponibilité au format AAAA-MM-JJ.
        date_fin: Dernier jour au format AAAA-MM-JJ (si vide : même jour que le début).
        motif: Raison de l'indisponibilité (ex: 'gastro', 'déplacement pro').
    """
    try:
        today = datetime.date.today()
        d1 = datetime.date.fromisoformat(parse_relative_date(date_debut) or today.isoformat())
        d2 = datetime.date.fromisoformat(parse_relative_date(date_fin) or d1.isoformat())
        if d1 > d2:
            d1, d2 = d2, d1
        d1 = max(d1, today)
        if d2 < d1:
            return "⚠️ Cette période est dans le passé, aucune séance supprimée."
        nb_jours = (d2 - d1).days + 1
        if nb_jours > MAX_UNAVAILABILITY_DAYS:
            return (f"⚠️ Période trop longue ({nb_jours} jours). Maximum {MAX_UNAVAILABILITY_DAYS} jours "
                    f"par demande : précise une période plus courte.")

        save_note(f"Indisponibilité du {d1} au {d2} : {motif}")
        events = _list_events(d1.isoformat(), d2.isoformat())
        if not events:
            return f"⚠️ Aucune séance programmée entre le {d1} et le {d2}."

        batch = datetime.datetime.now().isoformat()
        supprimees = [e.get("name", "Séance") for e in events if _delete_event(e, batch)]
        invalidate_cache()
        if not supprimees:
            return "⚠️ Intervals.icu a refusé les suppressions."
        return f"✅ {len(supprimees)} séance(s) supprimée(s) du {d1} au {d2} (motif : {motif}) : {', '.join(supprimees)}."
    except DateError as e:
        return f"⚠️ {e}"
    except Exception as err:
        logging.exception("gerer_indisponibilite")
        return f"⚠️ Erreur de connexion Intervals : {err}"


def restaurer_suppression() -> str:
    """Annule la dernière suppression : recrée dans Intervals.icu les séances supprimées lors de la dernière opération."""
    try:
        rows = db_exec("SELECT batch FROM deleted_events ORDER BY id DESC LIMIT 1", fetch=True)
        if not rows:
            return "⚠️ Aucune suppression récente à restaurer."
        items = db_exec("SELECT id, payload FROM deleted_events WHERE batch=?", (rows[0][0],), fetch=True)
        restaurees = []
        for row_id, payload in items:
            ev = json.loads(payload)
            body = {k: ev[k] for k in ("category", "type", "name", "description", "start_date_local")
                    if ev.get(k) is not None}
            r = requests.post(f"{BASE}/events", auth=AUTH, json=body, timeout=8)
            if r.status_code in (200, 201):
                restaurees.append(f"{ev.get('name', 'Séance')} ({str(ev.get('start_date_local', ''))[:10]})")
                db_exec("DELETE FROM deleted_events WHERE id=?", (row_id,))
        invalidate_cache()
        if not restaurees:
            return "⚠️ Impossible de restaurer les séances."
        return f"✅ {len(restaurees)} séance(s) restaurée(s) : {', '.join(restaurees)}."
    except Exception as err:
        logging.exception("restaurer_suppression")
        return f"⚠️ Erreur de restauration : {err}"


TOOLS = [planifier_seance, supprimer_seance, deplacer_seance, gerer_indisponibilite, restaurer_suppression]
TOOLS_MAP = {f.__name__: f for f in TOOLS}


# --------------------------------------------------------------------------
# GEMINI
# --------------------------------------------------------------------------
SYSTEM_INSTRUCTION = (
    "Tu es l'entraîneur personnel de course à pied de cet athlète. Tu réponds en français, de façon directe, "
    "précise et encourageante.\n"
    "RÈGLES :\n"
    "1. Les listes de santé et d'activités sont triées du plus récent au plus ancien. La première ligne de santé "
    "peut être incomplète (nuit du jour pas encore synchronisée) : ne conclus rien d'une valeur manquante.\n"
    "2. Si le statut de la SURVEILLANCE est DANGER, alerte fermement l'athlète et adapte l'intensité à la baisse "
    "ou conseille du repos. Si le statut est INCONNU, ne dis pas que tout va bien.\n"
    "3. Pour modifier le calendrier, appelle l'outil (planifier_seance, deplacer_seance, supprimer_seance, "
    "gerer_indisponibilite, restaurer_suppression). Ne dis JAMAIS qu'une séance est planifiée, déplacée ou "
    "supprimée sans avoir appelé l'outil. N'appelle aucun outil quand l'athlète demande seulement un conseil "
    "ou une analyse.\n"
    "4. Donne toujours les dates aux outils au format AAAA-MM-JJ, en t'appuyant sur les repères de dates fournis.\n"
    "5. Format Telegram HTML : uniquement <b>, <i> et <code>. Aère avec des lignes vides. Aucun Markdown "
    "(pas de #, pas de **), aucune autre balise.\n"
    "6. Si l'athlète mentionne un fait durable (blessure, contrainte, préférence, déplacement), termine ton "
    "message par une ligne : [MEMOIRE] fait à retenir\n"
    "7. Tu n'es pas médecin : en cas de douleur persistante ou de symptôme inquiétant, recommande de consulter."
)


def _retry_delay(err_str):
    m = re.search(r"retry in ([\d.]+)\s*s", err_str) or re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)s", err_str)
    return float(m.group(1)) if m else None


def call_gemini(contents, allow_tools=True):
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=TOOLS if allow_tools else None,
        temperature=0.3,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    last = ""
    for attempt in range(3):
        try:
            with AI_LOCK:
                return ai_client.models.generate_content(model=MODEL_NAME, contents=contents, config=cfg)
        except Exception as e:
            last = str(e)
            logging.error(f"Erreur Gemini (essai {attempt + 1}/3) : {last[:300]}")
            if "429" in last or "RESOURCE_EXHAUSTED" in last:
                if "PerDay" in last:
                    raise AIError(
                        "⚠️ Quota journalier Gemini atteint. Il se réinitialise à minuit (heure du Pacifique). "
                        "Vérifie aussi que ta clé API appartient à un projet avec facturation activée."
                    ) from e
                time.sleep(min(_retry_delay(last) or 10, 60) + 1)
                continue
            if any(c in last for c in ("500", "503", "UNAVAILABLE", "DEADLINE")):
                time.sleep(2 * (attempt + 1))
                continue
            raise AIError(f"⚠️ Erreur Gemini : {last[:200]}") from e
    raise AIError("⚠️ Gemini est saturé, réessaie dans une minute.")


def generate_ai(contents, allow_tools=True) -> str:
    """Un seul appel Gemini. Les outils sont executes une seule fois, hors de la boucle de retry."""
    r = call_gemini(contents, allow_tools=allow_tools)
    calls = (r.function_calls or []) if allow_tools else []
    if calls:
        results = []
        for c in calls:
            fn = TOOLS_MAP.get(c.name)
            if not fn:
                results.append(f"⚠️ Action inconnue : {c.name}")
                continue
            try:
                results.append(fn(**dict(c.args or {})))
            except TypeError as e:
                logging.error(f"Paramètres invalides pour {c.name} : {e}")
                results.append(f"⚠️ Paramètres invalides pour {c.name}, reformule ta demande.")
            except Exception as e:
                logging.exception(f"Erreur outil {c.name}")
                results.append(f"⚠️ Erreur pendant {c.name} : {e}")
        return "\n\n".join(results)

    text = (r.text or "").strip()
    if not text:
        raise AIError("⚠️ Gemini a renvoyé une réponse vide, reformule ou réessaie.")
    return text


def _block(label, data):
    if not data:
        return f"{label} : (indisponible ou vide)"
    return f"{label} : {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}"


def make_prompt(prof, weather, well, acts, evts, user_msg):
    today = datetime.date.today()
    days_left = (GOAL_DATE - today).days
    goal_line = (f"J-{days_left}" if days_left >= 0 else "objectif passé")
    reperes = ", ".join(
        f"{JOURS_FR[d.weekday()]} {d.isoformat()}"
        for d in (today + datetime.timedelta(days=i) for i in range(8))
    )
    return (
        f"Objectif prioritaire : {GOAL_TEXT} le {GOAL_DATE.strftime('%d/%m/%Y')} ({goal_line}).\n"
        f"Aujourd'hui : {JOURS_FR[today.weekday()]} {today.isoformat()}.\n"
        f"Repères de dates : {reperes}.\n\n"
        f"{_block('SURVEILLANCE SURCHARGE (ACWR) ET VRC', evaluate_injury_risk(well))}\n"
        f"{_block('MÉTÉO', weather)}\n"
        f"{_block('PROFIL ATHLÈTE', prof)}\n"
        f"{_block('HISTORIQUE RÉCENT DE CONVERSATION (du plus ancien au plus récent)', get_chat_history(6))}\n"
        f"{_block('MÉMOIRE DURABLE', get_notes())}\n"
        f"{_block('SANTÉ 3 DERNIERS MOIS (du plus récent au plus ancien)', well)}\n"
        f"{_block('ACTIVITÉS 3 DERNIERS MOIS (du plus récent au plus ancien)', acts.get('brut_3_mois'))}\n"
        f"{_block('MEILLEURES ALLURES DE COURSE (6 mois, sorties de 3 km et plus)', acts.get('top_performances_recentes'))}\n"
        f"{_block('AGRÉGATS HEBDO 3 À 6 MOIS', acts.get('agregat_semaines_3_a_6_mois'))}\n"
        f"{_block('CALENDRIER (28 derniers jours + 90 jours à venir)', evts)}\n\n"
        f"MESSAGE DE L'ATHLÈTE :\n\"{user_msg}\""
    )


# --------------------------------------------------------------------------
# TELEGRAM
# --------------------------------------------------------------------------
_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "code", "pre"}


def sanitize_html(text: str) -> str:
    """Nettoie la sortie du modèle pour le HTML de Telegram."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|ul|ol)>", "\n", text, flags=re.I)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", text)

    def _tag(m):
        name = m.group(2).lower()
        return f"<{m.group(1)}{name}>" if name in _ALLOWED_TAGS else ""

    text = re.sub(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", _tag, text)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+);)", "&amp;", text)
    text = re.sub(r"<(?!/?[a-zA-Z])", "&lt;", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, limit: int = 3800):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            head, para = para[:limit], para[limit:]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(head)
        if cur and len(cur) + len(para) + 2 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


def tg_send(chat_id, text):
    """Envoi synchrone : HTML nettoyé, découpé à 4096 caractères, repli en texte brut."""
    text = sanitize_html(text or "")
    if not text:
        return
    for chunk in split_message(text):
        try:
            r = requests.post(f"{TG_API}/sendMessage",
                              json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}, timeout=15)
            if r.status_code == 200:
                continue
            logging.warning(f"Telegram a refusé le HTML ({r.status_code}) : {r.text[:200]}")
        except Exception as err:
            logging.error(f"Erreur envoi Telegram : {err}")
        try:
            plain = html.unescape(re.sub(r"<[^>]+>", "", chunk))
            requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": plain}, timeout=15)
        except Exception:
            logging.exception("Envoi Telegram en texte brut impossible")


def finalize_reply(text: str, save: bool = True) -> str:
    """Extrait la ligne [MEMOIRE], l'enregistre, et sauvegarde la réponse dans l'historique."""
    text = text or ""
    if "[MEMOIRE]" in text:
        head, _, tail = text.partition("[MEMOIRE]")
        note = tail.strip().split("\n")[0].strip()
        if note:
            save_note(note)
        text = head.strip()
    if save and text:
        save_chat_msg("coach", text)
    return text


async def send_reply(cid, text, save=True):
    text = finalize_reply(text, save)
    await asyncio.to_thread(tg_send, cid, text)


def _authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and u.id == TG_USER


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message or not update.message.text:
        return
    cid = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    msg = update.message.text
    save = True
    try:
        prof, weather, well, acts, evts = await asyncio.to_thread(get_all_data)
        prompt = make_prompt(prof, weather, well, acts, evts, msg)
        save_chat_msg("athlete", msg)   # après make_prompt : le message courant n'est pas dans l'historique
        ans = await asyncio.to_thread(generate_ai, prompt)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_text")
        ans, save = "⚠️ Erreur interne, réessaie dans un instant.", False
    await send_reply(cid, ans, save=save)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    v = update.message.voice or update.message.audio
    if not v:
        return
    cid = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.RECORD_VOICE)
    save = True
    try:
        f = await context.bot.get_file(v.file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        mime = getattr(v, "mime_type", None) or "audio/ogg"

        prof, weather, well, acts, evts = await asyncio.to_thread(get_all_data)
        text_prompt = make_prompt(prof, weather, well, acts, evts,
                                  "Message vocal de l'athlète (écoute l'audio joint et réponds-y).")
        save_chat_msg("athlete", "[Message vocal]")
        contents = [text_prompt, types.Part.from_bytes(data=buf.getvalue(), mime_type=mime)]
        ans = await asyncio.to_thread(generate_ai, contents)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_voice")
        ans, save = "⚠️ Erreur interne, réessaie dans un instant.", False
    await send_reply(cid, ans, save=save)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Exception Telegram", exc_info=context.error)


# --------------------------------------------------------------------------
# TACHES AUTOMATIQUES
# --------------------------------------------------------------------------
AUTO_SUFFIX = "\n(Message automatique : n'appelle aucun outil, propose seulement.)"


def _push(header, ans, tag):
    text = finalize_reply(ans, save=False)
    if text:
        save_chat_msg("coach", f"[{tag}] {text}")
        tg_send(TG_USER, f"{header}\n\n{text}")


def bg_init():
    try:
        _, _, well, acts, _ = get_all_data()
        for a in acts.get("brut_3_mois", []):
            mark_seen("seen_acts", str(a["id"]))
        if well and well[0].get("d"):
            mark_seen("seen_well", str(well[0]["d"]))
    except Exception:
        logging.exception("Erreur init bg_loop")


def bg_tick():
    prof, weather, well, acts, evts = get_all_data()
    today = datetime.date.today()

    # 1. Nouvelles activités (au plus MAX_DEBRIEFS_PER_CYCLE débriefs, les autres sont marquées vues)
    brut = acts.get("brut_3_mois", [])
    unseen = [a for a in brut if not is_seen("seen_acts", str(a["id"]))]
    for a in unseen[MAX_DEBRIEFS_PER_CYCLE:]:
        mark_seen("seen_acts", str(a["id"]))
    for a in reversed(unseen[:MAX_DEBRIEFS_PER_CYCLE]):
        act_id = str(a["id"])
        try:
            age = (today - datetime.date.fromisoformat(a["d"])).days
        except Exception:
            age = 0
        if age > 3:
            mark_seen("seen_acts", act_id)
            continue
        try:
            msg = f"Débrief séance terminée : {json.dumps(a, ensure_ascii=False)}{AUTO_SUFFIX}"
            ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg), allow_tools=False)
        except AIError as e:
            logging.warning(f"Débrief différé ({act_id}) : {e}")
            break
        _push("🏁 <b>Nouvelle séance détectée !</b>", ans, "Débrief auto")
        mark_seen("seen_acts", act_id)

    # 2. Brief réveil (nuit synchronisée)
    if well and well[0].get("d") and well[0].get("sleep_h") and not is_seen("seen_well", str(well[0]["d"])):
        try:
            msg = f"Brief réveil : {json.dumps(well[0], ensure_ascii=False)}{AUTO_SUFFIX}"
            ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg), allow_tools=False)
            _push("☀️ <b>Réveil détecté</b>", ans, "Brief auto")
            mark_seen("seen_well", str(well[0]["d"]))
        except AIError as e:
            logging.warning(f"Brief réveil différé : {e}")

    # 3. Bilan hebdomadaire (dimanche soir, heure locale)
    now = datetime.datetime.now()
    if now.weekday() == 6 and now.hour >= 19:
        iso = now.isocalendar()
        week_id = f"bilan_{iso[0]}_{iso[1]}"
        if not is_seen("seen_reports", week_id):
            try:
                msg = ("C'est dimanche soir. Rédige le BILAN HEBDOMADAIRE complet : volume en km réalisé vs prévu, "
                       "charge, fatigue, et présente les 3 séances clés de la semaine à venir." + AUTO_SUFFIX)
                ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg), allow_tools=False)
                _push("📊 <b>Bilan hebdomadaire du Coach</b>", ans, "Bilan Hebdo")
                mark_seen("seen_reports", week_id)
            except AIError as e:
                logging.warning(f"Bilan hebdo différé : {e}")


def bg_loop():
    time.sleep(20)
    bg_init()
    while True:
        time.sleep(BG_INTERVAL)
        try:
            bg_tick()
        except Exception:
            logging.exception("Erreur boucle bg_loop")


# --------------------------------------------------------------------------
# DEMARRAGE
# --------------------------------------------------------------------------
def apply_timezone():
    """Aligne l'horloge du serveur sur ton fuseau (TZ_NAME, sinon celui du profil Intervals)."""
    tz_name = os.environ.get("TZ_NAME", "").strip() or (fetch_profile().get("timezone") or "")
    if not tz_name:
        logging.warning("Fuseau inconnu : les dates suivent l'horloge du serveur. Définis TZ_NAME (ex: Europe/Paris).")
        return
    try:
        ZoneInfo(tz_name)
        os.environ["TZ"] = tz_name
        if hasattr(time, "tzset"):
            time.tzset()
        logging.info(f"Fuseau appliqué : {tz_name} (il est {datetime.datetime.now():%Y-%m-%d %H:%M})")
    except Exception as err:
        logging.warning(f"Fuseau '{tz_name}' non appliqué ({err}). Installe 'tzdata' si besoin.")


def check_config():
    missing = [n for n, v in (("INTERVALS_API_KEY", INTERVALS_KEY), ("GEMINI_API_KEY", GEMINI_KEY),
                              ("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_USER_ID", TG_USER)) if not v]
    if missing:
        raise SystemExit(f"Variables d'environnement manquantes : {', '.join(missing)}")


if __name__ == "__main__":
    check_config()
    init_db()
    apply_timezone()
    logging.info(f"Bot Coach Running démarré (modèle : {MODEL_NAME})")
    threading.Thread(target=bg_loop, daemon=True).start()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)
