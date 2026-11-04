"""Fetch Austin's, Jordan Hemus's, and Ethan's Garmin Personal API data and
rebuild data.js for the public leaderboard (auswoo.github.io/garmin-leaderboard/).

Runs on GitHub Actions, where the API keys live as repository secrets
(GARMIN_API_KEY_AUSTIN, GARMIN_API_KEY_JORDAN, GARMIN_API_KEY_ETHAN). No key, token, or JWT is
ever printed or written to any file. Logs carry only statuses and counts.

Auth flow (Garmin Personal API):
  - POST the API key to the token endpoint with header `Authorization: ApiKey <key>`
    to get a 24h JWT, then call data endpoints with `Authorization: Bearer <jwt>`.
  - A fresh JWT is exchanged on every run; nothing is cached.

Data window: the last 7 days ending today, America/Los_Angeles. The output
always has exactly 7 day entries (nulls for missing days) so the board's
Daily view keeps yesterday at index 5 and today at index 6.

Board math (must match the board's info sheet):
  - Recovery (Est.) = 50% sleep score + 30% HRV component + 20% Body
    Battery charged, rounded to the nearest whole number (half up).
    HRV component = 50 + (last night's HRV - person's own window-mean HRV
    rounded to 1 decimal, the weekly average shown on the board) * 2,
    clamped to 0-100. Null if sleep, HRV, or Body Battery charged is missing.
  - Strain (Est.) = min(21, max(0, steps/1000
    + max(0, maxHR - restingHR - 40)/10 + min(6, strength_minutes/15))),
    rounded to the nearest whole number (half up). Strength minutes are not
    available from these endpoints yet, so that term is 0 for now and the
    board shows "-" for strength minutes.
  - Weekly aggregates use recorded days only. Weekly strain is the average
    of daily Strain (Est.) over recorded days, with the week's total also
    reported.

Steps, distance, and sleep stages are extracted best-effort: the exact field
names in these endpoints were never confirmed against a live response, so
several candidate paths are tried and the raw record keys are logged (keys
only, no values) on each run. If a field is absent, the board shows "-" for it.

Atomicity: if either account's token exchange or any data call fails, the
script exits non-zero WITHOUT touching data.js, so a partial board is never
published. An endpoint that succeeds but returns no rows is fine (nulls).
"""

import datetime
import json
import math
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://personal.connectapi.garmin.com"
PT = ZoneInfo("America/Los_Angeles")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PEOPLE = [
    ("Austin", "GARMIN_API_KEY_AUSTIN"),
    ("Jordan", "GARMIN_API_KEY_JORDAN"),
    ("Ethan", "GARMIN_API_KEY_ETHAN"),
]


def round_half_up(x, ndigits=0):
    """Round half up (matches JS Math.round for non-negative values)."""
    factor = 10 ** ndigits
    return math.floor(x * factor + 0.5) / factor


def day_label(date):
    return "%s %d" % (MONTHS[date.month - 1], date.day)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def call(req, timeout=90):
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, str(e)[:200])


