"""Coach running : bot Telegram (Intervals.icu + Gemini + Open-Meteo + SQLite)."""
import os
import io
import re
import json
import time
import zlib
import bisect
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
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

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
DETAIL_RUNS = int(os.environ.get("DETAIL_RUNS", "6"))   # courses détaillées (laps + durée en zones)
DETAIL_FULL = 3                 # parmi elles, nb de courses avec splits au kilomètre
DETAIL_VERSION = 2              # incrémenter pour forcer le recalcul du cache de détails
STREAM_VERSION = 1
RAW_DAYS = int(os.environ.get("RAW_DAYS", "90"))   # activités listées une par une ; au-delà (jusqu'à 6 mois) : agrégats hebdo
DESC_DAYS = int(os.environ.get("DESC_DAYS", "21"))  # descriptions de séances envoyées à +/- N jours (noms seuls au-delà)
WELLNESS_FULL_DAYS = int(os.environ.get("WELLNESS_FULL_DAYS", "90"))   # jours de santé envoyés un par un
PRICE_IN = float(os.environ.get("PRICE_IN", "0.75"))          # $ par million de tokens (tarif Gemini 3.8 Flash jusqu'au 31/12/2026)
PRICE_CACHED = float(os.environ.get("PRICE_CACHED", "0.075"))
PRICE_OUT = float(os.environ.get("PRICE_OUT", "3.75"))        # la réflexion est facturée comme de la sortie
THINKING_ROUTINE = os.environ.get("THINKING_ROUTINE", "low").strip().lower()    # réflexion Gemini : messages courants
THINKING_DEEP = os.environ.get("THINKING_DEEP", "medium").strip().lower()       # réflexion Gemini : analyses de séance
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
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    db_exec("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, txt TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_acts (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_well (d TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS seen_reports (id TEXT PRIMARY KEY)")
    db_exec("CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, txt TEXT, d TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS deleted_events (id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, d TEXT, payload TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS act_details (id TEXT PRIMARY KEY, v INTEGER, payload TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS act_streams (id TEXT PRIMARY KEY, v INTEGER, blob BLOB)")
    db_exec("DELETE FROM act_details WHERE v != ?", (DETAIL_VERSION,))
    db_exec("DELETE FROM act_streams WHERE v != ?", (STREAM_VERSION,))
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
                row = {"d": d_str, "nom": e.get("name")}
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
                       "tsb": avg("tsb"), "atl": avg("atl"), "ctl": avg("ctl")})
    return daily, weekly


# --------------------------------------------------------------------------
# DETAIL DES COURSES : laps de la montre, splits km, série seconde par seconde (cache en base)
# --------------------------------------------------------------------------
SUMMARY_KEYS = {
    "max_heartrate": "fc_max", "average_cadence": "cad", "total_elevation_gain": "d+",
    "decoupling": "decouplage_pct", "icu_efficiency_factor": "ef", "icu_intensity": "intensite",
    "trimp": "trimp", "average_temp": "temp_c", "icu_rpe": "rpe", "feel": "feel",
}
STREAM_TYPES = "time,distance,heartrate,cadence,altitude,velocity_smooth"


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


def _ffill(arr):
    out, last = [], 0
    for v in arr:
        last = v if isinstance(v, (int, float)) else last
        out.append(last)
    return out


def _idx_at(times, t):
    return min(max(bisect.bisect_left(times, t), 0), len(times) - 1)


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


