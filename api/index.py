# =========================================================
#  TELEGRAM STRONG-DRAW PREDICTOR BOT
#  Provider : API-Sports (v3.football.api-sports.io)
#  Runtime  : Render Web Service — Polling + Flask keepalive
# =========================================================

import os
import math
import time
import asyncio
import threading
import requests
from datetime import datetime

from flask import Flask, request as flask_request
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes


# ---------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
API_FOOTBALL_KEY  = os.getenv("API_FOOTBALL_KEY") or os.getenv("RAPIDAPI_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is not set.")
if not API_FOOTBALL_KEY:
    raise RuntimeError("API_FOOTBALL_KEY environment variable is not set.")

BASE_URL = "https://v3.football.api-sports.io"
HEADERS  = {"x-apisports-key": API_FOOTBALL_KEY}


# ---------------------------------------------------------
# 2. ALLOWED LEAGUES — Self-Healing Whitelist
# ---------------------------------------------------------
# Define leagues by (country, name) profile instead of by hard-coded ID.
# At startup, verify_and_fix_league_ids() calls /leagues once and
# resolves the IDs against the live API catalog. If the API renumbers
# leagues for a new season, the bot self-corrects with no code change.
#
# Each profile:
#   country      : country name as published by API-Sports
#   name         : canonical league name
#   aliases      : alternative names the API may use
#   suppression  : True for low-scoring leagues that get the +2 bonus

TARGET_LEAGUES = [
    # ----- Italy
    {"country": "Italy",        "name": "Serie B",                 "aliases": []},
    {"country": "Italy",        "name": "Serie C",                 "aliases": ["Serie C - Girone A", "Serie C Group A"]},

    # ----- Spain
    {"country": "Spain",        "name": "Segunda División",        "aliases": ["Segunda Division", "LaLiga 2", "La Liga 2"]},
    {"country": "Spain",        "name": "Primera RFEF",            "aliases": ["Primera Federacion", "Primera División RFEF"]},
    {"country": "Spain",        "name": "Segunda RFEF",            "aliases": ["Segunda Federacion", "Segunda División RFEF"]},

    # ----- France
    {"country": "France",       "name": "Ligue 2",                 "aliases": []},
    {"country": "France",       "name": "National 1",              "aliases": ["National", "Championnat National"]},

    # ----- England
    {"country": "England",      "name": "League One",              "aliases": ["EFL League One"]},
    {"country": "England",      "name": "League Two",              "aliases": ["EFL League Two"]},

    # ----- Germany
    {"country": "Germany",      "name": "3. Liga",                 "aliases": ["3 Liga", "Liga 3"]},

    # ----- Netherlands
    {"country": "Netherlands",  "name": "Eerste Divisie",          "aliases": ["Keuken Kampioen Divisie"]},

    # ----- Portugal
    {"country": "Portugal",     "name": "Liga Portugal 2",         "aliases": ["Segunda Liga", "Liga 2 Portugal", "LigaPro"]},

    # ----- Belgium
    {"country": "Belgium",      "name": "Challenger Pro League",   "aliases": ["First Division B", "1B Pro League"]},

    # ----- Austria
    {"country": "Austria",      "name": "2. Liga",                 "aliases": ["Erste Liga", "Admiral 2. Liga"]},

    # ----- Switzerland
    {"country": "Switzerland",  "name": "Challenge League",        "aliases": ["Brack.ch Challenge League"]},

    # ----- Scandinavia / Nordics
    {"country": "Sweden",       "name": "Superettan",              "aliases": []},
    {"country": "Norway",       "name": "OBOS-ligaen",             "aliases": ["OBOS Ligaen", "1. divisjon", "Eliteserien 2"]},
    {"country": "Denmark",      "name": "1. Division",             "aliases": ["NordicBet Liga", "Division 1"]},
    {"country": "Finland",      "name": "Ykkönen",                 "aliases": ["Ykkonen", "Ykkösliiga"]},

    # ----- Eastern Europe
    {"country": "Romania",      "name": "Liga II",                 "aliases": ["Liga 2"]},
    {"country": "Poland",       "name": "I Liga",                  "aliases": ["1 Liga", "Fortuna 1 Liga"]},
    {"country": "Czech-Republic","name": "FNL",                    "aliases": ["Fortuna Národní Liga", "FORTUNA:LIGA 2", "Czech Liga 2"]},
    {"country": "Turkey",       "name": "1. Lig",                  "aliases": ["TFF First League", "1 Lig"]},
    {"country": "Greece",       "name": "Super League 2",          "aliases": ["Super League 2 - Group A"]},
    {"country": "Hungary",      "name": "NB II",                   "aliases": ["NB 2", "Nemzeti Bajnokság II"]},

    # ----- South America
    {"country": "Argentina",    "name": "Primera Nacional",        "aliases": ["Primera B Nacional"]},
    {"country": "Paraguay",     "name": "Division Intermedia",     "aliases": ["División Intermedia"]},
    {"country": "Uruguay",      "name": "Segunda División",        "aliases": ["Segunda Division", "Segunda División Profesional"]},
    {"country": "Colombia",     "name": "Primera B",               "aliases": ["Torneo BetPlay"]},
    {"country": "Chile",        "name": "Primera B",               "aliases": ["Campeonato Ascenso"]},
    {"country": "Peru",         "name": "Liga 2",                  "aliases": ["Segunda División"]},

    # ----- Brazil
    {"country": "Brazil",       "name": "Serie B",                 "aliases": ["Brasileirão Série B"]},
    {"country": "Brazil",       "name": "Serie C",                 "aliases": ["Brasileirão Série C"]},

    # ----- Suppression leagues (low-scoring → +2 bonus, top-of-list priority)
    {"country": "Iran",         "name": "Persian Gulf Pro League", "aliases": ["Iran Pro League", "Pro League"], "suppression": True},
    {"country": "South-Africa", "name": "Premier Soccer League",   "aliases": ["PSL", "DSTV Premiership", "Premiership"], "suppression": True},
    {"country": "Morocco",      "name": "Botola Pro",              "aliases": ["Botola Pro Inwi", "Botola"], "suppression": True},
    {"country": "Algeria",      "name": "Ligue 1",                 "aliases": ["Ligue Professionnelle 1", "Ligue Professionnelle"], "suppression": True},
]