def fetch_person(api_key, start, end):
    """Return (ok, day_rows_dict). ok=False on any transport/auth failure."""
    req = urllib.request.Request(
        BASE + "/personalapitoken-service/personal-api/personal-api-key/token",
        data=b"",
        method="POST",
        headers={"Authorization": "ApiKey " + api_key, "Accept": "*/*"},
    )
    status, body = call(req)
    if status != 200 or not body or not body.startswith("eyJ"):
        print("token_exchange_failed status=%s" % status, file=sys.stderr)
        return False, {}
    token = body.strip()
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

    def get(path):
        req = urllib.request.Request(BASE + path, headers=headers)
        status, body = call(req)
        if status != 200:
            print("GET_FAILED %s status=%s" % (path.split("?")[0], status),
                  file=sys.stderr)
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            print("GET_BAD_JSON %s" % path.split("?")[0], file=sys.stderr)
            return None

    summaries = get("/usersummary-service/v2/dailysummaries?startDate=%s&endDate=%s"
                    % (start, end))
    hrv = get("/hrv-service/hrv/daily/%s/%s" % (start, end))
    sleep = get("/sleep-service/sleep/dailySleepSummariesByDate"
                "?startDate=%s&endDate=%s" % (start, end))
    if summaries is None or hrv is None or sleep is None:
        return False, {}
    # PROBE ONLY, Nov 3, 2026: inspect the dailyHeartRate response shape for a
    # possible future full-day-HR strain formula. Schema and counts only, no
    # values are logged, and this never affects the returned data or data.js.
    hr_probe = get("/wellness-service/wellness/dailyHeartRate?date=%s" % end)
    if hr_probe is None:
        print("probe dailyHeartRate: no data", file=sys.stderr)
    elif isinstance(hr_probe, dict):
        print("probe dailyHeartRate dict keys=%s" % sorted(hr_probe.keys()), file=sys.stderr)
        for k, v in hr_probe.items():
            if isinstance(v, list):
                print("probe dailyHeartRate list %s len=%d first_keys=%s" % (k, len(v), sorted(v[0].keys()) if v and isinstance(v[0], dict) else []), file=sys.stderr)
            elif isinstance(v, dict):
                print("probe dailyHeartRate dict %s keys=%s" % (k, sorted(v.keys())), file=sys.stderr)
    elif isinstance(hr_probe, list):
        print("probe dailyHeartRate list len=%d first_keys=%s" % (len(hr_probe), sorted(hr_probe[0].keys()) if hr_probe and isinstance(hr_probe[0], dict) else []), file=sys.stderr)
    else:
        print("probe dailyHeartRate type=%s" % type(hr_probe).__name__, file=sys.stderr)

    # Log raw record keys only (schema, no values) to aid extraction fixes.
    def first_record_keys(payload, *keys):
        rows = payload
        if isinstance(payload, dict):
            for k in keys:
                rows = payload.get(k, [])
                if isinstance(rows, list):
                    break
            else:
                rows = []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return sorted(rows[0].keys())
        return []

    print("keys dailySummary: %s" % first_record_keys(summaries, "data"),
          file=sys.stderr)
    print("keys hrv: %s" % first_record_keys(hrv, "hrvSummaries"),
          file=sys.stderr)
    print("keys sleep: %s" % first_record_keys(sleep, "data", "sleepSummaries"),
          file=sys.stderr)
    # Steps are not a top-level summary field, so they must live inside
    # movement. Log movement's own keys (schema only) to pin down the shape.
    srows = summaries.get("data", []) if isinstance(summaries, dict) else summaries
    if isinstance(srows, list) and srows and isinstance(srows[0], dict):
        mv = srows[0].get("movement")
        if isinstance(mv, dict):
            print("keys movement: %s" % sorted(mv.keys()), file=sys.stderr)
        elif isinstance(mv, list):
            print("movement type: list of %d items" % len(mv), file=sys.stderr)
            if mv and isinstance(mv[0], dict):
                print("keys movement[0]: %s" % sorted(mv[0].keys()),
                      file=sys.stderr)
        else:
            print("movement type: %s" % type(mv).__name__, file=sys.stderr)

    days = {}

    def rows_of(payload, *keys):
        if isinstance(payload, list):
            return payload
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
        return []

    for d in rows_of(summaries, "data"):
        date = d.get("calendarDate")
        if not date:
            continue
        hr = d.get("heartRate") or {}
        bb = d.get("bodyBattery") or {}
        st = d.get("stress") or {}
        mv = d.get("movement")
        steps = extract_steps(d, mv)
        row = days.setdefault(date, {})
        row["restingHR"] = hr.get("restingValue")
        row["maxHR"] = hr.get("maxValue")
        row["stress"] = st.get("avgLevel")
        row["battery"] = [bb.get("latestValue"), bb.get("chargedValue"),
                          bb.get("drainedValue")]
        row["steps"] = steps
        row["dist"] = extract_distance(d, mv, steps)

    for s in rows_of(hrv, "hrvSummaries"):
        date = s.get("calendarDate")
        if not date:
            continue
        row = days.setdefault(date, {})
        row["hrv"] = s.get("lastNightAvg")
        row["hrvWeeklyAvg"] = s.get("weeklyAvg")

    for s in rows_of(sleep, "data", "sleepSummaries"):
        date = s.get("calendarDate")
        if not date:
            continue
        sc = s.get("sleepScores") or {}
        ov = sc.get("overall") or {}
        row = days.setdefault(date, {})
        row["sleep"] = ov.get("value")
        row["stages"] = extract_stages(s)

    # BACKTEST ONLY, Nov 3, 2026: compute v3b strain per recorded day from
    # dailyHeartRate. Logs derived loads and strains only, never raw heart
    # rates, and never affects the returned days or data.js.
    for _date, _row in sorted(days.items()):
        _rhr = _row.get("restingHR")
        _steps = _row.get("steps")
        _maxhr = _row.get("maxHR")
        if _rhr is None:
            continue
        _hr = get("/wellness-service/wellness/dailyHeartRate?date=%s" % _date)
        if not isinstance(_hr, dict):
            print("backtest %s no data" % _date, file=sys.stderr)
            continue
        _vals = _hr.get("heartRateValues") or []
        _descs = _hr.get("heartRateValueDescriptors") or []
        _hr_idx = None
        _ts_idx = None
        for _d in _descs:
            if isinstance(_d, dict):
                _key = str(_d.get("key") or "").lower()
                if "heart" in _key:
                    _hr_idx = _d.get("index")
                elif "time" in _key or "stamp" in _key:
                    _ts_idx = _d.get("index")
        if _hr_idx is None or _ts_idx is None:
            print("backtest %s descriptors=%s" % (_date, [(d.get("index"), d.get("key")) for d in _descs if isinstance(d, dict)]), file=sys.stderr)
            continue
        _div = 60000.0
        _first = None
        for _s in _vals:
            if isinstance(_s, (list, tuple)) and len(_s) > max(_hr_idx, _ts_idx):
                try:
                    _first = float(_s[_ts_idx])
                except (TypeError, ValueError):
                    _first = None
                break
        if _first is not None and _first <= 1e12:
            _div = 60.0 if _first > 1e9 else 1.0
        _excess = 0.0
        _prev_ts = None
        for _s in _vals:
            if not isinstance(_s, (list, tuple)) or len(_s) <= max(_hr_idx, _ts_idx):
                continue
            try:
                _h = float(_s[_hr_idx])
                _t = float(_s[_ts_idx])
            except (TypeError, ValueError):
                continue
            if _prev_ts is not None:
                _dt = (_t - _prev_ts) / _div
                if 0 < _dt <= 30:
                    _excess += max(0.0, _h - _rhr) * _dt
            _prev_ts = _t
        _load = min(15.0, _excess / 3000.0)
        if _steps is None or _maxhr is None:
            print("backtest %s samples=%d hr_load=%.2f v2=None v3b=None" % (_date, len(_vals), _load), file=sys.stderr)
            continue
        _v2 = int(round_half_up(min(21, max(0, _steps / 1000.0 + max(0, _maxhr - _rhr - 40) / 10.0))))
        _v3b = int(round_half_up(min(21, _load + min(5, _steps / 2000.0))))
        print("backtest %s samples=%d hr_load=%.2f steps=%s v2=%d v3b=%d" % (_date, len(_vals), _load, _steps, _v2, _v3b), file=sys.stderr)

    return True, days


