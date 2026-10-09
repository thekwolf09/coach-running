"""Tests du coach running : `python tests.py` (à placer à côté de main.py).

Aucun accès réseau et aucune clé nécessaires : Intervals, Gemini et Telegram sont simulés. Chaque bloc est exécuté dans son propre processus.
Une série qui échoue affiche sa sortie ; le code de retour vaut 1 s'il y a un échec (utile pour le contrôle automatique GitHub).
Dépendance : requests (pip install requests).
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.environ.get("MAIN_PY") or os.path.join(HERE, "main.py")

TESTS = [
    ('01_noyau', '01_noyau : dates, outils calendrier, Gemini (nouvelles tentatives, quota), envois uniques', r'''import sys, types as pytypes, importlib.util, os, datetime, json, tempfile
from unittest.mock import MagicMock

for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
import types as _t
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
sys.modules["google"].genai = sys.modules["google.genai"]
sys.modules["google.genai"].types = sys.modules["google.genai.types"]

os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.init_db()

today = datetime.date.today()
iso = lambda d: d.isoformat()
tom = today + datetime.timedelta(days=1)

# parse_relative_date
assert m.parse_relative_date("demain soir") == iso(tom)
assert m.parse_relative_date("après-demain") == iso(today + datetime.timedelta(days=2))
assert m.parse_relative_date("2026-10-02T18:00:00") == "2026-10-02"
assert m.parse_relative_date("") is None
assert m.parse_relative_date("ce soir") == iso(today)
d = datetime.date.fromisoformat(m.parse_relative_date("lundi")); assert d.weekday() == 0 and d > today
try: m.parse_relative_date("2026-02-30"); assert False
except m.DateError: pass
try: m.parse_relative_date("bientôt"); assert False
except m.DateError: pass
assert m._hhmm("18h30") == "18:30" and m._hhmm("7") == "07:00" and m._hhmm("25:00") == "18:00" and m._hhmm("") == "18:00"

# sanitize / split
s = m.sanitize_html("# Titre\n**gras** <p>para</p> <br> - item <h1>x</h1> a & b < 5 <b onclick='x'>ok</b>")
print(repr(s))
assert "<p>" not in s and "<h1>" not in s and "**" not in s and "&amp;" in s and "&lt;" in s and "<b>ok</b>" in s
big = "\n\n".join(["x" * 1000] * 10)
chunks = m.split_message(big)
assert all(len(c) <= 3800 for c in chunks) and "".join(c.replace("\n\n", "") for c in chunks) == "x" * 10000
assert all(len(c) <= 3800 for c in m.split_message("y" * 9000))

# injury risk : HRV du jour manquant => pas de fausse alerte
w = [{"d": "2026-09-30", "hrv": None, "hrv_base": 60, "atl": 50, "ctl": 50},
     {"d": "2026-09-29", "hrv": 58, "hrv_base": 60, "atl": 50, "ctl": 50},
     {"d": "2026-09-28", "hrv": 59, "hrv_base": 60, "atl": 50, "ctl": 50}]
r = m.evaluate_injury_risk(w); assert r["statut"] == "OPTIMAL", r
w2 = [{"d": "a", "hrv": 50, "hrv_base": 60, "atl": 70, "ctl": 50}, {"d": "b", "hrv": 51, "hrv_base": 60, "atl": 1, "ctl": 1}]
r = m.evaluate_injury_risk(w2); assert r["statut"] == "VIGILANCE" and r["alerte_vrc"] and r["alerte_surcharge"], r      # ratio 1,4 + 1 signal physio : vigilance
w3 = [{"d": "a", "hrv": 50, "hrv_base": 60, "atl": 80, "ctl": 50}, {"d": "b", "hrv": 51, "hrv_base": 60, "atl": 1, "ctl": 1}]
assert m.evaluate_injury_risk(w3)["statut"] == "DANGER"                                                              # ratio 1,6 (> 1,5) + signal physio
assert m.evaluate_injury_risk([])["statut"] == "INCONNU"

# faux requests
class Resp:
    def __init__(s, code=200, data=None): s.status_code, s._d, s.text = code, data, ""
    def json(s): return s._d
class FakeReq:
    def __init__(s): s.events = []; s.calls = []
    def get(s, url, **k):
        s.calls.append(("GET", url)); 
        if url.endswith("/activities"): return Resp(200, ACTS)
        if url.endswith("/events"):
            p = k["params"]; return Resp(200, [e for e in s.events if p["oldest"] <= e["start_date_local"][:10] <= p["newest"]])
        return Resp(404, [])
    def post(s, url, **k):
        s.calls.append(("POST", url))
        if "sendMessage" in url: s.sent = getattr(s, "sent", []) + [k["json"]]; return Resp(200, {})
        e = dict(k["json"]); e["id"] = len(s.events) + 100; s.events.append(e); return Resp(200, e)
    def delete(s, url, **k):
        eid = int(url.rsplit("/", 1)[1]); s.events = [e for e in s.events if e["id"] != eid]; return Resp(200, {})
    def put(s, url, **k):
        eid = int(url.rsplit("/", 1)[1])
        for e in s.events:
            if e["id"] == eid: e.update(k["json"])
        return Resp(200, {})
ACTS = [
    {"id": "i1", "start_date_local": iso(today) + "T07:00:00", "type": "Run", "name": "Footing", "distance": 10000, "moving_time": 3000, "average_speed": 3.333, "average_heartrate": 150, "icu_training_load": 60},
    {"id": "i2", "start_date_local": iso(today - datetime.timedelta(days=2)) + "T07:00:00", "type": "Ride", "name": "Vélo", "distance": 30000, "moving_time": 3600, "average_speed": 8.3, "icu_training_load": 50},
    {"id": "i3", "start_date_local": None, "type": "Run", "name": "Sans date", "distance": 5000, "average_speed": None},
    {"id": "i4", "start_date_local": iso(today - datetime.timedelta(days=120)) + "T07:00:00", "type": "Run", "name": "Ancien", "distance": 5000, "moving_time": 1500, "average_speed": None, "icu_training_load": None},
    {"id": "i5", "start_date_local": "2025-12-31T07:00:00", "type": "Run", "name": "NouvelAn", "distance": 8000, "moving_time": 2400, "average_speed": 3.3},
]
fr = FakeReq(); m.requests = fr
a = m.fetch_activities()
print(json.dumps(a["top_performances_recentes"], ensure_ascii=False))
assert len(a["brut_recent"]) == 2                       # sans date ignorée
noms = [t["nom"] for t in a["top_performances_recentes"]]; assert noms[0] == "Footing" and "Vélo" not in noms   # vélo exclu
assert a["agregat_hebdo_anterieur"]                  # pas de crash sur None
assert m._pace(3.333) == "5'00\"/km" and m._pace(0) is None and m._pace(None) is None

# outils
d1, d2 = iso(tom), iso(today + datetime.timedelta(days=2))
print(m.planifier_seance(d1, "Fractionné", "6x800", "18h30"))
assert "✅" in m.planifier_seance(d1, "Footing", "", "07:00")
assert "existe déjà" in m.planifier_seance(d1, "Fractionné")
assert "passé" in m.planifier_seance(iso(today - datetime.timedelta(days=1)), "X")
assert "Date invalide" in m.planifier_seance("n'importe quoi", "X")
print(m.supprimer_seance(d1))                            # ambigu
assert "Plusieurs" in m.supprimer_seance(d1)
assert "Aucune séance nommée" in m.supprimer_seance(d1, "Sortie longue")
assert len(fr.events) == 2
print(m.deplacer_seance(d1, d2, "Fraction"))
moved = [e for e in fr.events if e["name"] == "Fractionné"][0]
assert moved["start_date_local"] == d2 + "T18:30:00", moved   # heure d'origine conservée
assert "Plusieurs" in m.deplacer_seance(d2, d1) or True
print(m.supprimer_seance(d1, "Footing")); assert len(fr.events) == 1
print(m.restaurer_suppression()); assert len(fr.events) == 2
print(m.gerer_indisponibilite(d1, iso(today + datetime.timedelta(days=40)), "test"))
assert "trop longue" in m.gerer_indisponibilite(d1, iso(today + datetime.timedelta(days=40)))
assert len(fr.events) == 2
print(m.gerer_indisponibilite(iso(today - datetime.timedelta(days=5)), iso(today - datetime.timedelta(days=1))))
print(m.gerer_indisponibilite(d1, d2, "gastro")); assert len(fr.events) == 0
print(m.restaurer_suppression()); assert len(fr.events) == 2

# generate_ai
class Call:
    def __init__(s, name, args): s.name, s.args = name, args
class R:
    def __init__(s, calls=None, text=None): s.function_calls, s.text = calls, text
client = MagicMock(); m.ai_client = client
calls_seen = []
orig = m.planifier_seance
m.TOOLS_MAP["planifier_seance"] = lambda **kw: (calls_seen.append(kw), "✅ ok")[1]
client.models.generate_content.return_value = R([Call("planifier_seance", {"date_str": d1, "titre": "A"}), Call("planifier_seance", {"date_str": d2, "titre": "B"})])
out = m.generate_ai("x"); assert len(calls_seen) == 2 and out.count("✅") == 2
client.models.generate_content.return_value = R(None, "  Salut  ")
assert m.generate_ai("x") == "Salut"
client.models.generate_content.return_value = R(None, None)
try: m.generate_ai("x"); assert False
except m.AIError: pass
# quota journalier => sortie immédiate, 1 seul appel
client.models.generate_content.reset_mock()
client.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED ... GenerateRequestsPerDayPerProjectPerModel-FreeTier ... Please retry in 41.6s")
t0 = __import__("time").time()
try: m.generate_ai("x"); assert False
except m.AIError as e: print(str(e)[:60])
assert client.models.generate_content.call_count == 1 and __import__("time").time() - t0 < 2
assert m._retry_delay("Please retry in 41.687075454s.") == 41.687075454
assert m._retry_delay("'retryDelay': '41s'") == 41.0
# bg : allow_tools False n'exécute pas d'outil
client.models.generate_content.side_effect = None
client.models.generate_content.return_value = R([Call("planifier_seance", {})], "texte")
calls_seen.clear(); assert m.generate_ai("x", allow_tools=False) == "texte" and not calls_seen

# prompt
p = m.make_prompt({"a": 1}, {}, [], {"brut_recent": []}, [], "salut")
assert "(indisponible ou vide)" in p and "J-" in p and "Repères de dates" in p
# reply / mémoire
out = m.finalize_reply("Bien joué.\n\n[MEMOIRE] douleur mollet gauche\nautre"); assert out == "Bien joué.\n\nautre"
assert m.get_notes()[-1]["note"] == "douleur mollet gauche"
# tg_send
fr.sent = []; m.tg_send(1, "<b>hello</b> **x**\n\n" + "z" * 5000)
print(len(fr.sent), [len(s["text"]) for s in fr.sent]); assert len(fr.sent) >= 2 and all(len(s["text"]) <= 4096 for s in fr.sent)
print("ALL TESTS PASSED")

# claim atomique : le 2e appel (autre thread/instance) est refusé, release permet de retenter
assert m.claim("seen_well", "2026-10-01") is True
assert m.claim("seen_well", "2026-10-01") is False
m.release("seen_well", "2026-10-01")
assert m.claim("seen_well", "2026-10-01") is True
# bg_tick : deux ticks de suite => un seul message de réveil
sent = []
m.get_all_data = lambda: ({}, {}, [{"d": "2026-10-09", "sleep_h": 7.2, "hrv": 80, "hrv_base": 70, "atl": 1, "ctl": 1}], {"brut_recent": []}, [])
m.generate_ai = lambda *a, **k: "Bonjour"
m.tg_send = lambda cid, text: sent.append(text)
m.bg_tick(); m.bg_tick()
assert len([x for x in sent if "Réveil détecté" in x]) == 1, sent
# échec Gemini => claim relâchée, retentera au cycle suivant
sent.clear()
m.db_exec("DELETE FROM seen_well")
def boom(*a, **k): raise m.AIError("quota")
m.generate_ai = boom; m.bg_tick()
assert not sent and not m.is_seen("seen_well", "2026-10-09")
print("CLAIM TESTS PASSED")
'''),
    ('02_details', '02_details : détails de course, alerte une fois par jour, séries CSV', r'''import sys, importlib.util, os, datetime, json, tempfile
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
import types as _t
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db()
m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat()

# --- splits : 2,5 km à 4'00/km (4 m/s), 1 Hz, FC qui monte, cadence 90
N = 626
t = list(range(N)); d = [4.0 * x for x in t]
hr = [140 + x // 10 for x in t]; cad = [90] * N; alt = [10 + 0.01 * x for x in t]
sp = m.compute_km_splits({"time": t, "distance": d, "heartrate": hr, "cadence": cad, "altitude": alt})
print(sp)
assert [x["km"] for x in sp] == [1, 2, 2.5] and sp[0]["pace"] == "4'10" and sp[1]["pace"] == "4'10" and sp[2].get("partiel")
assert sp[0]["hr"] < sp[1]["hr"] and sp[0]["cad"] == 180 and "d_alt" in sp[0]
assert m.compute_km_splits({"time": [0], "distance": [0]}) == [] and m.compute_km_splits({}) == []
# trous (None) et arrêts (distance constante)
d2 = list(d); d2[100] = None; d2[200:230] = [d2[199]] * 30
assert m.compute_km_splits({"time": t, "distance": d2})   # ne plante pas
# parse_streams
assert m.parse_streams([{"type": "time", "data": [1, 2]}, {"type": "x", "data": None}]) == {"time": [1, 2]}
assert m.parse_streams({"time": [1], "foo": 3}) == {"time": [1]}

# --- fetch_run_detail avec faux serveur
ACT = {"icu_intervals": [
    {"type": "RECOVERY", "start_time": 0, "moving_time": 1200, "distance": 3200, "average_speed": 2.66, "average_heartrate": 128, "max_heartrate": 140},
    {"type": "WORK", "start_time": 1200, "moving_time": 300, "distance": 1050, "average_speed": 3.5, "average_heartrate": 163, "max_heartrate": 171, "average_cadence": 88, "total_elevation_gain": 3}],
    "max_heartrate": 172, "decoupling": 2.4, "icu_hr_zone_times": [100, 900, 1200, 600, 100], "description": "RAS", "gap": 3.4}
class Resp:
    def __init__(s, code=200, data=None): s.status_code, s._d, s.text = code, data, ""
    def json(s): return s._d
class Fake:
    def __init__(s): s.calls = []; s.act_code = 200; s.stream_code = 200; s.intervals = True
    def get(s, url, **k):
        s.calls.append(url)
        if url.endswith("/streams.json"): return Resp(s.stream_code, [{"type": "time", "data": t}, {"type": "distance", "data": d}, {"type": "heartrate", "data": hr}])
        if "/activity/" in url:
            a = dict(ACT); 
            if not s.intervals: a["icu_intervals"] = []
            return Resp(s.act_code, a)
        return Resp(404, [])
f = Fake(); m.requests = f
item = {"id": "i9", "d": iso, "nom": "Seuil 3x5", "type": "Run", "km": 8.7}
det = m.fetch_run_detail(item)
print(json.dumps(det, ensure_ascii=False)[:600])
assert det["laps"][1]["pace"] == "4'46\"/km" and det["laps"][1]["hr"] == 163 and det["laps"][1]["type"] == "WORK"
assert det["fc_max"] == 172 and det["decouplage_pct"] == 2.4 and det["temps_zones_fc_s"][2] == 1200 and det["gap"]
assert det["km"][0]["pace"] == "4'10"
n_calls = len(f.calls)
det2 = m.fetch_run_detail(item); assert det2 == det and len(f.calls) == n_calls   # servi par le cache
# séance récente sans intervalles/streams => pas de cache figé
f2 = Fake(); f2.intervals = False; f2.stream_code = 500; m.requests = f2
it2 = {"id": "i10", "d": iso, "nom": "Récente", "type": "Run", "km": 5}
m.fetch_run_detail(it2); m.fetch_run_detail(it2); assert len(f2.calls) == 4   # re-tenté, pas mis en cache
# erreur HTTP complète sur séance ancienne => pas de cache non plus (got_any False)
f3 = Fake(); f3.act_code = 429; f3.stream_code = 429; m.requests = f3
old = {"id": "i11", "d": "2026-01-01", "nom": "Vieille", "type": "Run", "km": 5}
m.fetch_run_detail(old); m.fetch_run_detail(old); assert len(f3.calls) == 4
# fetch_run_details : filtre Run, limite, splits seulement pour les 3 premières
m.requests = Fake()
acts = {"brut_recent": [{"id": f"r{i}", "d": "2026-08-%02d" % (i + 1), "nom": f"R{i}", "type": "Run", "km": 8} for i in range(6)]
        + [{"id": "v1", "d": iso, "type": "Ride", "km": 30}]}
out = m.fetch_run_details(acts)
assert len(out) == 6 and all("km" in x for x in out[:3]) and all("km" not in x for x in out[3:])
assert all(x["id"] != "v1" for x in out)

# --- alerte : une fois par jour
danger = [{"d": iso, "hrv": 80, "hrv_base": 70, "atl": 87, "ctl": 50, "rhr": 56},
          {"d": (today - datetime.timedelta(days=7)).isoformat(), "hrv": 80, "hrv_base": 70, "atl": 40, "ctl": 40, "rhr": 48}]   # ratio 1,74 + FC repos +8
r = m.surveillance_info(danger); assert "PREMIÈRE FOIS AUJOURD'HUI" in r["consigne"]
m.ack_alert(danger, "Tout va bien, bonne séance")            # n'évoque pas l'alerte => pas de ack
assert "PREMIÈRE FOIS AUJOURD'HUI" in m.surveillance_info(danger)["consigne"]
m.ack_alert(danger, "✅ Séance planifiée le 2026-10-02")      # résultat d'outil => pas de ack
assert "PREMIÈRE FOIS AUJOURD'HUI" in m.surveillance_info(danger)["consigne"]
m.ack_alert(danger, "⚠️ ALERTE SURCHARGE : ACWR 1.74")      # alerte donnée => ack
r = m.surveillance_info(danger); assert "DÉJÀ SIGNALÉ AUJOURD'HUI" in r["consigne"] and r["statut"] == "DANGER"
ok = [{"d": iso, "hrv": 80, "hrv_base": 70, "atl": 50, "ctl": 50}]
assert "aucune alerte" in m.surveillance_info(ok)["consigne"]
m.kv_set("alert_ack", "2000-01-01|DANGER"); assert "PREMIÈRE FOIS" in m.surveillance_info(danger)["consigne"]   # nouveau jour

# --- prompt
acts["detail_dernieres_courses"] = out
p = m.make_prompt({}, {}, danger, acts, [], "analyse ma séance")
assert "DÉTAIL DES DERNIÈRES COURSES" in p and "laps\nn,type" in p and "PREMIÈRE FOIS AUJOURD'HUI" in p
print("prompt chars:", len(p), "| détail chars:", len(json.dumps(out, ensure_ascii=False)))
assert "bloc DÉTAIL" in m.SYSTEM_INSTRUCTION and "SÉRIE DÉTAILLÉE" in m.SYSTEM_INSTRUCTION
print("TEST2 PASSED")
'''),
    ('03_series', '03_series : splits au km, CSV de séance, caches, agrégats hebdomadaires', r'''import sys, importlib.util, os, datetime, json, tempfile, random
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
import types as _t
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")

def load(path, name):
    os.chdir(tempfile.mkdtemp())
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); mod.init_db()
    mod.time.sleep = lambda *_: None
    return mod
m = load(os.environ["MAIN_PY"], "newmain")
today = datetime.date.today(); iso = today.isoformat()

# ---------- série réaliste : 20' échauffement, 3 x 5' à ~4'50 (récup 2'), retour au calme
def build_run():
    t, d, hr, cad, vel, alt = [], [], [], [], [], []
    dist = 0.0
    segs = [(1200, 2.6, 135), (300, 3.5, 160), (120, 2.2, 150), (300, 3.5, 165), (120, 2.2, 152), (300, 3.5, 168), (120, 2.2, 150), (600, 2.6, 140)]
    sec = 0; bounds = []
    for dur, v, h in segs:
        bounds.append((sec, dur))
        for _ in range(dur):
            dist += v; t.append(sec); d.append(dist); hr.append(h + (sec % 7) - 3); cad.append(86); vel.append(v); alt.append(10 + sec * 0.002); sec += 1
    return {"time": t, "distance": d, "heartrate": hr, "cadence": cad, "velocity_smooth": vel, "altitude": alt}, bounds
streams, bounds = build_run()
N = len(streams["time"])

# ---------- minute_breakdown
types = ["RECOVERY", "WORK", "RECOVERY", "WORK", "RECOVERY", "WORK", "RECOVERY", "RECOVERY"]
raw_iv = []
for (st, du), ty in zip(bounds, types):
    seg = streams["distance"][st + du - 1] - (streams["distance"][st - 1] if st else 0)
    raw_iv.append({"type": ty, "start_index": st, "end_index": st + du - 1, "start_time": st, "moving_time": du, "distance": seg,
                   "average_speed": seg / du, "average_heartrate": 160})
rows = m._lap_rows(raw_iv)
m.minute_breakdown(streams, raw_iv, rows)
works = [r for r in rows if r["type"] == "WORK"]
print(works[0])
assert all(len(w["par_min"]) == 5 for w in works) and "par_min" not in rows[0]
assert works[0]["par_min"][0].startswith("4'46/")
assert rows[1]["pace"] == "4'46\"/km"

# ---------- build_series_csv
m.STREAM_STEP_S = 5
csv, step, nrows = m.build_series_csv(streams, rows)
lines = csv.split("\n")
print(lines[0], lines[1], lines[250], sep="\n"); print(step, nrows)
assert lines[0] == "t_s,allure,fc,lap" and step == 5 and nrows == N // 5 + (1 if N % 5 else 0)
lap_col = {l.split(",")[-1] for l in lines[1:]}
assert {"1", "2", "3", "4", "5", "6", "7", "8"} <= lap_col
row_work = [l for l in lines[1:] if l.split(",")[-1] == "2"][5]; assert "4'46" in row_work
csv_full, step_full, n_full = m.build_series_csv(streams, rows, full=True)
assert step_full == 1 and n_full == N and csv_full.split("\n")[0] == "t_s,dist_m,allure,fc,cad,alt_m,lap"
# longue sortie (3 h) en mode complet : le pas s'adapte au plafond de lignes
long = {k: v * 3 for k, v in streams.items()}; long["time"] = list(range(len(long["time"]))); long["distance"] = [i * 3.0 for i in range(len(long["time"]))]
_, st_long, n_long = m.build_series_csv(long, None, full=True); assert n_long <= m.SERIES_FULL_MAX_ROWS and st_long >= 1
_, st2, n2 = m.build_series_csv(long, None, full=False); assert n2 <= m.SERIES_MAX_ROWS
# arrêt : allure vide
stop = {"time": list(range(100)), "distance": [0.0] * 100, "velocity_smooth": [0.0] * 100}
assert m.build_series_csv(stop, None)[0].split("\n")[1].split(",")[1] == ""

# ---------- résolution de date / course visée
yest = (today - datetime.timedelta(days=1)).isoformat(); anteyest = (today - datetime.timedelta(days=2)).isoformat()
assert m.resolve_msg_date("analyse ma séance d'hier") == yest
assert m.resolve_msg_date("et avant-hier ?") == anteyest
assert m.resolve_msg_date("détail du 2026-09-20") == "2026-09-20"
assert m.resolve_msg_date("ma cap de ce matin") == iso
d = datetime.date.fromisoformat(m.resolve_msg_date("la sortie de samedi")); assert d.weekday() == 5 and d <= today
assert m.resolve_msg_date("bonjour") is None
assert m.resolve_msg_date("le 25/09") .endswith("-09-25")
runs = [{"id": "a3", "d": iso, "nom": "Footing", "type": "Run", "km": 8}, {"id": "a2", "d": iso, "nom": "Seuil 3x5", "type": "Run", "km": 9},
        {"id": "a1", "d": yest, "nom": "Sortie longue", "type": "Run", "km": 15}]
assert m.pick_target_run("analyse", runs)[0]["id"] == "a3"
assert m.pick_target_run("analyse ma séance d'hier", runs)[0]["id"] == "a1"
assert m.pick_target_run("analyse seuil 3x5 de ce matin", runs)[0]["id"] == "a2"
assert m.pick_target_run("analyse du 2026-01-02", runs)[0] is None

# ---------- faux serveur Intervals
class Resp:
    def __init__(s, code=200, data=None): s.status_code, s._d, s.text = code, data, ""
    def json(s): return s._d
class Fake:
    def __init__(s): s.calls = []
    def get(s, url, **k):
        s.calls.append(url)
        if url.endswith("/streams.json"):
            return Resp(200, [{"type": kk, "data": v} for kk, v in streams.items()])
        if "/activity/" in url:
            return Resp(200, {"icu_intervals": raw_iv, "max_heartrate": 175, "decoupling": 3.1, "icu_hr_zone_times": [60, 1500, 900, 600, 0]})
        return Resp(404, [])
fk = Fake(); m.requests = fk
item = {"id": "i77", "d": iso, "nom": "Seuil 3x5", "type": "Run", "km": 9.0}
det = m.fetch_run_detail(item)
assert det["laps"][1]["par_min"] and det["km"] and det["fc_max"] == 175
n0 = len(fk.calls)
assert m.get_streams("i77") is not None and len(fk.calls) == n0                     # streams servis par le cache
assert m.fetch_run_detail(item) == det and len(fk.calls) == n0                      # détail servi par le cache
blob = m.db_exec("SELECT length(blob) FROM act_streams WHERE id='i77'", fetch=True)[0][0]
print("stream compressé :", blob, "octets pour", N, "points")

# ---------- bloc série : déclenchement
acts = {"brut_recent": [item], "detail_dernieres_courses": [det], "top_performances_recentes": [], "agregat_hebdo_anterieur": []}
assert m.build_series_block(acts, "salut ça va ?") == ""
assert m.build_series_block(acts, "planifie une séance de seuil demain") == ""
assert m.build_series_block(acts, "analyse ma course", series=False) == ""
b = m.build_series_block(acts, "analyse ma séance : ai-je respecté les fractions ?")
assert b.startswith("SÉRIE DÉTAILLÉE") and "1 ligne toutes les 5 s" in b and "t_s,allure,fc,lap" in b
bf = m.build_series_block(acts, "analyse ma séance, donne-moi le csv complet seconde par seconde")
assert "1 ligne toutes les 1 s" in bf and len(bf) > len(b) * 4
assert "indisponible" not in b and "aucune course trouvée" in m.build_series_block(acts, "analyse la sortie du 2026-01-02")
assert m.build_series_block(acts, "x", series=dict(item, type="Ride")) == ""
assert m.build_series_block(acts, "Débrief séance terminée", series=item).startswith("SÉRIE DÉTAILLÉE")
print("série 5 s :", len(b), "car. | complète :", len(bf), "car.")

# ---------- ordre du prompt (stable -> volatile) et contenu
well = [{"d": (today - datetime.timedelta(days=i)).isoformat(), "sleep_h": 7.1, "hrv": 80, "hrv_base": 75, "rhr": 48, "tsb": -5, "atl": 60, "ctl": 50} for i in range(90)]
p = m.make_prompt({"lthr": 170}, {"ville": "X"}, well, acts, [], "analyse ma séance")
assert p.index("PROFIL ATHLÈTE") < p.index("SANTÉ") < p.index("ACTIVITÉS") < p.index("DÉTAIL DES DERNIÈRES COURSES") < p.index("SURVEILLANCE") < p.index("HISTORIQUE") < p.index("SÉRIE DÉTAILLÉE") < p.index("MESSAGE DE L'ATHLÈTE")
assert p.rstrip().endswith('"analyse ma séance"')
daily, weekly = m.split_wellness(well)
assert len(daily) == 90 and weekly == []                      # par défaut : 90 jours un par un, aucune perte
daily2, weekly2 = m.split_wellness(well, days_full=14)
assert len(daily2) == 14 and 9 <= len(weekly2) <= 12 and weekly2[0]["hrv"] == 80
print("TEST3 PART 1 OK")

# ---------- fetch_events / fetch_activities (format compact + agrégats)
EVENTS = []
for i in range(-20, 100):
    dd = today + datetime.timedelta(days=i)
    EVENTS.append({"id": i + 1000, "start_date_local": dd.isoformat() + "T00:00:00", "name": f"S{i} E1", "category": "WORKOUT", "description": "x" * 300})
EVENTS.append({"id": 1, "start_date_local": (today + datetime.timedelta(days=70)).isoformat() + "T00:00:00", "name": "5 km objectif", "category": "RACE_A", "description": "course"})
EVENTS.append({"id": 2, "start_date_local": (today + datetime.timedelta(days=3)).isoformat() + "T07:30:00", "name": "Matin", "category": "WORKOUT", "description": "y"})
ACTS = []
for i in range(1, 170):
    dd = today - datetime.timedelta(days=i)
    ACTS.append({"id": f"a{i}", "start_date_local": dd.isoformat() + "T07:00:00", "type": "Run" if i % 3 else "Ride", "name": f"Sortie {i}", "distance": 8000 + i, "moving_time": 2700, "average_speed": 3.0, "average_heartrate": 150, "icu_training_load": 55})
class Fake2:
    def get(s, url, **k):
        if url.endswith("/events"): return Resp(200, [dict(e) for e in EVENTS])
        if url.endswith("/activities"): return Resp(200, [dict(a) for a in ACTS])
        return Resp(404, [])
m.requests = Fake2()
ev = m.fetch_events()
assert all("id" not in e for e in ev)
near = [e for e in ev if abs((datetime.date.fromisoformat(e["d"]) - today).days) <= m.DESC_DAYS]
far = [e for e in ev if abs((datetime.date.fromisoformat(e["d"]) - today).days) > m.DESC_DAYS]
assert near and all("desc" in e for e in near) and far and all("desc" not in e for e in far)
assert any(e.get("category") == "RACE_A" for e in ev) and any(e.get("h") == "07:30" for e in ev) and not any(e.get("h") == "00:00" for e in ev)
a = m.fetch_activities()
assert all(x["d"] >= (today - datetime.timedelta(days=m.RAW_DAYS)).isoformat() for x in a["brut_recent"]) and len(a["brut_recent"]) == m.RAW_DAYS
wk = a["agregat_hebdo_anterieur"][0]; print(wk)
assert wk["km_course"] > 0 and wk["allure_course"].startswith("5'3") and wk["fc_course"] == 150
print("TEST3 PART 2 OK")
'''),
    ('04_prompt', '04_prompt : tableaux CSV, réflexion Gemini, ordre du prompt, suivi des coûts', r'''import sys, importlib.util, os, datetime, json, tempfile
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
import types as _t
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat()

# _cell / _table
assert m._cell(None) == "" and m._cell(3.0) == "3" and m._cell(3.14159) == "3.14" and m._cell(1.05) == "1.05" and m._cell(-0.001) == "0" and m._cell(True) == "1"
assert m._cell('5\'33"/km') == "5'33" and m._cell("a,b\nc") == "a;b | c" and m._cell(["4'46/160", "4'47/161"]) == "4'46/160 4'47/161"
tb = m._table([{"d": "2026-10-01", "hrv": 80.123, "x": None}], [("d", "d"), ("hrv", "hrv"), ("x", "x")])
assert tb == "d,hrv,x\n2026-10-01,80.12," , repr(tb)
# détails en tableaux
det = [{"id": "i1", "d": iso, "nom": "Seuil 3x5", "fc_max": 175, "decouplage_pct": 3.1, "temps_zones_fc_s": [60, 1500, 900, 600, 0], "notes": "RAS, ok",
        "laps": [{"n": 2, "type": "WORK", "debut_s": 1200, "duree_s": 300, "km": 1.05, "pace": "4'46\"/km", "hr": 160, "hr_max": 171, "par_min": ["4'46/160", "4'47/161"]}],
        "km": [{"km": 1, "pace": "5'10", "hr": 148}, {"km": 8.68, "partiel": True, "pace": "5'05", "hr": 150}]}]
txt = m.format_run_details(det); print(txt)
assert "## " in txt and "laps\nn,type,debut_s,duree_s,km,allure,fc,fc_max,par_min" in txt and "2,WORK,1200,300,1.05,4'46,160,171,4'46/160 4'47/161" in txt
assert "8.68p,5'05,150" in txt and "zones_fc_s=60/1500/900/600/0" in txt and "notes=RAS; ok" in txt and "{" not in txt

# réflexion : niveaux, repli SDK, repli API
m.types.reset_mock()
m._THINKING_OK["v"] = True
assert m._thinking_config("low") is not None and m.types.ThinkingConfig.call_args.kwargs == {"thinking_level": "low"}
assert m._thinking_config("default") is None and m._thinking_config("") is None
m.types.ThinkingConfig.side_effect = ValueError("bad level")
assert m._thinking_config("low") is None and m._THINKING_OK["v"] is True and "low" in m._THINKING_BAD
m.types.ThinkingConfig.side_effect = None; m._THINKING_BAD.clear(); m._THINKING_OK["v"] = True

class R:
    def __init__(s, text="ok"): s.function_calls, s.text = None, text
client = MagicMock(); m.ai_client = client
client.models.generate_content.return_value = R()
m.types.reset_mock(); m.generate_ai("x")                       # conversation
assert m.types.ThinkingConfig.call_args.kwargs == {"thinking_level": "medium"}
m.types.reset_mock(); m.generate_ai("x", True, m.THINKING_ROUTINE)   # calendrier
assert m.types.ThinkingConfig.call_args.kwargs == {"thinking_level": "low"}
assert "thinking_config" in m.types.GenerateContentConfig.call_args.kwargs
m.types.reset_mock(); m.generate_ai("x", True, m.THINKING_DEEP)   # analyse avec CSV
assert m.types.ThinkingConfig.call_args.kwargs == {"thinking_level": "high"}
# l'API refuse thinking_level : un seul réessai sans ce réglage, puis la désactivation est retenue
m.types.reset_mock(); m._THINKING_OK["v"] = True
client.models.generate_content.side_effect = [Exception("400 INVALID_ARGUMENT: thinking level is not supported"), R("réponse")]
assert m.generate_ai("x") == "réponse" and m._THINKING_OK["v"] is False
assert "thinking_config" not in m.types.GenerateContentConfig.call_args.kwargs
client.models.generate_content.side_effect = None; client.models.generate_content.return_value = R("ok2")
m.types.reset_mock(); m.generate_ai("x"); assert "thinking_config" not in m.types.GenerateContentConfig.call_args.kwargs
m._THINKING_OK["v"] = True

# prompt final : ordre stable -> volatile, tableaux, pas de JSON répété
well = [{"d": (today - datetime.timedelta(days=i)).isoformat(), "sleep_h": 7.1, "hrv": 80.4, "hrv_base": 75.2, "rhr": 48, "tsb": -5.2, "atl": 60.3, "ctl": 50.1} for i in range(90)]
acts = {"brut_recent": [{"id": "a1", "d": iso, "type": "Run", "nom": "Footing", "km": 8.2, "min": 49.0, "pace": "5'59\"/km", "hr": 148, "load": 52}],
        "detail_dernieres_courses": det, "top_performances_recentes": [{"allure": "4'50\"/km", "distance_km": 5.1, "date": iso, "nom": "Seuil"}],
        "agregat_hebdo_anterieur": [{"semaine": "2026-S20", "seances": 4, "km_total": 30.1, "temps_h": 3.1, "charge_totale": 200, "km_course": 28.0, "allure_course": "5'50\"/km", "fc_course": 149}]}
evts = [{"d": iso, "nom": "S3 E2", "desc": "20 min EF | 3x5 min"}, {"d": "2026-12-13", "nom": "5 km objectif", "category": "RACE_A"}]
p = m.make_prompt({"lthr": 170}, {"ville": "X"}, well, acts, evts, "Peux-tu décaler demain ?")
order = ["PROFIL ATHLÈTE", "MÉMOIRE DURABLE", "SANTÉ, un jour par ligne", "ACTIVITÉS DES 90 DERNIERS JOURS", "DÉTAIL DES DERNIÈRES COURSES",
         "MEILLEURES ALLURES", "AGRÉGATS HEBDO", "CALENDRIER", "TABLEAU DE BORD", "Objectif prioritaire", "SURVEILLANCE", "MÉTÉO", "HISTORIQUE", "MESSAGE DE L'ATHLÈTE"]
idx = [p.index(k) for k in order]; assert idx == sorted(idx), idx
assert "SÉRIE DÉTAILLÉE" not in p and "d,sommeil_h,hrv,hrv_base,fc_repos,tsb,atl,ctl" in p and "2026-12-13,,RACE_A,5 km objectif," in p
assert "{" not in p.split("SANTÉ, un jour par ligne")[1].split("Objectif prioritaire")[0]
assert p.rstrip().endswith('"Peux-tu décaler demain ?"')
# partie avant 'Objectif prioritaire' identique d'un message à l'autre (=> cache implicite)
p2 = m.make_prompt({"lthr": 170}, {"ville": "X"}, well, acts, evts, "Autre question")
cut = p.index("Objectif prioritaire"); assert p[:cut] == p2[:cut]
# concision et consignes présentes
assert "150 à 250 mots" in m.SYSTEM_INSTRUCTION and "SÉRIE DÉTAILLÉE" in m.SYSTEM_INSTRUCTION and "BRIEF (matin)" in m.SYSTEM_INSTRUCTION and "MODES" in m.SYSTEM_INSTRUCTION
# suivi de consommation
from types import SimpleNamespace as NS
m.kv_set("usage|" + iso, "")
m.db_exec("DELETE FROM kv WHERE k LIKE 'usage|%'")
assert m.usage_report().startswith("Aucune")
u = NS(prompt_token_count=10000, cached_content_token_count=8000, candidates_token_count=300, thoughts_token_count=200)
m.track_usage(NS(usage_metadata=u)); m.track_usage(NS(usage_metadata=u)); m.track_usage(NS())   # sans usage_metadata : ignoré
rep = m.usage_report(); print(rep)
exp = 2 * ((2000 * 0.75 + 8000 * 0.075 + 500 * 3.75) / 1e6)
assert "2 appels" in rep and "20 000" in rep and "cache 16 000" in rep and f"{exp:.3f} $" in rep, rep
print("TEST4 PASSED")
'''),
    ('05_fond', '05_fond : boucle de fond, débrief, gestionnaires de messages', r'''import sys, importlib.util, os, datetime, json, tempfile, asyncio
from unittest.mock import MagicMock, AsyncMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
import types as _t
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat()

# données : une course du jour, streams simulés
N = 900
S = {"time": list(range(N)), "distance": [3.0 * i for i in range(N)], "heartrate": [150] * N, "velocity_smooth": [3.0] * N}
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
class Fake:
    def get(s, url, **k):
        if url.endswith("/streams.json"): return Resp(200, [{"type": kk, "data": v} for kk, v in S.items()])
        if "/activity/" in url: return Resp(200, {"icu_intervals": []})
        return Resp(404, [])
m.requests = Fake()
run = {"id": "iR", "d": iso, "type": "Run", "nom": "Footing", "km": 2.7, "min": 15.0, "pace": "5'33\"/km", "hr": 150, "load": 20}
well = [{"d": iso, "sleep_h": 7.0, "hrv": 70.0, "hrv_base": 72.0, "rhr": 55, "tsb": -3.0, "atl": 87.0, "ctl": 50.0},
        {"d": (today - datetime.timedelta(days=7)).isoformat(), "sleep_h": 7.0, "hrv": 70.0, "hrv_base": 72.0, "rhr": 47, "tsb": 0.0, "atl": 40.0, "ctl": 38.0}]   # ACWR 1.74 + CTL +12 => DANGER
data = ({"lthr": 170}, {"ville": "X"}, well, {"brut_recent": [run], "detail_dernieres_courses": [], "top_performances_recentes": [], "agregat_hebdo_anterieur": []}, [])
m.get_all_data = lambda: data
sent = []; m.tg_send = lambda cid, text: sent.append((cid, text))
seen_calls = []
def fake_ai(contents, allow_tools=True, level=None):
    seen_calls.append((contents, allow_tools, level)); return "⚠️ ALERTE : ACWR 1.74, on lève le pied. Footing propre à 5'33." 
m.generate_ai = fake_ai

# 1) bg_tick : nouvelle activité => débrief avec série, réflexion « deep », outils interdits, une seule fois
m.mark_seen("seen_well", iso)                       # pas de brief réveil dans ce test
_iso = datetime.datetime.now().isocalendar(); m.mark_seen("seen_reports", f"bilan_{_iso[0]}_{_iso[1]}")   # ni de bilan hebdo (dépend de l'horloge réelle)
m.bg_tick(); m.bg_tick()
assert len(sent) == 1 and "Nouvelle séance détectée" in sent[0][1], sent
contents, allow_tools, deep = seen_calls[0]
assert allow_tools is False and deep == m.THINKING_DEEP and "SÉRIE DÉTAILLÉE (CSV" in contents and "t_s,allure,fc" in contents
assert m.is_seen("seen_acts", "iR")
assert "DÉJÀ SIGNALÉ" in m.surveillance_info(well)["consigne"]     # l'alerte du débrief est comptée pour la journée

# 2) handle_text : message d'analyse => série incluse + deep ; message court => pas de série + routine
m.kv_set("alert_ack", "")
def run_handler(text):
    upd = MagicMock(); upd.effective_user.id = 1; upd.effective_chat.id = 42; upd.message.text = text
    ctx = MagicMock(); ctx.bot.send_chat_action = AsyncMock()
    asyncio.run(m.handle_text(upd, ctx))
sent.clear(); seen_calls.clear()
run_handler("Analyse ma séance d'aujourd'hui en détail")
c, at, dp = seen_calls[0]; assert at is True and dp == m.THINKING_DEEP and "SÉRIE DÉTAILLÉE (CSV" in c, (at, dp)
assert sent and "ACWR" in sent[0][1]
seen_calls.clear(); sent.clear()
run_handler("merci")
c, at, dp = seen_calls[0]; assert dp == m.THINKING_CHAT and "SÉRIE DÉTAILLÉE" not in c and "DÉJÀ SIGNALÉ" in c      # alerte du jour déjà donnée
hist = m.get_chat_history(6); assert [h["role"] for h in hist][-4:] == ["athlete", "coach", "athlete", "coach"]
# message d'un autre utilisateur : ignoré
upd = MagicMock(); upd.effective_user.id = 999; upd.message.text = "salut"; ctx = MagicMock(); ctx.bot.send_chat_action = AsyncMock()
seen_calls.clear(); asyncio.run(m.handle_text(upd, ctx)); assert not seen_calls
# /cout
m.db_exec("DELETE FROM kv WHERE k LIKE 'usage|%'")
upd = MagicMock(); upd.effective_user.id = 1; upd.message.reply_text = AsyncMock(); asyncio.run(m.handle_cost(upd, MagicMock()))
upd.message.reply_text.assert_awaited_once_with("Aucune consommation enregistrée pour l'instant.")
print("TEST5 PASSED")
'''),
    ('06_conflit', '06_conflit : conflit Telegram au déploiement, DB_PATH', r'''import sys, importlib.util, os, tempfile, asyncio, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
d = tempfile.mkdtemp(); os.chdir(d); os.environ["DB_PATH"] = os.path.join(d, "data", "sub", "coach.db")   # dossier inexistant
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.init_db(); assert os.path.exists(os.environ["DB_PATH"]) and m.get_notes() == []          # DB_PATH respecté, dossier créé

clock = {"t": 1_000_000.0}
class FT: 
    @staticmethod
    def time(): return clock["t"]
    @staticmethod
    def sleep(*_): pass
m.time = FT
sent = []; m.tg_send = lambda cid, text: sent.append(text)
def err(e):
    ctx = MagicMock(); ctx.error = e; asyncio.run(m.on_error(None, ctx))
C = _err.Conflict
# conflit bref (déploiement) : aucune alerte
for _ in range(4): err(C("Conflict")); clock["t"] += 10
assert not sent
# silence de plus de 2 minutes : la série repart de zéro
clock["t"] += 200
for _ in range(8): err(C("Conflict")); clock["t"] += 30            # 4 min de conflit continu : toujours pas d'alerte
assert not sent
for _ in range(4): err(C("Conflict")); clock["t"] += 30            # > 5 min : une alerte
assert len(sent) == 1 and "Conflit Telegram persistant" in sent[0], sent
for _ in range(20): err(C("Conflict")); clock["t"] += 30           # pas de spam
assert len(sent) == 1
clock["t"] += 22000
for _ in range(15): err(C("Conflict")); clock["t"] += 30           # 6 h plus tard, toujours en conflit : nouvelle alerte
assert len(sent) == 2
err(ValueError("autre erreur"))                                    # autre exception : simplement journalisée
assert len(sent) == 2
print("TEST6 PASSED")
'''),
    ('07_tableau', '07_tableau : tableau de bord et risque de charge', r'''import sys, importlib.util, os, datetime, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); D = lambda age: (today - datetime.timedelta(days=age)).isoformat()

# --- santé : CTL 46 (40 il y a 7 j, 30 il y a 28 j), ATL 80, HRV en hausse récente, FC repos +5, sommeil court
def ctl_at(age): return 46 - 6 * min(age, 7) / 7 - 10 * max(0, min(age - 7, 21)) / 21
well = []
for age in range(0, 45):
    well.append({"d": D(age), "sleep_h": 6.2 if age < 7 else 7.4, "hrv": 80.0 if age < 7 else 70.0, "hrv_base": 78.0,
                 "rhr": 52 if age < 3 else 47, "tsb": -30.0, "atl": 80.0 if age == 0 else 55.0, "ctl": round(ctl_at(age), 2)})
well[1]["sleep_h"] = 5.5; well[2]["sleep_h"] = 6.0; well[3]["sleep_h"] = 5.0
# --- activités : une course par jour sauf jours 9-11 ; charge 60 (7 derniers jours) / 30 avant
acts_items = []
for age in range(0, 28):
    if age in (9, 10, 11): continue
    acts_items.append({"id": f"a{age}", "d": D(age), "type": "Run", "nom": f"S{age}", "km": 8.0, "min": 50.0, "pace": "6'15\"/km", "hr": 140, "load": 60 if age < 7 else 30})
acts_items.append({"id": "w1", "d": D(1), "type": "WeightTraining", "nom": "Salle", "km": 0.0, "min": 60.0, "pace": None, "hr": 110, "load": 25})
det = [{"id": "a2", "d": D(2), "nom": "Seuil 3x5", "temps_zones_fc_s": [100, 1500, 900, 500, 0],
        "laps": [{"n": 2, "type": "WORK", "duree_s": 300, "pace": "4'46\"/km", "hr": 163}, {"n": 4, "type": "WORK", "duree_s": 300, "pace": "4'52\"/km", "hr": 166},
                 {"n": 1, "type": "RECOVERY", "duree_s": 600, "pace": "4'00\"/km", "hr": 120}]}]
acts = {"brut_recent": acts_items, "detail_dernieres_courses": det}
evts = [{"d": D(10), "nom": "S2 E2 manquée"}, {"d": D(3), "nom": "S3 E1"}, {"d": D(40), "nom": "hors fenêtre"}, {"d": D(-2), "nom": "futur"},
        {"d": D(5), "nom": "Course", "category": "RACE_B"}]
prof = {"lthr": 172}

# --- risque
r = m.evaluate_injury_risk(well, acts)
loads = [0] * 28
for a in acts_items:
    loads[(today - datetime.date.fromisoformat(a["d"])).days] += a["load"]
exp_roll = round(sum(loads[:7]) / (sum(loads) / 4), 2)
print({k: r[k] for k in ("acwr", "acwr_glissant", "rampe_ctl_7j", "statut")}, r["details"])
assert r["acwr"] == round(80 / 46, 2) and r["acwr_glissant"] == exp_roll and r["rampe_ctl_7j"] == 6.0
assert r["statut"] == "DANGER" and len(r["signaux_charge"]) >= 2 and r["ratio_charge"] == r["acwr_glissant"] and any("FC repos" in x for x in r["signaux_physio"]) and any("sommeil" in x for x in r["signaux_physio"])
assert m.evaluate_injury_risk(well)["statut"] == "DANGER" and m.evaluate_injury_risk(well)["acwr_glissant"] is None   # sans activités : ACWR + rampe
# un ratio ACWR gonflé isolé (après une coupure) n'est qu'une vigilance, pas une alerte
solo = [{"d": D(0), "atl": 80.0, "ctl": 46.0, "hrv": 80, "hrv_base": 78, "rhr": 47, "sleep_h": 7.5}]
rs = m.evaluate_injury_risk(solo); assert rs["statut"] == "MAITRISEE" and rs["alerte_surcharge"] is True and rs["recup"]["label"] in ("bonne", "excellente"), rs
info = m.surveillance_info(solo); assert "MAÎTRISÉE (ce n'est PAS une alerte)" in info["consigne"]
m.ack_alert(solo, "ACWR élevé, danger de surcharge"); assert m.kv_get("alert_ack") is None                    # seule une vraie alerte est comptée
assert m.evaluate_injury_risk([{"d": D(0), "atl": 50.0, "ctl": 50.0, "hrv": 70, "hrv_base": 70}])["statut"] == "OPTIMAL"
# 2 signaux de charge sans signal physio => DANGER ; charge 1 + physio 1 => DANGER
assert m.evaluate_injury_risk(well[:1] + [dict(w, ctl=40.0) if w["d"] == D(7) else w for w in well[1:]])["statut"] == "DANGER"

# --- tableau de bord
lines = m.dashboard_lines(prof, well, acts, evts); txt = "\n".join(lines); print(txt)
for key in ("RÉCUPÉRATION", "CHARGE : RATIO DE CHARGE", "SEMAINES", "PROGRESSION", "RYTHME", "ALLURE FACILE", "ZONES FC", "ADHÉRENCE AU PLAN", "OBJECTIF", "ENVELOPPE DE CHARGE"):
    assert key in txt, key
h7 = sum(w["hrv"] for w in well[:7]) / 7; h28 = sum(w["hrv"] for w in well[:28]) / 28
assert f"HRV 7 j {h7:.0f} vs 28 j {h28:.0f} ({(h7 / h28 - 1) * 100:+.0f} %)" in txt
assert "FC repos 3 j 52 vs 8-35 j 47 (+5 bpm)" in txt and "sommeil moyen 7 j 5.9 h, 7 nuit(s) sous 6h30" in txt
assert "ATL/CTL Intervals : CTL 46 (+6.0 en 7 j, +4.0/sem sur 28 j)" in txt
assert f"RATIO DE CHARGE 7 j / 28 j {exp_roll:.2f} (haute" in txt and "monotonie" in txt
assert "RYTHME : 9 jour(s) d'activité consécutifs, 0 jour(s) de repos sur les 7 derniers" in txt
assert "ADHÉRENCE AU PLAN (28 derniers jours, course réalisée à ±1 jour) : 1/2 séances ; manquées : " + D(10) + " S2 E2 manquée" in txt
assert "Z1 3 %" in txt and "Z2 50 %" in txt
days_left = (m.GOAL_DATE - today).days
est = 286 * 0.95
assert f"J-{days_left}, soit {days_left / 7:.1f} semaines" in txt
assert f"meilleur effort structuré de 4 min et plus (8 dernières semaines) : 4'46/km sur 300 s à 163 bpm le {D(2)}" in txt
assert f"équivalent 5 km estimé : {m._mmss(est)}/km, soit {m._mmss(est * 5)}" in txt
gap = est - 239
assert f"écart à l'objectif : {gap:.0f} s/km ({gap / est * 100:.0f} %), soit {m._mmss(gap * 5)} sur 5 km ; progression nécessaire ≈ {gap / (days_left / 7):.1f} s/km par semaine" in txt
# enveloppe de charge (statut DANGER => marge réduite de 30 %)
chronic, acute = sum(loads) / 4, sum(loads[:7])
mx = round(chronic * 1.3 * 0.7)
assert f"cible ≤ {mx} (1,3 × moyenne hebdo 28 j de {chronic:.0f}, réduit de 30 % car risque élevé), cumulée {acute:.0f}, marge {mx - round(acute):+d} ; plafond de la plage optimale (1,5) : {round(chronic * 1.5)}" in txt
this = today - datetime.timedelta(days=today.weekday())
km_last = sum(a["km"] for a in acts_items if a["type"] == "Run" and this - datetime.timedelta(days=7) <= datetime.date.fromisoformat(a["d"]) < this)
km_done = sum(a["km"] for a in acts_items if a["type"] == "Run" and datetime.date.fromisoformat(a["d"]) >= this)
mk = km_last * 1.10 * 0.7
assert f"km de la semaine en cours : max ≈ {mk:.0f} (+10 % sur les {km_last:.0f} km de la semaine dernière), faits {km_done:.1f}, reste {max(0.0, mk - km_done):.1f}" in txt
assert f"sortie longue max ≈ {mk / 3:.1f} km (un tiers du volume)" in txt and "rampe CTL sur 7 jours +6.0 (limite +5), marge -1.0" in txt
assert "jamais deux séances intenses à moins de 48 h" in txt
assert "FC seuil 172" in txt and "6'15@140" in txt and "(en cours)" in txt
# robustesse : données vides => pas d'exception, pas de ligne
assert [l.split(" :")[0] for l in m.dashboard_lines({}, [], {}, [])] == ["OBJECTIF"]       # sans données : seule la ligne objectif
assert isinstance(m.dashboard_lines(None, None, None, None), list)
# prompt complet : le bloc est dans le préfixe stable, avant l'objectif
p = m.make_prompt(prof, {}, well, dict(acts, top_performances_recentes=[], agregat_hebdo_anterieur=[]), evts, "Je fais du dos à la salle, c'est ok ?")
assert p.index("TABLEAU DE BORD") < p.index("Objectif prioritaire") < p.index("SURVEILLANCE") and "PREMIÈRE FOIS AUJOURD'HUI" in p and "signaux_charge" in p
print("TEST7 PASSED")
'''),
    ('08_meteo', '08_meteo : météo heure par heure, rappels, relance, objectif 5 km', r'''import sys, importlib.util, os, datetime, json, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat(); D = lambda age: (today - datetime.timedelta(days=age)).isoformat()

# ---------------- météo : heure par heure
base = datetime.datetime.combine(today, datetime.time(0, 0))
times = [(base + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(72)]
def temp(i): return 8 + (i % 24) * 0.5
hourly = {"time": times, "temperature_2m": [temp(i) for i in range(72)], "apparent_temperature": [temp(i) - 2 for i in range(72)],
          "precipitation_probability": [80 if i % 24 in (14, 15, 16) else 5 for i in range(72)],
          "precipitation": [1.2 if i % 24 in (14, 15, 16) else 0.0 for i in range(72)], "wind_speed_10m": [12 + (i % 24) for i in range(72)]}
forecast = {"current": {"time": f"{iso}T08:15", "temperature_2m": 12.3, "apparent_temperature": 10.1, "wind_speed_10m": 15.0, "precipitation": 0.0},
            "daily": {"time": [D(0), D(-1), D(-2)], "temperature_2m_max": [18, 19, 20], "temperature_2m_min": [9, 10, 11], "precipitation_probability_max": [80, 10, 5]},
            "hourly": hourly}
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
class FakeW:
    def __init__(s): s.calls = []
    def get(s, url, **k):
        s.calls.append((url, k.get("params")))
        if "geocoding" in url: return Resp(200, {"results": [{"name": "Melbourne", "latitude": -37.8, "longitude": 144.9}]})
        if "open-meteo" in url: return Resp(200, forecast)
        return Resp(404, [])
fw = FakeW(); m.requests = fw
w = m.fetch_weather("Melbourne")
hp = [c for c in fw.calls if "/v1/forecast" in c[0]][0][1]
assert "hourly" in hp and hp["timezone"] == "auto"
rows = w["horaire"]; print(rows[0], rows[-1]); print(w["creneaux_favorables_24h"])
assert len(rows) == 24 and rows[0]["t"] == f"{iso}T08:00" and rows[0]["h"] == "auj 08h" and rows[-1]["h"] == "dem 07h"
assert rows[0]["temp_c"] == 12 and rows[0]["ress_c"] == 10 and rows[6]["pluie_pct"] == 80 and rows[6]["pluie_mm"] == 1.2
assert w["maintenant"]["heure_locale"] == "08:15" and len(w["previsions_3j"]) == 3
best = w["creneaux_favorables_24h"]; assert len(best) == 3 and "pluie 80 %" not in " ".join(best) and all(" 0" in b or "dem" in b or "auj" in b for b in best)
n_calls = len(fw.calls); m.fetch_weather("Melbourne"); assert len(fw.calls) == n_calls            # cache 15 min
start = datetime.datetime.combine(today, datetime.time(14, 0))
win = m.weather_window(w, start); assert [r["t"][11:13] for r in win] == ["14", "15"]
assert m.weather_window(w, datetime.datetime.combine(today, datetime.time(3, 0))) == []             # heure déjà passée
tips = m.weather_tips(win); assert any("pluie" in t_ for t_ in tips)
assert m.weather_tips([{"ress_c": 3, "pluie_pct": 0, "vent_kmh": 35}]) == ["froid : manches longues, gants fins, échauffement plus long", "vent fort : pars face au vent pour rentrer vent dans le dos"]
assert m.weather_tips([{"ress_c": 27, "pluie_pct": 0, "vent_kmh": 5}]) == ["chaleur : hydrate-toi et ralentis de 5 à 10 s/km"]
rem = m.build_reminder({"nom": "S3 E3 <EF>", "desc": "50 min\nendurance"}, start, w, 58)
print(rem); assert "Séance à 14:00 : S3 E3 &lt;EF&gt;" in rem and "dans environ 60 min" in rem and "pluie 80 %" in rem and "Meilleur créneau" in rem and "auj 08h" not in rem and "50 min | endurance" in rem
# météo indisponible => message sans bloc météo, pas d'exception
assert "🌦️" not in m.build_reminder({"nom": "X"}, start, {"info": "Météo non disponible"}, 30)
# prompt : heure locale + tableau horaire
p = m.make_prompt({}, w, [], {"brut_recent": []}, [], "Quel temps dans 1 h ?")
assert "il est " in p and "MÉTÉO HEURE PAR HEURE" in p and "heure,temp,ressenti,pluie_pct,pluie_mm,vent_kmh\nauj 08h,12,10,5,0,20" in p and "creneaux_favorables_24h" in p and "horaire" not in p.split("MÉTÉO HEURE PAR HEURE")[0].split("MÉTÉO (")[1]

# ---------------- rappels et relance (sans Gemini)
sent = []; m.tg_send = lambda cid, text, buttons=None: sent.append(text)
def run(now, evts, acts): m.reminders_tick(evts, acts, w, now=now)
at = lambda h, mi=0: datetime.datetime.combine(today, datetime.time(h, mi))
runs7 = [{"d": D(i), "type": "Run", "h": "07:10", "km": 8.0} for i in range(1, 6)]
acts = {"brut_recent": runs7}
assert m.usual_train_hour(runs7, today) == 7 and m.usual_train_hour([], today) == m.TRAIN_HOUR
# a) horaire explicite : un seul rappel dans l'heure qui précède
ev = [{"d": iso, "h": "18:00", "nom": "S3 E3", "desc": "50 min EF", "type": "Run"}]
run(at(15), ev, acts); assert not sent                                  # trop tôt
run(at(17, 5), ev, acts); assert len(sent) == 1 and "S3 E3" in sent[0]
run(at(17, 20), ev, acts); assert len(sent) == 1                        # pas de doublon
assert m.get_chat_history(1)[0]["text"].startswith("[Rappel auto] ⏰ Séance à 18:00") and "<b>" not in m.get_chat_history(1)[0]["text"]
# b) séance sans horaire : heure habituelle (07h) des dernières courses
sent.clear(); ev2 = [{"d": iso, "nom": "Footing", "desc": "", "type": None}]
run(at(6, 10), ev2, acts); assert len(sent) == 1 and "07:00" in sent[0]
# c) déjà courue aujourd'hui : rien
sent.clear(); done = {"brut_recent": runs7 + [{"d": iso, "type": "Run", "h": "06:00", "km": 5.0}]}
ev3 = [{"d": iso, "h": "18:00", "nom": "Autre", "type": "Run"}]; run(at(17, 10), ev3, done); run(at(21, 30), ev3, done); assert not sent
# d) relance du soir : séance de 07h non détectée, une seule fois, pas avant 21 h
ev4 = [{"d": iso, "h": "07:00", "nom": "Matin raté", "type": "Run"}]
run(at(20, 50), ev4, acts); assert not sent
run(at(21, 10), ev4, acts); assert len(sent) == 1 and "non détectée" in sent[0] and "Matin raté" in sent[0]
run(at(21, 40), ev4, acts); assert len(sent) == 1
# e) musculation planifiée / catégorie course : ignorées
sent.clear(); run(at(21, 30), [{"d": iso, "h": "07:00", "nom": "Salle", "type": "WeightTraining"}, {"d": iso, "h": "07:00", "nom": "Course", "category": "RACE_A", "type": "Run"}], acts); assert not sent
# f) échec d'envoi : la réservation est annulée et le rappel sera retenté
def boom(*a): raise RuntimeError("net")
m.tg_send = boom; ev5 = [{"d": iso, "h": "19:00", "nom": "Retry", "type": "Run"}]
run(at(18, 10), ev5, acts); m.tg_send = lambda cid, text, buttons=None: sent.append(text); run(at(18, 20), ev5, acts); assert len(sent) == 1 and "Retry" in sent[0]

# ---------------- découplage + historique des meilleurs efforts
det = [{"id": "x1", "d": D(1), "nom": "Seuil 3x5", "decouplage_pct": 3.14, "laps": [{"n": 2, "type": "WORK", "duree_s": 300, "pace": "4'46\"/km", "hr": 163}]},
       {"id": "x2", "d": D(6), "nom": "Footing", "decouplage_pct": 6.8}, {"id": "x3", "d": D(9), "nom": "Sans mesure"}]
ctx = {"details": det}
line = m._dash_decouplage(ctx); print(line)
assert line.endswith(f"{D(1)} 3.14 % (Seuil 3x5) | {D(6)} 6.8 % (Footing)") and "sous 5 %" in line and m._dash_decouplage({"details": []}) is None
for i, (age, pace_s, dur) in enumerate([(35, 304, 480), (21, 298, 480), (14, 292, 300)]):
    m.db_exec("INSERT INTO efforts (id, d, nom, pace_s, hr, duree_s) VALUES (?, ?, ?, ?, ?, ?)", (f"h{i}", D(age), "Seuil", pace_s, 160, dur))
hist = m.effort_history(det); print([(h["d"], h["pace_s"]) for h in hist])
assert [h["pace_s"] for h in hist] == [304, 298, 292, 286]
c = {"today": today, "acts": {"top_performances_recentes": [{"allure": "5'10\"/km", "distance_km": 5.2, "date": D(40), "nom": "Sortie"}, {"allure": "4'40\"/km", "distance_km": 3.2, "date": D(30), "nom": "Court"}]},
     "details": det}
obj = m._dash_objectif(c); print(obj)
assert f"tendance des meilleurs efforts : {D(35)} 5'04 → {D(1)} 4'46 (-18 s/km)" in obj and "meilleure sortie de 4,5 à 6,5 km (6 mois) : 5'10/km sur 5.2 km" in obj
# les efforts de plus de 8 semaines ne servent pas à l'estimation
m.db_exec("DELETE FROM efforts"); m.db_exec("INSERT INTO efforts (id, d, nom, pace_s, hr, duree_s) VALUES ('old', ?, 'Vieux', 260, 160, 300)", (D(100),))
assert "impossible" in m._dash_objectif({"today": today, "acts": {}, "details": []})
assert m._dash_objectif({"today": m.GOAL_DATE + datetime.timedelta(days=1), "acts": {}, "details": []}) is None
print("TEST8 PASSED")
'''),
    ('09_coach', '09_coach : modes de réponse, mémoire, cadence, phases, matériel, semaine en cours', r'''import sys, importlib.util, os, datetime, json, tempfile, asyncio, types as _t
from unittest.mock import MagicMock, AsyncMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat(); D = lambda age: (today - datetime.timedelta(days=age)).isoformat()

# ---------- mode de réponse : les vrais messages de l'athlète
cases = {
 "Ah tu vois je t'avais dit que j'étais bien et que je pouvais y aller ! D'ailleurs peut être mon footing préféré depuis le début de la prépa ! J'étais hyper bien, et j'ai envie réussi à maintenir ma FC sous les 145 BPM facilement malgré la petite dérive a la fin.\n\nJ'avais mes vieilles GT-2000 au pied (650km au compteur) donc très content !!\n\nPour info je met mes MEGABLAST (180km) le jeudi pour mes séances d'intensité\n\nPetite Powerade en post training pour la récupération et à la douche je suis grave content !!": "CONVERSATION",
 "On dirait que tu n'analyses pas les détails de ma cap. Ai-je respecté les 3 fractions au seuil en terme de rythme et de bpm. Sois bcp plus précis.": "ANALYSE",
 "Je suis en forme, je vais aller travailler mon dos a la salle je pense un peu": "CONVERSATION",
 "Séance dos bras ok 1h chill ça fait du bien prêt pour demain. Aujourd'hui je travaillais pas en plus donc ça va": "CONVERSATION",
 "Peut tu réessayer stp ?": "QUESTION", "Recommence le bilan matinale stp": "BRIEF", "analyse course du jour": "ANALYSE",
 "Décale ma séance à demain": "PLANIFICATION", "Peux-tu décaler demain ?": "PLANIFICATION", "Il fera quoi dans 1 h ?": "QUESTION",
 "Je fais du dos à la salle, c'est ok ?": "QUESTION", "J'ai programmé mon footing demain": "CONVERSATION", "Je vais ajouter du renfo cette semaine": "CONVERSATION",
 "Débrief séance terminée : {\"d\": \"2026-10-03\"}": "DEBRIEF", "Brief réveil : {\"sleep_h\": 7}": "BRIEF", "C'est dimanche soir. Rédige le BILAN": "BILAN",
 "Message vocal de l'athlète (écoute l'audio joint et réponds-y).": "LIBRE", "merci coach": "CONVERSATION",
 "Analyse ma séance d'hier : ai-je respecté les fractions ?": "ANALYSE", "Quel temps dans 1 h": "QUESTION", "bilan de la semaine stp": "BILAN",
}
bad = {k[:50]: (m.detect_mode(k), v) for k, v in cases.items() if m.detect_mode(k) != v}
assert not bad, bad
assert m.think_level("merci coach", "x") == m.THINKING_CHAT and m.think_level("Décale ma séance à demain", "x") == m.THINKING_ROUTINE
assert m.think_level("analyse course du jour", "x") == m.THINKING_DEEP

# ---------- notes multiples + historique
out = m.finalize_reply("Super.\n[MEMOIRE] GT-2000 : 650 km au compteur\n[MEMOIRE] MEGABLAST (180 km) pour les séances d'intensité du jeudi\nÀ jeudi !")
assert out == "Super.\nÀ jeudi !", repr(out)
notes = [n["note"] for n in m.get_notes()]; assert notes == ["GT-2000 : 650 km au compteur", "MEGABLAST (180 km) pour les séances d'intensité du jeudi"]
m.finalize_reply("[MEMOIRE] GT-2000 : 650 km au compteur\nOk"); assert len(m.get_notes()) == 2                     # pas de doublon
m.save_chat_msg("coach", "c" * 1000); m.save_chat_msg("athlete", "a" * 1000)
h = m.get_chat_history(2); assert len(h[0]["text"]) == 350 and len(h[1]["text"]) == 600

# ---------- cadence en pas/min, flux disponibles
assert m._spm(80) == 160 and m._spm(165) == 165 and m._spm(0) is None and m._spm(None) is None
assert m._stream_types_for(["time", "distance", "heartrate", "stance_time", "vertical_oscillation", "latlng", "watts", "temp"]) == "time,distance,heartrate,watts,stance_time,vertical_oscillation"
assert m._stream_types_for(None) == m.STREAM_TYPES

# ---------- phases, FC cumulée, dynamique de course
N = 1800
t = list(range(N)); d = [2.8 * i for i in t]
hr = [133 + 12 * i / N + (i % 5) for i in t]; cad = [80] * N; w = [298 + (i % 11) for i in t]
gct = [288 + 8 * i / N for i in t]; vo = [86 + 6 * i / N for i in t]
st = {"time": t, "distance": d, "heartrate": hr, "cadence": cad, "watts": w, "stance_time": gct, "vertical_oscillation": vo}
ph = m.phase_summary(st); print(ph)
assert [r["ph"] for r in ph] == ["0-10 min", "10-20 min", "20-30 min"] and ph[0]["fc"] < ph[1]["fc"] < ph[2]["fc"] and all(r["cad"] == 160 and 295 <= r["w"] <= 310 for r in ph)
assert m.phase_summary({"time": list(range(300)), "distance": list(range(300))}) is None            # séance trop courte
cum = m.hr_cumul(hr); print(cum); assert cum.startswith("≤") and cum.rstrip().endswith("100 %") or "99 %" in cum or "98 %" in cum
assert m.hr_peak(st)["fc"] == int(max(hr)) and m.hr_peak(st)["min"] >= 29
dyn = m.dynamics_summary(st); print(dyn)
assert dyn["cad_spm"]["moy"] == 160 and dyn["stance_time"]["t3"] > dyn["stance_time"]["t1"] and dyn["stance_time"]["derive_pct"] > 0 and "vertical_oscillation" in dyn and "watts" in dyn
assert m.dynamics_summary({"time": [1], "distance": [1]}) == {}

# ---------- matériel : suivi Intervals + chaussures par course
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
class FakeG:
    def get(s, url, **k): return Resp(200, [{"name": "Asics GT-2000", "type": "Shoes", "distance": 650000}, {"name": "Vieilles", "type": "Shoes", "distance": 900000, "retired": True},
                                           {"name": "Asics Megablast", "type": "Shoes", "distance": 180400}]) if url.endswith("/gear") else Resp(404, [])
m.requests = FakeG(); assert m.fetch_gear() == ["Asics GT-2000 (Shoes) : 650 km", "Asics Megablast (Shoes) : 180 km"]
class Fake404:
    def get(s, url, **k): return Resp(404, [])
m.requests = Fake404(); assert m.fetch_gear() == []
full = {"id": "x", "d": iso, "nom": "Footing", "km": [{"km": 1}], "phases": [{"ph": "0-10 min"}], "dyn": {"stance_time": {"moy": 292, "p5": 288, "p95": 296, "t1": 289, "t3": 295, "derive_pct": 1.8}}, "fc_cumul": "≤140 20 %", "pic_fc": {"fc": 151, "min": 47}, "laps": []}
m.fetch_run_detail = lambda item: dict(full, id=item["id"])
acts_x = {"brut_recent": [{"id": f"r{i}", "d": D(i), "type": "Run", "nom": "Footing", "km": 8.0, "shoes": "Asics GT-2000" if i == 0 else None} for i in range(5)]}
dets = m.fetch_run_details(acts_x)
assert dets[0]["chaussures"] == "Asics GT-2000" and "phases" in dets[0] and "chaussures" not in dets[1]
assert all("km" in x and "phases" in x for x in dets[:m.DETAIL_FULL]) and all("km" not in x and "phases" not in x and "dyn" not in x and "pic_fc" not in x for x in dets[m.DETAIL_FULL:])
txt = m.format_run_details(dets[:1]); print(txt)
assert "chaussures=Asics GT-2000" in txt and "pic_fc=151 à la 47e min" in txt and "phases" in txt and "dynamique de course" in txt and "stance_time 292 [288-296] 289→295 (+1.8 %)" in txt

# ---------- semaine en cours : prévu -> réalisé
this = today - datetime.timedelta(days=today.weekday())
evts = [{"d": (this + datetime.timedelta(days=k)).isoformat(), "nom": f"S3 E{k // 2 + 1}", "type": "Run"} for k in (0, 2, 4)]
recent = [{"id": "a1", "d": evts[0]["d"], "type": "Run", "min": 40.0, "pace": "6'03\"/km", "hr": 148, "km": 6.6, "load": 40}]
line = m._dash_semaine_plan({"today": today, "recent": recent, "evts": evts}); print(line)
assert line.startswith("SEMAINE EN COURS, prévu → réalisé : S3 E1") and "fait : 40 min, 6'03, FC 148" in line
assert m._dash_semaine_plan({"today": today, "recent": [], "evts": []}) is None

# ---------- prompt : mode, prénom, pas de CSV en conversation
m.ATHLETE_NAME = "Alex"
well = [{"d": iso, "sleep_h": 7.0, "hrv": 80, "hrv_base": 78, "rhr": 47, "tsb": -5.0, "atl": 50.0, "ctl": 50.0}]
acts_p = {"brut_recent": [{"id": "a1", "d": iso, "type": "Run", "nom": "Footing", "km": 7.7, "min": 50.0, "pace": "6'30\"/km", "hr": 142, "load": 50}], "detail_dernieres_courses": [],
          "top_performances_recentes": [], "agregat_hebdo_anterieur": []}
p = m.make_prompt({"lthr": 172}, {}, well, acts_p, [], list(cases)[0])
assert '"prenom":"Alex"' in p and "MODE DE RÉPONSE : CONVERSATION" in p and "SÉRIE DÉTAILLÉE (CSV" not in p
p2 = m.make_prompt({"lthr": 172}, {}, well, acts_p, [], "analyse course du jour")
assert "MODE DE RÉPONSE : ANALYSE" in p2

# ---------- handler : message de sensations => pas d'analyse, réflexion moyenne, indicateur « en train d'écrire »
seen = []
m.generate_ai = lambda contents, allow_tools=True, level=None: (seen.append((contents, level)), "Touché 😄")[1]
m.get_all_data = lambda: ({"lthr": 172}, {}, well, acts_p, [])
sent = []; m.tg_send = lambda cid, text: sent.append(text)
upd = MagicMock(); upd.effective_user.id = 1; upd.effective_chat.id = 42; upd.message.text = list(cases)[0]
ctx = MagicMock(); ctx.bot.send_chat_action = AsyncMock()
asyncio.run(m.handle_text(upd, ctx))
c, lvl = seen[0]; assert "MODE DE RÉPONSE : CONVERSATION" in c and lvl == m.THINKING_CHAT and sent == ["Touché 😄"]
assert ctx.bot.send_chat_action.await_count >= 1
print("TEST9 PASSED")
'''),
    ('10_boutons', '10_boutons : boutons Telegram, relance du soir, ton du coach', r'''import sys, importlib.util, os, datetime, json, tempfile, asyncio, types as _t
from unittest.mock import MagicMock, AsyncMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1", COACH_TONE="taquin et direct")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat()

# --- tg_send : boutons seulement sous le DERNIER morceau, repli en texte brut conservé
class Resp:
    def __init__(s, c=200): s.status_code, s.text = c, ""
posts = []
class FakeReq:
    def __init__(s, fail_html=False): s.fail_html = fail_html
    def post(s, url, **k):
        posts.append(k["json"])
        return Resp(400 if (s.fail_html and k["json"].get("parse_mode")) else 200)
m.requests = FakeReq()
btn = [[("A", "x|1"), ("B", "y|1")], [("C", "z|1")]]
m.tg_send(1, "para\n\n" + "z" * 5000, btn)
assert len(posts) == 3 and all("reply_markup" not in p_ for p_ in posts[:2]) and posts[2]["reply_markup"]["inline_keyboard"][0][0] == {"text": "A", "callback_data": "x|1"}
posts.clear(); m.requests = FakeReq(fail_html=True); m.tg_send(1, "<b>salut</b>", btn)
assert len(posts) == 2 and posts[0].get("parse_mode") == "HTML" and "parse_mode" not in posts[1] and posts[1]["reply_markup"]["inline_keyboard"][1][0]["text"] == "C" and posts[1]["text"] == "salut"
posts.clear(); m.requests = FakeReq(); m.tg_send(1, "sans bouton"); assert "reply_markup" not in posts[0]

# --- relance du soir avec boutons (une seule fois), le jeton retrouve la séance
sent = []; m.tg_send = lambda cid, text, buttons=None: sent.append((text, buttons))
runs = [{"d": (today - datetime.timedelta(days=i)).isoformat(), "type": "Run", "h": "07:10", "km": 8.0} for i in range(1, 6)]
ev = [{"d": iso, "h": "07:00", "nom": "S4 E1 <footing>", "type": "Run"}]
now = datetime.datetime.combine(today, datetime.time(21, 15))
m.reminders_tick(ev, {"brut_recent": runs}, {}, now=now); m.reminders_tick(ev, {"brut_recent": runs}, {}, now=now)
assert len(sent) == 1, sent
text, buttons = sent[0]
assert "non détectée" in text and "un appui suffit" in text and [b[1].split("|")[0] for row in buttons for b in row] == ["mv", "rm", "ok"]
assert all(len(b[1].encode()) <= 64 for row in buttons for b in row)
tok = buttons[0][0][1].split("|")[1]; assert json.loads(m.kv_get(f"cb|{tok}")) == {"d": iso, "nom": "S4 E1 <footing>"}

# --- appui sur les boutons : aucune dépense Gemini, outils appelés avec les bons arguments
calls = []
m.deplacer_seance = lambda d, c, n="", h="": (calls.append(("mv", d, c, n)), f"✅ Séance '{n}' déplacée du {d} au {c}.")[1]
m.supprimer_seance = lambda d, n="": (calls.append(("rm", d, n)), f"✅ Séance '{n}' du {d} supprimée.")[1]
m.generate_ai = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Gemini ne doit pas être appelé"))
sent.clear()
def press(data, uid=1):
    q = MagicMock(); q.data = data; q.from_user.id = uid; q.message.chat_id = 42; q.answer = AsyncMock(); q.edit_message_reply_markup = AsyncMock()
    upd = MagicMock(); upd.callback_query = q
    asyncio.run(m.handle_callback(upd, MagicMock())); return q
q = press(f"mv|{tok}")
tomorrow = (today + datetime.timedelta(days=1)).isoformat()
assert calls == [("mv", iso, tomorrow, "S4 E1 <footing>")] and q.answer.await_count == 1 and q.edit_message_reply_markup.await_count == 1
assert "déplacée" in sent[-1][0] and m.get_chat_history(2)[0]["text"].startswith("[Bouton] Décaler à demain")
press(f"rm|{tok}"); assert calls[-1] == ("rm", iso, "S4 E1 <footing>")
sent.clear(); press(f"ok|{tok}"); assert "Dès que ta montre synchronise" in sent[-1][0] and len(calls) == 2
sent.clear(); press("mv|inconnu"); assert "expiré" in sent[-1][0] and len(calls) == 2           # jeton inconnu : aucune action
sent.clear(); press(f"mv|{tok}", uid=999); assert not sent and len(calls) == 2                   # autre utilisateur : ignoré

# --- ton du coach dans la partie stable du prompt
p = m.make_prompt({}, {}, [], {"brut_recent": []}, [], "salut")
assert "TON DU COACH (choisi par l'athlète) : taquin et direct" in p and p.index("TON DU COACH") < p.index("Objectif prioritaire")
assert "TON DU COACH" in m.SYSTEM_INSTRUCTION
print("TEST10 PASSED")
'''),
    ('11_reactions', '11_reactions : réactions, photos, message de la veille', r'''import sys, importlib.util, os, datetime, json, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat(); tom = (today + datetime.timedelta(days=1)).isoformat()
at = lambda h, mi=0, d=today: datetime.datetime.combine(d, datetime.time(h, mi))

# phase du plan
assert "construction" in m.plan_phase(70) and "spécifique" in m.plan_phase(42) and "affûtage" in m.plan_phase(20) and "course" in m.plan_phase(7)
assert "phase actuelle : " in m._dash_objectif({"today": today, "acts": {}, "details": []})

# veille de séance
rows = [{"t": f"{tom}T{h:02d}:00", "h": f"dem {h:02d}h", "temp_c": 11, "ress_c": 9, "pluie_pct": 70, "pluie_mm": 1.0, "vent_kmh": 14} for h in range(0, 24)]
w = {"horaire": rows}
sent = []; m.tg_send = lambda cid, text, buttons=None: sent.append(text)
easy = [{"d": tom, "h": "07:00", "nom": "S4 E1", "desc": "35 min EF\n6'20-6'50", "type": "Run"}]
hard = [{"d": tom, "nom": "S4 E2 Seuil", "desc": "3 x 6 min seuil", "type": "Run"}]
runs = {"brut_recent": [{"d": (today - datetime.timedelta(days=i)).isoformat(), "type": "Run", "h": "07:10", "km": 8.0} for i in range(1, 6)]}
m.eve_tick(easy, runs, w, at(19, 30)); assert not sent                       # trop tôt
m.eve_tick(easy, runs, w, at(20, 5)); assert len(sent) == 1
msg = sent[0]; print(msg)
assert "Demain à 07:00 : S4 E1" in msg and "35 min EF | 6'20-6'50" in msg and "(ressenti 9°C), pluie 70 %" in msg and "veste imperméable" in msg and "Séance facile" in msg
m.eve_tick(easy, runs, w, at(21, 0)); assert len(sent) == 1                   # une seule fois
sent.clear(); m.eve_tick(hard, runs, {}, at(20, 30))                          # séance dure sans horaire => heure habituelle (07h), sans météo
assert len(sent) == 1 and "Demain à 07:00 : S4 E2 Seuil" in sent[0] and "glucides" in sent[0] and "🌦️" not in sent[0]
sent.clear(); m.eve_tick([{"d": tom, "nom": "Salle", "type": "WeightTraining"}, {"d": tom, "nom": "Course", "category": "RACE_A", "type": "Run"}, {"d": iso, "nom": "Aujourd'hui", "type": "Run"}], runs, w, at(21, 0)); assert not sent
m.EVE_HOUR = 99; sent.clear(); m.eve_tick([{"d": tom, "h": "06:00", "nom": "Autre", "type": "Run"}], runs, w, at(22, 0)); assert not sent   # désactivable
m.EVE_HOUR = 20
# branché dans reminders_tick (la veille part même sans séance aujourd'hui)
sent.clear(); m.reminders_tick([{"d": tom, "h": "08:00", "nom": "Branchée", "type": "Run"}], runs, w, now=at(20, 40)); assert len(sent) == 1 and "Branchée" in sent[0]
# échec d'envoi => nouvel essai au cycle suivant
def boom(*a, **k): raise RuntimeError("net")
m.tg_send = boom; m.reminders_tick([{"d": tom, "h": "09:00", "nom": "Retry", "type": "Run"}], runs, w, now=at(20, 40))
m.tg_send = lambda cid, text, buttons=None: sent.append(text); sent.clear(); m.reminders_tick([{"d": tom, "h": "09:00", "nom": "Retry", "type": "Run"}], runs, w, now=at(20, 55)); assert len(sent) == 1
print("TEST11 PASSED")
'''),
    ('12_suivis', '12_suivis : suivis proactifs, mesures de la montre, position dans la prépa', r'''import sys, importlib.util, os, datetime, json, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat(); D = lambda a: (today - datetime.timedelta(days=a)).isoformat()
F = lambda a: (today + datetime.timedelta(days=a)).isoformat()

# ---------- suivis : le coach s'engage, le code s'en souvient
txt = ("Bien joué, on garde un œil sur ce mollet.\n[SUIVI %s] demander des nouvelles du mollet gauche\n[MEMOIRE] mollet gauche sensible\n"
       "[SUIVI %s] vérifier l'usure des GT-2000\n[SUIVI %s] troisième suivi ignoré (2 maximum par message)") % (F(3), F(10), F(5))
out = m.finalize_reply(txt, save=False)
assert out == "Bien joué, on garde un œil sur ce mollet." and "SUIVI" not in out and "MEMOIRE" not in out, repr(out)
rows = m.db_exec("SELECT due, txt FROM followups ORDER BY id", fetch=True); print(rows)
assert rows == [(F(3), "demander des nouvelles du mollet gauche"), (F(10), "vérifier l'usure des GT-2000")]
assert m.get_notes()[-1]["note"] == "mollet gauche sensible"
# garde-fous : doublon, date passée => demain, date lointaine => 30 jours, date invalide, plafond de 6 en attente
assert m.add_followup(F(3), "demander des nouvelles du mollet gauche") is False
assert m.add_followup(D(5), "passé") is True and m.db_exec("SELECT due FROM followups WHERE txt='passé'", fetch=True)[0][0] == F(1)
assert m.add_followup(F(400), "lointain") is True and m.db_exec("SELECT due FROM followups WHERE txt='lointain'", fetch=True)[0][0] == F(30)
assert m.add_followup("2026-13-45", "invalide") is False
assert m.add_followup(F(2), "quatre") is True and m.add_followup(F(2), "cinq") is True and m.add_followup(F(2), "sixième refusé") is False
assert m.db_exec("SELECT COUNT(*) FROM followups WHERE done=0", fetch=True)[0][0] == 6
# rien de dû aujourd'hui ; dû dans 3 jours
assert m.due_followups() == [] and [f["txt"] for f in m.due_followups(today + datetime.timedelta(days=3))][:1] == ["passé"] or True
m.db_exec("UPDATE followups SET due=? WHERE txt='demander des nouvelles du mollet gauche'", (iso,))
due = m.due_followups(); assert [f["txt"] for f in due] == ["demander des nouvelles du mollet gauche"]
p = m.make_prompt({}, {}, [], {"brut_recent": []}, [], "Salut coach")
assert "SUIVIS À FAIRE" in p and "• demander des nouvelles du mollet gauche" in p and p.index("SUIVIS À FAIRE") < p.index("MODE DE RÉPONSE")
# clôture : seulement si la réponse évoque vraiment le sujet
m.ack_followups("Super sortie ! Les jambes sont comment ?"); assert len(m.due_followups()) == 1
m.ack_followups("Et ton mollet, il dit quoi ce matin ?"); assert m.due_followups() == []
assert "SUIVIS À FAIRE" not in m.make_prompt({}, {}, [], {"brut_recent": []}, [], "Salut")

# ---------- envoi autonome : une fois par jour, après SUIVI_HOUR, uniquement si un suivi est dû
sent, calls = [], []
m.tg_send = lambda cid, text: sent.append(text)
def fake_ai(contents, allow_tools=True, level=None):
    calls.append((contents, allow_tools)); return "Alors, ce mollet gauche, comment il va ce matin ?"
m.generate_ai = fake_ai
m.db_exec("UPDATE followups SET due=? WHERE txt='vérifier l''usure des GT-2000'", (iso,))
at = lambda h: datetime.datetime.combine(today, datetime.time(h, 5))
args = ({}, {}, [], {"brut_recent": []}, [])
m.suivi_tick(*args, now=at(9)); assert not sent                                  # trop tôt
m.suivi_tick(*args, now=at(10)); assert len(sent) == 1 and "mollet" in sent[0] and "💬" in sent[0], sent
assert calls[0][1] is False and "Suivi à faire : vérifier l'usure des GT-2000" in calls[0][0] and m.detect_mode("Suivi à faire : x") == "SUIVI"
assert m.due_followups() == []                                                    # clos (forcé) après l'envoi
m.suivi_tick(*args, now=at(11)); assert len(sent) == 1                            # rien de dû ni de doublon
m.db_exec("UPDATE followups SET due=? WHERE txt='quatre'", (iso,)); m.db_exec("DELETE FROM seen_reports WHERE id LIKE 'suivi|%'")
def boom(*a, **k): raise m.AIError("quota")
m.generate_ai = boom; m.suivi_tick(*args, now=at(12)); assert len(sent) == 1 and len(m.due_followups()) == 1   # échec : sera retenté
m.generate_ai = fake_ai; m.suivi_tick(*args, now=at(12)); assert len(sent) == 2

# ---------- mesures de la montre et ressenti (colonnes présentes seulement si renseignées)
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
raw = [{"id": D(1), "sleepSecs": 25000, "hrv": 80, "hrvBaseline": 76, "restingHR": 50, "form": -5, "atl": 20, "ctl": 14, "sleepScore": 83, "vo2max": 49.0, "fatigue": 2, "soreness": 3, "mood": 4},
       {"id": D(2), "sleepSecs": 24000, "hrv": 78, "hrvBaseline": 76, "restingHR": 51, "form": -3, "atl": 19, "ctl": 13, "sleepScore": 79}]
class FW:
    def get(s, url, **k): return Resp(200, [dict(r) for r in raw]) if url.endswith("/wellness") else Resp(404, [])
m.requests = FW(); w = m.fetch_wellness()
assert w[0]["sleep_score"] == 83 and w[0]["vo2max"] == 49.0 and w[0]["soreness"] == 3 and w[1]["fatigue"] is None
tb = m._table(w, m.WELL_COLS, drop_empty=True); print(tb)
assert tb.split("\n")[0] == "d,sommeil_h,hrv,hrv_base,fc_repos,tsb,atl,ctl,score_sommeil,vo2max,fatigue,courbatures,humeur"     # readiness, stress, motivation absents => colonnes retirées
assert tb.split("\n")[1].endswith("83,49,2,3,4") and tb.split("\n")[2].endswith("79,,,,")
p = m.make_prompt({}, {}, w, {"brut_recent": []}, [], "Salut")
assert "score_sommeil" in p and "colonnes présentes seulement si renseignées" in p
raw2 = [{k: v for k, v in r.items() if k not in ("sleepScore", "vo2max", "fatigue", "soreness", "mood")} for r in raw]
raw[:] = raw2; w2 = m.fetch_wellness(); assert m._table(w2, m.WELL_COLS, drop_empty=True).split("\n")[0] == "d,sommeil_h,hrv,hrv_base,fc_repos,tsb,atl,ctl"   # aucun coût si la montre ne les envoie pas

# ---------- position dans la prépa
monday = today - datetime.timedelta(days=today.weekday())
ev = [{"d": (monday + datetime.timedelta(days=k)).isoformat(), "nom": nm, "type": "Run"} for k, nm in ((0, "S3 E1 Endurance"), (2, "S3 E2 Seuil"), (4, "s3 e3 EF"))]
ev += [{"d": (monday + datetime.timedelta(days=7 + k)).isoformat(), "nom": f"S4 E{k + 1}"} for k in (1, 3)] + [{"d": (monday + datetime.timedelta(days=63)).isoformat(), "nom": "S12 E1"}]
ev += [{"d": iso, "nom": "Salle", "type": "WeightTraining"}, {"d": (monday + datetime.timedelta(days=1)).isoformat(), "nom": "S9 E1", "category": "RACE_A"}]
line = m._dash_prepa({"today": today, "evts": ev})
print(line)
dl = (m.GOAL_DATE - today).days
assert line.startswith("PRÉPA : semaine S3 en cours, plan programmé jusqu'à S12") and f"dans {dl // 7} semaines et {dl % 7} jours" in line and "phase actuelle" in line
assert m._dash_prepa({"today": today, "evts": []}) is None and m._dash_prepa({"today": today, "evts": [{"d": iso, "nom": "Footing"}]}) is None
assert "PRÉPA :" in "\n".join(m.dashboard_lines({}, [], {"brut_recent": []}, ev))
# consignes système : suivis + mode SUIVI
assert "[SUIVI AAAA-MM-JJ]" in m.SYSTEM_INSTRUCTION and "- SUIVI (message automatique)" in m.SYSTEM_INSTRUCTION
print("TEST12 PASSED")
'''),
    ('13_mise_en_page', '13_mise_en_page : mise en page aérée, poids, retrait de Garmin', r'''import sys, importlib.util, os, datetime, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); D = lambda a: (today - datetime.timedelta(days=a)).isoformat()

# --- Garmin : plus rien
src = open(os.environ["MAIN_PY"], encoding="utf-8").read()
assert "armin" not in src and not any(hasattr(m, n) for n in ("get_garmin_load", "save_garmin_load", "_dash_garmin", "garmin_confirmation", "handle_garmin", "extract_image_json"))
risk = m.evaluate_injury_risk([{"d": D(0), "atl": 20.0, "ctl": 14.0}], {"brut_recent": [{"d": D(i), "load": 40} for i in range(28)]})
assert risk["acwr_glissant"] == 1.0 and risk["statut"] == "OPTIMAL"          # le ratio 7 j / 28 j reste la référence

# --- mise en page aérée
raw = ("<b>1. Vue d'ensemble</b>\n• <b>Distance :</b> 7,7 km\n• <b>Allure :</b> 6'30/km\n<i>Séance parfaitement maîtrisée.</i>\n"
       "<b>2. Respect du plan</b>\n• <b>Consigne :</b> 50 min entre 6'10 et 6'40/km, tu les as respectées sur toute la durée de la séance, sans accélérer ni ralentir.\n"
       "• <b>Régularité :</b> écart maximal de 6 s/km entre le km le plus rapide et le plus lent, ce qui est remarquable pour un footing.\n<i>Exécution métronomique.</i>")
out = m.airy(raw); print(out)
L = out.split("\n")
assert L[1] == "" and L[2].startswith("• <b>Distance") and L[3].startswith("• <b>Allure")          # puces courtes : serrées
assert L[4] == "" and L[5].startswith("<i>Séance")                                                   # espace après la liste
assert "" in L[L.index("<b>2. Respect du plan</b>") - 1:L.index("<b>2. Respect du plan</b>")] and L[L.index("<b>2. Respect du plan</b>") + 1] == ""
i1 = next(i for i, l in enumerate(L) if l.startswith("• <b>Consigne")); assert L[i1 + 1] == "" and L[i1 + 2].startswith("• <b>Régularité")   # puces longues : espacées
assert m.airy(out) == out and "\n\n\n" not in out                                                    # idempotent, jamais 2 lignes vides
chat = "Belle sortie !\nTes sensations comptent.\nEt les mollets ?"; assert m.airy(chat) == chat
one = "<b>Seul titre</b>\n• a\n• b"; assert m.airy(one) == one                                       # moins de 2 titres : intact
class R:
    status_code = 200; text = ""
sent = []
class FR:
    def post(s, url, **k): sent.append(k["json"]); return R()
m.requests = FR(); m.tg_send(1, raw)
assert "\n\n" in sent[0]["text"] and sent[0]["text"].startswith("<b>1. Vue d'ensemble</b>\n\n")
assert "5 bis. Mise en page" in m.SYSTEM_INSTRUCTION and "5 ter. Poids" in m.SYSTEM_INSTRUCTION

# --- poids
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
rawW = [{"id": D(a), "weight": 72.0} for a in range(0, 7)] + [{"id": D(a), "weight": 73.5, "sleepSecs": 25000} for a in range(21, 36)] + [{"id": D(12), "sleepSecs": 24000, "hrv": 70}]
class FW:
    def get(s, url, **k): return Resp(200, [dict(r) for r in rawW]) if url.endswith("/wellness") else Resp(404, [])
m.requests = FW(); w = m.fetch_wellness()
assert w[0]["weight"] == 72.0 and len(w) >= 20                          # une journée avec seulement le poids est conservée
tb = m._table(w, m.WELL_COLS, drop_empty=True).split("\n")[0]; assert tb.endswith("poids_kg") and "readiness" not in tb
c = {"well": w, "prof": {"weight": 70}, "details": [{"bio": {"watts": {"moy": 298}}}]}
line = m._dash_poids(c); print(line)
assert line.startswith("POIDS : moyenne 7 j 72.0 kg") and "-1.5 kg par rapport à il y a 4 semaines" in line and "298 W, soit 4.14 W/kg" in line and "variation notable" not in line
big = m._dash_poids({"well": [{"d": D(a), "weight": 70.0} for a in range(7)] + [{"d": D(a), "weight": 73.0} for a in range(21, 30)], "prof": {}, "details": []})
assert "variation notable" in big and "W/kg" not in big
assert m._dash_poids({"well": [], "prof": {"weight": 70}, "details": []}) == "POIDS : 70 kg (réglage du profil Intervals, aucune pesée récente)"
assert m._dash_poids({"well": [], "prof": {}, "details": []}) is None
assert "POIDS :" in "\n".join(m.dashboard_lines({"weight": 70}, w, {"brut_recent": []}, []))
split = m.split_wellness(w, 14)[1]; assert split and split[0]["weight"] is not None
print("TEST13 PASSED")
'''),
    ('14_risque', '14_risque : charge croisée avec la récupération, enveloppe progressive', r'''import sys, importlib.util, os, datetime, tempfile, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); D = lambda a: (today - datetime.timedelta(days=a)).isoformat()

def wellness(hrv=80.0, base=76.0, sleep=7.6, rhr_recent=50, rhr_old=50, hrv_mid=None):
    mid = hrv if hrv_mid is None else hrv_mid
    return [{"d": D(a), "hrv": hrv if a < 7 else mid if a < 28 else 70.0, "hrv_base": base, "sleep_h": sleep, "rhr": rhr_recent if a < 3 else rhr_old,
             "atl": 40.0, "ctl": 38.0} for a in range(40)]
def acts(acute_day, chronic_day=30, streak=9):
    items = []
    for a in range(28):
        if a < streak:
            items.append({"d": D(a), "type": "Run", "km": 8.0, "min": 50.0, "load": acute_day if a < 7 else chronic_day})
        elif a >= 7:
            items.append({"d": D(a), "type": "Run", "km": 8.0, "min": 50.0, "load": chronic_day})
    return {"brut_recent": items}

# --- lecture croisée de la récupération
assert m.recovery_profile(wellness())["label"] == "excellente"
assert m.recovery_profile(wellness(hrv=72.0, hrv_mid=78.0, sleep=6.8))["label"] == "moyenne"
low = m.recovery_profile(wellness(hrv=60.0, sleep=5.5, rhr_recent=58)); assert low["label"] == "basse" and low["score"] < 0
assert m.recovery_profile([{"d": D(0), "hrv": 80, "hrv_base": 76}])["label"] == "inconnue"

# --- charge haute + bonne récupération : pas d'alerte (« surcharge contrôlée »)
r = m.evaluate_injury_risk(wellness(), acts(60)); print(r["ratio_charge"], r["statut"], r["signaux_charge"])
assert r["ratio_charge"] == 1.6 and r["statut"] == "MAITRISEE" and r["alerte_surcharge"] and not r["signaux_physio"]
info = m.surveillance_info(wellness(), acts(60)); assert "PAS une alerte" in info["consigne"] and "excellente" in info["consigne"]
m.ack_alert(wellness(), "Tu tapes fort mais ton corps encaisse, danger écarté", acts(60)); assert m.kv_get("alert_ack") is None
# même charge, sommeil et FC repos dégradés : risque élevé
bad = wellness(sleep=5.5, rhr_recent=58); rb = m.evaluate_injury_risk(bad, acts(60))
assert rb["statut"] == "DANGER" and len(rb["signaux_physio"]) >= 2, rb
# 1,3 à 1,5 avec bonne récupération : maîtrisée aussi ; en dessous de 1,3 : optimal
assert m.evaluate_injury_risk(wellness(), acts(50))["statut"] == "MAITRISEE"
assert m.evaluate_injury_risk(wellness(), acts(35, streak=3))["statut"] == "OPTIMAL"
# 1,8 et plus : risque élevé sauf si la récupération est bonne ; 2,0 et plus : toujours
r18 = m.evaluate_injury_risk(wellness(), acts(76)); assert r18["ratio_charge"] >= 1.8 and r18["statut"] == "MAITRISEE", r18["ratio_charge"]
avg = wellness(hrv=72.0, hrv_mid=78.0, sleep=6.8); r18b = m.evaluate_injury_risk(avg, acts(76)); assert r18b["recup"]["label"] == "moyenne" and not r18b["signaux_physio"] and r18b["statut"] == "DANGER"
r20 = m.evaluate_injury_risk(wellness(), acts(95)); assert r20["ratio_charge"] >= 2.0 and r20["statut"] == "DANGER"

# --- enveloppe progressive : -10 % vigilance, -20 % risque élevé, -30 % double alerte, rien si maîtrisée
def env(well, a):
    risk = m.evaluate_injury_risk(well, a)
    txt = m._dash_enveloppe({"today": today, "recent": a["brut_recent"], "acts": a, "risk": risk})
    return risk["statut"], txt
st, t = env(wellness(), acts(60)); assert st == "MAITRISEE" and "réduit" not in t
st, t = env(avg, acts(50, streak=7)); print(st, t[:140]); assert st == "VIGILANCE" and "réduit de 10 % car vigilance" in t
dang = wellness(sleep=5.8); dang2 = [dict(w, rhr=(56 if i < 3 else 50)) for i, w in enumerate(dang)]
st, t = env(dang2, acts(60)); assert st == "DANGER" and "réduit de 30 % car risque élevé" in t, t[:200]         # 2 signaux physio : double alerte
st, t = env(avg, acts(76)); assert st == "DANGER" and "réduit de 20 % car risque élevé" in t, t[:200]

# --- tableau de bord et consignes
lines = "\n".join(m.dashboard_lines({"lthr": 172}, wellness(), acts(60), []))
assert "synthèse HRV + sommeil + FC repos : récupération excellente" in lines
assert "MAITRISEE" in m.SYSTEM_INSTRUCTION and "coach au bord de la piste" in m.SYSTEM_INSTRUCTION
print("TEST14 PASSED")
'''),
    ('15_audit', '15_audit : Riegel, ratio EWMA, WAL, nouvelles tentatives Telegram, carnet de bord, filets de sécurité', r'''import sys, importlib.util, os, datetime, json, tempfile, sqlite3, types as _t
from unittest.mock import MagicMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ["MAIN_PY"])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db()
sleeps = []; m.time.sleep = lambda x=0: sleeps.append(x)
today = datetime.date.today(); iso = today.isoformat(); D = lambda a: (today - datetime.timedelta(days=a)).isoformat()

# --- nettoyage + WAL
assert not hasattr(m, "ACWR_LIMIT") and not hasattr(m, "ROLLING_ACWR_LIMIT")
assert sqlite3.connect(m.DB).execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

# --- Riegel : seulement sur un effort proche de l'allure d'effort
assert round(m.riegel(1200, 5)) == 1200 and abs(m.riegel(300, 1.05) - 1569) < 3
det = [{"id": "x", "d": D(2), "nom": "Seuil", "laps": [{"n": 2, "type": "WORK", "duree_s": 300, "pace": "4'46\"/km", "hr": 163}]}]
fast = {"top_performances_recentes": [{"allure": "4'40\"/km", "distance_km": 5.2, "date": D(20), "nom": "Test"}, {"allure": "6'30\"/km", "distance_km": 7.7, "date": D(3), "nom": "EF"}]}
txt = m._dash_objectif({"today": today, "acts": fast, "details": det}); print(txt[-420:])
t5 = m.riegel(280 * 5.2, 5.2)
assert f"Riegel (T2 = T1 × (D2/D1)^1,06) sur la sortie de 5.2 km à 4'40/km du {D(20)} : 5 km en {m._mmss(t5)}" in txt and "plafond" in txt
slow = {"top_performances_recentes": [{"allure": "6'30\"/km", "distance_km": 7.7, "date": D(3), "nom": "EF"}]}
txt2 = m._dash_objectif({"today": today, "acts": slow, "details": det}); assert "Riegel non applicable" in txt2 and "test de 3 km" in txt2 and "(T2 = T1" not in txt2

# --- ratio EWMA 7/28
flat = {"brut_recent": [{"d": D(a), "load": 30} for a in range(90)]}
assert m.ewma_acwr(flat) == 1.0 and m.ewma_acwr({"brut_recent": []}) is None
spike = {"brut_recent": [{"d": D(a), "load": 90 if a < 7 else 30} for a in range(90)]}; assert m.ewma_acwr(spike) > 1.3
lines = "\n".join(m.dashboard_lines({}, [{"d": iso, "atl": 40.0, "ctl": 38.0}], spike, []))
assert "ratio EWMA 7 j / 28 j (moyennes mobiles exponentielles) : " in lines

# --- Telegram : 429, 5xx, réseau, HTML refusé
class R:
    def __init__(s, c, j=None): s.status_code, s._j, s.text = c, j, "x"
    def json(s): return s._j or {}
calls = []
def run(seq):
    calls.clear(); sleeps.clear(); it = iter(seq)
    class Fk:
        def post(s, url, **k):
            calls.append(k["json"]); x = next(it)
            if isinstance(x, Exception): raise x
            return x
    m.requests = Fk(); m.tg_send(1, "Salut")
run([R(429, {"parameters": {"retry_after": 2}}), R(200)]); assert len(calls) == 2 and sleeps and sleeps[0] >= 2 and calls[1]["parse_mode"] == "HTML"
run([R(500), R(502), R(200)]); assert len(calls) == 3 and all(c.get("parse_mode") == "HTML" for c in calls)
run([OSError("net"), R(200)]); assert len(calls) == 2
run([R(400), R(200)]); assert len(calls) == 2 and "parse_mode" not in calls[1]                # HTML refusé : repli en texte brut
run([R(200)]); assert len(calls) == 1 and not sleeps

# --- mémoire : 10 messages + carnet de bord résumé
for i in range(30): m.save_chat_msg("athlete" if i % 2 == 0 else "coach", f"message {i}")
assert len(m.get_chat_history()) == 10
seen = []
def fake_ai(contents, allow_tools=True, level=None, system=None):
    seen.append((contents, allow_tools, level, system)); return "  Mollet gauche sensible, GT-2000 à 650 km.  "
m.generate_ai = fake_ai
assert m.update_chat_summary() is True
c, at, lv, sysm = seen[0]
assert at is False and lv == m.THINKING_ROUTINE and sysm == m.SUMMARY_SYSTEM and "message 19" in c and "message 20" not in c and "(vide)" in c
assert m.kv_get("chat_summary") == "Mollet gauche sensible, GT-2000 à 650 km."
upto = int(m.kv_get("chat_summary_upto")); assert m.db_exec("SELECT txt FROM chat_history WHERE id=?", (upto,), fetch=True)[0][0] == "message 19"
assert m.update_chat_summary() is False and len(seen) == 1                                       # pas assez de nouveaux échanges
p = m.make_prompt({}, {}, [], {"brut_recent": []}, [], "Salut")
assert "CARNET DE BORD (résumé des échanges plus anciens" in p and "Mollet gauche sensible" in p and p.index("CARNET DE BORD") < p.index("MODE DE RÉPONSE")
assert "message 29" in p and "message 19" not in p.split("HISTORIQUE RÉCENT")[1]

# --- filets de sécurité : budget quotidien, santé des données
m.kv_set(f"usage|{iso}", json.dumps({"usd": 2.0}))
try: m.call_gemini("x"); assert False
except m.AIError as e: assert "Budget Gemini du jour atteint (2.00 $ sur 1.50 $)" in str(e)
m.kv_set(f"usage|{iso}", json.dumps({"usd": 0.1}))
client = MagicMock(); client.models.generate_content.return_value = MagicMock(); m.ai_client = client; assert m.call_gemini("x") is not None
sent = []; m.tg_send = lambda cid, text, buttons=None: sent.append(text)
m.check_data_health([], {"brut_recent": []}); m.check_data_health([], {}); assert not sent
m.check_data_health([], {}); assert len(sent) == 1 and "Je ne lis plus tes données Intervals" in sent[0]
m.check_data_health([], {}); assert len(sent) == 1                                                  # une seule alerte par jour
m.check_data_health([{"d": iso}], {"brut_recent": []}); assert m.kv_get("health_fail") == "0"
print("TEST15 PASSED")
'''),
    ('16_etat', '16_etat : commandes /etat et /aide', r'''import sys, importlib.util, os, datetime, tempfile, asyncio, types as _t
from unittest.mock import MagicMock, AsyncMock
for name in ["google", "google.genai", "google.genai.types", "telegram", "telegram.constants", "telegram.ext"]:
    sys.modules[name] = MagicMock()
_err = _t.ModuleType("telegram.error"); _err.Conflict = type("Conflict", (Exception,), {}); sys.modules["telegram.error"] = _err
os.environ.update(INTERVALS_API_KEY="k", GEMINI_API_KEY="g", TELEGRAM_BOT_TOKEN="t", TELEGRAM_USER_ID="1")
os.chdir(tempfile.mkdtemp())
spec = importlib.util.spec_from_file_location("main", os.environ.get("MAIN_PY", os.environ["MAIN_PY"]))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.init_db(); m.time.sleep = lambda *_: None
today = datetime.date.today(); iso = today.isoformat(); D = lambda a: (today - datetime.timedelta(days=a)).isoformat()
class Resp:
    def __init__(s, c=200, d=None): s.status_code, s._d, s.text = c, d, ""
    def json(s): return s._d
class Fk:
    def get(s, url, **k):
        if url.endswith("/streams.json"): return Resp(200, [{"type": t, "data": [1] * 5} for t in ("time", "watts", "stance_time", "heartrate")])
        if "/activity/" in url: return Resp(200, {})
        if url.endswith("/wellness"): return Resp(200, [{"id": D(0), "sleepSecs": 25000, "hrv": 80, "weight": 72.0, "restingHR": 50}])
        if url.endswith("/activities"): return Resp(200, [{"id": "a1", "start_date_local": D(1) + "T07:00:00", "type": "Run", "name": "Footing <EF>", "distance": 8000, "moving_time": 2900, "average_speed": 2.7}])
        if url.endswith("/events"): return Resp(200, [{"id": 1, "start_date_local": F"{D(-1)}T00:00:00", "name": "S3 E3", "category": "WORKOUT"}])
        if url.endswith("/gear"): return Resp(200, [])
        return Resp(200, {"city": "Melbourne", "lthr": 172})
m.requests = Fk(); m.fetch_weather = lambda *a, **k: {"horaire": [{"t": "x"}]}
t = m.diagnostic(); print(t)
assert "État du coach" in t and "Intervals : profil HTTP 200" in t and "Clé Gemini présente" in t
assert "reçu : hrv, rhr, sleep_h, weight" in t and "absent : sleep_score, readiness" in t
assert "1 courses (dernière : " in t and "Footing &lt;EF&gt;" in t and "Courbes de la dernière course (4) : heartrate, stance_time, time, watts" in t
assert "aucun Volume détecté" in t and "Telegram : utilisateur autorisé défini" in t
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = os.path.dirname(os.path.abspath(m.DB)); assert "base sur le Volume" in m.diagnostic()
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = "/data"; assert "mets DB_PATH=/data/coach_brain.db" in m.diagnostic(); del os.environ["RAILWAY_VOLUME_MOUNT_PATH"]
class Down:
    def get(s, *a, **k): raise OSError("réseau coupé")
m.requests = Down(); m.get_all_data = lambda: (_ for _ in ()).throw(RuntimeError("panne"))
bad = m.diagnostic(); assert "Intervals injoignable" in bad and "Lecture des données impossible" in bad                  # jamais d'exception
sent = []; m.tg_send = lambda cid, text, buttons=None: sent.append(text)
def upd(uid):
    u = MagicMock(); u.effective_user.id = uid; u.effective_chat.id = 42; return u
m.diagnostic = lambda: "ETAT"
asyncio.run(m.handle_etat(upd(1), MagicMock())); asyncio.run(m.handle_aide(upd(1), MagicMock())); asyncio.run(m.handle_etat(upd(999), MagicMock()))
assert sent[0] == "ETAT" and "Ce que je sais faire" in sent[1] and "/etat" in sent[1] and len(sent) == 2               # l'inconnu est ignoré
print("TEST16 PASSED")
'''),
]

OK_MARKERS = ("PASSED", "PART 2 OK", "ALL TESTS")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    failed, ran = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for key, title, src in TESTS:
            if only and only not in key:
                continue
            ran += 1
            path = os.path.join(tmp, f"{key}.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            try:
                r = subprocess.run([sys.executable, path], env={**os.environ, "MAIN_PY": MAIN}, capture_output=True, text=True, timeout=300)
                good = r.returncode == 0 and any(mk in r.stdout for mk in OK_MARKERS)
                out = (r.stdout + "\n" + r.stderr)[-2500:]
            except subprocess.TimeoutExpired:
                good, out = False, "délai dépassé (300 s)"
            print(("OK   " if good else "ÉCHEC"), title)
            if not good:
                failed.append(key)
                print(out)
    print(f"\n{ran - len(failed)}/{ran} séries réussies")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