# Hard-coded fallback used only if the live /leagues call fails
# (e.g. API key suspended, network down). Keeps the bot bootable.
FALLBACK_LEAGUES = {
    137: "Serie B", 72: "Serie C",
    141: "Segunda Division", 138: "Primera RFEF", 142: "Segunda RFEF",
    65: "Ligue 2", 66: "National",
    41: "League One", 42: "League Two",
    78: "3. Liga",
    89: "Eerste Divisie",
    94: "Liga Portugal 2",
    144: "Challenger Pro League",
    218: "2. Liga",
    207: "Challenge League",
    113: "Superettan", 104: "OBOS Ligaen", 119: "Division 1", 244: "Ykkonen",
    284: "Liga II Romania", 106: "I Liga Poland", 345: "FNL Czech",
    203: "1. Lig Turkey", 210: "Super League 2 Greece", 271: "NB II Hungary",
    128: "Primera Nacional Argentina", 289: "Division Intermedia Paraguay",
    292: "Segunda Uruguay", 239: "Primera B Colombia", 266: "Primera B Chile",
    281: "Liga 2 Peru",
    71: "Serie B Brazil", 73: "Serie C Brazil",
    290: "Iran Pro League", 288: "South Africa PSL",
    200: "Morocco Botola Pro", 188: "Algeria Ligue 1",
}
FALLBACK_SUPPRESSION = {290, 288, 200, 188}


# Populated by verify_and_fix_league_ids() at startup
ALLOWED_LEAGUES     = {}
SUPPRESSION_LEAGUES = set()


def _norm(s):
    """Lowercase + strip diacritics + collapse punctuation for fuzzy match."""
    if not s:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c.lower() for c in s if c.isalnum() or c.isspace()).strip()


def _league_matches(api_name, target_name, aliases):
    """Match API league name against target + aliases (normalized)."""
    api_n = _norm(api_name)
    candidates = [target_name] + (aliases or [])
    for cand in candidates:
        cand_n = _norm(cand)
        if api_n == cand_n:
            return True
    return False


def verify_and_fix_league_ids(targets):
    """Resolve TARGET_LEAGUES → {id: name} via a single /leagues call.

    Returns (allowed_dict, suppression_set, report_list).
    Falls back to the hard-coded baseline if the API is unreachable.
    """
    print("[INIT] Verifying league IDs against live API catalog...")
    data = _api_get("/leagues", {"current": "true"})

    if not data or not data.get("response"):
        print("[INIT] ⚠️  /leagues call failed — using hard-coded fallback whitelist.")
        return dict(FALLBACK_LEAGUES), set(FALLBACK_SUPPRESSION), [
            {"status": "FALLBACK", "reason": "API unreachable"}
        ]

    api_leagues = data["response"]   # list of {league:{id,name,type}, country:{name}}
    allowed = {}
    suppression = set()
    report = []

    # Index API leagues by normalized country for fast per-country lookup
    by_country = {}
    for entry in api_leagues:
        lg = entry.get("league") or {}
        ct = entry.get("country") or {}
        if lg.get("type") != "League":   # skip Cups, Friendlies, etc.
            continue
        ck = _norm(ct.get("name"))
        by_country.setdefault(ck, []).append({
            "id":   lg.get("id"),
            "name": lg.get("name"),
            "country": ct.get("name"),
        })

    for tgt in targets:
        country_n = _norm(tgt["country"])
        candidates = by_country.get(country_n, [])
        # Some API countries use hyphens vs spaces ("South-Africa" vs "South Africa")
        if not candidates:
            country_alt = _norm(tgt["country"].replace("-", " "))
            candidates = by_country.get(country_alt, [])

        match = next(
            (c for c in candidates
             if _league_matches(c["name"], tgt["name"], tgt.get("aliases", []))),
            None,
        )

        if match:
            allowed[match["id"]] = match["name"]
            if tgt.get("suppression"):
                suppression.add(match["id"])
            print(f"[INIT] ✅  {tgt['country']:<14} {tgt['name']:<32} → ID {match['id']}")
            report.append({
                "status":  "OK",
                "country": tgt["country"],
                "target":  tgt["name"],
                "api_name": match["name"],
                "id":      match["id"],
                "suppression": bool(tgt.get("suppression")),
            })
        else:
            print(f"[INIT] ⚠️   {tgt['country']:<14} {tgt['name']:<32} → NOT FOUND")
            report.append({
                "status":  "NOT_FOUND",
                "country": tgt["country"],
                "target":  tgt["name"],
                "candidates": [c["name"] for c in candidates][:5],
            })

    print(f"[INIT] Whitelist resolved: {len(allowed)} leagues "
          f"({len(suppression)} suppression).")
    return allowed, suppression, report


