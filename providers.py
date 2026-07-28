"""Oura and Strava — OAuth, fetching, and merging into one dataset.

Garmin has no self-serve API, so activities arrive via Strava instead: Garmin
Connect auto-uploads every run to Strava, and Strava's API is self-serve. Both
connections here are official.
"""

import datetime as dt
import json
import time
import urllib.parse
import urllib.request

import store

OURA_AUTH = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN = "https://api.ouraring.com/oauth/token"
OURA_API = "https://api.ouraring.com/v2/usercollection"
OURA_SCOPES = "daily heartrate personal session workout"

STRAVA_AUTH = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_SCOPES = "activity:read_all"


# --------------------------------------------------------------- http helpers

def _post(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(url, token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------------- OAuth

def authorize_url(provider, client_id, redirect_uri, state):
    if provider == "oura":
        return OURA_AUTH + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id,
            "redirect_uri": redirect_uri, "scope": OURA_SCOPES, "state": state})
    return STRAVA_AUTH + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "scope": STRAVA_SCOPES,
        "approval_prompt": "auto", "state": state})


def exchange(provider, code, client_id, client_secret, redirect_uri):
    fields = {"grant_type": "authorization_code", "code": code,
              "client_id": client_id, "client_secret": client_secret}
    if provider == "oura":
        fields["redirect_uri"] = redirect_uri
        tok = _post(OURA_TOKEN, fields)
    else:
        tok = _post(STRAVA_TOKEN, fields)
    _save(provider, tok)
    return tok


def _save(provider, tok):
    tok = dict(tok)
    if "expires_at" not in tok:
        tok["expires_at"] = time.time() + tok.get("expires_in", 86400)
    tok.pop("athlete", None)                       # not needed, and it's bulky
    store.put("token:" + provider, tok)


def connected(provider):
    return store.get("token:" + provider) is not None


def token(provider, client_id, client_secret):
    """Live access token, refreshed when it has less than five minutes left."""
    tok = store.get("token:" + provider)
    if not tok:
        raise RuntimeError(provider.title() + " is not connected yet.")
    if tok.get("expires_at", 0) - 300 > time.time():
        return tok["access_token"]
    url = OURA_TOKEN if provider == "oura" else STRAVA_TOKEN
    fresh = _post(url, {"grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"],
                        "client_id": client_id, "client_secret": client_secret})
    fresh.setdefault("refresh_token", tok["refresh_token"])
    _save(provider, fresh)
    return fresh["access_token"]


def disconnect(provider):
    store.drop("token:" + provider)


# ----------------------------------------------------------------------- Oura

def _paged(path, tok, start, end):
    url = f"{OURA_API}/{path}?start_date={start}&end_date={end}"
    rows, guard = [], 0
    while url and guard < 25:
        guard += 1
        page = _get(url, tok)
        rows.extend(page.get("data", []))
        nxt = page.get("next_token")
        url = f"{OURA_API}/{path}?start_date={start}&end_date={end}&next_token={nxt}" if nxt else None
    return rows


def fetch_oura(tok, start, end):
    out, sleep_c, ready_c = {}, {}, {}
    slot = lambda d: out.setdefault(d, {})

    for r in _paged("daily_sleep", tok, start, end):
        slot(r["day"])["sleep_score"] = r.get("score")
        if r.get("contributors"):
            sleep_c[r["day"]] = r["contributors"]
    for r in _paged("daily_readiness", tok, start, end):
        slot(r["day"])["readiness"] = r.get("score")
        slot(r["day"])["temp_dev"] = r.get("temperature_deviation")
        if r.get("contributors"):
            ready_c[r["day"]] = r["contributors"]

    best = {}
    for r in _paged("sleep", tok, start, end):
        tst = r.get("total_sleep_duration") or 0
        if tst < 3 * 3600:                          # naps are not the night
            continue
        d = r["day"]
        if d not in best or tst > best[d].get("total_sleep_duration", 0):
            best[d] = r

    for day, r in best.items():
        s = slot(day)
        hrs = lambda k: round((r.get(k) or 0) / 3600, 2)
        s.update({
            "tst": hrs("total_sleep_duration"), "awake": hrs("awake_time"),
            "deep": hrs("deep_sleep_duration"), "rem": hrs("rem_sleep_duration"),
            "light": hrs("light_sleep_duration"), "tib": hrs("time_in_bed"),
            "eff": r.get("efficiency"), "hrv": r.get("average_hrv"),
            "rhr": r.get("lowest_heart_rate"), "restless": r.get("restless_periods"),
            "lat": round((r.get("latency") or 0) / 60) or None,
            "br": round(r["average_breath"], 1) if r.get("average_breath") else None})
        for field, name in (("bedtime_start", "bed"), ("bedtime_end", "wake")):
            v = r.get(field)
            if v:
                try:
                    t = dt.datetime.fromisoformat(v)
                    s[name] = round(t.hour + t.minute / 60, 2)
                except ValueError:
                    pass
    return out, sleep_c, ready_c


# --------------------------------------------------------------------- Strava

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}