def extract_steps(summary, movement):
    """Best-effort daily steps. The movement object's shape was never
    confirmed against a live response (the old vault pull never got past
    auth, so its parsing was never validated), so several candidate paths
    are tried in order."""
    if isinstance(movement, dict):
        for key in ("value", "steps", "stepCount", "totalSteps",
                    "dailySteps", "stepTotal", "stepsCount"):
            v = movement.get(key)
            if isinstance(v, (int, float)):
                return v
        nested = movement.get("steps")
        if isinstance(nested, dict):
            v = nested.get("value")
            if isinstance(v, (int, float)):
                return v
    if isinstance(movement, list):
        for item in movement:
            if isinstance(item, dict):
                kind = str(item.get("type") or item.get("typeKey") or "")
                if "step" in kind.lower():
                    v = item.get("value")
                    if isinstance(v, (int, float)):
                        return v
    for key in ("steps", "stepCount", "totalSteps", "dailySteps"):
        v = summary.get(key)
        if isinstance(v, (int, float)):
            return v
    return None


def extract_distance(summary, movement, steps):
    """Best-effort distance in miles. Unknown units are the risk: a daily
    distance over 100 can only be meters, and a large value with almost no
    steps can only be meters. Otherwise assume miles."""
    candidates = [summary.get("distance"), summary.get("totalDistance"),
                  summary.get("distanceInMeters"),
                  summary.get("distanceMeters"),
                  summary.get("totalDistanceInMeters"),
                  summary.get("distanceMiles")]
    if isinstance(movement, dict):
        candidates += [movement.get("distance"),
                       movement.get("distanceInMeters")]
    value = next((v for v in candidates
                  if isinstance(v, (int, float))), None)
    if value is None:
        return None
    if value > 100 or (value > 1 and isinstance(steps, (int, float))
                       and steps < 500):
        return round_half_up(value / 1609.344, 2)
    return round_half_up(float(value), 2)