def _bootstrap_whitelist():
    """Run once at import. Safe under cold-start (Vercel) and local dev."""
    global ALLOWED_LEAGUES, SUPPRESSION_LEAGUES, _LAST_VERIFY_REPORT
    try:
        allowed, suppression, report = verify_and_fix_league_ids(TARGET_LEAGUES)
        if not allowed:                                  # all targets failed
            allowed     = dict(FALLBACK_LEAGUES)
            suppression = set(FALLBACK_SUPPRESSION)
            report.append({"status": "FALLBACK", "reason": "no targets resolved"})
        ALLOWED_LEAGUES.update(allowed)
        SUPPRESSION_LEAGUES.update(suppression)
        _LAST_VERIFY_REPORT = report
    except Exception as e:
        print(f"[INIT] ❌  Whitelist bootstrap crashed: {e} — using fallback.")
        ALLOWED_LEAGUES.update(FALLBACK_LEAGUES)
        SUPPRESSION_LEAGUES.update(FALLBACK_SUPPRESSION)
        _LAST_VERIFY_REPORT = [{"status": "FALLBACK", "reason": str(e)}]


_LAST_VERIFY_REPORT = []   # populated by _bootstrap_whitelist (called below)


# ---------------------------------------------------------
# 3. CACHING (12-hour TTL + manual daily reset)
# ---------------------------------------------------------

CACHE_TTL = 12 * 60 * 60   # 12 hours

standings_cache = {}   # key: f"{league_id}_{season}"
form_cache      = {}   # key: team_id
h2h_cache       = {}   # key: f"{home_id}_{away_id}"

daily_results = None   # last analysis output


def _cache_get(cache, key):
    entry = cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > CACHE_TTL:
        cache.pop(key, None)
        return None
    return value


def _cache_set(cache, key, value):
    cache[key] = (time.time(), value)


def reset_all_caches():
    standings_cache.clear()
    form_cache.clear()
    h2h_cache.clear()
    print("[INFO] All caches cleared (daily reset).")


# ---------------------------------------------------------
# 4. API REQUEST HELPER (retries + global throttle)
# ---------------------------------------------------------

MIN_API_GAP = 0.4                 # minimum spacing between any two requests
_throttle_lock = threading.Lock()
_last_api_call_time = 0.0


def _throttle():
    """Enforce a minimum gap between successive API calls (thread-safe)."""
    global _last_api_call_time
    with _throttle_lock:
        elapsed = time.time() - _last_api_call_time
        if elapsed < MIN_API_GAP:
            time.sleep(MIN_API_GAP - elapsed)
        _last_api_call_time = time.time()


def _api_get(path, params=None, timeout=10, retries=2, retry_delay=0.6):
    """All API-Sports GET requests go through here.
       Throttled (≥0.4s gap), retried on transient errors."""
    last_err = None
    for attempt in range(retries + 1):
        _throttle()
        try:
            r = requests.get(
                f"{BASE_URL}{path}",
                headers=HEADERS,
                params=params or {},
                timeout=timeout,
            )
            if 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code}"
                print(f"[API RETRY] {path} -> {last_err} (attempt {attempt + 1})")
                time.sleep(retry_delay)
                continue
            if r.status_code != 200:
                print(f"[API ERROR] {r.status_code} for {path} | params={params}")
                return None
            data = r.json()
            if data.get("errors"):
                print(f"[API ERROR-FIELD] {path} -> {data.get('errors')}")
            return data
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = type(e).__name__
            print(f"[API RETRY] {path} -> {last_err} (attempt {attempt + 1})")
            time.sleep(retry_delay)
            continue
        except Exception as e:
            print(f"[API ERROR] {path}: {e}")
            return None
    print(f"[API GIVE-UP] {path} after retries (last: {last_err})")
    return None


# Bootstrap the self-healing whitelist now that _api_get exists.
# Runs once per process (cold-start on Vercel, on import locally).
_bootstrap_whitelist()


# ---------------------------------------------------------
# 5. FIXTURES / STANDINGS / FORM / H2H
# ---------------------------------------------------------

def get_matches_by_date(date_str):
    print(f"[INFO] Fetching fixtures for {date_str}")
    data = _api_get("/fixtures", {"date": date_str})
    if not data:
        return []
    raw = data.get("response", []) or []
    league_ids = sorted({m.get("league", {}).get("id") for m in raw if m.get("league")})
    filtered = [m for m in raw if m.get("league", {}).get("id") in ALLOWED_LEAGUES]
    print(f"[INFO] Raw fixtures: {len(raw)}")
    print(f"[INFO] Filtered fixtures: {len(filtered)}")
    print(f"[INFO] League IDs detected (sample): {league_ids[:25]}")
    if len(filtered) < 40:
        print("WARNING: Too few matches after filtering.")
    return filtered


def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return get_matches_by_date(today)