def get_streams(act_id):
    """Données seconde par seconde d'une activité (cache compressé en base). None si Intervals ne répond pas."""
    row = db_exec("SELECT blob FROM act_streams WHERE id=? AND v=?", (act_id, STREAM_VERSION), fetch=True)
    if row:
        try:
            return json.loads(zlib.decompress(row[0][0]))
        except Exception:
            logging.warning(f"Cache streams illisible pour {act_id}, nouveau téléchargement")
    s = requests.get(f"{API_ROOT}/activity/{act_id}/streams.json", auth=AUTH,
                     params={"types": STREAM_TYPES}, timeout=25)
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
    """Splits au kilomètre : allure, FC moyenne, cadence, dénivelé net. Dernier tronçon inclus s'il fait 300 m et plus."""
    t = _ffill(streams.get("time") or [])
    d = _ffill(streams.get("distance") or [])
    n = min(len(t), len(d))
    if n < 2:
        return []
    hr = streams.get("heartrate") or []
    cad = streams.get("cadence") or []
    alt = streams.get("altitude") or []

    def stats(i_from, i_to):
        row = {}
        h = _mean(hr[i_from + 1:i_to + 1])
        c = _mean(cad[i_from + 1:i_to + 1])
        if h:
            row["hr"] = int(round(h))
        if c:
            row["cad"] = int(round(c))
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
            "cad": _num(iv.get("average_cadence"), 0), "d+": _num(iv.get("total_elevation_gain"), 0),
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
    laps, raw_intervals = [], []
    try:
        r = requests.get(f"{API_ROOT}/activity/{act_id}", auth=AUTH, params={"intervals": "true"}, timeout=12)
        if r.status_code == 200:
            got_any = True
            a = r.json()
            for src, dst in SUMMARY_KEYS.items():
                v = _num(a.get(src), 1)
                if v is not None:
                    detail[dst] = v
            gap = a.get("gap")
            if isinstance(gap, (int, float)) and gap > 0:
                detail["gap"] = _pace(gap)
            zt = a.get("icu_hr_zone_times")
            if isinstance(zt, list) and zt:
                detail["temps_zones_fc_s"] = [int(x) if isinstance(x, (int, float)) else 0 for x in zt]
            if a.get("description"):
                detail["notes"] = str(a["description"])[:300]
            raw_intervals = (a.get("icu_intervals") or [])[:MAX_LAPS]
            laps = _lap_rows(raw_intervals)
        else:
            logging.warning(f"Détail activité {act_id} : HTTP {r.status_code}")
        time.sleep(0.15)

        streams = get_streams(act_id)
        if streams:
            got_any = True
            km = compute_km_splits(streams)
            if km:
                detail["km"] = km
            if laps:
                minute_breakdown(streams, raw_intervals, laps)
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
        if i >= DETAIL_FULL:
            det = {k: v for k, v in det.items() if k != "km"}
        out.append(det)
    return out


def build_series_csv(streams, laps=None, full=False):
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

    step = 1 if full else max(1, STREAM_STEP_S)
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
        c = _mean([cad[i] for i in idxs if i < len(cad)])
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


# Quand joindre le CSV de la séance au prompt (sans appel Gemini supplémentaire)
_ANALYSE = re.compile(
    r"analys|détail|detail|fraction|\blaps?\b|\btours?\b|interval|\bbloc|bpm|\bfc\b|cardiaque|allure|rythme|dérive|derive|"
    r"régularit|regularit|seuil|tempo|\bcsv\b|seconde|\bbrut|respect|\bcap\b|sortie|course|footing|débrief|debrief|"
    r"\bvma\b|cadence|foulée|foulee", re.I)