def _stage_durations(record):
    """Return (deep, light, rem) durations in seconds, or None."""
    triples = [
        # Confirmed from the Oct 31 run's logs (exact field names).
        ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds"),
        ("deepSleepDurationInSeconds", "lightSleepDurationInSeconds",
         "remSleepDurationInSeconds"),
        ("deepSleepDuration", "lightSleepDuration", "remSleepDuration"),
        ("deepDuration", "lightDuration", "remDuration"),
    ]
    for keys in triples:
        vals = [record.get(k) for k in keys]
        if all(isinstance(v, (int, float)) for v in vals):
            if sum(vals) > 100000:  # milliseconds, not seconds
                vals = [v / 1000.0 for v in vals]
            return vals
    for container_key in ("sleepStages", "stages", "sleepLevels"):
        cont = record.get(container_key)
        if isinstance(cont, dict):
            vals = []
            ok = True
            for stage in ("deep", "light", "rem"):
                v = cont.get(stage)
                if isinstance(v, (int, float)):
                    vals.append(v)
                elif isinstance(v, list):
                    total = 0.0
                    for iv in v:
                        if isinstance(iv, dict):
                            d = iv.get("durationInSeconds",
                                       iv.get("duration", 0))
                            if isinstance(d, (int, float)):
                                total += d
                    vals.append(total)
                else:
                    ok = False
                    break
            if ok and sum(vals) > 0:
                if sum(vals) > 100000:
                    vals = [v / 1000.0 for v in vals]
                return vals
    return None


def extract_stages(record):
    durations = _stage_durations(record)
    if not durations:
        return None
    total = sum(durations)
    if total <= 0:
        return None
    return [int(round_half_up(d / total * 100)) for d in durations]