def get_standings(league_id, season):
    """Fetched once per (league, season) per cache window."""
    key = f"{league_id}_{season}"
    cached = _cache_get(standings_cache, key)
    if cached is not None:
        return cached

    data = _api_get("/standings", {"league": league_id, "season": season})
    if not data:
        return None
    groups = data.get("response", []) or []
    if not groups:
        return None
    try:
        standings = groups[0]["league"]["standings"][0]
    except (KeyError, IndexError, TypeError):
        return None

    result = {}
    for team in standings:
        try:
            tid = team["team"]["id"]
            result[tid] = {
                "rank":          team.get("rank", 99),
                "played":        team["all"]["played"],
                "draws":         team["all"]["draw"],
                "wins":          team["all"]["win"],
                "losses":        team["all"]["lose"],
                "goals_for":     team["all"]["goals"]["for"],
                "goals_against": team["all"]["goals"]["against"],
                "goal_diff":     team.get("goalsDiff", 0),
                # Home/Away splits — needed by the Advanced Stalemate brain
                "home_played":   team.get("home", {}).get("played", 0),
                "home_draws":    team.get("home", {}).get("draw",   0),
                "away_played":   team.get("away", {}).get("played", 0),
                "away_draws":    team.get("away", {}).get("draw",   0),
            }
        except (KeyError, TypeError):
            continue

    _cache_set(standings_cache, key, result)
    return result


def get_recent_form(team_id, league_id, season):
    """Last-5 results list ['W','D','L',...]. Cached per team_id."""
    cached = _cache_get(form_cache, team_id)
    if cached is not None:
        return cached

    data = _api_get("/fixtures", {
        "team":   team_id,
        "league": league_id,
        "season": season,
        "last":   5,
    })
    if not data:
        _cache_set(form_cache, team_id, [])
        return []

    fixtures = data.get("response", []) or []
    results = []
    for game in fixtures:
        try:
            hg = game["goals"]["home"]
            ag = game["goals"]["away"]
            if hg is None or ag is None:
                continue
            home_id = game["teams"]["home"]["id"]
            if hg == ag:
                results.append("D")
            elif hg > ag:
                results.append("W" if team_id == home_id else "L")
            else:
                results.append("L" if team_id == home_id else "W")
        except (KeyError, TypeError):
            continue

    _cache_set(form_cache, team_id, results)
    return results


def get_h2h(home_id, away_id):
    """{'total': n, 'draw_rate': r, 'over15_rate': r, 'under35_rate': r, 'btts_rate': r}.
       Cache-checked by callers — this function still caches on its own as a safety net."""
    key = f"{home_id}_{away_id}"
    cached = _cache_get(h2h_cache, key)
    if cached is not None:
        return cached

    data = _api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 5})
    if not data:
        return None
    fixtures = data.get("response", []) or []
    total = len(fixtures)
    draws = over15 = under35 = btts = 0
    for g in fixtures:
        try:
            hg = g["goals"]["home"]
            ag = g["goals"]["away"]
            if hg is None or ag is None:
                continue
            tot = hg + ag
            if hg == ag:           draws  += 1
            if tot >= 2:           over15 += 1
            if tot <= 3:           under35 += 1
            if hg >= 1 and ag >= 1: btts  += 1
        except (KeyError, TypeError):
            continue
    if total == 0:
        result = {"total": 0, "draw_rate": 0.0, "over15_rate": 0.0,
                  "under35_rate": 0.0, "btts_rate": 0.0}
    else:
        result = {
            "total":        total,
            "draw_rate":    draws  / total,
            "over15_rate":  over15 / total,
            "under35_rate": under35 / total,
            "btts_rate":    btts   / total,
        }
    _cache_set(h2h_cache, key, result)
    return result


# ---------------------------------------------------------
# 6. DRAW HARD FILTERS  (H2H is NOT a hard filter)
# ---------------------------------------------------------

def passes_draw_filters(home, away, form_h, form_a):
    """Hard Gates — Advanced Stalemate brain."""
    if not home or not away:
        return False

    # 1. PPG (Points Per Game) Parity Gate
    ppg_h = (home["wins"] * 3 + home["draws"]) / max(home["played"], 1)
    ppg_a = (away["wins"] * 3 + away["draws"]) / max(away["played"], 1)
    if abs(ppg_h - ppg_a) > 0.75:
        return False  # Too big of a quality gap

    # 2. Total Goal "Chaos" Gate
    avg_h = (home["goals_for"] + home["goals_against"]) / max(home["played"], 1)
    avg_a = (away["goals_for"] + away["goals_against"]) / max(away["played"], 1)
    if (avg_h + avg_a) / 2 > 3.0:
        return False  # Exclude high-scoring/unstable games

    return True


# ---------------------------------------------------------
# 7. SCORING — DRAW MARKET (base score, then conditional H2H)
# ---------------------------------------------------------

def score_draw_advanced(home, away, form_h, form_a, league_id=None):
    """Advanced Stalemate — weighted scoring (10 pts + optional league bonus)."""
    score = 0

    # Parity (3 pts) — PPG difference < 0.3
    ppg_h = (home["wins"] * 3 + home["draws"]) / max(home["played"], 1)
    ppg_a = (away["wins"] * 3 + away["draws"]) / max(away["played"], 1)
    if abs(ppg_h - ppg_a) < 0.3:
        score += 3

    # Low-Score Multiplier (3 pts) — combined avg goals per game < 2.4
    avg_h = (home["goals_for"] + home["goals_against"]) / max(home["played"], 1)
    avg_a = (away["goals_for"] + away["goals_against"]) / max(away["played"], 1)
    if (avg_h + avg_a) / 2 < 2.4:
        score += 3

    # Away Draw Specialist (2 pts) — away_draws / away_played > 30%
    if away.get("away_played", 0) > 0:
        if (away["away_draws"] / away["away_played"]) > 0.30:
            score += 2

    # Form Symmetry (2 pts) — both teams ≥ 2 draws in last 5
    if form_h and form_a and form_h.count("D") >= 2 and form_a.count("D") >= 2:
        score += 2

    # League-Specific Suppression Bonus (+2) — tactical low-scoring leagues
    if league_id in SUPPRESSION_LEAGUES:
        score += 2

    return score