_PLANIF = re.compile(r"planifi|programme|ajoute|supprime|déplace|deplace|décale|decale|annule|remplace|reporte|restaure|indispo", re.I)
_FULLRES = re.compile(r"seconde par seconde|\b1 ?hz\b|\bcsv\b|\bbrut|complet|toutes? les donn|tout le d[ée]tail", re.I)


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
    else:
        if not _ANALYSE.search(msg or "") or _PLANIF.search(msg or ""):
            return ""
        run, warn = pick_target_run(msg, runs)
    full = bool(_FULLRES.search(msg or ""))
    if not run:
        return f"SÉRIE DÉTAILLÉE : {warn}"
    try:
        streams = get_streams(str(run["id"]))
        laps = fetch_run_detail(run).get("laps") if streams else None
        res = build_series_csv(streams, laps, full) if streams else None
    except Exception:
        logging.exception("build_series_block")
        res = None
    if not res:
        return f"SÉRIE DÉTAILLÉE : indisponible pour '{run.get('nom')}' du {run.get('d')} (données seconde par seconde absentes ou illisibles)."
    csv_text, step, nrows = res
    return (f"SÉRIE DÉTAILLÉE (CSV de la séance '{run.get('nom')}' du {run.get('d')} : 1 ligne toutes les {step} s, {nrows} lignes ; "
            f"t_s = secondes depuis le départ, allure en m'ss/km (vide à l'arrêt), fc en bpm, cad = cadence, alt_m = altitude, "
            f"lap = numéro du lap du bloc DÉTAIL) :\n{csv_text}")


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
SYSTEM_INSTRUCTION = (
    "Tu es l'entraîneur personnel de course à pied de cet athlète. Tu réponds en français, de façon directe, "
    "précise et encourageante.\n"
    "RÈGLES :\n"
    "1. Les listes de santé et d'activités sont triées du plus récent au plus ancien. La première ligne de santé "
    "peut être incomplète (nuit du jour pas encore synchronisée) : ne conclus rien d'une valeur manquante.\n"
    "2. Alerte surcharge : suis la consigne du bloc SURVEILLANCE. Une alerte déjà communiquée n'est JAMAIS répétée, "
    "ni en cours de message ni en conclusion. Quand elle est active, tiens-en compte dans tes recommandations d'intensité "
    "sans la rappeler. Si le statut est INCONNU, ne dis pas que tout va bien.\n"
    "3. Pour modifier le calendrier, appelle l'outil (planifier_seance, deplacer_seance, supprimer_seance, "
    "gerer_indisponibilite, restaurer_suppression). Ne dis JAMAIS qu'une séance est planifiée, déplacée ou "
    "supprimée sans avoir appelé l'outil. N'appelle aucun outil quand l'athlète demande seulement un conseil "
    "ou une analyse.\n"
    "4. Donne toujours les dates aux outils au format AAAA-MM-JJ, en t'appuyant sur les repères de dates fournis.\n"
    "5. Format Telegram HTML : uniquement <b>, <i> et <code>. Aère avec des lignes vides. Aucun Markdown "
    "(pas de #, pas de **), aucune autre balise.\n"
    "6. Si l'athlète mentionne un fait durable (blessure, contrainte, préférence, déplacement), termine ton "
    "message par une ligne : [MEMOIRE] fait à retenir\n"
    "7. Tu n'es pas médecin : en cas de douleur persistante ou de symptôme inquiétant, recommande de consulter.\n"
    "8. Pour analyser une séance, utilise le bloc DÉTAIL DES DERNIÈRES COURSES : cite les chiffres lap par lap, minute par minute "
    "(par_min = allure/FC) et km par km, compare-les aux allures et durées prévues dans la description de la séance du CALENDRIER "
    "à la même date, puis conclus précisément (cible respectée ou non, régularité, dérive cardiaque). "
    "Ne dis jamais que tu n'as pas accès aux détails quand ces données sont présentes.\n"
    "9. Si un bloc SÉRIE DÉTAILLÉE est fourni, c'est le CSV de la séance : exploite-le point par point (la colonne lap renvoie "
    "aux laps du bloc DÉTAIL). S'il n'est pas fourni et que l'analyse demande ce niveau de finesse, invite l'athlète à écrire "
    "« analyse détaillée de [la séance ou la date] » (ajouter « csv complet » pour avoir la seconde par seconde).\n"
    "10. Sois concis : va droit au but, sans préambule ni formule de politesse, sans répéter ce que l'athlète sait déjà. "
    "Message courant : 80 mots maximum. Analyse de séance : 300 mots maximum, centrés sur les chiffres qui comptent."
)


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