def compute_person(name, dates, rows):
    """Build the board's person object from raw day rows (None-padded)."""
    sleep, recovery, hrv, rhr, steps, stages = [], [], [], [], [], []
    battery, stress, maxhr, dist = [], [], [], []
    hrv_weekly = []

    for date in dates:
        r = rows.get(date.isoformat(), {})
        sleep.append(r.get("sleep"))
        hrv.append(r.get("hrv"))
        hrv_weekly.append(r.get("hrvWeeklyAvg"))
        rhr.append(r.get("restingHR"))
        steps.append(r.get("steps"))
        stages.append(r.get("stages"))
        battery.append(r.get("battery") or [None, None, None])
        stress.append(r.get("stress"))
        maxhr.append(r.get("maxHR"))
        dist.append(r.get("dist"))

    recorded_hrv = [v for v in hrv if v is not None]
    # Baseline is the weekly average as displayed on the board (1 decimal),
    # not the unrounded mean: with the exact mean, Jordan Oct 20 lands on
    # exactly 48.5, but the board says 48, which the 1-decimal baseline
    # (48.48) reproduces.
    mean_hrv = (round_half_up(sum(recorded_hrv) / len(recorded_hrv), 1)
                if recorded_hrv else None)

    for i, date in enumerate(dates):
        if (sleep[i] is None or hrv[i] is None or mean_hrv is None
                or battery[i][1] is None):
            recovery.append(None)
            continue
        component = 50 + (hrv[i] - mean_hrv) * 2
        component = max(0, min(100, component))
        rec = 0.5 * sleep[i] + 0.3 * component + 0.2 * battery[i][1]
        recovery.append(int(round_half_up(rec)))

    def strain(i):
        if steps[i] is None or maxhr[i] is None or rhr[i] is None:
            return None
        val = (steps[i] / 1000.0
               + max(0, maxhr[i] - rhr[i] - 40) / 10.0)
        return int(round_half_up(min(21, max(0, val))))

    def hrv_line(i):
        if hrv[i] is None:
            return "HRV \u2014"
        base = hrv_weekly[i]
        if base is None:
            base = mean_hrv
        if base is None:
            return "HRV %s" % hrv[i]
        return "HRV %s vs own avg %d" % (hrv[i], int(round_half_up(base)))

    def mean_or_zero(vals):
        rec = [v for v in vals if v is not None]
        return (sum(rec) / len(rec)) if rec else 0

    def best(metric_vals):
        best_i, best_v = None, None
        for i, v in enumerate(metric_vals):
            if v is not None and (best_v is None or v > best_v):
                best_i, best_v = i, v
        if best_i is None:
            return "\u2014"
        return "%s (%s)" % (day_label(dates[best_i]), best_v)

    strains = [strain(i) for i in range(len(dates))]
    weekly = {
        "steps": sum(v for v in steps if v is not None),
        "dist": round_half_up(sum(v for v in dist if v is not None), 2),
        "avgSleep": round_half_up(mean_or_zero(sleep), 1),
        "avgRecovery": int(round_half_up(mean_or_zero(recovery))),
        "avgHrv": round_half_up(mean_or_zero(hrv), 1),
        "avgStrain": round_half_up(mean_or_zero(strains), 1),
        "totalStrain": sum(v for v in strains if v is not None),
        "lowRhr": min((v for v in rhr if v is not None), default=999),
        "bestRec": best(recovery),
        "bestSleep": best(sleep),
    }

    return {
        "name": name,
        "sleep": sleep,
        "recovery": recovery,
        "hrv": hrv,
        "rhr": rhr,
        "steps": steps,
        "stages": stages,
        "battery": battery,
        "stress": stress,
        "maxhr": maxhr,
        "dist": dist,
        "strain": strains,
        "strength": [None] * len(dates),
        "strainYesterday": strain(5),
        "strainToday": strain(6),
        "hrvVsOwnYesterday": hrv_line(5),
        "hrvVsOwnToday": hrv_line(6),
        "weekly": weekly,
    }


def fmt(n):
    return "%s" % format(n, ",") if isinstance(n, int) else str(n)