# Back-compat alias so other scorers (e.g. score_all_markets) keep working.
score_draw_base = score_draw_advanced


def calculate_draw_symmetry(home_stats, away_stats):
    """
    Analyzes the 'Cancellation Effect' between home and away styles.
    Returns a score from 0-5.
    """
    sym_score = 0

    # A. Goal Expectancy Symmetry (The 1-1 or 0-0 Predictor)
    # If Home Scored ~= Away Conceded AND Away Scored ~= Home Conceded
    home_attack  = home_stats['goals_for']     / max(home_stats['played'], 1)
    away_defense = away_stats['goals_against'] / max(away_stats['played'], 1)

    away_attack  = away_stats['goals_for']     / max(away_stats['played'], 1)
    home_defense = home_stats['goals_against'] / max(home_stats['played'], 1)

    if abs(home_attack - away_defense) < 0.2 and abs(away_attack - home_defense) < 0.2:
        sym_score += 3  # High tactical symmetry

    # B. The 'Unstoppable Object vs Immovable Post'
    # Home team doesn't win much, Away team doesn't lose much
    home_win_rate  = home_stats['wins']   / max(home_stats['played'], 1)
    away_loss_rate = away_stats['losses'] / max(away_stats['played'], 1)

    if home_win_rate < 0.35 and away_loss_rate < 0.35:
        sym_score += 2

    return sym_score


# ---------------------------------------------------------
# 8. SCORING — ALTERNATIVE MARKETS (for accumulator fallback)
# ---------------------------------------------------------

def _avg_goals_per_game(team):
    p = max(team["played"], 1)
    return (team["goals_for"] + team["goals_against"]) / p


def _scoring_rate(team):
    p = max(team["played"], 1)
    return team["goals_for"] / p


def _conceding_rate(team):
    p = max(team["played"], 1)
    return team["goals_against"] / p


def _scored_in_rate(team):
    """Proxy for 'matches in which team scored at least 1 goal'.
       Uses a Poisson-style mapping from goals/game (1 - e^(-λ))."""
    p = max(team["played"], 1)
    lam = team["goals_for"] / p
    return 1 - math.exp(-lam)


def _clean_sheet_rate(team):
    """Proxy for 'clean sheet rate' from goals-against per game (e^(-λ))."""
    p = max(team["played"], 1)
    lam = team["goals_against"] / p
    return math.exp(-lam)


def score_double_chance(home, away, form_h, form_a, h2h):
    score = 0
    stronger = home if home["rank"] <= away["rank"] else away
    weaker   = away if stronger is home else home
    loss_rate = stronger["losses"] / max(stronger["played"], 1)
    if loss_rate <= 0.20:
        score += 4
    elif loss_rate <= 0.30:
        score += 3
    elif loss_rate <= 0.40:
        score += 2

    f = form_h if stronger is home else form_a
    nondefeats = f.count("W") + f.count("D")
    if nondefeats >= 4: score += 3
    elif nondefeats >= 3: score += 2

    if abs(home["rank"] - away["rank"]) <= 5:
        score += 2

    if h2h and h2h["total"] >= 2:
        score += 1
    return score


def score_over_15(home, away, form_h, form_a, h2h):
    score = 0
    avg_h = _avg_goals_per_game(home)
    avg_a = _avg_goals_per_game(away)
    combined = avg_h + avg_a
    if combined >= 3.2:   score += 5
    elif combined >= 2.8: score += 4
    elif combined >= 2.4: score += 3
    elif combined >= 2.0: score += 2

    if _scoring_rate(home) >= 1.0 and _scoring_rate(away) >= 1.0:
        score += 2
    elif _scoring_rate(home) >= 0.8 or _scoring_rate(away) >= 0.8:
        score += 1

    # Market-specific weight: both teams score in 75%+ of their matches
    if _scored_in_rate(home) >= 0.75 and _scored_in_rate(away) >= 0.75:
        score += 3

    if h2h and h2h["over15_rate"] >= 0.80:
        score += 3
    elif h2h and h2h["over15_rate"] >= 0.60:
        score += 2
    return score


def score_under_35(home, away, form_h, form_a, h2h):
    score = 0
    avg_h = _avg_goals_per_game(home)
    avg_a = _avg_goals_per_game(away)
    combined = avg_h + avg_a
    if combined <= 2.2:   score += 5
    elif combined <= 2.5: score += 4
    elif combined <= 2.8: score += 3
    elif combined <= 3.0: score += 2

    if _conceding_rate(home) <= 1.1 and _conceding_rate(away) <= 1.1:
        score += 2
    elif _conceding_rate(home) <= 1.3 or _conceding_rate(away) <= 1.3:
        score += 1

    # Market-specific weight: combined clean sheet rate > 40%
    combined_cs = (_clean_sheet_rate(home) + _clean_sheet_rate(away)) / 2
    if combined_cs > 0.40:
        score += 3

    if h2h and h2h["under35_rate"] >= 0.80:
        score += 3
    elif h2h and h2h["under35_rate"] >= 0.60:
        score += 2
    return score