def _build_config(allow_tools, deep):
    kwargs = dict(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=TOOLS if allow_tools else None,
        temperature=0.3,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    tc = _thinking_config(THINKING_DEEP if deep else THINKING_ROUTINE)
    if tc is not None:
        kwargs["thinking_config"] = tc
    return types.GenerateContentConfig(**kwargs)


def call_gemini(contents, allow_tools=True, deep=False):
    last = ""
    for attempt in range(3):
        cfg = _build_config(allow_tools, deep)
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


def generate_ai(contents, allow_tools=True, deep=False) -> str:
    """Un seul appel Gemini. Les outils sont executes une seule fois, hors de la boucle de retry."""
    r = call_gemini(contents, allow_tools=allow_tools, deep=deep)
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


def _table(rows, cols):
    """CSV compact : l'en-tête n'est écrit qu'une fois (un JSON répète chaque nom de champ à chaque ligne)."""
    lines = [",".join(h for h, _ in cols)]
    lines += [",".join(_cell(r.get(k)) for _, k in cols) for r in rows]
    return "\n".join(lines)


def _tblock(label, rows, cols):
    if not rows:
        return f"{label} : (indisponible ou vide)"
    return f"{label} :\n{_table(rows, cols)}"


WELL_COLS = [("d", "d"), ("sommeil_h", "sleep_h"), ("hrv", "hrv"), ("hrv_base", "hrv_base"), ("fc_repos", "rhr"),
             ("tsb", "tsb"), ("atl", "atl"), ("ctl", "ctl")]
ACT_COLS = [("d", "d"), ("type", "type"), ("nom", "nom"), ("km", "km"), ("min", "min"), ("allure", "pace"),
            ("fc", "hr"), ("charge", "load")]
WEEK_COLS = [("semaine", "semaine"), ("seances", "seances"), ("km", "km_total"), ("h", "temps_h"), ("charge", "charge_totale"),
             ("km_course", "km_course"), ("allure_course", "allure_course"), ("fc_course", "fc_course")]
TOP_COLS = [("allure", "allure"), ("km", "distance_km"), ("date", "date"), ("nom", "nom")]
EVT_COLS = [("d", "d"), ("h", "h"), ("cat", "category"), ("nom", "nom"), ("description", "desc")]
LAP_COLS = [("n", "n"), ("type", "type"), ("label", "label"), ("debut_s", "debut_s"), ("duree_s", "duree_s"), ("km", "km"),
            ("allure", "pace"), ("fc", "hr"), ("fc_max", "hr_max"), ("cad", "cad"), ("d+", "d+"), ("par_min", "par_min")]
KM_COLS = [("km", "km"), ("allure", "pace"), ("fc", "hr"), ("cad", "cad"), ("d_alt", "d_alt")]


def format_run_details(details):
    """Laps et splits km de chaque course, en petits tableaux CSV."""
    out = []
    for d in details or []:
        extras = [f"{k}={_cell(d[k])}" for k in ("fc_max", "cad", "d+", "decouplage_pct", "ef", "intensite", "trimp", "temp_c", "rpe", "feel", "gap")
                  if d.get(k) is not None]
        if d.get("temps_zones_fc_s"):
            extras.append("zones_fc_s=" + "/".join(str(x) for x in d["temps_zones_fc_s"]))
        if d.get("notes"):
            extras.append("notes=" + _cell(d["notes"]))
        part = [f"## {d.get('d')} {_cell(d.get('nom'))} " + " ".join(extras)]
        if d.get("laps"):
            part.append("laps\n" + _table(d["laps"], LAP_COLS))
        if d.get("km"):
            km_rows = [dict(r, km=(f"{r['km']}p" if r.get("partiel") else r["km"])) for r in d["km"]]
            part.append("km\n" + _table(km_rows, KM_COLS))
        out.append("\n".join(part))
    return "\n".join(out)


_ALERT_WORDS = re.compile(r"acwr|danger|surcharge|blessure", re.I)


def surveillance_info(well):
    """Risque + consigne : l'alerte est donnée une fois par jour, pas à chaque message."""
    risk = evaluate_injury_risk(well)
    if risk["statut"] != "DANGER":
        risk["consigne"] = "Aucune alerte active : n'en invente pas."
    elif kv_get("alert_ack") == f"{datetime.date.today().isoformat()}|DANGER":
        risk["consigne"] = ("ALERTE DÉJÀ COMMUNIQUÉE AUJOURD'HUI : ne la répète pas, ne la rappelle pas, ne conclus pas par un rappel. "
                            "N'y reviens que si l'athlète parle de douleur ou de fatigue, ou veut planifier ou intensifier une séance.")
    else:
        risk["consigne"] = "PREMIÈRE ALERTE DU JOUR : signale-la en 2 phrases maximum, puis réponds à la demande sans y revenir."
    return risk


def ack_alert(well, reply_text):
    """À appeler après une réponse réussie : marque l'alerte comme communiquée si la réponse l'a vraiment évoquée."""
    try:
        if evaluate_injury_risk(well)["statut"] == "DANGER" and _ALERT_WORDS.search(reply_text or ""):
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
    hist = "\n".join(f"{h['role']}: {h['text']}" for h in get_chat_history(4)) or "(vide)"
    parts = [
        _block("PROFIL ATHLÈTE", prof),
        _block("MÉMOIRE DURABLE", get_notes()),
        _tblock("SANTÉ, un jour par ligne, du plus récent au plus ancien (sommeil_h, hrv, hrv_base = référence HRV, fc_repos, tsb, atl, ctl)", daily, WELL_COLS),
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
        "",
        f"Objectif prioritaire : {GOAL_TEXT} le {GOAL_DATE.strftime('%d/%m/%Y')} ({goal_line}).",
        f"Aujourd'hui : {JOURS_FR[today.weekday()]} {today.isoformat()}.",
        f"Repères de dates : {reperes}.",
        _block("SURVEILLANCE SURCHARGE (ACWR) ET VRC", surveillance_info(well)),
        _block("MÉTÉO", weather),
        "HISTORIQUE RÉCENT DE CONVERSATION (du plus ancien au plus récent) :\n" + hist,
    ]
    if series_block:
        parts += ["", series_block]
    parts += ["", f'MESSAGE DE L\'ATHLÈTE :\n"{user_msg}"']
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
        prompt = await asyncio.to_thread(make_prompt, prof, weather, well, acts, evts, msg)
        save_chat_msg("athlete", msg)   # après make_prompt : le message courant n'est pas dans l'historique
        ans = await asyncio.to_thread(generate_ai, prompt, True, "SÉRIE DÉTAILLÉE (CSV" in prompt)
        ack_alert(well, ans)
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
        text_prompt = await asyncio.to_thread(
            make_prompt, prof, weather, well, acts, evts,
            "Message vocal de l'athlète (écoute l'audio joint et réponds-y).", False)
        save_chat_msg("athlete", "[Message vocal]")
        contents = [text_prompt, types.Part.from_bytes(data=buf.getvalue(), mime_type=mime)]
        ans = await asyncio.to_thread(generate_ai, contents)
        ack_alert(well, ans)
    except AIError as e:
        ans, save = str(e), False
    except Exception:
        logging.exception("handle_voice")
        ans, save = "⚠️ Erreur interne, réessaie dans un instant.", False
    await send_reply(cid, ans, save=save)


async def handle_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update) or not update.message:
        return
    await update.message.reply_text(usage_report())