def fetch_strava(tok, start, end):
    after = int(dt.datetime.fromisoformat(start).timestamp())
    before = int((dt.datetime.fromisoformat(end) + dt.timedelta(days=1)).timestamp())
    raw, page = [], 1
    while page <= 12:
        url = f"{STRAVA_API}/athlete/activities?after={after}&before={before}&per_page=200&page={page}"
        batch = _get(url, tok)
        if not batch:
            break
        raw.extend(batch)
        if len(batch) < 200:
            break
        page += 1

    days, detail = {}, []
    for a in raw:
        if (a.get("sport_type") or a.get("type")) not in RUN_TYPES:
            continue
        stamp = a.get("start_date_local") or ""
        day = stamp[:10]
        if not day:
            continue
        km = (a.get("distance") or 0) / 1000.0
        sec = int(a.get("moving_time") or 0)
        hr = a.get("average_heartrate")
        d = days.setdefault(day, {"km": 0.0, "sec": 0, "runs": 0, "hrs": [],
                                  "elev": None, "loc": None})
        d["km"] += km
        d["sec"] += sec
        d["runs"] += 1
        if hr:
            d["hrs"].append(hr)
        if d["elev"] is None and a.get("elev_low") is not None:
            d["elev"] = round(a["elev_low"])
        if not d["loc"]:
            d["loc"] = a.get("location_city") or a.get("name") or "Run"
        if km > 0 and sec > 0:
            pace = sec / km
            detail.append({
                "day": day, "t": stamp[11:16], "km": round(km, 2),
                "mi": round(km * 0.621371, 2), "sec": sec,
                "pace": f"{int(pace // 60)}:{int(pace % 60):02d}",
                "hr": round(hr) if hr else None,
                "maxhr": round(a["max_heartrate"]) if a.get("max_heartrate") else None,
                "asc": round(a["total_elevation_gain"]) if a.get("total_elevation_gain") else None,
                "elev": round(a["elev_low"]) if a.get("elev_low") is not None else None,
                "loc": a.get("name") or "Run"})

    out = {}
    for day, d in days.items():
        out[day] = {"km": round(d["km"], 1), "mi": round(d["km"] * 0.621371, 1),
                    "runs": d["runs"], "min": round(d["sec"] / 60), "elev": d["elev"],
                    "loc": d["loc"],
                    "hr": round(sum(d["hrs"]) / len(d["hrs"])) if d["hrs"] else None}
    detail.sort(key=lambda x: (x["day"], x["t"]), reverse=True)
    return out, detail


# ----------------------------------------------------------------------- merge

def build(runs, oura, planned, start, end, activities=None,
          sleep_c=None, ready_c=None):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    daily, cur = [], d0
    while cur <= d1:
        k = cur.isoformat()
        row = {"day": k, "km": 0, "mi": 0, "runs": 0}
        row.update(runs.get(k, {}))
        row.update(oura.get(k, {}))
        daily.append(row)
        cur += dt.timedelta(days=1)

    kms = [r.get("km") or 0 for r in daily]
    for i, row in enumerate(daily):
        a, c = kms[max(0, i - 6):i + 1], kms[max(0, i - 27):i + 1]
        atl, ctl = sum(a) / len(a), sum(c) / len(c)
        row["atl"], row["ctl"] = round(atl, 1), round(ctl, 1)
        row["acwr"] = round(atl / ctl, 2) if ctl else None

    weeks = {}
    for row in daily:
        d = dt.date.fromisoformat(row["day"])
        wk = (d - dt.timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"start": wk, "km": 0.0, "mi": 0.0, "runs": 0})
        w["km"] += row.get("km") or 0
        w["mi"] += row.get("mi") or 0
        w["runs"] += row.get("runs") or 0
    weeks = [dict(w, km=round(w["km"], 1), mi=round(w["mi"], 1))
             for w in sorted(weeks.values(), key=lambda x: x["start"])]

    def mean(f):
        v = [r[f] for r in daily if r.get(f) is not None]
        return sum(v) / len(v) if v else None

    def last(f):
        v = [r["day"] for r in daily if r.get(f)]
        return v[-1] if v else None

    def rnd(f, n=0):
        m = mean(f)
        return round(m, n) if m is not None else None

    beds = [r["bed"] for r in daily if r.get("bed") is not None]
    beds = [b - 24 if b > 18 else b for b in beds]
    bm = sum(beds) / len(beds) if beds else None
    bsd = None
    if len(beds) > 1:
        bsd = round((sum((b - bm) ** 2 for b in beds) / (len(beds) - 1)) ** 0.5, 2)

    stats = {
        "nights": len([r for r in daily if r.get("tst") is not None]),
        "runs": len(activities or []),
        "total_km": round(sum(kms)),
        "sleep_mean": rnd("tst", 2),
        "awake_mean": round(mean("awake") * 60) if mean("awake") else None,
        "eff_mean": rnd("eff"), "hrv_mean": rnd("hrv"), "rhr_mean": rnd("rhr", 1),
        "bed_mean": round(bm + 24, 2) if bm is not None else None, "bed_sd": bsd,
        "wake_mean": rnd("wake", 2),
        "oura_last": last("tst"), "garmin_last": last("runs"),
        "synced_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    }
    return {"daily": daily, "weeks": weeks, "stats": stats, "planned": planned,
            "activities": activities or [], "sleep_contrib": sleep_c or {},
            "ready_contrib": ready_c or {}}