def score_btts_yes(home, away, form_h, form_a, h2h):
    # Hard gate: only suggest if both teams' goal-diff sits in [-5, +5]
    # (high parity, competitive scoring).
    if not (-5 <= home["goal_diff"] <= 5) or not (-5 <= away["goal_diff"] <= 5):
        return 0

    score = 0
    sh = _scoring_rate(home)
    sa = _scoring_rate(away)
    ch = _conceding_rate(home)
    ca = _conceding_rate(away)

    if sh >= 1.2 and sa >= 1.2:   score += 4
    elif sh >= 1.0 and sa >= 1.0: score += 3
    elif sh >= 0.8 and sa >= 0.8: score += 2

    if ch >= 1.0 and ca >= 1.0:   score += 3
    elif ch >= 0.8 and ca >= 0.8: score += 2

    if h2h and h2h["btts_rate"] >= 0.80:
        score += 3
    elif h2h and h2h["btts_rate"] >= 0.60:
        score += 2
    return score


def score_draw_no_bet(home, away, form_h, form_a, h2h):
    stronger = home if home["rank"] <= away["rank"] else away
    weaker   = away if stronger is home else home

    # Hard gate: only suggest DNB on a side with loss-rate < 20% AND draw-rate > 25%
    s_played = max(stronger["played"], 1)
    s_loss_rate = stronger["losses"] / s_played
    s_draw_rate = stronger["draws"]  / s_played
    if not (s_loss_rate < 0.20 and s_draw_rate > 0.25):
        return 0

    score = 0
    win_rate = stronger["wins"] / s_played
    if win_rate >= 0.50:   score += 4
    elif win_rate >= 0.40: score += 3
    elif win_rate >= 0.33: score += 2

    f = form_h if stronger is home else form_a
    if f.count("W") >= 3:   score += 3
    elif f.count("W") >= 2: score += 2

    if (stronger["goal_diff"] - weaker["goal_diff"]) >= 5:
        score += 2
    elif (stronger["goal_diff"] - weaker["goal_diff"]) >= 2:
        score += 1

    if h2h and h2h["total"] >= 2:
        score += 1
    return score


def score_all_markets(home, away, form_h, form_a, h2h):
    return {
        "Draw":           score_draw_base(home, away, form_h, form_a) + (
                              2 if (h2h and h2h.get("draw_rate", 0) >= 0.30) else 0),
        "Double Chance":  score_double_chance(home, away, form_h, form_a, h2h),
        "Over 1.5":       score_over_15(home, away, form_h, form_a, h2h),
        "Under 3.5":      score_under_35(home, away, form_h, form_a, h2h),
        "BTTS Yes":       score_btts_yes(home, away, form_h, form_a, h2h),
        "Draw No Bet":    score_draw_no_bet(home, away, form_h, form_a, h2h),
    }


# ---------------------------------------------------------
# 9. OUTPUT FORMATTING
# ---------------------------------------------------------

def _draw_line(n, c):
    lock = "🔒 LOCK DRAW — " if c.get("is_lock") else ""
    sym  = c.get("sym", 0)
    return (f"\n{n}) {lock}{c['match']}\n"
            f"   🏆 {c['league']}\n"
            f"   ⭐ Score: {c['score']}/15  (symmetry +{sym})\n")


def format_draw_results(strong, backup):
    if not strong and not backup:
        return None
    out = "🎯 Strong Draw Picks\n"
    n = 0
    for c in strong:
        n += 1
        out += _draw_line(n, c)
    if backup:
        out += "\n────────────────────\n\n📌 Backup Picks\n"
        for c in backup:
            n += 1
            out += _draw_line(n, c)
    return out.strip()


def format_accumulator(picks, reserves):
    if not picks:
        return "⚽ No accumulator picks could be built today."
    out = "🎯 ACCUMULATOR PICKS\n"
    for i, p in enumerate(picks, 1):
        out += (f"\n{i}) {p['match']}\n"
                f"   🏆 {p['league']}\n"
                f"   📈 Market: {p['market']}\n"
                f"   ⭐ Confidence: {p['score']}/10\n")
    if reserves:
        out += "\n────────────────────\n\n🛟 Reserve Picks\n"
        for j, p in enumerate(reserves, len(picks) + 1):
            out += (f"\n{j}) {p['match']}\n"
                    f"   🏆 {p['league']}\n"
                    f"   📈 Market: {p['market']}\n"
                    f"   ⭐ Confidence: {p['score']}/10\n")
    return out.strip()


# ---------------------------------------------------------
# 10. MAIN ANALYSIS — DRAW MODE first, ACCUMULATOR fallback
# ---------------------------------------------------------

