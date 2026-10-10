"""Coach running : bot Telegram (Intervals.icu + Gemini + Open-Meteo + SQLite)."""
import os
import io
import re
import json
import hashlib
import math
import time
import zlib
import bisect
import html
import sqlite3
import gzip
import shutil
import tempfile
import datetime
import asyncio
import contextlib
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
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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
ATHLETE_NAME = os.environ.get("ATHLETE_NAME", "").strip()      # prénom utilisé par le coach (optionnel)
COACH_TONE = os.environ.get("COACH_TONE", "").strip()          # ex. : "taquin et direct", "très encourageant", "sobre et factuel"

BG_INTERVAL = 900               # secondes entre deux verifications automatiques
MAX_DEBRIEFS_PER_CYCLE = 2      # limite d'appels Gemini automatiques par cycle
MAX_UNAVAILABILITY_DAYS = 14    # plage maximale supprimable en une demande
DATA_TTL = 120                  # cache des donnees Intervals (secondes)
DETAIL_RUNS = int(os.environ.get("DETAIL_RUNS", "6"))   # courses détaillées (laps + durée en zones)
DETAIL_FULL = 3                 # parmi elles, nb de courses avec splits au kilomètre
DETAIL_VERSION = 3              # incrémenter pour forcer le recalcul du cache de détails
STREAM_VERSION = 2
RAW_DAYS = int(os.environ.get("RAW_DAYS", "90"))   # activités listées une par une ; au-delà (jusqu'à 6 mois) : agrégats hebdo
DESC_DAYS = int(os.environ.get("DESC_DAYS", "21"))  # descriptions de séances envoyées à +/- N jours (noms seuls au-delà)
WELLNESS_FULL_DAYS = int(os.environ.get("WELLNESS_FULL_DAYS", "90"))   # jours de santé envoyés un par un
PRICE_IN = float(os.environ.get("PRICE_IN", "0.75"))          # $ par million de tokens (tarif Gemini 3.8 Flash jusqu'au 31/12/2026)
PRICE_CACHED = float(os.environ.get("PRICE_CACHED", "0.075"))
PRICE_OUT = float(os.environ.get("PRICE_OUT", "3.75"))        # la réflexion est facturée comme de la sortie
REMINDER_LEAD_MIN = int(os.environ.get("REMINDER_LEAD_MIN", "60"))   # rappel météo N minutes avant une séance
EVE_HOUR = int(os.environ.get("EVE_HOUR", "20"))                    # message de la veille de séance (99 pour le désactiver)
FOLLOWUP_HOUR = int(os.environ.get("FOLLOWUP_HOUR", "21"))          # relance si la séance du jour n'est pas détectée
TRAIN_HOUR = int(os.environ.get("TRAIN_HOUR", "18"))                # heure par défaut si tes courses n'ont pas d'horaire habituel
THINKING_ROUTINE = os.environ.get("THINKING_ROUTINE", "low").strip().lower()    # gestion du calendrier (planifier, décaler...)
THINKING_CHAT = os.environ.get("THINKING_CHAT", "medium").strip().lower()       # conversation, brief réveil, bilan
THINKING_DEEP = os.environ.get("THINKING_DEEP", "high").strip().lower()         # analyse d'une séance avec son CSV
STREAM_STEP_S = int(os.environ.get("STREAM_STEP_S", "5"))   # résolution de la série envoyée à Gemini (s)
SERIES_MAX_ROWS = 1200          # plafond de lignes (mode normal)
SERIES_FULL_MAX_ROWS = 4000     # plafond de lignes (mode « csv complet », 1 point par seconde)
MAX_LAPS = 30

ai_client = genai.Client(api_key=GEMINI_KEY)
AUTH = HTTPBasicAuth("API_KEY", INTERVALS_KEY)
API_ROOT = "https://intervals.icu/api/v1"
BASE = f"{API_ROOT}/athlete/{ATHLETE_ID}"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
DB = os.environ.get("DB_PATH", "coach_brain.db").strip()    # sur Railway : DB_PATH=/data/coach_brain.db avec un Volume monté sur /data
STARTUP_DELAY_S = int(os.environ.get("STARTUP_DELAY_S", "25"))   # laisse l'ancienne instance s'arrêter avant de lancer le polling
POOL = ThreadPoolExecutor(max_workers=5)
AI_LOCK = threading.Lock()

JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
JOURS_MAP = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
KEEP_CATEGORIES = {"WORKOUT", "RACE_A", "RACE_B", "RACE_C", "SICK", "INJURY", "HOLIDAY"}
LOAD_DANGER, LOAD_EXTREME = 1.8, 2.0     # au-delà, le risque est élevé même si la récupération est bonne (extrême), ou sauf si elle est bonne (1,8)
LOAD_LOW, LOAD_CAUTION, LOAD_HIGH = 0.8, 1.3, 1.5   # ratio de charge 7 j / 28 j : plage optimale 0,8 à 1,5
ALLOWED_REACTIONS = {"👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🎉", "🤩", "👌", "💯", "⚡", "🏆", "😎", "🤝", "🫡"}


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
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10)    # WAL : lectures et écritures simultanées (boucle de fond + conversation) sans « database is locked »
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    finally:
        conn.close()
    db_exec("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, txt TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_reports (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, txt TEXT, d TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS deleted_events (id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, d TEXT, payload TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS act_details (id TEXT PRIMARY KEY, v INTEGER, payload TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS act_streams (id TEXT PRIMARY KEY, v INTEGER, blob BLOB)")
    db_exec("CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY AUTOINCREMENT, due TEXT, txt TEXT, done INTEGER DEFAULT 0, created TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS efforts (id TEXT PRIMARY KEY, d TEXT, nom TEXT, pace_s INTEGER, hr INTEGER, duree_s INTEGER)")
    try:   # conserve l'historique des meilleurs efforts avant de purger les anciens détails mis en cache
        for (payload,) in db_exec("SELECT payload FROM act_details", fetch=True) or []:
            for pt in _effort_points([json.loads(payload)]):
                save_effort(pt)
    except Exception:
        logging.exception("migration des efforts")
    db_exec("DELETE FROM act_details WHERE v != ?", (DETAIL_VERSION,))
    db_exec("DELETE FROM act_streams WHERE v != ?", (STREAM_VERSION,))
    db_exec("DELETE FROM deleted_events WHERE d < date('now', '-30 day')")


def save_note(txt: str):
    db_exec("INSERT INTO notes (d, txt) VALUES (?, ?)", (datetime.date.today().isoformat(), txt[:500]))


def get_notes():
    rows = db_exec("SELECT d, txt FROM notes ORDER BY id DESC LIMIT 30", fetch=True)
    return [{"date": r[0], "note": r[1]} for r in reversed(rows)]


def save_chat_msg(role: str, text: str):
    db_exec("INSERT INTO chat_history (role, txt, d) VALUES (?, ?, ?)",
            (role, text[:1500], datetime.datetime.now().isoformat()))
    db_exec("DELETE FROM chat_history WHERE id NOT IN (SELECT id FROM chat_history ORDER BY id DESC LIMIT 200)")


def get_chat_history(limit: int = 10):
    rows = db_exec("SELECT role, txt FROM chat_history ORDER BY id DESC LIMIT ?", (limit,), fetch=True)
    return [{"role": r[0], "text": r[1][:(600 if r[0] == "athlete" else 350)]} for r in reversed(rows)]


def is_seen(table: str, key: str) -> bool:
    col = "d" if table == "seen_well" else "id"
    return bool(db_exec(f"SELECT 1 FROM {table} WHERE {col}=?", (key,), fetch=True))


def mark_seen(table: str, key: str):
    db_exec(f"INSERT OR IGNORE INTO {table} VALUES (?)", (key,))