_CONFLICT = {"first": 0.0, "last": 0.0, "n": 0, "alerted": 0.0}


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


def bg_init():
    try:
        _, _, well, acts, _ = get_all_data()
        for a in acts.get("brut_recent", []):
            mark_seen("seen_acts", str(a["id"]))
        if well and well[0].get("d"):
            mark_seen("seen_well", str(well[0]["d"]))
    except Exception:
        logging.exception("Erreur init bg_loop")


def bg_tick():
    prof, weather, well, acts, evts = get_all_data()
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
            ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=a), allow_tools=False, deep=True)
            _push("🏁 <b>Nouvelle séance détectée !</b>", ans, "Débrief auto")
            ack_alert(well, ans)
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
            ack_alert(well, ans)
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
                msg = ("C'est dimanche soir. Rédige le BILAN HEBDOMADAIRE complet : volume en km réalisé vs prévu, "
                       "charge, fatigue, et présente les 3 séances clés de la semaine à venir." + AUTO_SUFFIX)
                ans = generate_ai(make_prompt(prof, weather, well, acts, evts, msg, series=False), allow_tools=False, deep=True)
                _push("📊 <b>Bilan hebdomadaire du Coach</b>", ans, "Bilan Hebdo")
                ack_alert(well, ans)
            except Exception as e:
                release("seen_reports", week_id)
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
    if STARTUP_DELAY_S > 0:
        logging.info(f"Attente de {STARTUP_DELAY_S} s : l'ancienne instance doit s'arrêter avant le polling Telegram")
        time.sleep(STARTUP_DELAY_S)
    logging.info(f"Bot Coach Running démarré (modèle : {MODEL_NAME})")
    threading.Thread(target=bg_loop, daemon=True).start()
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CommandHandler("cout", handle_cost))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)