def run_analysis():
    """Pipeline: scan fixtures → score draw → if no draws, build accumulator."""
    global daily_results
    print("[INFO] Running draw analysis...")

    matches = get_today_matches()

    strong_draws = []
    backup_draws = []
    enriched     = []                 # for accumulator fallback
    seen_draws   = set()              # dedupe across draw lists

    for game in matches:
        try:
            league = game.get("league", {})
            league_id   = league.get("id")
            # FIX 4 — season auto-fallback
            season      = league.get("season") or datetime.utcnow().year
            league_name = ALLOWED_LEAGUES.get(league_id, league.get("name", "Unknown"))

            if league_id not in ALLOWED_LEAGUES:
                continue

            home_id   = game["teams"]["home"]["id"]
            away_id   = game["teams"]["away"]["id"]
            home_name = game["teams"]["home"]["name"]
            away_name = game["teams"]["away"]["name"]
            match_label = f"{home_name} vs {away_name}"

            standings = get_standings(league_id, season)
            if not standings:
                continue
            home = standings.get(home_id)
            away = standings.get(away_id)
            if not home or not away:
                continue

            form_h = get_recent_form(home_id, league_id, season)
            form_a = get_recent_form(away_id, league_id, season)

            # ---- DRAW PATH ----
            draw_score = 0
            h2h = None
            sym_score = 0
            is_lock = False
            if passes_draw_filters(home, away, form_h, form_a):
                base = score_draw_advanced(home, away, form_h, form_a, league_id)

                # FIX 2 — only call H2H if base ≥ 5 AND not already cached
                if base >= 5:
                    h2h_key = f"{home_id}_{away_id}"
                    cached_h2h = _cache_get(h2h_cache, h2h_key)
                    if cached_h2h is not None:
                        h2h = cached_h2h
                    else:
                        h2h = get_h2h(home_id, away_id)
                    if h2h and h2h.get("draw_rate", 0) >= 0.30:
                        base += 2

                # Stalemate Symmetry — extra 'Draw Awareness' layer (0-5)
                sym_score = calculate_draw_symmetry(home, away)
                draw_score = base + sym_score

                # Golden pick rule: advanced score (post-H2H) ≥7 AND symmetry ≥4
                is_lock = (base >= 7) and (sym_score >= 4)

            entry = {
                "match":     match_label,
                "league":    league_name,
                "league_id": league_id,
                "score":     draw_score,
                "sym":       sym_score,
                "is_lock":   is_lock,
            }
            if match_label not in seen_draws:
                if draw_score >= 8:
                    strong_draws.append(entry)
                    seen_draws.add(match_label)
                elif draw_score in (6, 7):
                    backup_draws.append(entry)
                    seen_draws.add(match_label)

            # Stash enriched data for fallback accumulator (no extra API calls)
            enriched.append({
                "home": home, "away": away,
                "form_h": form_h, "form_a": form_a,
                "h2h": h2h,                         # may be None
                "label": match_label,
                "league": league_name,
            })

        except Exception as e:
            print(f"[ERROR] Skipping match: {e}")
            continue

    # Sort priority for Strong Draws:
    #   1. Suppression-league picks with high symmetry (sym ≥ 4) bubble to the top
    #   2. Then by total score
    #   3. Then by symmetry score (tiebreaker)
    def _strong_key(x):
        priority = 1 if (x.get("league_id") in SUPPRESSION_LEAGUES
                         and x.get("sym", 0) >= 4) else 0
        return (priority, x["score"], x.get("sym", 0))

    strong_draws.sort(key=_strong_key, reverse=True)
    backup_draws.sort(key=lambda x: x["score"], reverse=True)
    print(f"[INFO] Strong draws: {len(strong_draws)}")
    print(f"[INFO] Backup draws: {len(backup_draws)}")

    # ---- DRAW MODE OUTPUT ----
    if strong_draws:
        daily_results = format_draw_results(strong_draws[:3], backup_draws[:4])
        return daily_results

    # ---- ACCUMULATOR FALLBACK ----
    print("[INFO] No strong draws → building accumulator...")
    acca_candidates = []
    seen_acca = set()                              # FIX 5 — dedupe accumulator
    discarded_gated = 0
    for e in enriched:
        if e["label"] in seen_acca:
            continue
        markets = score_all_markets(e["home"], e["away"], e["form_h"], e["form_a"], e["h2h"])

        # Discard rule: if 2+ markets returned 0 due to hard gates,
        # the match is structurally unfit — drop it entirely.
        zero_markets = sum(1 for v in markets.values() if v == 0)
        if zero_markets >= 2:
            discarded_gated += 1
            continue

        best_mkt, best_score = max(markets.items(), key=lambda kv: kv[1])
        if best_score >= 7:
            acca_candidates.append({
                "match":  e["label"],
                "league": e["league"],
                "market": best_mkt,
                "score":  best_score,
            })
            seen_acca.add(e["label"])

    acca_candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"[INFO] Accumulator candidates: {len(acca_candidates)} "
          f"(discarded by hard gates: {discarded_gated})")

    # FIX 6 — graceful fallback if accumulator can't reach the 6-pick minimum
    if len(acca_candidates) < 6:
        print(f"[INFO] Acca below 6 ({len(acca_candidates)}) — falling back to backup draws.")
        if backup_draws:
            daily_results = format_draw_results([], backup_draws[:4])
            return daily_results
        if not acca_candidates:
            daily_results = "⚽ No strong picks (draws or accumulator) found today."
            return daily_results

    main_picks = acca_candidates[:12]
    reserves   = acca_candidates[len(main_picks):len(main_picks) + 4]
    daily_results = format_accumulator(main_picks, reserves)
    return daily_results