def claim(table: str, key: str) -> bool:
    """Réserve atomiquement une clé AVANT de générer le message. False si déjà prise (autre thread/instance)."""
    col = "d" if table == "seen_well" else "id"
    conn = sqlite3.connect(DB, timeout=10)
    try:
        cur = conn.execute(f"INSERT OR IGNORE INTO {table} ({col}) VALUES (?)", (key,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release(table: str, key: str):
    col = "d" if table == "seen_well" else "id"
    db_exec(f"DELETE FROM {table} WHERE {col}=?", (key,))


def kv_get(k: str):
    rows = db_exec("SELECT v FROM kv WHERE k=?", (k,), fetch=True)
    return rows[0][0] if rows else None


def kv_set(k: str, v: str):
    db_exec("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v))


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
WEATHER_TTL = 900


def _comfort_penalty(r):
    """Plus c'est bas, mieux c'est pour courir : confort thermique, pluie, vent."""
    ress = r.get("ress_c")
    if ress is None:
        return None
    return (max(0.0, ress - 16) * 1.0 + max(0.0, 6 - ress) * 0.8 + (r.get("pluie_pct") or 0) * 0.05
            + (r.get("pluie_mm") or 0) * 5 + max(0.0, (r.get("vent_kmh") or 0) - 20) * 0.3)


def best_windows(rows, n=3):
    cand = []
    for r in rows:
        try:
            hh = int(r["t"][11:13])
        except Exception:
            continue
        pen = _comfort_penalty(r) if 5 <= hh <= 21 else None
        if pen is not None:
            cand.append((pen, r))
    cand.sort(key=lambda x: x[0])
    return [f"{r['h']} (ressenti {r['ress_c']}°C, pluie {r['pluie_pct']} %, vent {r['vent_kmh']} km/h)" for _, r in cand[:n]]


def weather_window(weather, start, hours=2):
    """Lignes horaires couvrant une séance qui démarre à `start` (datetime local)."""
    rows = (weather or {}).get("horaire") or []
    key = start.strftime("%Y-%m-%dT%H:00")
    for i, r in enumerate(rows):
        if r.get("t") == key:
            return rows[i:i + hours]
    return []


def weather_tips(rows):
    tips = []
    if not rows:
        return tips
    ress = [r["ress_c"] for r in rows if r.get("ress_c") is not None]
    if max((r.get("pluie_pct") or 0) for r in rows) >= 50:
        tips.append("pluie probable : veste imperméable, ou décale d'un créneau")
    if ress and min(ress) <= 6:
        tips.append("froid : manches longues, gants fins, échauffement plus long")
    if ress and max(ress) >= 24:
        tips.append("chaleur : hydrate-toi et ralentis de 5 à 10 s/km")
    if max((r.get("vent_kmh") or 0) for r in rows) >= 30:
        tips.append("vent fort : pars face au vent pour rentrer vent dans le dos")
    return tips


def fetch_weather(city_name: str = ""):
    city = (city_name or "").strip() or DEFAULT_CITY
    cached = _WEATHER_CACHE.get(city)
    if cached and time.time() - cached[0] < WEATHER_TTL:
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
                    "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,wind_speed_10m",
                    "forecast_days": 3,
                    "timezone": "auto",
                },
                timeout=8,
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

                hourly = data.get("hourly", {})
                times = hourly.get("time", [])
                cur_time = str(cur.get("time") or "")
                cur_date = cur_time[:10] or (times[0][:10] if times else "")
                start_i = next((i for i, t in enumerate(times) if t >= cur_time[:13] + ":00"), 0)

                def hv(key, i, nd=0):
                    arr = hourly.get(key, [])
                    v = arr[i] if i < len(arr) else None
                    return None if v is None else (round(v, nd) if nd else int(round(v)))

                rows = []
                for i in range(start_i, min(start_i + 24, len(times))):
                    day = times[i][:10]
                    tomorrow = (datetime.date.fromisoformat(cur_date) + datetime.timedelta(days=1)).isoformat() if cur_date else ""
                    label = "auj" if day == cur_date else ("dem" if day == tomorrow else day[5:])
                    rows.append({"t": times[i], "h": f"{label} {times[i][11:13]}h", "temp_c": hv("temperature_2m", i),
                                 "ress_c": hv("apparent_temperature", i), "pluie_pct": hv("precipitation_probability", i),
                                 "pluie_mm": hv("precipitation", i, 1), "vent_kmh": hv("wind_speed_10m", i)})
                out = {
                    "ville": g.get("name", city),
                    "maintenant": {
                        "heure_locale": cur_time[11:16] or None,
                        "temp_c": cur.get("temperature_2m"),
                        "ressenti_c": cur.get("apparent_temperature"),
                        "vent_kmh": cur.get("wind_speed_10m"),
                        "pluie_mm": cur.get("precipitation"),
                    },
                    "previsions_3j": forecast,
                    "creneaux_favorables_24h": best_windows(rows),
                    "horaire": rows,
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
                if s_sec or j.get("hrv") or j.get("ctl") or j.get("weight"):
                    res.append({
                        "d": j.get("id"),
                        "sleep_h": round(s_sec / 3600, 1) if s_sec else None,
                        "hrv": j.get("hrv"),
                        "hrv_base": j.get("hrvBaseline"),
                        "rhr": j.get("restingHR"),
                        "tsb": j.get("form"),
                        "atl": j.get("atl"),
                        "ctl": j.get("ctl"),
                        "weight": j.get("weight"),
                        "sleep_score": j.get("sleepScore"),
                        "readiness": j.get("readiness"),
                        "vo2max": j.get("vo2max"),
                        "fatigue": j.get("fatigue"),
                        "soreness": j.get("soreness"),
                        "stress": j.get("stress"),
                        "mood": j.get("mood"),
                        "motivation": j.get("motivation"),
                    })
            return res
        logging.error(f"fetch_wellness : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_wellness : {err}")
    return []


RAMP_LIMIT = 5.0              # points de CTL gagnés en 7 jours


def _age_days(d_str, today=None):
    try:
        return ((today or datetime.date.today()) - datetime.date.fromisoformat(str(d_str))).days
    except Exception:
        return None


def _vals(well, field, a_from, a_to):
    """Valeurs numériques d'un champ de santé pour les jours a_from..a_to en arrière (0 = aujourd'hui)."""
    out = []
    for w in well or []:
        age = _age_days(w.get("d"))
        v = w.get(field)
        if age is not None and a_from <= age <= a_to and isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(v)
    return out


def _avg(vals):
    return sum(vals) / len(vals) if vals else None


def _std(vals):
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))


def _entry_near(well, age_target, max_gap=3, field="ctl"):
    best = None
    for w in well or []:
        age = _age_days(w.get("d"))
        if age is None or not (w.get(field) or 0) > 0:
            continue
        gap = abs(age - age_target)
        if gap <= max_gap and (best is None or gap < best[0]):
            best = (gap, w)
    return best[1] if best else None


def _daily_loads(acts, days):
    """Charge d'entraînement par jour, index 0 = aujourd'hui."""
    today = datetime.date.today()
    loads = {}
    for a in (acts or {}).get("brut_recent", []):
        loads[a["d"]] = loads.get(a["d"], 0) + (a.get("load") or 0)
    return [loads.get((today - datetime.timedelta(days=i)).isoformat(), 0) for i in range(days)]


def _week_km(acts):
    km = {}
    for a in (acts or {}).get("brut_recent", []):
        if a.get("type") in RUN_TYPES:
            try:
                ws = _monday(datetime.date.fromisoformat(a["d"]))
            except Exception:
                continue
            km[ws] = km.get(ws, 0.0) + (a.get("km") or 0)
    return km


def _training_streak(acts):
    today = datetime.date.today()
    days = {a["d"] for a in (acts or {}).get("brut_recent", []) if (a.get("min") or 0) >= 15 or (a.get("km") or 0) >= 2}
    k, n = (0 if today.isoformat() in days else 1), 0
    while (today - datetime.timedelta(days=k + n)).isoformat() in days:
        n += 1
    return n


def recovery_profile(well):
    """Croise HRV, sommeil et FC repos : la même charge ne coûte pas pareil quand le corps récupère bien. Score de -6 à +6."""
    score, pts, seen = 0, [], 0
    h7, h28 = _avg(_vals(well, "hrv", 0, 6)), _avg(_vals(well, "hrv", 0, 27))
    if h7 and h28:
        seen += 1
        r = h7 / h28
        score += 2 if r >= 1.0 else 1 if r >= 0.95 else 0 if r >= 0.90 else -2
        pts.append(f"HRV 7 j {(r - 1) * 100:+.0f} % vs 28 j")
    last = next((w for w in well if w.get("hrv") and w.get("hrv_base")), None)
    if last:
        r = last["hrv"] / last["hrv_base"]
        score += 1 if r >= 1.0 else -1 if r < 0.90 else 0
        pts.append(f"dernière HRV {(r - 1) * 100:+.0f} % vs référence")
    sl = _avg(_vals(well, "sleep_h", 0, 6))
    if sl is not None:
        seen += 1
        score += 2 if sl >= 7.5 else 1 if sl >= 7.0 else 0 if sl >= 6.5 else -1 if sl >= 6.0 else -2
        pts.append(f"sommeil moyen {sl:.1f} h sur 7 jours")
    r3, r28 = _avg(_vals(well, "rhr", 0, 2)), _avg(_vals(well, "rhr", 7, 34))
    if r3 and r28:
        seen += 1
        score += 1 if r3 - r28 <= 2 else -2 if r3 - r28 >= 5 else 0
        pts.append(f"FC repos {r3 - r28:+.0f} bpm vs base")
    label = "inconnue" if seen < 2 else "excellente" if score >= 4 else "bonne" if score >= 2 else "moyenne" if score >= 0 else "basse"
    return {"score": score, "label": label, "points": pts}


def evaluate_injury_risk(well, acts=None):
    """Risque de surcharge. Référence : le ratio de charge 7 j / 28 j (plage optimale 0,8 à 1,5). Le ratio ATL/CTL est gonflé après une coupure : il ne sert qu'en dernier recours.
    La charge est croisée avec la récupération (HRV, sommeil, FC repos) : charge haute + bonne récupération = MAITRISEE (pas une alerte).
    DANGER : ratio au-dessus de la plage ET une confirmation sans bonne récupération, ratio >= 1,8 sans bonne récupération, ratio >= 2,0,
    ou ratio haut + 2 signaux physiologiques. VIGILANCE : ratio en haut de plage ou un signal isolé."""
    if not well:
        return {"acwr": None, "acwr_glissant": None, "rampe_ctl_7j": None, "ratio_charge": None, "alerte_surcharge": False,
                "alerte_vrc": False, "statut": "INCONNU", "details": ["Données de santé indisponibles"]}

    today = datetime.date.today()
    ref = next((w for w in well if (w.get("atl") or 0) > 0 and (w.get("ctl") or 0) > 0), None)
    acwr = round(ref["atl"] / ref["ctl"], 2) if ref else None
    ramp = None
    if ref:
        old = _entry_near(well, (_age_days(ref.get("d")) or 0) + 7)
        if old is not None:
            ramp = round(ref["ctl"] - old["ctl"], 1)
    ctl_max = max((w.get("ctl") or 0 for w in well), default=0)
    reprise = bool(ref and ctl_max > 0 and ref["ctl"] < 0.75 * ctl_max)

    rolling = None
    if acts and acts.get("brut_recent"):
        loads = _daily_loads(acts, 28)
        chronic_week = sum(loads) / 4
        if chronic_week > 0:
            rolling = round(sum(loads[:7]) / chronic_week, 2)

    if rolling is not None:
        ratio, source = rolling, "Intervals 7 j / 28 j"
    elif acwr is not None and not reprise:
        ratio, source = acwr, "ATL/CTL"
    else:
        ratio, source = None, None

    pts = [w for w in well if w.get("hrv") and w.get("hrv_base")][:2]
    alerte_vrc = len(pts) == 2 and all(w["hrv"] < w["hrv_base"] * 0.90 for w in pts)
    rhr_recent, rhr_base = _avg(_vals(well, "rhr", 0, 2)), _avg(_vals(well, "rhr", 7, 34))
    rhr_delta = round(rhr_recent - rhr_base, 1) if rhr_recent and rhr_base else None
    sleep7 = _avg(_vals(well, "sleep_h", 0, 6))

    charge, confirm, physio, level = [], [], [], 0
    if ratio is not None:
        if ratio >= LOAD_HIGH:
            level = 2
            charge.append(f"ratio de charge {ratio:g} ({source}) au-dessus de la plage optimale ({LOAD_LOW:g} à {LOAD_HIGH:g})")
        elif ratio >= LOAD_CAUTION:
            level = 1
            charge.append(f"ratio de charge {ratio:g} ({source}) en haut de la plage optimale ({LOAD_LOW:g} à {LOAD_HIGH:g})")
    km = _week_km(acts)
    this = _monday(today)
    last = km.get(this - datetime.timedelta(days=7), 0.0)
    prev = [km.get(this - datetime.timedelta(days=7 * k), 0.0) for k in (2, 3, 4)]
    if sum(prev) > 0 and last >= 10 and last / (sum(prev) / 3) - 1 > 0.30:
        confirm.append(f"volume {(last / (sum(prev) / 3) - 1) * 100:+.0f} % la semaine dernière par rapport aux 3 précédentes")
    streak = _training_streak(acts)
    if streak >= 6:
        confirm.append(f"{streak} jours d'activité consécutifs")
    if alerte_vrc:
        physio.append("VRC sous 90 % de la référence sur les deux dernières mesures")
    if rhr_delta is not None and rhr_delta >= 5:
        physio.append(f"FC repos +{rhr_delta:g} bpm par rapport à la base")
    if sleep7 is not None and sleep7 < 6.0:
        physio.append(f"sommeil moyen {sleep7:.1f} h sur 7 jours")

    recup = recovery_profile(well)
    good = recup["label"] in ("excellente", "bonne") and not physio
    if ((level >= 2 and (confirm or physio) and not good) or (ratio is not None and ratio >= LOAD_EXTREME)
            or (ratio is not None and ratio >= LOAD_DANGER and not good) or (level >= 1 and len(physio) >= 2)):
        statut = "DANGER"
    elif level >= 1 and good:
        statut = "MAITRISEE"
    elif level >= 1 or physio:
        statut = "VIGILANCE"
    elif ratio is None and len(pts) < 2:
        statut = "INCONNU"
    else:
        statut = "OPTIMAL"
    return {"acwr": acwr, "acwr_glissant": rolling, "rampe_ctl_7j": ramp, "ratio_charge": ratio, "ratio_source": source,
            "reprise": reprise, "alerte_surcharge": level >= 1, "alerte_vrc": alerte_vrc, "statut": statut,
            "signaux_charge": charge + confirm, "signaux_physio": physio, "details": charge + confirm + physio,
            "recup": recup, "signaux_recup": recup["points"]}


# ---- Tableau de bord : indicateurs calculés par le code (un LLM calcule mal des moyennes sur 90 lignes)
def _pace_sec(p):
    mt = re.match(r"\s*(\d+)'(\d{2})", str(p or ""))
    return int(mt.group(1)) * 60 + int(mt.group(2)) if mt else None


def _monday(d):
    return d - datetime.timedelta(days=d.weekday())


def _dash_recup(c):
    well, parts = c["well"], []
    row = next((w for w in well if w.get("hrv") or w.get("sleep_h")), None)
    if row:
        seg = []
        if row.get("sleep_h") is not None:
            seg.append(f"sommeil {_cell(row['sleep_h'])} h")
        if row.get("hrv"):
            t = f"HRV {_cell(row['hrv'])}"
            if row.get("hrv_base"):
                t += f" (réf {_cell(row['hrv_base'])}, {(row['hrv'] / row['hrv_base'] - 1) * 100:+.0f} %)"
            seg.append(t)
        if row.get("rhr"):
            seg.append(f"FC repos {_cell(row['rhr'])}")
        parts.append(f"dernière mesure {row.get('d')} : " + ", ".join(seg))
    h7, h28 = _avg(_vals(well, "hrv", 0, 6)), _avg(_vals(well, "hrv", 0, 27))
    if h7 and h28:
        t = f"HRV 7 j {h7:.0f} vs 28 j {h28:.0f} ({(h7 / h28 - 1) * 100:+.0f} %)"
        sd = _std(_vals(well, "hrv", 0, 6))
        if sd:
            t += f", variabilité {sd / h7 * 100:.0f} %"
        parts.append(t)
    r3, r28 = _avg(_vals(well, "rhr", 0, 2)), _avg(_vals(well, "rhr", 7, 34))
    if r3 and r28:
        parts.append(f"FC repos 3 j {r3:.0f} vs 8-35 j {r28:.0f} ({r3 - r28:+.0f} bpm)")
    sl = _vals(well, "sleep_h", 0, 6)
    if sl:
        parts.append(f"sommeil moyen 7 j {_avg(sl):.1f} h, {sum(1 for x in sl if x < 6.5)} nuit(s) sous 6h30")
    rc = (c.get("risk") or {}).get("recup")
    if rc and rc["label"] != "inconnue":
        parts.append(f"synthèse HRV + sommeil + FC repos : récupération {rc['label']} ({rc['score']:+d} sur 6)")
    return "RÉCUPÉRATION : " + " | ".join(parts) if parts else None


def _dash_charge(c):
    well, acts, risk = c["well"], c["acts"], c["risk"]
    parts = []
    loads = _daily_loads(acts, 28)
    if sum(loads) > 0:
        acute, chronic = sum(loads[:7]), sum(loads) / 4
        ratio = acute / chronic
        tag = "optimale" if LOAD_LOW <= ratio <= LOAD_HIGH else ("haute" if ratio > LOAD_HIGH else "basse")
        t = (f"RATIO DE CHARGE 7 j / 28 j {ratio:.2f} ({tag} : plage optimale {LOAD_LOW:g} à {LOAD_HIGH:g} ; "
             f"charge 7 j {acute:.0f} vs moyenne hebdo 28 j {chronic:.0f})")
        l7 = loads[:7]
        sd = _std(l7)
        if sd:
            mono = (sum(l7) / 7) / sd
            t += f" ; monotonie {mono:.1f}, tension {acute * mono:.0f}"
        parts.append(t)
    ew = ewma_acwr(acts)
    if ew is not None:
        gap_m = f" ; les deux méthodes divergent ({risk.get('ratio_charge')} en moyenne simple) : un pic isolé ou une reprise, relativise" \
            if risk.get("ratio_charge") and abs(ew - risk["ratio_charge"]) > 0.3 else ""
        parts.append(f"ratio EWMA 7 j / 28 j (moyennes mobiles exponentielles) : {ew}{gap_m}")
    ref = next((w for w in well if (w.get("atl") or 0) > 0 and (w.get("ctl") or 0) > 0), None)
    if ref:
        age = _age_days(ref.get("d")) or 0
        c7, c28 = _entry_near(well, age + 7), _entry_near(well, age + 28, max_gap=4)
        bits = []
        if c7:
            bits.append(f"{ref['ctl'] - c7['ctl']:+.1f} en 7 j")
        if c28:
            bits.append(f"{(ref['ctl'] - c28['ctl']) / 4:+.1f}/sem sur 28 j")
        t = (f"ATL/CTL Intervals : CTL {_cell(ref['ctl'])}" + (f" ({', '.join(bits)})" if bits else "")
             + f", ATL {_cell(ref['atl'])}, TSB {_cell(ref.get('tsb'))}, ratio ATL/CTL {risk.get('acwr')}")
        if risk.get("reprise"):
            ctl_max = max((w.get("ctl") or 0 for w in well), default=0)
            t += f" → peu fiable en reprise (CTL {ref['ctl']:.0f} pour un pic de {ctl_max:.0f} sur 90 j) : ne pas s'en servir pour alerter"
        parts.append(t)
    return "CHARGE : " + " | ".join(parts) if parts else None



def _easy_weeks(c):
    lthr = (c["prof"] or {}).get("lthr")
    if not lthr:
        return []
    thr, weeks = round(lthr * 0.85), {}
    for a in c["recent"]:
        if a.get("type") in RUN_TYPES and a.get("hr") and a["hr"] <= thr and (a.get("km") or 0) >= 4 and (a.get("min") or 0) > 0:
            ws = _monday(datetime.date.fromisoformat(a["d"]))
            w = weeks.setdefault(ws, {"sec": 0.0, "km": 0.0, "hr_w": 0.0})
            w["sec"] += a["min"] * 60
            w["km"] += a["km"]
            w["hr_w"] += a["hr"] * a["min"]
    return [(ws, w["sec"] / w["km"], w["hr_w"] / (w["sec"] / 60)) for ws, w in sorted(weeks.items(), reverse=True)]


def _dash_faits(c):
    """Vraies réussites détectées par le code : de quoi féliciter précisément, sans rien inventer."""
    today, recent, well = c["today"], c["recent"], c["well"]
    runs = [a for a in recent if a.get("type") in RUN_TYPES and (a.get("km") or 0) >= 1]
    out = []
    try:
        if len(runs) >= 8 and (_age_days(runs[0]["d"], today) or 99) <= 2:
            longest = max(runs, key=lambda a: a["km"])
            if longest["id"] == runs[0]["id"] and longest["km"] >= 8:
                out.append(f"plus longue sortie des {RAW_DAYS} derniers jours : {longest['km']:.1f} km")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    try:
        km = _week_km(c["acts"])
        last = _monday(today) - datetime.timedelta(days=7)
        hist = [v for k, v in km.items() if k < _monday(today) and k >= _monday(today) - datetime.timedelta(days=7 * 12)]
        if len(hist) >= 4 and km.get(last, 0) >= max(hist) and km.get(last, 0) >= 15:
            out.append(f"semaine du {last.isoformat()} : record de volume sur 12 semaines ({km[last]:.1f} km)")
        weeks_ok = 0
        for k in range(1, 9):
            ws = _monday(today) - datetime.timedelta(days=7 * k)
            if sum(1 for a in runs if _monday(datetime.date.fromisoformat(a["d"])) == ws) >= 3:
                weeks_ok += 1
            else:
                break
        if weeks_ok >= 3:
            out.append(f"{weeks_ok} semaines d'affilée avec au moins 3 courses")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    try:
        ew = _easy_weeks(c)
        if len(ew) >= 3 and ew[0][1] < min(x[1] for x in ew[1:]) - 4:
            out.append(f"allure facile record à FC basse : {_mmss(ew[0][1])} @ {ew[0][2]:.0f} bpm (précédent meilleur {_mmss(min(x[1] for x in ew[1:]))})")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    try:
        hist = effort_history(c["details"])
        if len(hist) >= 3 and hist[-1]["pace_s"] < min(h["pace_s"] for h in hist[:-1]) and (_age_days(hist[-1]["d"], today) or 99) <= 3:
            out.append(f"meilleur effort structuré jamais enregistré : {_mmss(hist[-1]['pace_s'])}/km (précédent {_mmss(min(h['pace_s'] for h in hist[:-1]))})")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    try:
        hrv = _vals(well, "hrv", 0, 27)
        if len(hrv) >= 10 and hrv[0] >= max(hrv) and (_age_days(next((w["d"] for w in well if w.get("hrv")), None), today) or 99) <= 2:
            out.append(f"HRV au plus haut depuis 4 semaines ({hrv[0]:.0f})")
        rhr = _vals(well, "rhr", 0, 27)
        if len(rhr) >= 10 and min(rhr[:3]) <= min(rhr[3:]) - 1:
            out.append(f"FC de repos au plus bas depuis 4 semaines ({min(rhr[:3]):.0f})")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    try:
        for g in (c["prof"] or {}).get("materiel") or []:
            mt = re.search(r"^(.*?) \(.*\) : (\d+) km", g)
            if mt and int(mt.group(2)) >= 600:
                out.append(f"{mt.group(1)} : {mt.group(2)} km, zone de renouvellement à partir de 700-800 km")
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    return ("FAITS MARQUANTS (détectés par le code : félicite uniquement ce qui est réel) : " + " | ".join(out[:4])) if out else None


def _dash_semaines(c):
    today, recent = c["today"], c["recent"]
    if not recent:
        return None
    weeks = {}
    for a in recent:
        try:
            ws = _monday(datetime.date.fromisoformat(a["d"]))
        except Exception:
            continue
        w = weeks.setdefault(ws, {"km": 0.0, "runs": 0, "load": 0.0, "long": 0.0, "other": 0})
        if a.get("type") in RUN_TYPES:
            w["km"] += a.get("km") or 0
            w["runs"] += 1
            w["long"] = max(w["long"], a.get("km") or 0)
        else:
            w["other"] += 1
        w["load"] += a.get("load") or 0
    this = _monday(today)
    rows = []
    for k in range(5):
        ws = this - datetime.timedelta(days=7 * k)
        w = weeks.get(ws, {"km": 0.0, "runs": 0, "load": 0.0, "long": 0.0, "other": 0})
        rows.append(f"sem. du {ws.isoformat()}{' (en cours)' if k == 0 else ''} : {w['km']:.1f} km en {w['runs']} course(s), "
                    f"sortie longue {w['long']:.1f} km, {w['other']} autre(s) activité(s), charge {w['load']:.0f}")
    out = "SEMAINES : " + " | ".join(rows)
    prev = [weeks.get(this - datetime.timedelta(days=7 * k), {"km": 0.0})["km"] for k in (2, 3, 4)]
    last = weeks.get(this - datetime.timedelta(days=7), {"km": 0.0})["km"]
    if sum(prev) > 0:
        out += f"\nPROGRESSION : semaine précédente {last:.1f} km vs moyenne des 3 semaines d'avant {sum(prev) / 3:.1f} km ({(last / (sum(prev) / 3) - 1) * 100:+.0f} %)"
    return out


def _dash_semaine_plan(c):
    today, recent = c["today"], c["recent"]
    this = _monday(today)
    lo, hi = this.isoformat(), (this + datetime.timedelta(days=6)).isoformat()
    planned = sorted((e for e in c["evts"] if e.get("category") in (None, "WORKOUT") and e.get("type") in (None, *RUN_TYPES)
                      and lo <= e.get("d", "") <= hi), key=lambda e: e["d"])
    if not planned:
        return None
    runs = [a for a in recent if a.get("type") in RUN_TYPES]
    out = []
    for e in planned:
        d = datetime.date.fromisoformat(e["d"])
        near = [a for a in runs if abs((datetime.date.fromisoformat(a["d"]) - d).days) <= 1]
        match = next((a for a in near if a["d"] == e["d"]), near[0] if near else None)
        label = f"{_cell(e.get('nom'))} ({JOURS_FR[d.weekday()][:3]} {d:%d/%m})"
        if match:
            out.append(f"{label} → fait : {match.get('min', 0):.0f} min, {_cell(match.get('pace'))}, FC {_cell(match.get('hr'))}")
        elif e["d"] < today.isoformat():
            out.append(f"{label} → non faite")
        else:
            out.append(f"{label} → {'aujourd hui' if e['d'] == today.isoformat() else 'à venir'}")
    return "SEMAINE EN COURS, prévu → réalisé : " + " | ".join(out)


def _dash_rythme(c):
    today, recent = c["today"], c["recent"]
    days = set()
    for a in recent:
        if (a.get("min") or 0) >= 15 or (a.get("km") or 0) >= 2:
            days.add(a["d"])
    if not days:
        return None
    k = 0 if today.isoformat() in days else 1
    streak = 0
    while (today - datetime.timedelta(days=k + streak)).isoformat() in days:
        streak += 1
    last7 = sum(1 for i in range(7) if (today - datetime.timedelta(days=i)).isoformat() in days)
    return f"RYTHME : {streak} jour(s) d'activité consécutifs, {7 - last7} jour(s) de repos sur les 7 derniers"


def _dash_allure_facile(c):
    lthr = (c["prof"] or {}).get("lthr")
    if not lthr:
        return None
    thr = round(lthr * 0.85)
    weeks = {}
    for a in c["recent"]:
        if a.get("type") in RUN_TYPES and a.get("hr") and a["hr"] <= thr and (a.get("km") or 0) >= 4 and (a.get("min") or 0) > 0:
            ws = _monday(datetime.date.fromisoformat(a["d"]))
            w = weeks.setdefault(ws, {"sec": 0.0, "km": 0.0, "hr_w": 0.0, "n": 0})
            w["sec"] += a["min"] * 60
            w["km"] += a["km"]
            w["hr_w"] += a["hr"] * a["min"]
            w["n"] += 1
    if not weeks:
        return None
    rows = [f"{ws.isoformat()} {_mmss(w['sec'] / w['km'])}@{w['hr_w'] / (w['sec'] / 60):.0f} ({w['n']})"
            for ws, w in sorted(weeks.items(), reverse=True)[:8]]
    return (f"ALLURE FACILE (courses de 4 km et plus à FC moyenne ≤ {thr}, soit 85 % de la FC seuil {lthr}), par semaine, "
            f"format allure@FC (nb de courses) : " + " | ".join(rows))


def _dash_zones(c):
    tot = None
    for d in c["details"]:
        age = _age_days(d.get("d"))
        z = d.get("temps_zones_fc_s")
        if age is not None and age <= 14 and isinstance(z, list) and z:
            tot = z[:] if tot is None else [a + b for a, b in zip(tot + [0] * (len(z) - len(tot)), z + [0] * (len(tot) - len(z)))]
    if not tot or sum(tot) <= 0:
        return None
    return "ZONES FC sur les courses détaillées des 14 derniers jours : " + " | ".join(
        f"Z{i + 1} {round(100 * x / sum(tot))} %" for i, x in enumerate(tot))


def _dash_adherence(c):
    today = c["today"]
    run_days = {a["d"] for a in c["recent"] if a.get("type") in RUN_TYPES}
    planned = [e for e in c["evts"] if e.get("category") in (None, "WORKOUT") and e.get("type") in (None, *RUN_TYPES)
               and (_age_days(e.get("d"), today) or 0) >= 1 and (_age_days(e.get("d"), today) or 0) <= 28]
    if not planned:
        return None
    missed = []
    for e in planned:
        d = datetime.date.fromisoformat(e["d"])
        near = {(d + datetime.timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
        if not (near & run_days):
            missed.append(f"{e['d']} {e.get('nom')}")
    done = len(planned) - len(missed)
    t = f"ADHÉRENCE AU PLAN (28 derniers jours, course réalisée à ±1 jour) : {done}/{len(planned)} séances"
    return t + (" ; manquées : " + "; ".join(missed[:5]) if missed else "")


def _effort_points(details):
    """Meilleur lap WORK de 4 min et plus de chaque course."""
    out = []
    for d in details or []:
        best = None
        for lap in d.get("laps") or []:
            sec = _pace_sec(lap.get("pace"))
            if lap.get("type") == "WORK" and (lap.get("duree_s") or 0) >= 240 and sec and (best is None or sec < best[0]):
                best = (sec, lap)
        if best:
            out.append({"id": d.get("id"), "d": d.get("d"), "nom": d.get("nom"), "pace_s": best[0],
                        "hr": best[1].get("hr"), "duree_s": best[1].get("duree_s")})
    return out


def save_effort(pt):
    db_exec("INSERT OR REPLACE INTO efforts (id, d, nom, pace_s, hr, duree_s) VALUES (?, ?, ?, ?, ?, ?)",
            (str(pt.get("id") or pt.get("d")), pt.get("d"), pt.get("nom"), pt.get("pace_s"), pt.get("hr"), pt.get("duree_s")))


def effort_history(details=None):
    """Historique des meilleurs efforts : table `efforts` (s'allonge à chaque séance) + les courses détaillées en cours."""
    points = {}
    try:
        for r in db_exec("SELECT id, d, nom, pace_s, hr, duree_s FROM efforts", fetch=True) or []:
            points[r[0]] = {"id": r[0], "d": r[1], "nom": r[2], "pace_s": r[3], "hr": r[4], "duree_s": r[5]}
    except Exception:
        logging.exception("effort_history")
    for pt in _effort_points(details):
        points[str(pt["id"] or pt["d"])] = pt
    return sorted(points.values(), key=lambda x: str(x["d"]))


def _dash_decouplage(c):
    rows = sorted((d for d in c["details"] if d.get("decouplage_pct") is not None), key=lambda d: str(d.get("d")), reverse=True)[:6]
    if not rows:
        return None
    return ("DÉCOUPLAGE FC/allure (dérive cardiaque : sous 5 % = bon, au-delà de 8 % = fatigue ou intensité trop haute) : "
            + " | ".join(f"{d['d']} {_cell(d['decouplage_pct'])} % ({_cell(d.get('nom'))})" for d in rows))


def plan_phase(days_left):
    """Phase d'une préparation 5 km selon le temps restant : le coach situe chaque conseil dans le plan."""
    w = days_left / 7
    if w > 8:
        return "construction aérobie (volume, endurance, un peu de seuil)"
    if w > 4:
        return "développement spécifique (seuil, VMA, allure 5 km)"
    if w > 1.5:
        return "affûtage (on garde l'intensité, on réduit le volume)"
    return "semaine de course (fraîcheur avant tout)"


def riegel(t1, d1_km, d2_km=5.0, k=1.06):
    """Formule de Riegel : T2 = T1 × (D2 / D1)^1,06. Valable pour un effort MAXIMAL de 3 min à 3 h, pas pour une séance à allure contrôlée."""
    return t1 * (d2_km / d1_km) ** k


def ewma_acwr(acts, days=90):
    """Ratio charge aiguë / chronique par moyennes mobiles exponentielles (7 j / 28 j, Williams) : réagit plus vite qu'une moyenne simple."""
    loads = list(reversed(_daily_loads(acts, days)))
    if sum(loads) <= 0:
        return None
    a = cr = loads[0]
    for x in loads[1:]:
        a += (x - a) * 2 / 8
        cr += (x - cr) * 2 / 29
    return round(a / cr, 2) if cr > 0 else None


def _dash_objectif(c):
    today = c["today"]
    days_left = (GOAL_DATE - today).days
    if days_left < 0:
        return None
    mt = re.search(r"(\d+)'(\d{2})", GOAL_TEXT)
    goal_pace = int(mt.group(1)) * 60 + int(mt.group(2)) if mt else None
    parts = [f"{GOAL_TEXT} le {GOAL_DATE.strftime('%d/%m/%Y')} : J-{days_left}, soit {days_left / 7:.1f} semaines",
             f"phase actuelle : {plan_phase(days_left)}"]
    hist = effort_history(c["details"])
    est = None
    recent_pts = [h for h in hist if (_age_days(h["d"]) if _age_days(h["d"]) is not None else 999) <= 56]
    if recent_pts:
        best = min(recent_pts, key=lambda h: h["pace_s"])
        est = best["pace_s"] * 0.95
        parts.append(f"meilleur effort structuré de 4 min et plus (8 dernières semaines) : {_mmss(best['pace_s'])}/km sur {best['duree_s']} s "
                     f"à {best.get('hr') or '?'} bpm le {best['d']} ; équivalent 5 km estimé : {_mmss(est)}/km, soit {_mmss(est * 5)} "
                     f"(hypothèse : allure 5 km = allure de ces efforts − 5 %, ordre de grandeur prudent)")
        tr = hist[-4:]
        if len(tr) >= 2 and tr[0]["d"] != tr[-1]["d"]:
            parts.append(f"tendance des meilleurs efforts : {tr[0]['d']} {_mmss(tr[0]['pace_s'])} → {tr[-1]['d']} {_mmss(tr[-1]['pace_s'])} "
                         f"({tr[-1]['pace_s'] - tr[0]['pace_s']:+d} s/km)")
    else:
        parts.append("estimation du 5 km impossible : aucun effort structuré de 4 min et plus enregistré récemment")
    tops = [t for t in (c["acts"].get("top_performances_recentes") or []) if 4.5 <= (t.get("distance_km") or 0) <= 6.5 and _pace_sec(t.get("allure"))]
    if tops:
        t = min(tops, key=lambda x: _pace_sec(x["allure"]))
        parts.append(f"meilleure sortie de 4,5 à 6,5 km (6 mois) : {_mmss(_pace_sec(t['allure']))}/km sur {t['distance_km']} km le {t['date']}")
    if est:
        cands = [t for t in (c["acts"].get("top_performances_recentes") or []) if 3 <= (t.get("distance_km") or 0) <= 10 and _pace_sec(t.get("allure"))]
        near = [t for t in cands if _pace_sec(t["allure"]) <= est * 1.10]
        if near:
            t = min(near, key=lambda x: riegel(_pace_sec(x["allure"]) * x["distance_km"], x["distance_km"]))
            t5 = riegel(_pace_sec(t["allure"]) * t["distance_km"], t["distance_km"])
            parts.append(f"Riegel (T2 = T1 × (D2/D1)^1,06) sur la sortie de {t['distance_km']} km à {_mmss(_pace_sec(t['allure']))}/km du {t['date']} : "
                         f"5 km en {_mmss(t5)} ({_mmss(t5 / 5)}/km), à lire comme un plafond tant que ce n'était pas un effort maximal")
        else:
            parts.append("Riegel non applicable : aucune sortie de 3 à 10 km proche de ton allure d'effort, or la formule exige un effort maximal ; "
                         "un test de 3 km ou une course fiabiliserait l'estimation")
    if est and goal_pace:
        gap = est - goal_pace
        if gap > 0:
            parts.append(f"écart à l'objectif : {gap:.0f} s/km ({gap / est * 100:.0f} %), soit {_mmss(gap * 5)} sur 5 km ; "
                         f"progression nécessaire ≈ {gap / max(days_left / 7, 0.1):.1f} s/km par semaine")
        else:
            parts.append(f"objectif déjà couvert par l'estimation ({-gap:.0f} s/km de marge)")
    return "OBJECTIF : " + " | ".join(parts)


def _dash_enveloppe(c):
    today, recent, risk = c["today"], c["recent"], c["risk"]
    loads = _daily_loads(c["acts"], 28)
    if sum(loads) <= 0:
        return None
    st = risk.get("statut")
    coef = {"VIGILANCE": 0.9, "DANGER": 0.8}.get(st, 1.0)
    if st == "DANGER" and len(risk.get("signaux_physio") or []) >= 2:
        coef = 0.7     # double alerte : charge ET récupération dégradées
    why = {"DANGER": "risque élevé", "VIGILANCE": "vigilance"}.get(st, "")
    chronic, acute = sum(loads) / 4, sum(loads[:7])
    max_load = round(chronic * LOAD_CAUTION * coef)
    parts = [f"charge sur 7 jours glissants : cible ≤ {max_load} (1,3 × moyenne hebdo 28 j de {chronic:.0f}"
             f"{f', réduit de {round((1 - coef) * 100)} % car {why}' if coef < 1 else ''}), cumulée {acute:.0f}, marge {max_load - round(acute):+d} ; "
             f"plafond de la plage optimale (1,5) : {round(chronic * LOAD_HIGH)}"]
    this = _monday(today)
    km = {}
    for a in recent:
        if a.get("type") in RUN_TYPES:
            ws = _monday(datetime.date.fromisoformat(a["d"]))
            km[ws] = km.get(ws, 0.0) + (a.get("km") or 0)
    last, done = km.get(this - datetime.timedelta(days=7), 0.0), km.get(this, 0.0)
    if last > 0:
        max_km = last * 1.10 * coef
        parts.append(f"km de la semaine en cours : max ≈ {max_km:.0f} (+10 % sur les {last:.0f} km de la semaine dernière), "
                     f"faits {done:.1f}, reste {max(0.0, max_km - done):.1f}")
        parts.append(f"sortie longue max ≈ {max_km / 3:.1f} km (un tiers du volume)")
    ramp = risk.get("rampe_ctl_7j")
    if ramp is not None and not risk.get("reprise"):
        parts.append(f"rampe CTL sur 7 jours {ramp:+.1f} (limite +{RAMP_LIMIT:g}), marge {RAMP_LIMIT - ramp:+.1f}")
    parts.append("jamais deux séances intenses à moins de 48 h")
    return "ENVELOPPE DE CHARGE POUR PLANIFIER (garde-fous du code) : " + " | ".join(parts)


def _dash_prepa(c):
    """Semaine du plan en cours (noms « S3 E2 » du calendrier) : le coach situe l'athlète dans la préparation."""
    today = c["today"]
    nums, cur = [], {}
    for e in c["evts"]:
        mt = re.match(r"\s*S(\d{1,2})\s*E\d+", str(e.get("nom") or ""), re.I)
        if not mt or e.get("category") not in (None, "WORKOUT"):
            continue
        n = int(mt.group(1))
        nums.append(n)
        try:
            ws = _monday(datetime.date.fromisoformat(e["d"]))
        except Exception:
            continue
        cur.setdefault(ws, []).append(n)
    here = cur.get(_monday(today))
    if not here or not nums:
        return None
    wk = max(set(here), key=here.count)
    days_left = (GOAL_DATE - today).days
    return (f"PRÉPA : semaine S{wk} en cours, plan programmé jusqu'à S{max(nums)} ; objectif dans {days_left // 7} semaines et {days_left % 7} jours"
            + (f", phase actuelle : {plan_phase(days_left)}" if days_left >= 0 else ""))


def _dash_poids(c):
    """Tendance du poids (moyennes, jamais une pesée isolée) et puissance rapportée au poids."""
    well = c["well"]
    w7, w28, old = _avg(_vals(well, "weight", 0, 6)), _avg(_vals(well, "weight", 0, 27)), _avg(_vals(well, "weight", 21, 35))
    last = next((w for w in well if isinstance(w.get("weight"), (int, float)) and w["weight"] > 0), None)
    kg = w7 or (last["weight"] if last else None) or (c["prof"] or {}).get("weight")
    if not kg:
        return None
    if w7 is None and last is None:
        t = f"POIDS : {_cell(kg)} kg (réglage du profil Intervals, aucune pesée récente)"
    else:
        t = f"POIDS : moyenne 7 j {kg:.1f} kg"
        if w28:
            t += f", moyenne 28 j {w28:.1f} kg"
        if old:
            d = (w7 or kg) - old
            t += f", {d:+.1f} kg par rapport à il y a 4 semaines" + (" (variation notable : à relier à l'alimentation et à la charge, sans conclure)" if abs(d) >= 2 else "")
        if last:
            t += f" ; dernière pesée {_cell(last['weight'])} kg le {last.get('d')}"
    for d in c["details"]:
        watts = ((d.get("bio") or {}).get("watts") or {}).get("moy") or d.get("watts_moy")
        if watts:
            t += f" | puissance moyenne du dernier footing détaillé {watts:.0f} W, soit {watts / kg:.2f} W/kg"
            break
    return t


def dashboard_lines(prof, well, acts, evts, risk=None):
    ctx = {"prof": prof or {}, "well": well or [], "acts": acts or {}, "evts": evts or [], "today": datetime.date.today(),
           "recent": (acts or {}).get("brut_recent", []), "details": (acts or {}).get("detail_dernieres_courses") or [],
           "risk": risk or evaluate_injury_risk(well, acts)}
    out = []
    for fn in (_dash_recup, _dash_poids, _dash_charge, _dash_faits, _dash_semaines, _dash_semaine_plan, _dash_prepa, _dash_rythme, _dash_allure_facile, _dash_decouplage, _dash_zones,
               _dash_adherence, _dash_objectif, _dash_enveloppe):
        try:
            line = fn(ctx)
            if line:
                out.append(line)
        except Exception:
            logging.exception(f"Tableau de bord : {fn.__name__}")
    return out


def fetch_activities():
    today = datetime.date.today()
    d180 = (today - datetime.timedelta(days=180)).isoformat()
    d_raw = (today - datetime.timedelta(days=RAW_DAYS)).isoformat()
    empty = {"brut_recent": [], "agregat_hebdo_anterieur": [], "top_performances_recentes": []}
    try:
        r = requests.get(f"{BASE}/activities", auth=AUTH, params={"oldest": d180}, timeout=10)
        if r.status_code != 200:
            logging.error(f"fetch_activities : HTTP {r.status_code}")
            return empty

        raw = r.json()
        raw.sort(key=lambda x: str(x.get("start_date_local", "")), reverse=True)

        recent, older, runs = [], [], []
        for a in raw:
            d_str = str(a.get("start_date_local") or "")[:10]
            if len(d_str) != 10:
                continue
            spd = a.get("average_speed") or 0
            dist = a.get("distance") or 0
            mtime = a.get("moving_time") or 0
            gear = a.get("gear")
            item = {
                "id": a.get("id"),
                "d": d_str,
                "type": a.get("type"),
                "nom": a.get("name"),
                "km": round(dist / 1000, 2),
                "min": round(mtime / 60, 1),
                "pace": _pace(spd),
                "hr": _num(a.get("average_heartrate"), 0),
                "load": _num(a.get("icu_training_load"), 0),
                "h": str(a.get("start_date_local") or "")[11:16],
                "shoes": gear.get("name") if isinstance(gear, dict) else None,
            }
            if a.get("type") in RUN_TYPES and dist >= 3000 and spd > 0:
                runs.append((spd, item))
            (recent if d_str >= d_raw else older).append(item)

        runs.sort(key=lambda x: x[0], reverse=True)
        top_perfs = [
            {"allure": i["pace"], "distance_km": i["km"], "date": i["d"], "nom": i["nom"]}
            for _, i in runs[:5]
        ]

        weeks = {}
        for a in older:
            iso = datetime.date.fromisoformat(a["d"]).isocalendar()
            w = weeks.setdefault(f"{iso[0]}-S{iso[1]:02d}", {
                "km": 0.0, "min": 0.0, "load": 0.0, "n": 0, "run_km": 0.0, "run_min": 0.0, "hr_w": 0.0, "hr_min": 0.0})
            w["km"] += a["km"]
            w["min"] += a["min"]
            w["load"] += a["load"] or 0
            w["n"] += 1
            if a["type"] in RUN_TYPES:
                w["run_km"] += a["km"]
                w["run_min"] += a["min"]
                if a["hr"]:
                    w["hr_w"] += a["hr"] * a["min"]
                    w["hr_min"] += a["min"]

        weekly = []
        for k, v in sorted(weeks.items(), reverse=True):
            row = {"semaine": k, "seances": v["n"], "km_total": round(v["km"], 1),
                   "temps_h": round(v["min"] / 60, 1), "charge_totale": round(v["load"])}
            if v["run_km"] > 0 and v["run_min"] > 0:
                row["km_course"] = round(v["run_km"], 1)
                row["allure_course"] = _pace(v["run_km"] * 1000 / (v["run_min"] * 60))
            if v["hr_min"] > 0:
                row["fc_course"] = round(v["hr_w"] / v["hr_min"])
            weekly.append(row)

        return {"brut_recent": recent, "agregat_hebdo_anterieur": weekly, "top_performances_recentes": top_perfs}
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
            out = []
            for e in raw:
                if e.get("category") not in KEEP_CATEGORIES:
                    continue
                d_str = str(e.get("start_date_local") or "")[:10]
                try:
                    gap = abs((datetime.date.fromisoformat(d_str) - today).days)
                except ValueError:
                    gap = 999
                row = {"d": d_str, "nom": e.get("name"), "type": e.get("type")}
                hh = str(e.get("start_date_local") or "")[11:16]
                if hh and hh != "00:00":
                    row["h"] = hh
                if e.get("category") != "WORKOUT":
                    row["category"] = e.get("category")
                if gap <= DESC_DAYS:   # description complète seulement près d'aujourd'hui : économie de tokens
                    row["desc"] = (e.get("description") or "")[:400]
                out.append(row)
            return out
        logging.error(f"fetch_events : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_events : {err}")
    return []


def split_wellness(well, days_full=None):
    """(lignes quotidiennes, lignes hebdomadaires). Par défaut les 90 jours sont envoyés un par un (aucune perte)."""
    days_full = WELLNESS_FULL_DAYS if days_full is None else days_full
    daily = list(well[:days_full])
    weeks = {}
    for w in well[days_full:]:
        try:
            iso = datetime.date.fromisoformat(str(w["d"])).isocalendar()
        except Exception:
            continue
        weeks.setdefault(f"{iso[0]}-S{iso[1]:02d}", []).append(w)
    weekly = []
    for k, rows in sorted(weeks.items(), reverse=True):
        def avg(field, nd=1):
            vals = [x[field] for x in rows if isinstance(x.get(field), (int, float))]
            return round(sum(vals) / len(vals), nd) if vals else None
        weekly.append({"d": k, "sleep_h": avg("sleep_h"), "hrv": avg("hrv"), "rhr": avg("rhr"),
                       "tsb": avg("tsb"), "atl": avg("atl"), "ctl": avg("ctl"), "weight": avg("weight")})
    return daily, weekly


# --------------------------------------------------------------------------
# DETAIL DES COURSES : laps de la montre, splits km, série seconde par seconde (cache en base)
# --------------------------------------------------------------------------
SUMMARY_KEYS = {
    "max_heartrate": "fc_max", "total_elevation_gain": "d+",
    "decoupling": "decouplage_pct", "icu_efficiency_factor": "ef", "icu_intensity": "intensite",
    "trimp": "trimp", "average_temp": "temp_c", "icu_rpe": "rpe", "feel": "feel",
    "icu_average_watts": "watts_moy", "icu_weighted_avg_watts": "watts_np", "average_stride": "foulee_m",
}
STREAM_TYPES = "time,distance,heartrate,cadence,altitude,velocity_smooth,watts"
CORE_STREAMS = set(STREAM_TYPES.split(","))
EXCLUDE_STREAMS = {"latlng", "moving", "grade_smooth", "fixed_watts", "fixed_heartrate", "raw_heartrate", "fixed_altitude",
                   "torque", "left_right_balance", "epoc", "vam", "temp"}


def _num(x, nd=1):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return round(x, nd) if nd else int(round(x))


def _mean(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and v > 0]
    return sum(vals) / len(vals) if vals else None


def _mmss(sec):
    total = round(sec)
    return f"{total // 60}'{total % 60:02d}"


def _spm(v):
    """Cadence de course en pas/min : certains appareils l'enregistrent par jambe (80-95), on double sous 125."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return v * 2 if v < 125 else v


def _ffill(arr):
    out, last = [], 0
    for v in arr:
        last = v if isinstance(v, (int, float)) else last
        out.append(last)
    return out


def _idx_at(times, t):
    return min(max(bisect.bisect_left(times, t), 0), len(times) - 1)


def _smart(v):
    return int(round(v)) if abs(v) >= 100 else (round(v, 1) if abs(v) >= 10 else round(v, 2))


def parse_streams(js):
    """Accepte [{'type': 'time', 'data': [...]}, ...] ou {'time': [...], ...}."""
    out = {}
    if isinstance(js, list):
        for st in js:
            if isinstance(st, dict) and st.get("type") and isinstance(st.get("data"), list):
                out[st["type"]] = st["data"]
    elif isinstance(js, dict):
        for k, v in js.items():
            if isinstance(v, list):
                out[k] = v
    return out


def _stream_types_for(stream_types):
    """Flux de base + toutes les mesures supplémentaires stockées pour l'activité (puissance, dynamique de course...)."""
    if not isinstance(stream_types, list) or not stream_types:
        return STREAM_TYPES
    base = ["time", "distance"] + [t for t in STREAM_TYPES.split(",") if t in stream_types and t not in ("time", "distance")]
    extras = [t for t in stream_types if isinstance(t, str) and t not in CORE_STREAMS and t not in EXCLUDE_STREAMS][:8]
    return ",".join(base + extras)


def get_streams(act_id, types=None):
    """Données seconde par seconde d'une activité (cache compressé en base). None si Intervals ne répond pas."""
    row = db_exec("SELECT blob FROM act_streams WHERE id=? AND v=?", (act_id, STREAM_VERSION), fetch=True)
    if row:
        try:
            return json.loads(zlib.decompress(row[0][0]))
        except Exception:
            logging.warning(f"Cache streams illisible pour {act_id}, nouveau téléchargement")
    s = requests.get(f"{API_ROOT}/activity/{act_id}/streams.json", auth=AUTH,
                     params={"types": types or STREAM_TYPES}, timeout=25)
    time.sleep(0.15)
    if s.status_code != 200:
        logging.warning(f"Streams activité {act_id} : HTTP {s.status_code}")
        return None
    streams = parse_streams(s.json())
    if len(streams.get("time", [])) >= 60:
        blob = zlib.compress(json.dumps(streams, separators=(",", ":")).encode("utf-8"))
        db_exec("INSERT OR REPLACE INTO act_streams (id, v, blob) VALUES (?, ?, ?)", (act_id, STREAM_VERSION, blob))
        db_exec("DELETE FROM act_streams WHERE id NOT IN (SELECT id FROM act_streams ORDER BY rowid DESC LIMIT 30)")
    elif streams:
        logging.info(f"Streams {act_id} trop courts ou sans 'time' : {list(streams)}")
    return streams


def compute_km_splits(streams):
    """Splits au kilomètre : allure, FC, cadence (pas/min), puissance, dénivelé net. Dernier tronçon inclus s'il fait 300 m et plus."""
    t = _ffill(streams.get("time") or [])
    d = _ffill(streams.get("distance") or [])
    n = min(len(t), len(d))
    if n < 2:
        return []
    hr = streams.get("heartrate") or []
    cad = streams.get("cadence") or []
    alt = streams.get("altitude") or []
    watts = streams.get("watts") or []

    def stats(i_from, i_to):
        row = {}
        h = _mean(hr[i_from + 1:i_to + 1])
        c = _spm(_mean(cad[i_from + 1:i_to + 1]))
        w = _mean(watts[i_from + 1:i_to + 1])
        if h:
            row["hr"] = int(round(h))
        if c:
            row["cad"] = int(round(c))
        if w:
            row["w"] = int(round(w))
        if i_from < len(alt) and i_to < len(alt) and isinstance(alt[i_from], (int, float)) and isinstance(alt[i_to], (int, float)):
            row["d_alt"] = int(round(alt[i_to] - alt[i_from]))
        return row

    splits, boundary = [], 1000.0
    prev_i, prev_t = 0, t[0]
    for i in range(1, n):
        while d[i] >= boundary and d[i] > d[i - 1]:
            frac = (boundary - d[i - 1]) / (d[i] - d[i - 1])
            tb = t[i - 1] + frac * (t[i] - t[i - 1])
            row = {"km": len(splits) + 1, "pace": _mmss(tb - prev_t)}
            row.update(stats(prev_i, i))
            splits.append(row)
            prev_t, prev_i = tb, i
            boundary += 1000.0
    rem = d[n - 1] - (boundary - 1000.0)
    if rem >= 300:
        row = {"km": round(d[n - 1] / 1000, 2), "partiel": True, "pace": _mmss((t[n - 1] - prev_t) / rem * 1000)}
        row.update(stats(prev_i, n - 1))
        splits.append(row)
    return splits


def _pctile(sorted_vals, q):
    return sorted_vals[int(q * (len(sorted_vals) - 1))]


def phase_summary(streams):
    """Séance découpée en 3 tiers (durée égale) : allure, FC moyenne et plage (5e-95e centile), cadence, puissance."""
    t = _ffill(streams.get("time") or [])
    d = _ffill(streams.get("distance") or [])
    n = min(len(t), len(d))
    if n < 600 or t[n - 1] - t[0] < 600:
        return None
    t, d = t[:n], d[:n]
    hr, cad, watts = streams.get("heartrate") or [], streams.get("cadence") or [], streams.get("watts") or []
    t0, dur = t[0], t[n - 1] - t[0]
    cuts = [0] + [_idx_at(t, t0 + dur * k / 3) for k in (1, 2)] + [n - 1]
    rows = []
    for k in range(3):
        i0, i1 = cuts[k], cuts[k + 1]
        if i1 <= i0:
            continue
        dist, sec = d[i1] - d[i0], t[i1] - t[i0]
        row = {"ph": f"{round((t[i0] - t0) / 60)}-{round((t[i1] - t0) / 60)} min", "km": f"{d[i0] / 1000:.1f}-{d[i1] / 1000:.1f}"}
        if dist > 0 and sec > 0:
            row["pace"] = _mmss(sec / dist * 1000)
        h = sorted(v for v in hr[i0:i1 + 1] if isinstance(v, (int, float)) and v > 40)
        if h:
            row["fc"] = int(round(sum(h) / len(h)))
            row["fc_plage"] = f"{int(_pctile(h, 0.05))}-{int(_pctile(h, 0.95))}"
        c = _spm(_mean(cad[i0:i1 + 1]))
        w = _mean(watts[i0:i1 + 1])
        if c:
            row["cad"] = int(round(c))
        if w:
            row["w"] = int(round(w))
        rows.append(row)
    return rows or None


def hr_cumul(hr):
    """Part du temps passée sous une FC donnée, par paliers de 5 bpm (ex. '≤140 25 % | ≤145 62 % | ≤150 98 %')."""
    vals = sorted(v for v in hr if isinstance(v, (int, float)) and v > 40)
    n = len(vals)
    if n < 120:
        return None
    b = int(_pctile(vals, 0.05) // 5 * 5) + 5
    parts = []
    while b <= int(vals[-1] // 5 * 5) + 5:
        pct = bisect.bisect_right(vals, b) * 100 / n
        parts.append(f"≤{b} {pct:.0f} %")
        if pct >= 99.5:
            break
        b += 5
    return " | ".join(parts)


def hr_peak(streams):
    t, hr = streams.get("time") or [], streams.get("heartrate") or []
    n = min(len(t), len(hr))
    pts = [(hr[i], i) for i in range(n) if isinstance(hr[i], (int, float)) and hr[i] > 40]
    if not pts:
        return None
    v, i = max(pts)
    t0 = t[0] if isinstance(t[0], (int, float)) else 0
    return {"fc": int(v), "min": round(((t[i] if isinstance(t[i], (int, float)) else 0) - t0) / 60)}


def dynamics_summary(streams):
    """Mesures supplémentaires du fichier (puissance, temps de contact, oscillation, longueur de foulée...) : moyenne,
    plage 5e-95e centile, 1er tiers → dernier tiers et dérive en %. Les noms et unités sont ceux du fichier."""
    out = {}
    for name, arr in streams.items():
        if name in ("time", "distance", "altitude", "velocity_smooth", "heartrate"):
            continue
        pos = [v for v in arr if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
        if len(pos) < 90:
            continue
        if name == "cadence":
            pos = [_spm(v) for v in pos]
        srt = sorted(pos)
        k = len(pos) // 3
        first, last, mean = sum(pos[:k]) / k, sum(pos[-k:]) / k, sum(pos) / len(pos)
        out["cad_spm" if name == "cadence" else name] = {
            "moy": _smart(mean), "p5": _smart(_pctile(srt, 0.05)), "p95": _smart(_pctile(srt, 0.95)),
            "t1": _smart(first), "t3": _smart(last), "derive_pct": round((last / first - 1) * 100, 1)}
    return out


def _lap_rows(intervals):
    rows = []
    for idx, iv in enumerate(intervals[:MAX_LAPS], 1):
        dist = iv.get("distance") or 0
        mt = iv.get("moving_time") or iv.get("elapsed_time") or 0
        spd = iv.get("average_speed") or (dist / mt if mt else 0)
        row = {
            "n": idx, "type": iv.get("type"), "label": iv.get("label"), "debut_s": _num(iv.get("start_time"), 0),
            "duree_s": _num(mt, 0), "km": round(dist / 1000, 2), "pace": _pace(spd),
            "hr": _num(iv.get("average_heartrate"), 0), "hr_max": _num(iv.get("max_heartrate"), 0),
            "cad": _num(_spm(iv.get("average_cadence")), 0), "d+": _num(iv.get("total_elevation_gain"), 0),
            "w": _num(iv.get("average_watts"), 0), "foulee": _num(iv.get("average_stride"), 2),
        }
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _lap_bounds(iv, t):
    n = len(t)
    i0, i1 = iv.get("start_index"), iv.get("end_index")
    if isinstance(i0, int) and isinstance(i1, int) and 0 <= i0 < i1 < n:
        return i0, i1
    st = iv.get("start_time")
    du = iv.get("moving_time") or iv.get("elapsed_time")
    if isinstance(st, (int, float)) and isinstance(du, (int, float)) and du > 0:
        i0, i1 = _idx_at(t, st), _idx_at(t, st + du)
        if i1 > i0:
            return i0, i1
    return None


def minute_breakdown(streams, raw_intervals, rows):
    """Ajoute 'par_min' (allure/FC minute par minute) aux laps d'effort de 1 à 15 minutes."""
    t = _ffill(streams.get("time") or [])
    d = _ffill(streams.get("distance") or [])
    n = min(len(t), len(d))
    if n < 2:
        return
    t, d = t[:n], d[:n]
    hr = streams.get("heartrate") or []
    typed = any(iv.get("type") for iv in raw_intervals)
    done = 0
    for iv, row in zip(raw_intervals, rows):
        if typed and iv.get("type") != "WORK":
            continue
        dur = row.get("duree_s") or 0
        if not 60 <= dur <= 900:
            continue
        bounds = _lap_bounds(iv, t)
        if not bounds:
            continue
        i0, i1 = bounds
        cells = []
        for k in range(int(dur // 60)):
            a = t[i0] + 60 * k
            b = a + 60
            if b > t[i1] + 1:
                break
            j0, j1 = _idx_at(t, a), _idx_at(t, b)
            if j1 <= j0 or d[j1] - d[j0] <= 0:
                continue
            pace = _mmss((t[j1] - t[j0]) / (d[j1] - d[j0]) * 1000)
            h = _mean(hr[j0 + 1:j1 + 1])
            cells.append(f"{pace}/{int(round(h))}" if h else pace)
        if cells:
            row["par_min"] = cells
            done += 1
            if done >= 12:
                break


def fetch_run_detail(item):
    act_id = str(item["id"])
    cached = db_exec("SELECT payload FROM act_details WHERE id=? AND v=?", (act_id, DETAIL_VERSION), fetch=True)
    if cached:
        return json.loads(cached[0][0])

    detail = {"id": act_id, "d": item.get("d"), "nom": item.get("nom")}
    got_any = False
    laps, raw_intervals, stream_types = [], [], None
    try:
        r = requests.get(f"{API_ROOT}/activity/{act_id}", auth=AUTH, params={"intervals": "true"}, timeout=12)
        if r.status_code == 200:
            got_any = True
            a = r.json()
            for src, dst in SUMMARY_KEYS.items():
                v = _num(a.get(src), 2 if dst == "foulee_m" else 1)
                if v is not None:
                    detail[dst] = v
            cad = _num(_spm(a.get("average_cadence")), 0)
            if cad:
                detail["cad"] = cad
            gap = a.get("gap")
            if isinstance(gap, (int, float)) and gap > 0:
                detail["gap"] = _pace(gap)
            zt = a.get("icu_hr_zone_times")
            if isinstance(zt, list) and zt:
                detail["temps_zones_fc_s"] = [int(x) if isinstance(x, (int, float)) else 0 for x in zt]
            if a.get("description"):
                detail["notes"] = str(a["description"])[:300]
            stream_types = a.get("stream_types")
            raw_intervals = (a.get("icu_intervals") or [])[:MAX_LAPS]
            laps = _lap_rows(raw_intervals)
        else:
            logging.warning(f"Détail activité {act_id} : HTTP {r.status_code}")
        time.sleep(0.15)

        streams = get_streams(act_id, _stream_types_for(stream_types))
        if streams:
            got_any = True
            km = compute_km_splits(streams)
            if km:
                detail["km"] = km
            if laps:
                minute_breakdown(streams, raw_intervals, laps)
            if sum(1 for lp in laps if lp.get("type") == "WORK") < 2:     # séance continue : lecture par tiers
                ph = phase_summary(streams)
                if ph:
                    detail["phases"] = ph
            cum = hr_cumul(streams.get("heartrate") or [])
            if cum:
                detail["fc_cumul"] = cum
            pk = hr_peak(streams)
            if pk:
                detail["pic_fc"] = pk
            dyn = dynamics_summary(streams)
            if dyn:
                detail["dyn"] = dyn
        if laps:
            detail["laps"] = laps
    except Exception as err:
        logging.error(f"Erreur détail activité {act_id} : {err}")
        return detail   # pas de mise en cache sur erreur

    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(str(item.get("d")))).days
    except Exception:
        age = 99
    # Une séance toute récente peut ne pas être encore analysée : on ne fige le cache que si c'est complet
    if got_any and (detail.get("laps") or detail.get("km") or age > 2):
        db_exec("INSERT OR REPLACE INTO act_details (id, v, payload) VALUES (?, ?, ?)",
                (act_id, DETAIL_VERSION, json.dumps(detail, ensure_ascii=False)))
        for pt in _effort_points([detail]):
            save_effort(pt)
    return detail


def fetch_run_details(acts):
    runs = [a for a in acts.get("brut_recent", []) if a.get("type") in RUN_TYPES and (a.get("km") or 0) >= 1]
    out = []
    for i, a in enumerate(runs[:DETAIL_RUNS]):
        try:
            det = fetch_run_detail(a)
        except Exception:
            logging.exception("fetch_run_detail")
            continue
        if i >= DETAIL_FULL:      # courses plus anciennes : résumé, laps (sans splits, phases ni dynamique)
            det = {k: v for k, v in det.items() if k not in ("km", "phases", "dyn", "fc_cumul", "pic_fc")}
        if a.get("shoes"):
            det = dict(det, chaussures=a["shoes"])
        out.append(det)
    return out


def build_series_csv(streams, laps=None, full=False, step_s=None):
    """CSV de la séance : 1 ligne toutes les STREAM_STEP_S secondes (ou chaque seconde si full). Retourne (csv, pas, nb_lignes)."""
    t = _ffill(streams.get("time") or [])
    d = _ffill(streams.get("distance") or [])
    n = min(len(t), len(d))
    if n < 2:
        return None
    hr = streams.get("heartrate") or []
    cad = streams.get("cadence") or []
    alt = streams.get("altitude") or []
    vel = streams.get("velocity_smooth") or []
    t0 = t[0]
    dur = t[n - 1] - t0
    if dur <= 0:
        return None

    step = 1 if full else max(1, step_s or STREAM_STEP_S)
    max_rows = SERIES_FULL_MAX_ROWS if full else SERIES_MAX_ROWS
    if dur / step > max_rows:
        step = int(-(-dur // max_rows))

    marks = [(lp["debut_s"], lp["debut_s"] + lp["duree_s"], lp["n"]) for lp in (laps or [])
             if lp.get("debut_s") is not None and lp.get("duree_s")]
    header = ["t_s", "dist_m", "allure", "fc", "cad", "alt_m"] if full else ["t_s", "allure", "fc"]
    if marks:
        header.append("lap")
    lines = [",".join(header)]
    state = {"d": d[0], "t": t0}

    def emit(wid, idxs):
        i_last = idxs[-1]
        h = _mean([hr[i] for i in idxs if i < len(hr)])
        c = _spm(_mean([cad[i] for i in idxs if i < len(cad)]))
        v = _mean([vel[i] for i in idxs if i < len(vel)])
        dt, dd = t[i_last] - state["t"], d[i_last] - state["d"]
        if not v and dt > 0 and dd > 0:
            v = dd / dt
        state["d"], state["t"] = d[i_last], t[i_last]
        pace = _mmss(1000 / v) if v and v >= 0.8 else ""
        a_val = alt[i_last] if i_last < len(alt) and isinstance(alt[i_last], (int, float)) else None
        h_txt = int(round(h)) if h else ""
        if full:
            cells = [wid * step, int(d[i_last]), pace, h_txt, int(round(c)) if c else "",
                     int(round(a_val)) if a_val is not None else ""]
        else:
            cells = [wid * step, pace, h_txt]
        if marks:
            mid = t0 + wid * step + step / 2
            cells.append(next((lap_n for s_, e_, lap_n in marks if s_ <= mid < e_), ""))
        lines.append(",".join(str(x) for x in cells))

    cur, idxs = None, []
    for i in range(n):
        wid = int((t[i] - t0) // step)
        if cur is None:
            cur = wid
        if wid != cur:
            emit(cur, idxs)
            idxs, cur = [], wid
        idxs.append(i)
    if idxs:
        emit(cur, idxs)
    return "\n".join(lines), step, len(lines) - 1


# --------------------------------------------------------------------------
# MODE DE RÉPONSE : le code décide si l'athlète converse, pose une question, demande une analyse...
# (avant : n'importe quel message contenant « footing » ou « FC » déclenchait une analyse complète)
# --------------------------------------------------------------------------
_STRONG_ANALYSE = re.compile(
    r"analys|en d[ée]tail|d[ée]taill|ai[- ]je respect|a[- ]t[- ]on respect|\bcsv\b|seconde par seconde|\bcompar(?:e|er|aison)\b|"
    r"d[ée]brief|qu'?en penses[- ]tu de (?:ma|la) (?:s[ée]ance|course|sortie|cap)|"
    r"comment (?:s'est pass[ée]e|[ée]tait) (?:ma|la) (?:s[ée]ance|course|sortie)", re.I)
_TOPIC_ANALYSE = re.compile(r"\blaps?\b|fraction|d[ée]rive|d[ée]couplage|r[ée]gularit|\bfc\b|bpm|allure|rythme|cardiaque", re.I)
_IMPERATIF = re.compile(r"\b(?:regarde|v[ée]rifie|dis[- ]moi|montre|donne[- ]moi|explique|pr[ée]cise|peux[- ]tu|pourrais[- ]tu|tu peux)\b", re.I)
_QUESTION_START = re.compile(
    r"^\s*(?:comment|pourquoi|quand|combien|quel|quelle|quels|quelles|est[- ]ce|puis[- ]je|peux[- ]tu|pourrais[- ]tu|dois[- ]je|"
    r"tu penses|que penses|qu'est[- ]ce|o[ùu])\b", re.I)
_BRIEF_RE = re.compile(r"bilan matinal|bilan du matin|brief (?:du )?(?:matin|r[ée]veil)|point du matin|recommence le (?:bilan|brief)", re.I)
_BILAN_RE = re.compile(r"bilan (?:de la )?semaine|bilan hebdo", re.I)
_PLANIF_REQ = re.compile(
    r"\b(?:planifie|programme|ajoute|supprime|d[ée]place|d[ée]cale|annule|remplace|reporte|restaure|retire)\b"
    r"|(?:peux[- ]tu|pourrais[- ]tu|tu peux|stp|s'il te pla[iî]t|je voudrais|je veux|j'aimerais).{0,60}"
    r"\b(?:planifier|programmer|ajouter|supprimer|d[ée]placer|d[ée]caler|annuler|remplacer|reporter|retirer)\b", re.I | re.S)
_FULLRES = re.compile(r"seconde par seconde|\b1 ?hz\b|\bcsv\b|\bbrut|complet|toutes? les donn|tout le d[ée]tail", re.I)


def detect_mode(msg):
    """CONVERSATION | QUESTION | ANALYSE | PLANIFICATION | BRIEF | BILAN | DEBRIEF | LIBRE (vocal)."""
    low = (msg or "").strip().lower()
    if low.startswith("débrief séance terminée"):
        return "DEBRIEF"
    if low.startswith("brief réveil"):
        return "BRIEF"
    if low.startswith("c'est dimanche soir"):
        return "BILAN"
    if low.startswith("message vocal") or low.startswith("photo de l'athlète"):
        return "LIBRE"
    if low.startswith("suivi à faire"):
        return "SUIVI"
    if _BRIEF_RE.search(low):
        return "BRIEF"
    if _BILAN_RE.search(low):
        return "BILAN"
    if _PLANIF_REQ.search(low):
        return "PLANIFICATION"
    question = "?" in low or bool(_QUESTION_START.search(low))
    if _STRONG_ANALYSE.search(low) or (_TOPIC_ANALYSE.search(low) and (question or _IMPERATIF.search(low))):
        return "ANALYSE"
    return "QUESTION" if question else "CONVERSATION"


def resolve_msg_date(msg):
    """Date visée par le message (ISO, jj/mm, hier, avant-hier, ce matin, nom de jour passé) ou None."""
    s = (msg or "").lower()
    today = datetime.date.today()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s)
    if m:
        try:
            yr = int(m.group(3)) if m.group(3) else today.year
            yr += 2000 if yr < 100 else 0
            dt = datetime.date(yr, int(m.group(2)), int(m.group(1)))
            if not m.group(3) and dt > today:
                dt = dt.replace(year=dt.year - 1)
            return dt.isoformat()
        except ValueError:
            return None
    if re.search(r"avant[- ]hier", s):
        return (today - datetime.timedelta(days=2)).isoformat()
    if re.search(r"\bhier\b", s):
        return (today - datetime.timedelta(days=1)).isoformat()
    if re.search(r"aujourd|ce matin|ce midi|tout à l'heure|ce soir", s):
        return today.isoformat()
    for name, wd in JOURS_MAP.items():
        if re.search(rf"\b{name}\b", s):
            return (today - datetime.timedelta(days=(today.weekday() - wd) % 7)).isoformat()
    return None


def pick_target_run(msg, runs):
    """Course visée par le message. runs est trié du plus récent au plus ancien. Retourne (course, avertissement)."""
    if not runs:
        return None, "aucune course récente trouvée."
    target = resolve_msg_date(msg)
    if not target:
        return runs[0], None
    same = [a for a in runs if a.get("d") == target]
    if not same:
        return None, f"aucune course trouvée le {target}."
    low = (msg or "").lower()
    for a in same:
        nm = str(a.get("nom") or "").strip().lower()
        if nm and nm in low:
            return a, None
    return same[0], None


def build_series_block(acts, msg, series="auto"):
    """series : 'auto' (détection selon le message), False (jamais), ou le dict d'une activité précise."""
    if series is False:
        return ""
    runs = [a for a in acts.get("brut_recent", []) if a.get("type") in RUN_TYPES and (a.get("km") or 0) >= 1]
    if isinstance(series, dict):
        if series.get("type") not in RUN_TYPES:
            return ""
        run, warn = series, None
        step_s = 10      # débrief automatique : résolution plus grossière (phases, pic et dynamique déjà calculés)
    else:
        if detect_mode(msg) != "ANALYSE":
            return ""
        run, warn = pick_target_run(msg, runs)
        step_s = None
    full = bool(_FULLRES.search(msg or ""))
    if not run:
        return f"SÉRIE DÉTAILLÉE : {warn}"
    try:
        laps = fetch_run_detail(run).get("laps")      # d'abord : met en cache les flux complets (puissance, dynamique)
        streams = get_streams(str(run["id"]))
        res = build_series_csv(streams, laps, full, step_s) if streams else None
    except Exception:
        logging.exception("build_series_block")
        res = None
    if not res:
        return f"SÉRIE DÉTAILLÉE : indisponible pour '{run.get('nom')}' du {run.get('d')} (données seconde par seconde absentes ou illisibles)."
    csv_text, step, nrows = res
    return (f"SÉRIE DÉTAILLÉE (CSV de la séance '{run.get('nom')}' du {run.get('d')} : 1 ligne toutes les {step} s, {nrows} lignes ; "
            f"t_s = secondes depuis le départ, allure en m'ss/km (vide à l'arrêt), fc en bpm, cad = pas par minute, alt_m = altitude, "
            f"lap = numéro du lap du bloc DÉTAIL) :\n{csv_text}")


def fetch_gear():
    """Matériel suivi dans Intervals (chaussures...). Liste vide si la fonction n'est pas utilisée."""
    try:
        r = requests.get(f"{BASE}/gear", auth=AUTH, timeout=8)
        if r.status_code == 200 and isinstance(r.json(), list):
            return [f"{g.get('name')} ({g.get('type') or '?'}) : {round((g.get('distance') or 0) / 1000)} km"
                    for g in r.json() if not g.get("retired")][:8]
        if r.status_code not in (200, 404):
            logging.warning(f"fetch_gear : HTTP {r.status_code}")
    except Exception as err:
        logging.error(f"Erreur fetch_gear : {err}")
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
    f_gear = POOL.submit(fetch_gear)
    prof = f_prof.result()
    gear = f_gear.result()
    if gear:
        prof = dict(prof, materiel=gear)
    weather = fetch_weather(prof.get("city") or DEFAULT_CITY)
    acts = f_acts.result()
    acts["detail_dernieres_courses"] = fetch_run_details(acts)
    return prof, weather, f_well.result(), acts, f_evts.result()


def get_all_data():
    with _DATA_LOCK:
        if _DATA_CACHE["v"] and time.time() - _DATA_CACHE["t"] < DATA_TTL:
            return _DATA_CACHE["v"]
        value = _load_all_data()
        if value[2] or value[3].get("brut_recent"):   # ne pas mettre en cache un chargement vide
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
SYSTEM_INSTRUCTION = """Tu es le coach personnel de course à pied de cet athlète, pas un tableau de bord. Parle comme un coach au bord de la piste, pas comme un analyste de données : un chiffre n'a de valeur que s'il soutient une phrase humaine. Tu as l'oeil d'un expert (physiologie de l'endurance, gestion de la charge, biomécanique) et la chaleur d'un coach qui connaît son athlète depuis des mois. Tu le tutoies et, si le champ prenom du PROFIL existe, tu l'appelles par son prénom de temps en temps. Tu écris un français naturel et vivant : phrases variées, humour léger quand il s'y prête, 0 à 2 émojis au maximum. Si un bloc TON DU COACH est fourni, adopte ce ton.

PERSONNALITÉ
- Tu écoutes d'abord. Quand l'athlète te raconte quelque chose (sensation, fierté, doute, matériel, anecdote, taquinerie), tu réagis à ce qu'il dit, avec ses mots, avant tout chiffre, et tu relèves ce qu'il y a derrière.
- Tu as un avis et tu l'assumes, avec nuance. Tu félicites précisément (ce qui est bon et pourquoi) et tu challenges honnêtement. Quand ses sensations contredisent les indicateurs et que les faits lui donnent raison, tu le reconnais franchement : l'ACWR et les autres ratios sont des outils, pas des vérités. Tu notes alors ce que tu apprends de lui.
- Tu te souviens : tu t'appuies sur l'historique de conversation, la MÉMOIRE DURABLE, le matériel et le calendrier, et tu le montres naturellement (chaussures, préférences, contraintes de la semaine). Tu ne répètes jamais des chiffres déjà donnés dans les derniers messages, sauf demande, et tu ne réutilises pas la structure de tes messages précédents.
- Au plus UNE question par message, seulement si la réponse changerait ton conseil (jambes, douleur, sommeil, stress, contraintes). Jamais de question pour la forme.

RÈGLES
1. Données : les listes sont triées du plus récent au plus ancien ; la première ligne de santé peut être incomplète (nuit du jour pas encore synchronisée). Le TABLEAU DE BORD est calculé par le code : cite ses chiffres, ne les recalcule pas. Appuie chaque conseil sur les données de l'athlète (allures et FC réellement observées, laps, phases, tendances). N'invente jamais une allure ou une FC cible : justifie-la par ses propres données. cad_spm est en pas par minute. Unités de la dynamique de course : stance_time (temps de contact au sol) en ms, vertical_oscillation en mm (divise par 10 pour des cm), step_length en mm (divise par 1000 pour des m), vertical_ratio en %, watts en W ; présente-les dans ces unités lisibles.
2. Charge : la référence est le RATIO DE CHARGE (charge des 7 derniers jours / moyenne hebdomadaire des 28 derniers jours), plage optimale 0,8 à 1,5. Le ratio ATL/CTL d'Intervals est gonflé après une coupure (CTL bas) : ne t'en sers pas pour alerter. Suis la consigne du bloc SURVEILLANCE : n'emploie le mot « alerte » que pour un risque ÉLEVÉ confirmé par plusieurs signaux, ne le répète jamais le même jour, ne dramatise ni un signal isolé ni un ratio dans la plage. Statut MAITRISEE = charge haute mais récupération bonne : présente-le comme « tu tapes fort et ton corps encaisse », jamais comme une alerte. Si les signaux se contredisent, dis-le.
3. Pour modifier le calendrier, appelle l'outil (planifier_seance, deplacer_seance, supprimer_seance, gerer_indisponibilite, restaurer_suppression). Ne dis JAMAIS qu'une séance est planifiée, déplacée ou supprimée sans avoir appelé l'outil. N'appelle aucun outil quand l'athlète demande seulement un conseil ou une analyse, ni dans les modes BRIEF, DEBRIEF et BILAN (messages automatiques).
4. Donne toujours les dates aux outils au format AAAA-MM-JJ, en t'appuyant sur les repères de dates fournis.
5. Format Telegram HTML : uniquement <b>, <i> et <code>, aucun Markdown (pas de #, pas de **), aucune autre balise. Pour les listes courtes, utilise des puces « • ».
5 ter. Poids : n'évoque que la tendance sur 7 à 28 jours, jamais une pesée isolée ni une fluctuation quotidienne ; reste factuel et neutre ; ne donne aucun conseil de régime ni d'objectif de poids sauf demande explicite de l'athlète ; relie-le seulement à la performance (W/kg, économie de course) ou à la récupération quand c'est pertinent.
5 bis. Mise en page des messages longs (DEBRIEF, ANALYSE, BRIEF, BILAN) : jamais de pavé de texte. Chaque section a son titre en gras sur sa propre ligne (<b>1. Vue d'ensemble</b>), avec une ligne vide avant et après. Sous un titre, une puce « • » par idée avec un libellé en gras (• <b>Allure moyenne :</b> 6'30/km), et une ligne vide entre deux puces dès qu'elles dépassent une ligne. Termine chaque section par une phrase de lecture en italique <i>…</i> qui dit ce que les chiffres signifient. Paragraphes de 3 lignes maximum, une ligne vide entre les sections. 
6. Suivis : quand tu t'engages à reprendre un sujet plus tard (douleur, matériel, décision, résultat à regarder), ajoute en fin de message une ligne [SUIVI AAAA-MM-JJ] ce qu'il faudra lui demander ou vérifier (2 maximum par message, dans 1 à 14 jours). Quand le bloc SUIVIS À FAIRE n'est pas vide, reviens dessus avec naturel, jamais comme une liste administrative.
6 bis. Mémoire : pour chaque fait durable (blessure, matériel et kilométrage, préférence, contrainte, façon dont l'athlète réagit à l'entraînement), ajoute en fin de message une ligne [MEMOIRE] fait à retenir. Plusieurs lignes possibles.
7. Tu n'es pas médecin : en cas de douleur persistante ou de symptôme inquiétant, recommande de consulter.
8. Si un bloc SÉRIE DÉTAILLÉE est fourni, c'est le CSV de la séance : exploite-le point par point (la colonne lap renvoie aux laps du bloc DÉTAIL). Si une analyse demande ce niveau de finesse et qu'il n'est pas fourni, invite l'athlète à écrire « analyse détaillée de [la séance ou la date] » (ajouter « csv complet » pour la seconde par seconde).
9. Planification : avant de planifier ou de valider une séance ou un volume, vérifie l'ENVELOPPE DE CHARGE. Si la demande la dépasse, dis-le avec les chiffres et propose une alternative qui rentre dans l'enveloppe. Si l'athlète insiste, planifie quand même en précisant le risque : c'est lui qui décide.
10. Météo : l'heure locale est fournie. Pour « dans 1 h », « ce soir », « demain matin », utilise le tableau MÉTÉO HEURE PAR HEURE et les créneaux favorables ; réponds avec les chiffres de l'heure demandée (ressenti, pluie, vent) et un conseil concret (tenue, créneau).
11. Si des données manquent ou se contredisent, dis-le et donne l'hypothèse la plus probable plutôt que d'affirmer. Chaque phrase doit servir : un fait, une décision, une émotion reconnue ou une question, jamais de remplissage.
12. Faits marquants : si le bloc FAITS MARQUANTS en contient, célèbre-les avec le chiffre dans le DEBRIEF, le BRIEF ou la CONVERSATION quand c'est pertinent. N'en invente jamais.
13. Suivi : si une note de la MÉMOIRE DURABLE datée des 7 derniers jours mentionne une gêne, une douleur ou une contrainte, prends-en des nouvelles dans le prochain BRIEF ou à la fin d'une conversation. Dans le BRIEF, reprends un élément de la veille (sensation, matériel, contrainte) pour montrer que tu suis.
14. Réaction : tu peux réagir à son message par un émoji en ajoutant, seule sur une ligne, [REACTION] 🔥 (au choix : 👍 ❤ 🔥 👏 🎉 💯 ⚡ 🏆 😁 🤔 🤝). Seulement en CONVERSATION ou QUESTION, quand ça sonne juste (belle performance, taquinerie, bonne nouvelle), jamais à chaque message.

MODES (la ligne MODE DE RÉPONSE du message indique lequel appliquer)
- CONVERSATION : l'athlète partage quelque chose. Réagis d'abord à ce qu'il dit, comme un coach qui l'écoute ; réponds aux sous-entendus ; apporte UNE valeur de coach liée à ses propos (ce que cela révèle, un conseil précis sur le matériel, la récupération, la suite). 1 ou 2 chiffres au maximum, seulement s'ils éclairent ce qu'il dit. Aucun titre, aucune liste, aucun bilan de séance, aucun rappel des splits. 40 à 120 mots.
- QUESTION : réponds franchement à la question dès la première phrase, puis donne le contexte chiffré qui compte. 60 à 180 mots.
- DEBRIEF (séance qui vient de se terminer, message automatique) et ANALYSE (l'athlète demande l'analyse d'une séance) : écris un vrai brief de course, en sections numérotées dont le titre est en gras (<b>1. Vue d'ensemble</b>) :
  1. Vue d'ensemble : une phrase de verdict qui accroche, puis quelques puces avec les valeurs clés en gras (distance, durée, allure moyenne, FC moyenne et max, puissance et cadence en pas/min si disponibles).
  2. Respect du plan et gestion de l'allure : consigne du calendrier par rapport au réalisé, et le choix de l'athlète (ce qu'il a bien décidé, ou pas).
  3. Réponse cardiaque et dérive : raconte la séance par phases (minutes ou km, plages de FC), en partant de l'enjeu que pose le contexte (séance dure récente, fatigue, chaleur) : « le risque était X, les chiffres montrent Y ». Ajoute la part du temps sous la FC cible (fc_cumul), le pic de FC et son moment, la dérive ou le découplage. Pour une séance à intervalles, lap par lap et minute par minute (par_min).
  4. Économie de course : seulement si cadence, puissance ou dynamique de course sont fournies. Stabilité du premier au dernier tiers (dérive en %) et ce que cela dit des muscles et des tendons.
  5. Bilan pour l'objectif et suite du programme : ce que la séance valide dans la semaine (SEMAINE EN COURS), l'effet sur l'objectif, puis « Pour la suite » avec les prochaines séances du calendrier, leurs consignes et ce qu'il faut surveiller.
  Pas de liste des splits km par km sauf anomalie. N'ouvre jamais par l'alerte de surcharge : si elle est active et pas encore donnée, une phrase dans la section 5. Termine par UNE question courte sur les sensations (jambes, souffle, douleurs), que les données ne disent pas. 300 à 450 mots. Pour une ANALYSE sur un point précis (« ai-je respecté les fractions ? »), réponds d'abord à ce point avec les chiffres lap par lap, puis le brief condensé.
- BRIEF (matin) : commence par une phrase humaine sur ce qui compte le plus ce matin, puis (a) récupération chiffrée par rapport à ses références ; (b) charge et tendance ; (c) séance du jour : la garder ou l'adapter, avec une prescription précise (durée, allure ou plage de FC justifiée par ses données, météo du créneau si elle compte) ; (d) un seul point de vigilance ; (e) objectif : une seule ligne, uniquement le lundi ou si un indicateur a nettement bougé. 150 à 250 mots, intertitres courts en gras autorisés.
- BILAN (hebdomadaire) : (a) volume réalisé par rapport à l'enveloppe, charge, fatigue, adhérence au plan ; (b) section OBJECTIF détaillée à partir de la ligne OBJECTIF du tableau de bord : estimation actuelle du 5 km, écart, progression nécessaire par semaine, tendance, verdict honnête sur la faisabilité (ni complaisant ni alarmiste) et ajustement du plan si besoin ; (c) les 3 séances clés de la semaine à venir, dans l'enveloppe de charge. 300 à 500 mots.
- SUIVI (message automatique) : prends des nouvelles comme un coach qui n'a rien oublié : 1 à 3 phrases chaleureuses, une seule question, aucun chiffre sauf s'il aide.
- PLANIFICATION : vérifie l'ENVELOPPE DE CHARGE, appelle l'outil, puis confirme en 1 à 3 phrases (ou explique ce qui dépasse l'enveloppe et propose une alternative).
- LIBRE (message vocal ou photo) : déduis le mode du contenu. Pour une photo (chaussures, repas, capture d'écran, paysage de sortie...), regarde l'image et réagis comme un coach qui la découvre, avec un conseil concret si elle s'y prête.

EXEMPLES DE TON (à ne pas recopier)
Athlète : « Super sortie, j'ai tenu mon cœur bas sans forcer, et mes vieilles chaussures (500 km) sont toujours aussi bonnes ! »
Coach : « Ça, c'est le genre de séance qui fait plaisir : même allure qu'il y a deux semaines avec un cœur plus bas, ton moteur aérobie avance. Pour les chaussures, 500 km c'est encore la zone confortable ; garde-les pour le foncier et les sorties longues, et réserve les plus vives pour jeudi. Les mollets, ils disent quoi ce soir ? »
Athlète : « Tu vois, je t'avais dit que je pouvais y aller ! »
Coach : « Touché, tu avais raison : sur cette séance tes sensations ont battu mes indicateurs. J'en retiens que tu encaisses bien les hausses de charge à allure facile. Je reste prudent pour les séances de seuil, mais je te fais plus confiance qu'hier.
[MEMOIRE] encaisse bien les hausses de charge à allure facile (l'ACWR a surestimé le risque) »"""


def _retry_delay(err_str):
    m = re.search(r"retry in ([\d.]+)\s*s", err_str) or re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)s", err_str)
    return float(m.group(1)) if m else None


_THINKING_OK = {"v": True}
_THINKING_BAD = set()


def _thinking_config(level):
    """Les tokens de réflexion sont facturés comme des tokens de sortie : niveau bas pour les messages courants."""
    if not _THINKING_OK["v"] or not level or level in ("default", "off", "none") or level in _THINKING_BAD:
        return None
    try:
        return types.ThinkingConfig(thinking_level=level)
    except Exception as err:
        logging.warning(f"thinking_level '{level}' refusé par le SDK ({err}) : réglage ignoré (pip install -U google-genai)")
        _THINKING_BAD.add(level)
        return None


def track_usage(r):
    """Journalise les tokens réellement facturés et cumule le total du jour (commande /cout)."""
    try:
        u = getattr(r, "usage_metadata", None)
        if not u:
            return
        p_in = int(getattr(u, "prompt_token_count", 0) or 0)
        cached = int(getattr(u, "cached_content_token_count", 0) or 0)
        out = int(getattr(u, "candidates_token_count", 0) or 0)
        think = int(getattr(u, "thoughts_token_count", 0) or 0)
        usd = ((p_in - cached) * PRICE_IN + cached * PRICE_CACHED + (out + think) * PRICE_OUT) / 1e6
        logging.info(f"Tokens : entrée={p_in} (cache={cached}) sortie={out} réflexion={think} -> {usd:.4f} $")
        key = f"usage|{datetime.date.today().isoformat()}"
        cur = json.loads(kv_get(key) or "{}")
        for k, v in (("calls", 1), ("in", p_in), ("cached", cached), ("out", out), ("think", think)):
            cur[k] = cur.get(k, 0) + v
        cur["usd"] = round(cur.get("usd", 0.0) + usd, 5)
        kv_set(key, json.dumps(cur))
    except Exception:
        logging.exception("track_usage")


def usage_report():
    today = datetime.date.today()
    lines, total = [], 0.0
    for i in range(7):
        d = (today - datetime.timedelta(days=i)).isoformat()
        raw = kv_get(f"usage|{d}")
        if not raw:
            continue
        u = json.loads(raw)
        total += u.get("usd", 0.0)
        lines.append(f"{d} : {u.get('calls', 0)} appels | entrée {u.get('in', 0):,} (cache {u.get('cached', 0):,}) | "
                     f"sortie {u.get('out', 0):,} | réflexion {u.get('think', 0):,} | {u.get('usd', 0.0):.3f} $".replace(",", " "))
    if not lines:
        return "Aucune consommation enregistrée pour l'instant."
    lines.append(f"Total 7 jours : {total:.2f} $ (tarifs en $ par million de tokens : entrée {PRICE_IN}, cache {PRICE_CACHED}, sortie {PRICE_OUT})")
    return "\n".join(lines)


def _build_config(allow_tools, level, system=None):
    kwargs = dict(
        system_instruction=system or SYSTEM_INSTRUCTION,
        tools=TOOLS if allow_tools else None,
        temperature=0.3,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    tc = _thinking_config(level or THINKING_CHAT)
    if tc is not None:
        kwargs["thinking_config"] = tc
    return types.GenerateContentConfig(**kwargs)


DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "1.5"))   # filet de sécurité : ~50 fois l'usage normal


def _spent_today():
    try:
        return float(json.loads(kv_get(f"usage|{datetime.date.today().isoformat()}") or "{}").get("usd", 0.0))
    except Exception:
        return 0.0


def call_gemini(contents, allow_tools=True, level=None, system=None):
    if _spent_today() >= DAILY_BUDGET_USD:
        raise AIError(f"⚠️ Budget Gemini du jour atteint ({_spent_today():.2f} $ sur {DAILY_BUDGET_USD:.2f} $). Il se réinitialise à minuit ; "
                      "augmente DAILY_BUDGET_USD si c'est voulu.")
    last = ""
    for attempt in range(3):
        cfg = _build_config(allow_tools, level, system)
        try:
            with AI_LOCK:
                r = ai_client.models.generate_content(model=MODEL_NAME, contents=contents, config=cfg)
            track_usage(r)
            return r
        except Exception as e:
            last = str(e)
            logging.error(f"Erreur Gemini (essai {attempt + 1}/3) : {last[:300]}")
            if "thinking" in last.lower() and ("400" in last or "INVALID_ARGUMENT" in last) and _THINKING_OK["v"]:
                logging.warning("Réglage de réflexion refusé par l'API : désactivé, nouvel essai")
                _THINKING_OK["v"] = False
                continue
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


def generate_ai(contents, allow_tools=True, level=None, system=None) -> str:
    """Un seul appel Gemini. Les outils sont executes une seule fois, hors de la boucle de retry."""
    r = call_gemini(contents, allow_tools=allow_tools, level=level, system=system)
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


def _strip_none(obj):
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


def _block(label, data):
    if not data:
        return f"{label} : (indisponible ou vide)"
    return f"{label} : {json.dumps(_strip_none(data), ensure_ascii=False, separators=(',', ':'))}"


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        t = f"{v:.2f}".rstrip("0").rstrip(".")
        return "0" if t in ("", "-0") else t
    if isinstance(v, (list, tuple)):
        return " ".join(_cell(x) for x in v)
    return str(v).replace('"/km', "").replace('"', "'").replace(",", ";").replace("\r", " ").replace("\n", " | ").strip()


def _table(rows, cols, drop_empty=False):
    """CSV compact : l'en-tête n'est écrit qu'une fois (un JSON répète chaque nom de champ à chaque ligne)."""
    if drop_empty:
        cols = [c for i, c in enumerate(cols) if i == 0 or any(_cell(r.get(c[1])) != "" for r in rows)]
    lines = [",".join(h for h, _ in cols)]
    lines += [",".join(_cell(r.get(k)) for _, k in cols) for r in rows]
    return "\n".join(lines)


def _tblock(label, rows, cols, drop_empty=False):
    if not rows:
        return f"{label} : (indisponible ou vide)"
    return f"{label} :\n{_table(rows, cols, drop_empty)}"


WELL_COLS = [("d", "d"), ("sommeil_h", "sleep_h"), ("hrv", "hrv"), ("hrv_base", "hrv_base"), ("fc_repos", "rhr"),
             ("tsb", "tsb"), ("atl", "atl"), ("ctl", "ctl"), ("score_sommeil", "sleep_score"), ("readiness", "readiness"),
             ("vo2max", "vo2max"), ("fatigue", "fatigue"), ("courbatures", "soreness"), ("stress", "stress"),
             ("humeur", "mood"), ("motivation", "motivation"), ("poids_kg", "weight")]
ACT_COLS = [("d", "d"), ("type", "type"), ("nom", "nom"), ("km", "km"), ("min", "min"), ("allure", "pace"),
            ("fc", "hr"), ("charge", "load")]
WEEK_COLS = [("semaine", "semaine"), ("seances", "seances"), ("km", "km_total"), ("h", "temps_h"), ("charge", "charge_totale"),
             ("km_course", "km_course"), ("allure_course", "allure_course"), ("fc_course", "fc_course")]
TOP_COLS = [("allure", "allure"), ("km", "distance_km"), ("date", "date"), ("nom", "nom")]
EVT_COLS = [("d", "d"), ("h", "h"), ("cat", "category"), ("nom", "nom"), ("description", "desc")]
LAP_COLS = [("n", "n"), ("type", "type"), ("label", "label"), ("debut_s", "debut_s"), ("duree_s", "duree_s"), ("km", "km"),
            ("allure", "pace"), ("fc", "hr"), ("fc_max", "hr_max"), ("cad_spm", "cad"), ("w", "w"), ("foulee_m", "foulee"),
            ("d+", "d+"), ("par_min", "par_min")]
KM_COLS = [("km", "km"), ("allure", "pace"), ("fc", "hr"), ("cad_spm", "cad"), ("w", "w"), ("d_alt", "d_alt")]
PHASE_COLS = [("phase", "ph"), ("km", "km"), ("allure", "pace"), ("fc", "fc"), ("fc_plage", "fc_plage"), ("cad_spm", "cad"), ("w", "w")]
WEATHER_COLS = [("heure", "h"), ("temp", "temp_c"), ("ressenti", "ress_c"), ("pluie_pct", "pluie_pct"), ("pluie_mm", "pluie_mm"), ("vent_kmh", "vent_kmh")]


def format_run_details(details):
    """Laps, phases, FC cumulée, dynamique de course et splits km de chaque course, en petits tableaux CSV."""
    out = []
    labels = {"cad": "cad_spm"}
    for d in details or []:
        extras = [f"{labels.get(k, k)}={_cell(d[k])}" for k in ("fc_max", "cad", "d+", "decouplage_pct", "ef", "intensite", "trimp", "temp_c",
                                                              "rpe", "feel", "gap", "watts_moy", "watts_np", "foulee_m") if d.get(k) is not None]
        if d.get("temps_zones_fc_s"):
            extras.append("zones_fc_s=" + "/".join(str(x) for x in d["temps_zones_fc_s"]))
        if d.get("pic_fc"):
            extras.append(f"pic_fc={d['pic_fc']['fc']} à la {d['pic_fc']['min']}e min")
        if d.get("chaussures"):
            extras.append("chaussures=" + _cell(d["chaussures"]))
        if d.get("notes"):
            extras.append("notes=" + _cell(d["notes"]))
        part = [f"## {d.get('d')} {_cell(d.get('nom'))} " + " ".join(extras)]
        if d.get("phases"):
            part.append("phases (tiers de la séance, plage = 5e-95e centile)\n" + _table(d["phases"], PHASE_COLS, drop_empty=True))
        if d.get("fc_cumul"):
            part.append("part du temps sous une FC donnée : " + d["fc_cumul"])
        if d.get("dyn"):
            part.append("dynamique de course (moyenne [5e-95e centile], 1er tiers→dernier tiers, dérive) : " + " | ".join(
                f"{k} {v['moy']} [{v['p5']}-{v['p95']}] {v['t1']}→{v['t3']} ({v['derive_pct']:+.1f} %)" for k, v in d["dyn"].items()))
        if d.get("laps"):
            part.append("laps\n" + _table(d["laps"], LAP_COLS, drop_empty=True))
        if d.get("km"):
            km_rows = [dict(r, km=(f"{r['km']}p" if r.get("partiel") else r["km"])) for r in d["km"]]
            part.append("km\n" + _table(km_rows, KM_COLS, drop_empty=True))
        out.append("\n".join(part))
    return "\n".join(out)


_ALERT_WORDS = re.compile(r"acwr|danger|surcharge|blessure|risque|ratio de charge|alerte", re.I)


def surveillance_info(well, acts=None):
    """Risque + consigne : un risque élevé n'est signalé qu'une fois par jour, un signal isolé n'est jamais dramatisé."""
    risk = evaluate_injury_risk(well, acts)
    if risk["statut"] == "DANGER":
        if kv_get("alert_ack") == f"{datetime.date.today().isoformat()}|DANGER":
            risk["consigne"] = ("RISQUE ÉLEVÉ DÉJÀ SIGNALÉ AUJOURD'HUI : ne le répète pas, ne le rappelle pas, ne conclus pas par un rappel. "
                                "N'y reviens que si l'athlète parle de douleur ou de fatigue, ou veut planifier ou intensifier une séance.")
        else:
            risk["consigne"] = ("RISQUE ÉLEVÉ, plusieurs signaux concordent (signaux_charge, signaux_physio) : PREMIÈRE FOIS AUJOURD'HUI, dis-le "
                                "calmement en 2 phrases maximum avec les signaux, puis réponds à la demande sans y revenir.")
    elif risk["statut"] == "MAITRISEE":
        risk["consigne"] = ("CHARGE ÉLEVÉE MAÎTRISÉE (ce n'est PAS une alerte) : la charge est haute (signaux_charge) mais la récupération est "
                            f"{risk['recup']['label']} (signaux_recup). Dis-le positivement en une phrase (« tu tapes fort et ton corps encaisse »), "
                            "garde un œil sur la récup, n'emploie jamais les mots alerte ou danger, ne recommande pas de baisser le volume, "
                            "et demande les sensations si c'est naturel.")
    elif risk["statut"] == "VIGILANCE":
        risk["consigne"] = "VIGILANCE (pas une alerte) : n'en parle que si cela change ta recommandation, en une phrase, sans dramatiser."
    elif risk["statut"] == "INCONNU":
        risk["consigne"] = "Données insuffisantes : ne conclus pas que tout va bien."
    else:
        risk["consigne"] = "Charge dans la plage optimale : aucune alerte, n'en invente pas."
    return risk


def ack_alert(well, reply_text, acts=None):
    """À appeler après une réponse réussie : marque l'alerte comme communiquée si la réponse l'a vraiment évoquée."""
    try:
        if evaluate_injury_risk(well, acts)["statut"] == "DANGER" and _ALERT_WORDS.search(reply_text or ""):
            kv_set("alert_ack", f"{datetime.date.today().isoformat()}|DANGER")
    except Exception:
        logging.exception("ack_alert")


def make_prompt(prof, weather, well, acts, evts, user_msg, series="auto"):
    """Données stables d'abord, éléments qui changent à chaque message en dernier : le préfixe identique
    est relu depuis le cache implicite de Gemini (tokens facturés environ 10 fois moins cher)."""
    today = datetime.date.today()
    days_left = (GOAL_DATE - today).days
    goal_line = f"J-{days_left}" if days_left >= 0 else "objectif passé"
    reperes = ", ".join(
        f"{JOURS_FR[d.weekday()]} {d.isoformat()}"
        for d in (today + datetime.timedelta(days=i) for i in range(8))
    )
    series_block = build_series_block(acts, user_msg, series)
    daily, weekly = split_wellness(well)
    hist = "\n".join(f"{'athlète' if h['role'] == 'athlete' else 'coach'}: {h['text']}" for h in get_chat_history(10)) or "(vide)"
    parts = [
        _block("PROFIL ATHLÈTE", dict(prof or {}, **({"prenom": ATHLETE_NAME} if ATHLETE_NAME else {}))),
        _block("MÉMOIRE DURABLE", get_notes()),
        *(["CARNET DE BORD (résumé des échanges plus anciens avec l'athlète) :\n" + kv_get("chat_summary")] if kv_get("chat_summary") else []),
        (f"TON DU COACH (choisi par l'athlète) : {COACH_TONE}" if COACH_TONE else "TON DU COACH : naturel, chaleureux et direct"),
        _tblock("SANTÉ, un jour par ligne, du plus récent au plus ancien (sommeil_h, hrv, hrv_base = référence HRV, fc_repos, tsb, atl, ctl ; score_sommeil, readiness, vo2max, fatigue, courbatures, stress, humeur, motivation = mesures de la montre et ressenti saisi par l'athlète, échelles d'Intervals, colonnes présentes seulement si renseignées)", daily, WELL_COLS, drop_empty=True),
    ]
    if weekly:
        parts.append(_tblock("SANTÉ, moyennes hebdomadaires plus anciennes", weekly, WELL_COLS))
    parts += [
        _tblock("ACTIVITÉS DES %d DERNIERS JOURS, du plus récent au plus ancien" % RAW_DAYS, acts.get("brut_recent"), ACT_COLS),
        "DÉTAIL DES DERNIÈRES COURSES (laps = intervalles de la montre : type WORK/RECOVERY, duree_s, allure, fc moyenne et max, "
        "par_min = allure/fc minute par minute ; km = splits au kilomètre, p = dernier tronçon partiel, d_alt = dénivelé net en m ; "
        "zones_fc_s = secondes par zone de FC) :\n" + (format_run_details(acts.get("detail_dernieres_courses")) or "(indisponible ou vide)"),
        _tblock("MEILLEURES ALLURES DE COURSE (6 mois, sorties de 3 km et plus)", acts.get("top_performances_recentes"), TOP_COLS),
        _tblock("AGRÉGATS HEBDO AU-DELÀ, JUSQU'À 6 MOIS (allure_course et fc_course = courses uniquement)", acts.get("agregat_hebdo_anterieur"), WEEK_COLS),
        _tblock("CALENDRIER (28 derniers jours + 90 jours à venir ; description fournie à +/- %d jours)" % DESC_DAYS, evts, EVT_COLS),
        "TABLEAU DE BORD (calculé par le code à partir des données ci-dessus : fiable, cite ces chiffres, ne les recalcule pas) :\n"
        + ("\n".join(dashboard_lines(prof, well, acts, evts)) or "(indisponible)"),
        "",
        f"Objectif prioritaire : {GOAL_TEXT} le {GOAL_DATE.strftime('%d/%m/%Y')} ({goal_line}).",
        f"Aujourd'hui : {JOURS_FR[today.weekday()]} {today.isoformat()}, il est {datetime.datetime.now():%H:%M} (heure locale).",
        f"Repères de dates : {reperes}.",
        _block("SURVEILLANCE SURCHARGE (statut, signaux et consigne)", surveillance_info(well, acts)),
        _block("MÉTÉO (ville, maintenant, 3 jours, créneaux les plus favorables)", {k: v for k, v in (weather or {}).items() if k != "horaire"}),
        _tblock("MÉTÉO HEURE PAR HEURE, 24 h, heure locale (pluie_pct = probabilité de pluie)", (weather or {}).get("horaire"), WEATHER_COLS),
        "HISTORIQUE RÉCENT DE CONVERSATION (du plus ancien au plus récent) :\n" + hist,
    ]
    due = due_followups()
    if due:
        parts += ["", "SUIVIS À FAIRE (promis par le coach, dus aujourd'hui : glisse-les naturellement dans ton message, comme un coach qui n'a pas oublié) :\n"
                  + "\n".join(f"• {f['txt']} (prévu le {f['due']})" for f in due)]
    if series_block:
        parts += ["", series_block]
    parts += ["", f"MODE DE RÉPONSE : {detect_mode(user_msg)}", f'MESSAGE DE L\'ATHLÈTE :\n"{user_msg}"']
    return "\n".join(parts)


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


_HEAD_RE = re.compile(r"^\s*(?:<b>\s*\d+[.)]\s+[^<\n]{2,90}</b>|\d+[.)]\s+<b>[^<\n]{2,90}</b>|<b>[^<\n]{3,80}</b>\s*:?)\s*$")


def airy(text):
    """Aère un message structuré (au moins 2 titres en gras) : ligne vide avant/après les titres, entre puces longues, après une liste."""
    lines = text.split("\n")
    if sum(1 for l in lines if _HEAD_RE.match(l)) < 2:
        return text
    out = []
    for i, l in enumerate(lines):
        head, bullet = bool(_HEAD_RE.match(l)), l.lstrip().startswith("•")
        prev = out[-1] if out else ""
        if out and prev.strip():
            if head or (prev.lstrip().startswith("•") and not bullet and l.strip()):
                out.append("")
            elif bullet and prev.lstrip().startswith("•") and (len(prev) > 90 or len(l) > 90):
                out.append("")
        out.append(l)
        if head and i + 1 < len(lines) and lines[i + 1].strip():
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _tg_post(method, payload, tries=3):
    """POST Telegram avec attente du délai imposé (429 retry_after) et nouvelles tentatives sur erreur réseau ou 5xx."""
    r = None
    for k in range(tries):
        try:
            r = requests.post(f"{TG_API}/{method}", json=payload, timeout=15)
        except Exception as err:
            logging.error(f"Erreur Telegram ({method}, essai {k + 1}/{tries}) : {err}")
            time.sleep(1 + 2 * k)
            continue
        if r.status_code == 429:
            try:
                wait = float((r.json().get("parameters") or {}).get("retry_after", 3))
            except Exception:
                wait = 3.0
            logging.warning(f"Telegram 429 : attente de {wait:g} s")
            time.sleep(min(wait, 20) + 0.5)
            continue
        if r.status_code >= 500:
            time.sleep(1 + 2 * k)
            continue
        return r
    return r


def tg_send(chat_id, text, buttons=None):
    """Envoi synchrone : HTML nettoyé, découpé à 4096 caractères, repli en texte brut.
    buttons : lignes de boutons [[(libellé, callback_data), ...], ...] affichées sous le dernier morceau."""
    text = airy(sanitize_html(text or ""))
    if not text:
        return
    markup = ({"inline_keyboard": [[{"text": lbl, "callback_data": data} for lbl, data in row] for row in buttons]}
              if buttons else None)
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        extra = {"reply_markup": markup} if (markup and i == len(chunks) - 1) else {}
        r = _tg_post("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", **extra})
        if r is not None and r.status_code == 200:
            continue
        if r is not None:
            logging.warning(f"Telegram a refusé le HTML ({r.status_code}) : {r.text[:200]}")
        plain = html.unescape(re.sub(r"<[^>]+>", "", chunk))
        r2 = _tg_post("sendMessage", {"chat_id": chat_id, "text": plain, **extra})
        if r2 is None or r2.status_code != 200:
            logging.error("Envoi Telegram en texte brut impossible")


def extract_reaction(text):
    """Sépare la ligne [REACTION] 🔥 du message ; retourne (texte, émoji autorisé ou None)."""
    text = text or ""
    mt = re.search(r"^[ \t]*\[REACTION\][ \t]*(\S+)[ \t]*$", text, re.M)
    emoji = mt.group(1).replace("\ufe0f", "") if mt else None
    clean = re.sub(r"[ \t]*\[REACTION\][^\n]*\n?", "", text).strip()
    return clean, (emoji if emoji in ALLOWED_REACTIONS else None)


def tg_react(chat_id, message_id, emoji):
    try:
        requests.post(f"{TG_API}/setMessageReaction",
                      json={"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]}, timeout=8)
    except Exception as err:
        logging.warning(f"Réaction Telegram impossible : {err}")


def add_followup(due, txt):
    """Enregistre un suivi promis par le coach (« reprendre des nouvelles du mollet jeudi »). Date : demain au plus tôt, 30 jours au plus tard."""
    txt = re.sub(r"\s+", " ", str(txt or "")).strip()[:200]
    try:
        d = datetime.date.fromisoformat(str(due))
    except ValueError:
        return False
    today = datetime.date.today()
    d = min(max(d, today + datetime.timedelta(days=1)), today + datetime.timedelta(days=30))
    if not txt or db_exec("SELECT 1 FROM followups WHERE done=0 AND txt=?", (txt,), fetch=True):
        return False
    if db_exec("SELECT COUNT(*) FROM followups WHERE done=0", fetch=True)[0][0] >= 6:
        return False
    db_exec("INSERT INTO followups (due, txt, created) VALUES (?, ?, ?)", (d.isoformat(), txt, today.isoformat()))
    return True


def due_followups(today=None, limit=3):
    iso = (today or datetime.date.today()).isoformat()
    rows = db_exec("SELECT id, due, txt FROM followups WHERE done=0 AND due<=? ORDER BY due, id LIMIT ?", (iso, limit), fetch=True) or []
    return [{"id": r[0], "due": r[1], "txt": r[2]} for r in rows]


def ack_followups(reply_text, force=False):
    """Un suivi est clos quand la réponse envoyée l'évoque (au moins un mot significatif du suivi) ; sinon il reste dû."""
    try:
        low = (reply_text or "").lower()
        for f in due_followups():
            words = [w for w in re.findall(r"[a-zà-ÿ]{5,}", f["txt"].lower())]
            if force or any(w in low for w in words):
                db_exec("UPDATE followups SET done=1 WHERE id=?", (f["id"],))
    except Exception:
        logging.exception("ack_followups")


def finalize_reply(text: str, save: bool = True) -> str:
    """Extrait les lignes [MEMOIRE] (plusieurs possibles), les enregistre, et sauvegarde la réponse dans l'historique."""
    text = extract_reaction(text)[0]
    notes = [n.strip() for n in re.findall(r"\[MEMOIRE\]\s*([^\n]+)", text) if n.strip()]
    if notes:
        known = {r[0] for r in db_exec("SELECT txt FROM notes", fetch=True) or []}
        for n in notes[:4]:
            if n[:500] not in known:
                save_note(n)
        text = re.sub(r"[ \t]*\[MEMOIRE\][^\n]*\n?", "", text).strip()
    for due, what in re.findall(r"\[SUIVI\s+(\d{4}-\d{2}-\d{2})\]\s*([^\n]+)", text)[:2]:
        add_followup(due, what)
    text = re.sub(r"[ \t]*\[SUIVI[^\]]*\][^\n]*\n?", "", text).strip()
    if save and text:
        save_chat_msg("coach", text)
    return text


async def send_reply(cid, text, save=True):
    text = finalize_reply(text, save)
    await asyncio.to_thread(tg_send, cid, text)


def _authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and u.id == TG_USER


def think_level(msg, prompt):
    """Réflexion Gemini selon le mode : élevée pour analyser/bilan, basse pour gérer le calendrier."""
    mode = detect_mode(msg)
    if mode in ("ANALYSE", "BILAN") or "SÉRIE DÉTAILLÉE (CSV" in prompt:
        return THINKING_DEEP
    if mode == "PLANIFICATION":
        return THINKING_ROUTINE
    return THINKING_CHAT


async def _typing_loop(bot, cid, action):
    """Garde l'indicateur « en train d'écrire » actif pendant toute la génération (Telegram l'efface après 5 s)."""
    try:
        while True:
            await bot.send_chat_action(chat_id=cid, action=action)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)


async def _stop_typing(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message or not update.message.text:
        return
    cid = update.effective_chat.id
    typing = asyncio.create_task(_typing_loop(context.bot, cid, ChatAction.TYPING))
    msg = update.message.text
    save, emoji = True, None
    try:
        prof, weather, well, acts, evts = await asyncio.to_thread(get_all_data)
        prompt = await asyncio.to_thread(make_prompt, prof, weather, well, acts, evts, msg)
        save_chat_msg("athlete", msg)   # après make_prompt : le message courant n'est pas dans l'historique
        ans = await asyncio.to_thread(generate_ai, prompt, True, think_level(msg, prompt))
        ack_alert(well, ans, acts)
        ack_followups(ans)
        ans, emoji = extract_reaction(ans)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_text")
        ans, save = "⚠️ Erreur interne, réessaie dans un instant.", False
    finally:
        await _stop_typing(typing)
    if emoji:
        await asyncio.to_thread(tg_react, cid, update.message.message_id, emoji)
    await send_reply(cid, ans, save=save)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    v = update.message.voice or update.message.audio
    if not v:
        return
    cid = update.effective_chat.id
    typing = asyncio.create_task(_typing_loop(context.bot, cid, ChatAction.RECORD_VOICE))
    save = True
    try:
        f = await context.bot.get_file(v.file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        mime = getattr(v, "mime_type", None) or "audio/ogg"

        prof, weather, well, acts, evts = await asyncio.to_thread(get_all_data)
        text_prompt = await asyncio.to_thread(
            make_prompt, prof, weather, well, acts, evts,
            "Message vocal de l'athlète (écoute l'audio joint et réponds-y).", False)
        save_chat_msg("athlete", "[Message vocal]")
        contents = [text_prompt, types.Part.from_bytes(data=buf.getvalue(), mime_type=mime)]
        ans = await asyncio.to_thread(generate_ai, contents)
        ack_alert(well, ans, acts)
        ack_followups(ans)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_voice")
        ans, save = "⚠️ Erreur interne, réessaie dans un instant.", False
    finally:
        await _stop_typing(typing)
    await send_reply(cid, ans, save=save)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo (chaussures, repas, paysage, capture d'écran...) : le coach la regarde et réagit."""
    if not _authorized(update) or not update.message or not update.message.photo:
        return
    cid = update.effective_chat.id
    caption = (update.message.caption or "").strip()
    typing = asyncio.create_task(_typing_loop(context.bot, cid, ChatAction.TYPING))
    save, emoji = True, None
    try:
        f = await context.bot.get_file(update.message.photo[-1].file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        data = buf.getvalue()
        prof, weather, well, acts, evts = await asyncio.to_thread(get_all_data)
        msg = "Photo de l'athlète" + (f" (légende : {caption})" if caption else "") + " : regarde l'image et réponds en coach."
        text_prompt = await asyncio.to_thread(make_prompt, prof, weather, well, acts, evts, msg, False)
        save_chat_msg("athlete", "[Photo]" + (f" {caption}" if caption else ""))
        ans = await asyncio.to_thread(generate_ai, [text_prompt, types.Part.from_bytes(data=data, mime_type="image/jpeg")])
        ack_alert(well, ans, acts)
        ack_followups(ans)
        ans, emoji = extract_reaction(ans)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_photo")
        ans, save = "⚠️ Je n'arrive pas à lire cette image, réessaie dans un instant.", False
    finally:
        await _stop_typing(typing)
    if emoji:
        await asyncio.to_thread(tg_react, cid, update.message.message_id, emoji)
    await send_reply(cid, ans, save=save)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons de la relance du soir : décaler à demain, annuler ou marquer comme faite, sans aucun appel Gemini."""
    q = update.callback_query
    if not q or not q.from_user or q.from_user.id != TG_USER:
        return
    try:
        await q.answer()
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    action, _, tok = str(q.data or "").partition("|")
    raw = kv_get(f"cb|{tok}")
    cid = q.message.chat_id if q.message else TG_USER
    if not raw or action not in ("mv", "rm", "ok"):
        await asyncio.to_thread(tg_send, cid, "⚠️ Ce bouton a expiré : écris-moi simplement ce que tu veux faire de la séance.")
        return
    info = json.loads(raw)
    d, nom = info.get("d"), info.get("nom") or ""
    try:
        await q.edit_message_reply_markup(reply_markup=None)    # un seul appui : le clavier disparaît
    except Exception:
        logging.debug("erreur ignorée", exc_info=True)
    if action == "mv":
        tomorrow = (datetime.date.fromisoformat(d) + datetime.timedelta(days=1)).isoformat()
        res = await asyncio.to_thread(deplacer_seance, d, tomorrow, nom)
        label = "Décaler à demain"
    elif action == "rm":
        res = await asyncio.to_thread(supprimer_seance, d, nom)
        label = "Annuler"
    else:
        res = "✅ Noté. Dès que ta montre synchronise, je te fais le débrief."
        label = "Déjà faite"
    save_chat_msg("athlete", f"[Bouton] {label} ({nom})")
    save_chat_msg("coach", "[Action] " + html.unescape(re.sub(r"<[^>]+>", "", res)))
    await asyncio.to_thread(tg_send, cid, res)


async def handle_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    await update.message.reply_text(usage_report())


_CONFLICT = {"first": 0.0, "last": 0.0, "n": 0, "alerted": 0.0}


# --------------------------------------------------------------------------
# SAUVEGARDE DE LA MEMOIRE DANS TELEGRAM (sans Volume Railway)
# Le fichier est envoyé en silence, épinglé dans ta conversation avec le bot, puis relu au démarrage si la base est vide.
# --------------------------------------------------------------------------
BACKUP_ON = os.environ.get("BACKUP_TELEGRAM", "1") != "0"
BACKUP_MIN_GAP_S = 600           # au plus une sauvegarde toutes les 10 minutes
BACKUP_NAME = "coach_backup"


def _tg_json(method, payload):
    r = _tg_post(method, payload)
    try:
        return r.json() if r is not None and r.status_code == 200 else {}
    except Exception:
        return {}


def _data_signature():
    """Change quand la mémoire durable change (notes, suivis, carnet, historique d'efforts), pas à chaque message."""
    row = db_exec("SELECT (SELECT COUNT(*) FROM notes), (SELECT COALESCE(MAX(id), 0) FROM notes), (SELECT COUNT(*) FROM followups), "
                  "(SELECT COUNT(*) FROM followups WHERE done=1), (SELECT COALESCE(LENGTH(v), 0) FROM kv WHERE k='chat_summary'), "
                  "(SELECT COUNT(*) FROM efforts)", fetch=True)[0]
    return "-".join(str(x) for x in row)


def make_backup_file():
    """Copie cohérente de la base (sans le cache des courbes, volumineux et recalculable), compressée. Retourne (chemin, dossier temporaire)."""
    tmp = tempfile.mkdtemp()
    snap = os.path.join(tmp, "snap.db")
    src, dst = sqlite3.connect(DB, timeout=10), sqlite3.connect(snap)
    try:
        src.backup(dst)
        try:
            dst.execute("DELETE FROM act_streams")
            dst.commit()
            dst.execute("VACUUM")
        except sqlite3.Error:
            logging.debug("cache des courbes non vidé", exc_info=True)
    finally:
        src.close()
        dst.close()
    gz = snap + ".gz"
    with open(snap, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    return gz, tmp


def backup_to_telegram():
    if not (BACKUP_ON and TG_TOKEN and TG_USER):
        return False
    tmp = None
    try:
        gz, tmp = make_backup_file()
        old = (_tg_json("getChat", {"chat_id": TG_USER}).get("result") or {}).get("pinned_message") or {}
        old_id = old.get("message_id") if str((old.get("document") or {}).get("file_name", "")).startswith(BACKUP_NAME) else None
        name = f"{BACKUP_NAME}_{datetime.datetime.now():%Y%m%d_%H%M}.db.gz"
        with open(gz, "rb") as fh:
            r = requests.post(f"{TG_API}/sendDocument", timeout=60, files={"document": (name, fh)},
                              data={"chat_id": TG_USER, "disable_notification": "true",
                                    "caption": "💾 Sauvegarde automatique de la mémoire du coach : ne la supprime pas, elle est remplacée toute seule."})
        res = (r.json().get("result") if r.status_code == 200 else None) or {}
        if not res.get("message_id"):
            logging.warning(f"Sauvegarde Telegram refusée ({r.status_code}) : {r.text[:200]}")
            return False
        _tg_post("pinChatMessage", {"chat_id": TG_USER, "message_id": res["message_id"], "disable_notification": True})
        if old_id and old_id != res["message_id"]:
            _tg_post("unpinChatMessage", {"chat_id": TG_USER, "message_id": old_id})
            _tg_post("deleteMessage", {"chat_id": TG_USER, "message_id": old_id})   # impossible après 48 h : sans gravité
        kv_set("backup_at", str(int(time.time())))
        kv_set("backup_sig", _data_signature())
        kv_set("backup_kb", str(os.path.getsize(gz) // 1024))
        logging.info(f"Sauvegarde Telegram envoyée ({os.path.getsize(gz) // 1024} Ko)")
        return True
    except Exception:
        logging.exception("backup_to_telegram")
        return False
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def maybe_backup(now=None):
    """Sauvegarde si la mémoire durable a changé (au plus toutes les 10 min) et au moins une fois par nuit."""
    if not BACKUP_ON:
        return False
    now = now or datetime.datetime.now()
    age = time.time() - int(kv_get("backup_at") or 0)
    changed = _data_signature() != kv_get("backup_sig")
    if (changed and age >= BACKUP_MIN_GAP_S) or (now.hour >= 3 and age >= 20 * 3600):
        return backup_to_telegram()
    return False


def db_is_blank():
    if not os.path.exists(DB) or os.path.getsize(DB) == 0:
        return True
    try:
        conn = sqlite3.connect(DB, timeout=5)
        try:
            n = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("notes", "chat_history", "followups", "kv"))
        finally:
            conn.close()
        return n == 0
    except sqlite3.Error:
        return True


def restore_from_telegram():
    """Au démarrage, base vide (déploiement sans Volume) : relit la sauvegarde épinglée dans la conversation."""
    if not (BACKUP_ON and TG_TOKEN and TG_USER) or not db_is_blank():
        return False
    try:
        doc = ((_tg_json("getChat", {"chat_id": TG_USER}).get("result") or {}).get("pinned_message") or {}).get("document") or {}
        if not str(doc.get("file_name", "")).startswith(BACKUP_NAME):
            return False
        path = (_tg_json("getFile", {"file_id": doc.get("file_id")}).get("result") or {}).get("file_path")
        if not path:
            return False
        r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
        if r.status_code != 200:
            return False
        raw = gzip.decompress(r.content)
        if not raw.startswith(b"SQLite format 3\x00"):
            return False
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
        tmp = DB + ".restore"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        chk = sqlite3.connect(tmp)
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
        finally:
            chk.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(DB + ext):
                os.remove(DB + ext)
        os.replace(tmp, DB)
        logging.info("Mémoire restaurée depuis la sauvegarde Telegram")
        return True
    except Exception:
        logging.exception("restore_from_telegram")
        return False


async def handle_sauvegarde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    ok = await asyncio.to_thread(backup_to_telegram)
    await asyncio.to_thread(tg_send, update.effective_chat.id,
                            "✅ Sauvegarde envoyée et épinglée en haut de cette conversation." if ok else "⚠️ Sauvegarde impossible, regarde /etat et les logs.")



BUILD = "2026-10-11"
_OPTIONAL_WELLNESS = ("hrv", "rhr", "sleep_h", "weight", "sleep_score", "readiness", "vo2max", "fatigue", "soreness", "stress", "mood", "motivation")


def diagnostic():
    """Autotest affiché par /etat : ce qui fonctionne et ce qui manque, sans aller lire les logs Railway."""
    ok, ko = "✅", "⚠️"
    L = [f"<b>État du coach</b> (version {BUILD})"]
    L.append(f"{ok} Heure locale {datetime.datetime.now():%H:%M}, fuseau {os.environ.get('TZ') or 'du serveur'} · modèle {_esc(MODEL_NAME)} · "
             f"réflexion {THINKING_ROUTINE}/{THINKING_CHAT}/{THINKING_DEEP}" + ("" if _THINKING_OK["v"] else f" ({ko} refusée par l'API, désactivée)"))
    L.append(f"{ok if GEMINI_KEY else ko} Clé Gemini {'présente' if GEMINI_KEY else 'ABSENTE'} · dépense du jour {_spent_today():.3f} $ sur {DAILY_BUDGET_USD:.2f} $")
    try:
        r = requests.get(BASE, auth=AUTH, timeout=8)
        L.append(f"{ok if r.status_code == 200 else ko} Intervals : profil HTTP {r.status_code}" + ("" if r.status_code == 200 else " (clé API ou identifiant athlète à vérifier)"))
    except Exception as err:
        L.append(f"{ko} Intervals injoignable : {_esc(str(err)[:120])}")
    try:
        prof, weather, well, acts, evts = get_all_data()
    except Exception as err:
        return "\n".join(L + [f"{ko} Lecture des données impossible : {_esc(str(err)[:150])}"])
    recent = acts.get("brut_recent", [])
    got = [k for k in _OPTIONAL_WELLNESS if any(w.get(k) is not None for w in well[:14])]
    miss = [k for k in _OPTIONAL_WELLNESS if k not in got]
    L.append(f"{ok if well else ko} Santé : {len(well)} jours (dernier {well[0].get('d') if well else '—'}) · reçu : {', '.join(got) or 'rien'}" + (f" · absent : {', '.join(miss)}" if miss else ""))
    runs = [a for a in recent if a.get("type") in RUN_TYPES]
    L.append(f"{ok if recent else ko} Activités : {len(recent)} sur {RAW_DAYS} jours, {len(runs)} courses" + (f" (dernière : {runs[0]['d']} {_esc(runs[0].get('nom') or '')})" if runs else ""))
    L.append(f"{ok if evts else ko} Calendrier : {len(evts)} événements · météo {'ok' if (weather or {}).get('horaire') else 'indisponible'} · matériel suivi : {len((prof or {}).get('materiel') or [])}")
    if runs:
        try:
            r = requests.get(f"{API_ROOT}/activity/{runs[0]['id']}/streams.json", auth=AUTH, timeout=25)
            names = sorted(parse_streams(r.json()).keys()) if r.status_code == 200 else []
            L.append(f"{ok if names else ko} Courbes de la dernière course ({len(names)}) : {_esc(', '.join(names)) or 'aucune'}")
        except Exception as err:
            L.append(f"{ko} Courbes illisibles : {_esc(str(err)[:100])}")
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if vol and os.path.abspath(DB).startswith(vol):
        pers = f"{ok} base sur le Volume ({_esc(vol)}) : la mémoire survit aux déploiements"
    elif vol:
        pers = f"{ko} Volume monté sur {_esc(vol)} mais la base est ailleurs : mets DB_PATH={_esc(vol)}/coach_brain.db"
    else:
        pers = f"{ko} aucun Volume détecté : la base est effacée à chaque déploiement (notes, carnet, suivis)"
    n = lambda q: db_exec(q, fetch=True)[0][0]
    size = os.path.getsize(DB) // 1024 if os.path.exists(DB) else 0
    L.append(pers)
    ba = int(kv_get("backup_at") or 0)
    L.append((f"{ok} Sauvegarde Telegram : dernière il y a {(time.time() - ba) / 3600:.0f} h ({kv_get('backup_kb') or '?'} Ko), restaurée seule après un déploiement"
              if ba else f"{ko} Sauvegarde Telegram : aucune pour l'instant (envoie /sauvegarde)") if BACKUP_ON else f"{ko} Sauvegarde Telegram désactivée")
    L.append(f"{ok} Mémoire : {n('SELECT COUNT(*) FROM notes')} notes · {n('SELECT COUNT(*) FROM followups WHERE done=0')} suivis en attente · "
             f"{n('SELECT COUNT(*) FROM chat_history')} messages · carnet {len(kv_get('chat_summary') or '')} car. · base {size} Ko")
    L.append(f"{ok if TG_USER else ko} Telegram : utilisateur autorisé {'défini' if TG_USER else 'ABSENT'}")
    return "\n".join(L)


HELP_TEXT = (
    "<b>Ce que je sais faire</b>\n\n"
    "• <b>Analyser :</b> « analyse ma séance d'hier », « donne-moi le csv complet »\n"
    "• <b>Planifier :</b> « planifie un footing demain 18h », « décale ma séance à jeudi », « annule la séance de samedi », « annule la dernière suppression »\n"
    "• <b>T'aider au quotidien :</b> « il fera quoi dans 1 h ? », « je suis malade jusqu'à vendredi », « recommence le bilan matinal »\n"
    "• <b>Discuter :</b> raconte-moi tes sensations, ton matériel, ta semaine : je retiens ce qui compte\n\n"
    "<b>Commandes :</b> /etat (autotest complet), /cout (dépense des 7 derniers jours), /sauvegarde (copie de ma mémoire dans cette conversation), /aide\n\n"
    "<b>Réglages (variables Railway) :</b> ATHLETE_NAME, COACH_TONE, THINKING_CHAT, DAILY_BUDGET_USD, REMINDER_LEAD_MIN, FOLLOWUP_HOUR, SUIVI_HOUR, DB_PATH")


async def handle_etat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    text = await asyncio.to_thread(diagnostic)
    await asyncio.to_thread(tg_send, update.effective_chat.id, text)


async def handle_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    await asyncio.to_thread(tg_send, update.effective_chat.id, HELP_TEXT)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        # Deux instances interrogent Telegram avec le même token : normal quelques secondes pendant un déploiement
        now = time.time()
        if now - _CONFLICT["last"] > 120:      # nouvelle série de conflits
            _CONFLICT.update(first=now, n=0)
        _CONFLICT["last"] = now
        _CONFLICT["n"] += 1
        logging.warning("Telegram Conflict n°%d : une autre instance utilise ce token (transitoire pendant un déploiement)", _CONFLICT["n"])
        if now - _CONFLICT["first"] >= 300 and now - _CONFLICT["alerted"] > 21600:
            _CONFLICT["alerted"] = now
            await asyncio.to_thread(
                tg_send, TG_USER,
                "⚠️ <b>Conflit Telegram persistant</b> (plus de 5 min) : une autre instance du bot tourne avec le même token "
                "(autre service Railway, ordinateur, ancien projet). Arrête-la, ou régénère le token dans BotFather "
                "et mets à jour TELEGRAM_BOT_TOKEN.")
        return
    logging.error("Exception Telegram", exc_info=err)


# --------------------------------------------------------------------------
# TACHES AUTOMATIQUES
# --------------------------------------------------------------------------
AUTO_SUFFIX = "\n(Message automatique : n'appelle aucun outil, propose seulement.)"


def _push(header, ans, tag):
    text = finalize_reply(ans, save=False)
    if text:
        save_chat_msg("coach", f"[{tag}] {text}")
        tg_send(TG_USER, f"{header}\n\n{text}")


def usual_train_hour(recent, today=None):
    """Heure médiane de tes courses des 28 derniers jours (sinon TRAIN_HOUR)."""
    today = today or datetime.date.today()
    since = (today - datetime.timedelta(days=28)).isoformat()
    hs = sorted(int(a["h"][:2]) for a in recent if a.get("type") in RUN_TYPES and a.get("h") and a.get("d", "") >= since)
    return hs[len(hs) // 2] if len(hs) >= 3 else TRAIN_HOUR


def _esc(x):
    return html.escape(str(x), quote=False)


def build_reminder(e, start, weather, mins):
    lines = [f"⏰ <b>Séance à {start:%H:%M} : {_esc(e.get('nom') or 'Séance')}</b> (dans environ {int(round(mins / 5) * 5)} min)"]
    desc = (e.get("desc") or "").strip().replace("\n", " | ")
    if desc:
        lines.append(_esc(desc[:220]))
    rows = weather_window(weather, start)
    if rows:
        r0 = rows[0]
        lines.append(f"🌦️ Pendant la séance : {r0['temp_c']}°C (ressenti {r0['ress_c']}°C), pluie {r0['pluie_pct']} %, vent {r0['vent_kmh']} km/h")
        tips = weather_tips(rows)
        if tips:
            now_key = (start - datetime.timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:00")
            alt = best_windows([r for r in (weather or {}).get("horaire") or [] if r.get("t", "") >= now_key], 1)
            lines.append("👉 " + " ; ".join(tips) + "." + (f" Meilleur créneau des 24 h : {_esc(alt[0])}." if alt and any("pluie" in t for t in tips) else ""))
    return "\n\n".join(lines)


def followup_buttons(e, iso):
    """Trois boutons sous la relance du soir ; l'action est retrouvée par un jeton court (callback_data limité à 64 octets)."""
    tok = hashlib.md5(f"{iso}|{e.get('nom')}".encode("utf-8")).hexdigest()[:10]
    kv_set(f"cb|{tok}", json.dumps({"d": iso, "nom": e.get("nom") or ""}, ensure_ascii=False))
    return [[("📅 Décaler à demain", f"mv|{tok}"), ("❌ Annuler", f"rm|{tok}")], [("✅ Déjà faite", f"ok|{tok}")]]


def build_followup(e):
    return (f"🕘 <b>Séance non détectée aujourd'hui : {_esc(e.get('nom') or 'Séance')}</b>\n\n"
            "Si elle est faite, elle n'est peut-être pas encore synchronisée. Sinon, un appui suffit pour que je mette "
            "le calendrier à jour.")


def _push_plain(text, tag, buttons=None):
    save_chat_msg("coach", f"[{tag}] " + html.unescape(re.sub(r"<[^>]+>", "", text)))
    tg_send(TG_USER, text, buttons) if buttons else tg_send(TG_USER, text)


def build_eve(e, start, weather):
    nom = e.get("nom") or "Séance"
    desc = (e.get("desc") or "").strip().replace("\n", " | ")
    hard = bool(re.search(r"seuil|fraction|vma|\d+\s*x\s*\d|tempo|allure sp[ée]cifique", f"{nom} {desc}", re.I))
    lines = [f"🌙 <b>Demain à {start:%H:%M} : {_esc(nom)}</b>"]
    if desc:
        lines.append(_esc(desc[:220]))
    rows = weather_window(weather, start)
    if rows:
        r0 = rows[0]
        meteo = f"🌦️ Prévu sur le créneau : {r0['temp_c']}°C (ressenti {r0['ress_c']}°C), pluie {r0['pluie_pct']} %, vent {r0['vent_kmh']} km/h"
        tips = weather_tips(rows)
        lines.append(meteo + (" — " + " ; ".join(tips) if tips else ""))
    lines.append("👉 " + ("Séance exigeante : repas riche en glucides ce soir, hydrate-toi et couche-toi tôt."
                          if hard else "Séance facile : hydrate-toi et vise une bonne nuit, le reste suivra."))
    return "\n\n".join(lines)


def eve_tick(evts, acts, weather, now):
    """Veille de séance : un seul message, à partir de EVE_HOUR, si une course est prévue demain."""
    if not (EVE_HOUR <= now.hour <= 23):
        return
    tomorrow = (now.date() + datetime.timedelta(days=1))
    tiso = tomorrow.isoformat()
    usual = usual_train_hour((acts or {}).get("brut_recent", []), now.date())
    for e in (evts or []):
        if e.get("d") != tiso or e.get("category") not in (None, "WORKOUT") or e.get("type") not in (None, *RUN_TYPES):
            continue
        try:
            start = datetime.datetime.combine(tomorrow, datetime.time.fromisoformat(e.get("h") or f"{usual:02d}:00"))
        except ValueError:
            continue
        key = f"veille|{tiso}|{e.get('nom') or 'Séance'}"
        if claim("seen_reports", key):
            try:
                _push_plain(build_eve(e, start, weather), "Veille auto")
            except Exception:
                release("seen_reports", key)
                logging.exception("veille")


def reminders_tick(evts, acts, weather, now=None):
    """Rappel météo avant la séance du jour, relance le soir si elle n'est pas détectée. Messages écrits par le code, sans Gemini."""
    now = now or datetime.datetime.now()
    eve_tick(evts, acts, weather, now)
    today = now.date()
    iso = today.isoformat()
    todays = [e for e in (evts or []) if e.get("d") == iso and e.get("category") in (None, "WORKOUT") and e.get("type") in (None, *RUN_TYPES)]
    if not todays:
        return
    recent = (acts or {}).get("brut_recent", [])
    if any(a.get("d") == iso and a.get("type") in RUN_TYPES for a in recent):
        return
    usual = usual_train_hour(recent, today)
    for e in todays:
        nom = e.get("nom") or "Séance"
        try:
            start = datetime.datetime.combine(today, datetime.time.fromisoformat(e.get("h") or f"{usual:02d}:00"))
        except ValueError:
            continue
        mins = (start - now).total_seconds() / 60
        if 0 < mins <= REMINDER_LEAD_MIN:
            key, text, tag = f"rappel|{iso}|{nom}", None, "Rappel auto"
            if claim("seen_reports", key):
                try:
                    _push_plain(build_reminder(e, start, weather, mins), tag)
                except Exception:
                    release("seen_reports", key)
                    logging.exception("rappel")
        elif now.hour >= FOLLOWUP_HOUR and mins <= -60:
            key = f"relance|{iso}|{nom}"
            if claim("seen_reports", key):
                try:
                    _push_plain(build_followup(e), "Relance auto", followup_buttons(e, iso))
                except Exception:
                    release("seen_reports", key)
                    logging.exception("relance")


def bg_init():
    try:
        _, _, well, acts, _ = get_all_data()
        for a in acts.get("brut_recent", []):
            mark_seen("seen_acts", str(a["id"]))
        if well and well[0].get("d"):
            mark_seen("seen_well", str(well[0]["d"]))
    except Exception:
        logging.exception("Erreur init bg_loop")


SUIVI_HOUR = int(os.environ.get("SUIVI_HOUR", "10"))   # heure à partir de laquelle un suivi dû est envoyé s'il n'a pas déjà été évoqué


def suivi_tick(prof, weather, well, acts, evts, now=None):
    """Un suivi dû et pas encore évoqué : message court et chaleureux, une fois par jour."""
    now = now or datetime.datetime.now()
    if now.hour < SUIVI_HOUR or not due_followups(now.date()):
        return
    key = f"suivi|{now.date().isoformat()}"
    if not claim("seen_reports", key):
        return
    try:
        todo = "; ".join(f["txt"] for f in due_followups(now.date()))
        msg = f"Suivi à faire : {todo}{AUTO_SUFFIX}"
        ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=False), allow_tools=False)
        _push("💬", ans, "Suivi auto")
        ack_followups(ans, force=True)
    except Exception as e:
        release("seen_reports", key)
        logging.warning(f"Suivi différé : {e}")


SUMMARY_SYSTEM = (
    "Tu tiens le carnet de bord d'un coach de course à pied. Résume en français, en 120 mots maximum, ce qui compte pour la suite : état physique "
    "et sensations de l'athlète, blessures, décisions prises, engagements du coach, matériel, humeur, préférences exprimées, sujets en cours. "
    "Ne répète pas les chiffres d'entraînement (le coach les a déjà), pas de formule de politesse, texte brut sans mise en forme.")
SUMMARY_EVERY, KEEP_VERBATIM = 16, 10
_BG_ERR = {"n": 0, "alerted": 0.0}


def update_chat_summary():
    """Résume en arrière-plan les échanges plus anciens que les 10 derniers messages (un appel court tous les ~16 messages)."""
    last = int(kv_get("chat_summary_upto") or 0)
    rows = db_exec("SELECT id, role, txt FROM chat_history WHERE id>? ORDER BY id", (last,), fetch=True) or []
    if len(rows) < SUMMARY_EVERY + KEEP_VERBATIM:
        return False
    batch = rows[:-KEEP_VERBATIM]
    convo = "\n".join(f"{'athlète' if r[1] == 'athlete' else 'coach'} : {r[2][:400]}" for r in batch)
    text = generate_ai(f"RÉSUMÉ ACTUEL :\n{kv_get('chat_summary') or '(vide)'}\n\nNOUVEAUX ÉCHANGES :\n{convo}\n\n"
                       "Écris le résumé mis à jour (garde l'essentiel de l'ancien, intègre le nouveau, supprime ce qui est dépassé).",
                       allow_tools=False, level=THINKING_ROUTINE, system=SUMMARY_SYSTEM)
    kv_set("chat_summary", text.strip()[:1200])
    kv_set("chat_summary_upto", str(batch[-1][0]))
    return True


def check_data_health(well, acts):
    """Prévient (une fois par jour) quand Intervals ne renvoie plus rien trois relevés de suite : clé invalide, service injoignable."""
    if not well and not (acts or {}).get("brut_recent"):
        n = int(kv_get("health_fail") or 0) + 1
        kv_set("health_fail", str(n))
        if n >= 3 and claim("seen_reports", f"alerte_donnees|{datetime.date.today().isoformat()}"):
            tg_send(TG_USER, "⚠️ <b>Je ne lis plus tes données Intervals</b> (3 relevés vides de suite) : clé API invalide, identifiant athlète ou service injoignable. Regarde les logs Railway.")
    else:
        kv_set("health_fail", "0")


def bg_tick():
    prof, weather, well, acts, evts = get_all_data()
    check_data_health(well, acts)
    today = datetime.date.today()

    # 1. Nouvelles activités (au plus MAX_DEBRIEFS_PER_CYCLE débriefs, les autres sont marquées vues)
    brut = acts.get("brut_recent", [])
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
        if not claim("seen_acts", act_id):
            continue
        try:
            msg = f"Débrief séance terminée : {json.dumps(a, ensure_ascii=False)}{AUTO_SUFFIX}"
            ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=a), allow_tools=False, level=THINKING_DEEP)
            _push("🏁 <b>Nouvelle séance détectée !</b>", ans, "Débrief auto")
            ack_alert(well, ans, acts)
            ack_followups(ans)
        except Exception as e:
            release("seen_acts", act_id)
            logging.warning(f"Débrief différé ({act_id}) : {e}")
            break

    # 2. Brief réveil (nuit synchronisée)
    if (well and well[0].get("d") and well[0].get("sleep_h")
            and not is_seen("seen_well", str(well[0]["d"])) and claim("seen_well", str(well[0]["d"]))):
        try:
            msg = f"Brief réveil : {json.dumps(well[0], ensure_ascii=False)}{AUTO_SUFFIX}"
            ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=False), allow_tools=False)
            _push("☀️ <b>Réveil détecté</b>", ans, "Brief auto")
            ack_alert(well, ans, acts)
            ack_followups(ans)
        except Exception as e:
            release("seen_well", str(well[0]["d"]))
            logging.warning(f"Brief réveil différé : {e}")

    # 3. Bilan hebdomadaire (dimanche soir, heure locale)
    now = datetime.datetime.now()
    if now.weekday() == 6 and now.hour >= 19:
        iso = now.isocalendar()
        week_id = f"bilan_{iso[0]}_{iso[1]}"
        if not is_seen("seen_reports", week_id) and claim("seen_reports", week_id):
            try:
                msg = ("C'est dimanche soir. Rédige le BILAN HEBDOMADAIRE complet : volume réalisé par rapport à l'enveloppe, "
                       "charge, fatigue, adhérence au plan ; section OBJECTIF détaillée ; les 3 séances clés de la semaine à venir "
                       "dans l'enveloppe de charge." + AUTO_SUFFIX)
                ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=False), allow_tools=False, level=THINKING_DEEP)
                _push("📊 <b>Bilan hebdomadaire du Coach</b>", ans, "Bilan Hebdo")
                ack_alert(well, ans, acts)
                ack_followups(ans)
            except Exception as e:
                release("seen_reports", week_id)
                logging.warning(f"Bilan hebdo différé : {e}")

    try:
        reminders_tick(evts, acts, weather)
        suivi_tick(prof, weather, well, acts, evts)
        update_chat_summary()
        maybe_backup()
    except Exception:
        logging.exception("reminders_tick")


def bg_loop():
    time.sleep(20)
    bg_init()
    while True:
        time.sleep(BG_INTERVAL)
        try:
            bg_tick()
            _BG_ERR["n"] = 0
        except Exception:
            logging.exception("Erreur boucle bg_loop")
            _BG_ERR["n"] += 1
            if _BG_ERR["n"] >= 4 and time.time() - _BG_ERR["alerted"] > 21600:
                _BG_ERR["alerted"] = time.time()
                try:
                    tg_send(TG_USER, "⚠️ <b>Erreurs répétées en arrière-plan</b> (4 cycles de suite) : les alertes automatiques ne partent peut-être plus. Regarde les logs Railway.")
                except Exception:
                    logging.debug("alerte impossible", exc_info=True)


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
    restored = restore_from_telegram()
    init_db()
    if restored:
        tg_send(TG_USER, "🔄 <b>Mémoire restaurée</b> depuis ta dernière sauvegarde : notes, carnet de bord et suivis sont de retour.")
    apply_timezone()
    if STARTUP_DELAY_S > 0:
        logging.info(f"Attente de {STARTUP_DELAY_S} s : l'ancienne instance doit s'arrêter avant le polling Telegram")
        time.sleep(STARTUP_DELAY_S)
    logging.info(f"Bot Coach Running démarré (modèle : {MODEL_NAME})")
    threading.Thread(target=bg_loop, daemon=True).start()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("cout", handle_cost))
    app.add_handler(CommandHandler("etat", handle_etat))
    app.add_handler(CommandHandler("aide", handle_aide))
    app.add_handler(CommandHandler("sauvegarde", handle_sauvegarde))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)