def build_summary(people):
    """Short data-driven weekly summary paragraphs (HTML strings).

    Leader lines compare the leader against the runner-up, so they work
    for any number of people on the board."""
    if len(people) < 2:
        return []
    lines = []

    def top_two(key):
        ranked = sorted(people, key=lambda p: p["weekly"][key], reverse=True)
        return ranked[0], ranked[1]

    leader, runner = top_two("steps")
    if leader["weekly"]["steps"] != runner["weekly"]["steps"]:
        ratio = ""
        if runner["weekly"]["steps"] > 0:
            ratio = " (%s\u00d7)" % round_half_up(
                leader["weekly"]["steps"] / runner["weekly"]["steps"], 1)
        lines.append("<b>%s leads steps</b> &mdash; %s vs %s this week%s."
                     % (leader["name"], fmt(leader["weekly"]["steps"]),
                        fmt(runner["weekly"]["steps"]), ratio))
    leader, runner = top_two("avgSleep")
    if leader["weekly"]["avgSleep"] != runner["weekly"]["avgSleep"]:
        lines.append("<b>%s leads sleep</b> &mdash; avg %s vs %s."
                     % (leader["name"], leader["weekly"]["avgSleep"],
                        runner["weekly"]["avgSleep"]))
    leader, runner = top_two("avgRecovery")
    if leader["weekly"]["avgRecovery"] != runner["weekly"]["avgRecovery"]:
        lines.append("<b>%s leads recovery</b> &mdash; avg %s vs %s."
                     % (leader["name"], leader["weekly"]["avgRecovery"],
                        runner["weekly"]["avgRecovery"]))
    daily = [p for p in people if p["recovery"][5] is not None]
    if daily:
        daily_leader = max(daily, key=lambda p: p["recovery"][5])
        weekly_leader = max(people,
                            key=lambda p: p["weekly"]["avgRecovery"])
        lines.append("<b>Bottom line</b> &mdash; %s leads the last full day; "
                     "%s leads the week."
                     % (daily_leader["name"], weekly_leader["name"]))
    return lines


def main():
    today_pt = datetime.datetime.now(PT).date()
    dates = [today_pt - datetime.timedelta(days=6 - i) for i in range(7)]
    start, end = dates[0].isoformat(), dates[-1].isoformat()

    people = []
    for name, env_var in PEOPLE:
        api_key = os.environ.get(env_var, "").strip()
        if not api_key:
            print("missing secret %s" % env_var, file=sys.stderr)
            return 2
        ok, rows = fetch_person(api_key, start, end)
        if not ok:
            print("fetch failed for %s, publishing nothing" % name,
                  file=sys.stderr)
            return 1
        recorded = {
            "sleep": sum(1 for d in dates if rows.get(d.isoformat(), {}).get("sleep") is not None),
            "hrv": sum(1 for d in dates if rows.get(d.isoformat(), {}).get("hrv") is not None),
            "steps": sum(1 for d in dates if rows.get(d.isoformat(), {}).get("steps") is not None),
        }
        print("%s: %d/7 sleep, %d/7 hrv, %d/7 steps"
              % (name, recorded["sleep"], recorded["hrv"], recorded["steps"]),
              file=sys.stderr)
        people.append(compute_person(name, dates, rows))

    now_pt = datetime.datetime.now(PT)
    hour12 = now_pt.hour % 12 or 12
    updated_time = "%d:%02d %s %s" % (
        hour12, now_pt.minute,
        "AM" if now_pt.hour < 12 else "PM",
        now_pt.strftime("%Z"))
    updated_long = "%s %d, %s" % (
        MONTHS[now_pt.month - 1], now_pt.day, updated_time)

    board = {
        "meta": {
            "startDate": start,
            "endDate": end,
            "days": [day_label(d) for d in dates],
            "updatedAt": datetime.datetime.now(datetime.timezone.utc)
                         .isoformat().replace("+00:00", "Z"),
            "updatedTimePT": updated_time,
            "updatedLongPT": updated_long,
            "live": True,
        },
        "days": [day_label(d) for d in dates],
        "people": [{k: v for k, v in p.items() if not k.startswith("_")}
                   for p in people],
        "summary": build_summary(people),
    }

    tmp_path = "data.js.tmp"
    with open(tmp_path, "w") as f:
        f.write("window.BOARD_DATA = ")
        json.dump(board, f, indent=1)
        f.write(";\n")
    os.replace(tmp_path, "data.js")
    print("wrote data.js for %s to %s" % (start, end), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