# ---------------------------------------------------------
# 11. TELEGRAM COMMAND HANDLERS
# ---------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("start",        "Show the welcome message and command guide"),
    BotCommand("strongdraws",  "View today's top draw picks (auto-updated 00:05 UTC)"),
    BotCommand("testdraws",    "Run a fresh analysis right now"),
    BotCommand("debugmatches", "Debug — fixture counts, leagues, samples"),
]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Welcome to the Strong Draw Predictor Bot!\n\n"
        "I scan football matches across selected lower leagues and surface the "
        "strongest draw candidates. If no strong draws exist today, I'll build "
        "an accumulator across draw / double chance / over 1.5 / under 3.5 / "
        "BTTS / draw-no-bet markets.\n\n"
        "📋 Commands:\n\n"
        "🎯 /strongdraws — Today's top draw picks (auto-updated daily at 00:05 UTC)\n"
        "🔍 /testdraws — Run a fresh analysis right now\n"
        "📊 /debugmatches — Fixture counts, detected leagues and samples\n"
        "ℹ️ /start — Show this help menu\n\n"
        "──────────────────────\n"
        "⭐ Score ≥7 = Strong, =6 = Backup. Accumulator picks need ≥7 confidence."
    )
    await update.message.reply_text(msg)


async def strongdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if daily_results:
        await update.message.reply_text(daily_results)
    else:
        await update.message.reply_text("📊 No results yet. Use /testdraws.")


async def testdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running full analysis now, please wait...")
    result = run_analysis()
    await update.message.reply_text(result)


async def debugmatches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        await update.message.reply_text(f"📊 Fetching matches for {today} (UTC)...")

        data = _api_get("/fixtures", {"date": today})
        raw = data.get("response", []) if data else []
        total_raw = len(raw)
        league_ids = sorted({m.get("league", {}).get("id") for m in raw if m.get("league")})
        filtered = [m for m in raw if m.get("league", {}).get("id") in ALLOWED_LEAGUES]

        msg = (
            f"📊 Debug — {today} (UTC)\n\n"
            f"📦 Total fixtures fetched : {total_raw}\n"
            f"✅ After league filter    : {len(filtered)}\n"
            f"🌍 Detected league IDs    : {league_ids[:25] or 'none'}\n\n"
        )

        if filtered:
            msg += "⚽ Sample fixtures (up to 5):\n\n"
            for m in filtered[:5]:
                home   = m["teams"]["home"]["name"]
                away   = m["teams"]["away"]["name"]
                lid    = m["league"]["id"]
                league = ALLOWED_LEAGUES.get(lid, m["league"]["name"])
                season = m["league"].get("season", "?")
                msg += f"• {home} vs {away}\n   🏆 {league} (season {season})\n\n"
        else:
            msg += "No fixtures matched the allowed-league whitelist today."

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching debug info: {e}")


# ---------------------------------------------------------
# 12. TELEGRAM APPLICATION  (Polling mode)
# ---------------------------------------------------------
# post_init is called by PTB *after* the app is fully initialised
# but *before* polling starts — the only safe place to call bot APIs
# at startup. This avoids the asyncio.get_event_loop() conflict that
# caused "NameError: name 'start' is not defined" on Python 3.10+.

async def _post_init(application):
    """Register slash-command descriptions in the Telegram menu."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    print("[INFO] Bot commands registered.")


tg_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .post_init(_post_init)
    .build()
)
tg_app.add_handler(CommandHandler("start",        start_command))
tg_app.add_handler(CommandHandler("strongdraws",  strongdraws_command))
tg_app.add_handler(CommandHandler("testdraws",    testdraws_command))
tg_app.add_handler(CommandHandler("debugmatches", debugmatches_command))


# ---------------------------------------------------------
# 13. FLASK APP  (keepalive Web Service for Render)
#
# Runs on a background daemon thread so Render's health-checks
# always get a 200, while the main thread drives PTB polling.
# ---------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/api/run_daily", methods=["GET", "POST"])
def run_daily():
    """Manual or external-cron trigger: reset caches then re-analyse."""
    reset_all_caches()
    result = run_analysis()
    preview = result[:120] if result else "No results"
    return f"✅ Analysis complete: {preview}", 200


@app.route("/api/verify_leagues", methods=["GET"])
def verify_leagues_endpoint():
    """On-demand re-verification of the league whitelist."""
    from flask import jsonify
    allowed, suppression, report = verify_and_fix_league_ids(TARGET_LEAGUES)
    if allowed:
        ALLOWED_LEAGUES.clear(); ALLOWED_LEAGUES.update(allowed)
        SUPPRESSION_LEAGUES.clear(); SUPPRESSION_LEAGUES.update(suppression)
    return jsonify({
        "resolved":          len([r for r in report if r.get("status") == "OK"]),
        "not_found":         len([r for r in report if r.get("status") == "NOT_FOUND"]),
        "suppression_count": len(suppression),
        "report":            report,
    }), 200


def _run_flask():
    """Start Flask on the PORT env-var (required by Render)."""
    port = int(os.getenv("PORT", "8080"))
    print(f"[INFO] Flask keepalive server starting on 0.0.0.0:{port}")
    # use_reloader=False is mandatory — reloader forks the process and
    # would launch a second polling loop, causing Telegram conflicts.
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------
# 14. ENTRYPOINT — Flask on daemon thread, PTB polling on main
# ---------------------------------------------------------

if __name__ == "__main__":
    # 1. Start Flask in the background so Render sees an HTTP service.
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    # 2. Start long-polling — blocks the main thread (intended).
    #    post_init (defined in section 12) handles bot-command registration
    #    automatically before polling begins, with no event-loop conflicts.
    #    drop_pending_updates=True discards messages that arrived while
    #    the bot was offline, preventing a replay flood on restart.
    print("[INFO] Starting Telegram polling...")
    tg_app.run_polling(drop_pending_updates=True)
