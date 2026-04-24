# =========================================================
#  TELEGRAM STRONG-DRAW PREDICTOR BOT
#  Provider : API-Sports (v3.football.api-sports.io)
#  Runtime  : Vercel webhook + cron (also runnable locally)
# =========================================================

import os
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
# 2. ALLOWED LEAGUES (final expanded whitelist)
# ---------------------------------------------------------

ALLOWED_LEAGUES = {
    137: "Serie B",
    72:  "Serie C",

    141: "Segunda Division",
    138: "Primera RFEF",
    142: "Segunda RFEF",

    65:  "Ligue 2",
    66:  "National",

    41:  "League One",
    42:  "League Two",

    78:  "3. Liga",

    89:  "Eerste Divisie",

    94:  "Liga Portugal 2",

    144: "Challenger Pro League",

    218: "2. Liga",

    207: "Challenge League",

    113: "Superettan",
    104: "OBOS Ligaen",
    119: "Division 1",
    244: "Ykkonen",

    284: "Liga II Romania",
    106: "I Liga Poland",
    345: "FNL Czech",
    203: "1. Lig Turkey",
    210: "Super League 2 Greece",
    271: "NB II Hungary",

    128: "Primera Nacional Argentina",
    289: "Division Intermedia Paraguay",
    292: "Segunda Uruguay",
    239: "Primera B Colombia",
    266: "Primera B Chile",
    281: "Liga 2 Peru",

    71:  "Serie B Brazil",
    73:  "Serie C Brazil",
}


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

def score_draw_advanced(home, away, form_h, form_a):
    """Advanced Stalemate — 10-point weighted scoring."""
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

    return score


# Back-compat alias so other scorers (e.g. score_all_markets) keep working.
score_draw_base = score_draw_advanced


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

    if h2h and h2h["under35_rate"] >= 0.80:
        score += 3
    elif h2h and h2h["under35_rate"] >= 0.60:
        score += 2
    return score


def score_btts_yes(home, away, form_h, form_a, h2h):
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
    score = 0
    stronger = home if home["rank"] <= away["rank"] else away
    weaker   = away if stronger is home else home

    win_rate = stronger["wins"] / max(stronger["played"], 1)
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

def format_draw_results(strong, backup):
    if not strong and not backup:
        return None
    out = "🎯 Strong Draw Picks\n"
    n = 0
    for c in strong:
        n += 1
        out += (f"\n{n}) {c['match']}\n"
                f"   🏆 {c['league']}\n"
                f"   ⭐ Score: {c['score']}/10\n")
    if backup:
        out += "\n────────────────────\n\n📌 Backup Picks\n"
        for c in backup:
            n += 1
            out += (f"\n{n}) {c['match']}\n"
                    f"   🏆 {c['league']}\n"
                    f"   ⭐ Score: {c['score']}/10\n")
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
            if passes_draw_filters(home, away, form_h, form_a):
                base = score_draw_advanced(home, away, form_h, form_a)

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
                draw_score = base

            entry = {"match": match_label, "league": league_name, "score": draw_score}
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

    strong_draws.sort(key=lambda x: x["score"], reverse=True)
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
    for e in enriched:
        if e["label"] in seen_acca:
            continue
        markets = score_all_markets(e["home"], e["away"], e["form_h"], e["form_a"], e["h2h"])
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
    print(f"[INFO] Accumulator candidates: {len(acca_candidates)}")

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
# 12. GLOBAL TELEGRAM APPLICATION  (FIX 1 — built once, reused)
#
# IMPORTANT: do NOT name this variable `application` — Vercel's Python
# runtime auto-detects WSGI by looking for a module-level `application`
# or `app`. The Flask WSGI is `app`; this Telegram object must use a
# different name or Vercel throws "could not determine application interface".
# ---------------------------------------------------------

tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
tg_app.add_handler(CommandHandler("start",        start_command))
tg_app.add_handler(CommandHandler("strongdraws",  strongdraws_command))
tg_app.add_handler(CommandHandler("testdraws",    testdraws_command))
tg_app.add_handler(CommandHandler("debugmatches", debugmatches_command))


# --- Persistent background event loop ----------------------------------
# PTB's httpx.AsyncClient is bound to the loop it was initialised in.
# Flask's per-request asyncio.run() spawns a new loop each time, which
# would orphan that client → "Event loop is closed". Instead we run ONE
# loop on a daemon thread and dispatch every coroutine onto it.
# -----------------------------------------------------------------------

_bg_loop = asyncio.new_event_loop()


def _bg_loop_runner():
    asyncio.set_event_loop(_bg_loop)
    _bg_loop.run_forever()


threading.Thread(target=_bg_loop_runner, daemon=True).start()


def _run_async(coro, timeout=30):
    """Schedule a coroutine on the persistent background loop and wait."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result(timeout=timeout)


# Initialise the Telegram app ONCE on the background loop.
_run_async(tg_app.initialize())


async def process_update(update_data: dict):
    """Reuses the global Telegram app on the persistent background loop."""
    update = Update.de_json(update_data, tg_app.bot)
    await tg_app.process_update(update)


# ---------------------------------------------------------
# 13. FLASK APP / ROUTES
# ---------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """FIX 10 — Always return 200, never crash, log any errors."""
    data = flask_request.get_json(force=True, silent=True)
    if not data:
        return "ok", 200
    try:
        _run_async(process_update(data))
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
    return "ok", 200


@app.route("/api/set_webhook", methods=["GET"])
def set_webhook():
    async def _set():
        await tg_app.bot.set_my_commands(BOT_COMMANDS)
        domain = flask_request.host_url.rstrip("/")
        webhook_url = f"{domain}/api/webhook"
        await tg_app.bot.set_webhook(url=webhook_url)
        return webhook_url

    try:
        webhook_url = _run_async(_set())
        return f"✅ Webhook set to: {webhook_url}", 200
    except Exception as e:
        print(f"[SET_WEBHOOK ERROR] {e}")
        return f"❌ Error: {e}", 500


@app.route("/api/run_daily", methods=["GET", "POST"])
def run_daily():
    """Vercel Cron @ 00:05 UTC.
       FIX 9 — strict order: reset_all_caches() THEN run_analysis()."""
    reset_all_caches()
    result = run_analysis()
    preview = result[:120] if result else "No results"
    return f"✅ Analysis complete: {preview}", 200


# ---------------------------------------------------------
# 14. LOCAL DEV ENTRYPOINT (Replit / direct run)
# ---------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"[INFO] Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
