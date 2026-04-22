# =========================================================
#  TELEGRAM STRONG-DRAW PREDICTOR BOT
#  Provider : API-Sports (v3.football.api-sports.io)
#  Runtime  : Vercel webhook + cron (also runnable locally)
# =========================================================

import os
import time
import asyncio
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

HEADERS = {
    "x-apisports-key": API_FOOTBALL_KEY,
}


# ---------------------------------------------------------
# 2. ALLOWED LEAGUES (expanded whitelist with display names)
# ---------------------------------------------------------

ALLOWED_LEAGUES = {
    # ITALY
    137: "Serie B (Italy)",
    72:  "Serie C (Italy)",

    # SPAIN
    141: "Segunda Division (Spain)",
    138: "Primera RFEF (Spain)",

    # FRANCE
    65:  "Ligue 2 (France)",
    66:  "National (France)",

    # NETHERLANDS
    89:  "Eerste Divisie",

    # GERMANY
    78:  "3. Liga",

    # PORTUGAL
    94:  "Liga 2",

    # ENGLAND
    41:  "League One",
    42:  "League Two",

    # SCANDINAVIA
    113: "Superettan (Sweden)",
    104: "OBOS Ligaen (Norway)",
    119: "Division 1 (Denmark)",
    244: "Ykkonen (Finland)",

    # EAST EUROPE
    284: "Liga II (Romania)",
    106: "I Liga (Poland)",
    345: "FNL (Czech Republic)",
    203: "1. Lig (Turkey)",
    210: "Super League 2 (Greece)",

    # SOUTH AMERICA
    128: "Primera Nacional (Argentina)",
    289: "Division Intermedia (Paraguay)",
    292: "Segunda Division (Uruguay)",
    239: "Primera B (Colombia)",
    266: "Primera B (Chile)",
    281: "Liga 2 (Peru)",

    # BRAZIL
    71:  "Serie B (Brazil)",
    73:  "Serie C (Brazil)",
}


# ---------------------------------------------------------
# 3. CACHING (12-hour TTL + manual daily reset)
# ---------------------------------------------------------

CACHE_TTL = 12 * 60 * 60   # 12 hours in seconds

standings_cache = {}   # key: f"{league_id}_{season}"   -> (timestamp, value)
form_cache      = {}   # key: team_id                   -> (timestamp, value)
h2h_cache       = {}   # key: f"{home_id}_{away_id}"    -> (timestamp, value)

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
    """Daily reset — clears all caches at the start of a new analysis day."""
    standings_cache.clear()
    form_cache.clear()
    h2h_cache.clear()
    print("[INFO] All caches cleared (daily reset).")


# ---------------------------------------------------------
# 4. API REQUEST HELPER (rate-limit protected)
# ---------------------------------------------------------

def _api_get(path, params=None, timeout=10):
    """Single wrapper for all API-Sports GET requests. Always sleeps 0.4s after."""
    try:
        r = requests.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params or {},
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[API ERROR] {r.status_code} for {path} | params={params}")
            time.sleep(0.4)
            return None
        data = r.json()
        if data.get("errors"):
            print(f"[API ERROR-FIELD] {path} -> {data.get('errors')}")
        time.sleep(0.4)
        return data
    except requests.Timeout:
        print(f"[API TIMEOUT] {path}")
        time.sleep(0.4)
        return None
    except Exception as e:
        print(f"[API ERROR] {path}: {e}")
        time.sleep(0.4)
        return None


# ---------------------------------------------------------
# 5. FIXTURE / STANDINGS / FORM / H2H
# ---------------------------------------------------------

def get_matches_by_date(date_str):
    """Fetch fixtures for a date and filter to allowed leagues only."""
    print(f"[INFO] Fetching fixtures for {date_str}")
    data = _api_get("/fixtures", {"date": date_str})
    if not data:
        return []
    raw = data.get("response", []) or []
    league_ids = sorted({m.get("league", {}).get("id") for m in raw if m.get("league")})
    filtered = [m for m in raw if m.get("league", {}).get("id") in ALLOWED_LEAGUES]
    print(f"Matches fetched: {len(raw)}")
    print(f"Matches after league filter: {len(filtered)}")
    print(f"[INFO] League IDs detected (sample): {league_ids[:25]}")
    return filtered


def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return get_matches_by_date(today)


def get_standings(league_id, season):
    """Return dict {team_id: stats} or None. Cached per (league, season)."""
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
                "goals_for":     team["all"]["goals"]["for"],
                "goals_against": team["all"]["goals"]["against"],
                "goal_diff":     team.get("goalsDiff", 0),
            }
        except (KeyError, TypeError):
            continue

    _cache_set(standings_cache, key, result)
    return result


def get_recent_form(team_id, league_id, season):
    """Return list of last-5 results like ['W','D','L',...]. Cached per team."""
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
    """Return {'total': n, 'draw_rate': r} or None. Cached per pair."""
    key = f"{home_id}_{away_id}"
    cached = _cache_get(h2h_cache, key)
    if cached is not None:
        return cached

    data = _api_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 5})
    if not data:
        return None
    fixtures = data.get("response", []) or []
    total = len(fixtures)
    draws = 0
    for g in fixtures:
        try:
            hg = g["goals"]["home"]
            ag = g["goals"]["away"]
            if hg is not None and ag is not None and hg == ag:
                draws += 1
        except (KeyError, TypeError):
            continue
    result = {"total": total, "draw_rate": (draws / total) if total > 0 else 0.0}
    _cache_set(h2h_cache, key, result)
    return result


# ---------------------------------------------------------
# 6. MATCH FILTERS
# ---------------------------------------------------------

def passes_filters(home, away, form_h, form_a, h2h):
    """Return True if a match meets all hard filters."""
    if not home or not away:
        return False

    if abs(home["rank"] - away["rank"]) > 3:
        return False

    if abs(home["goal_diff"] - away["goal_diff"]) > 6:
        return False

    hr = home["draws"] / max(home["played"], 1)
    ar = away["draws"] / max(away["played"], 1)
    if (hr + ar) / 2 < 0.25:
        return False

    if not h2h or h2h["total"] < 2:
        return False

    if not form_h or not form_a:
        return False

    return True


# ---------------------------------------------------------
# 7. SCORING (max 11 points after additions)
# ---------------------------------------------------------

def calculate_draw_score(home, away, form_h, form_a, h2h):
    score = 0

    # Position gap
    gap = abs(home["rank"] - away["rank"])
    if gap <= 1:
        score += 3
    elif gap <= 3:
        score += 2

    # Goal-difference similarity
    gd_diff = abs(home["goal_diff"] - away["goal_diff"])
    if gd_diff <= 5:
        score += 2

    # Goals-scored similarity (NEW)
    goals_scored_diff = abs(home["goals_for"] - away["goals_for"])
    if goals_scored_diff <= 10:
        score += 1

    # Season draw rate
    hr = home["draws"] / max(home["played"], 1)
    ar = away["draws"] / max(away["played"], 1)
    avg_dr = (hr + ar) / 2
    if avg_dr >= 0.30:
        score += 3
    elif avg_dr >= 0.25:
        score += 2

    # Recent form
    if (form_h.count("D") + form_a.count("D")) >= 3:
        score += 2

    # Head-to-head
    if h2h and h2h["draw_rate"] >= 0.30:
        score += 2

    return score


# ---------------------------------------------------------
# 8. ANALYSIS  (Strong Picks ≥7 + Backup Picks =6)
# ---------------------------------------------------------

def format_results(strong, backup):
    """Render Strong + Backup picks in the exact required format."""
    if not strong and not backup:
        return "⚽ No strong draw candidates found today."

    out = "🎯 Strong Draw Picks\n"
    n = 0
    for c in strong:
        n += 1
        out += (
            f"\n{n}) {c['match']}\n"
            f"   🏆 {c['league']}\n"
            f"   ⭐ Score: {c['score']}/10\n"
        )

    if backup:
        out += "\n────────────────────\n\n📌 Backup Picks\n"
        # Continue the numbering from where strong left off
        for c in backup:
            n += 1
            out += (
                f"\n{n}) {c['match']}\n"
                f"   🏆 {c['league']}\n"
                f"   ⭐ Score: {c['score']}/10\n"
            )

    return out.strip()


def run_analysis():
    """Full draw-prediction pipeline. Stores result in daily_results."""
    global daily_results
    print("[INFO] Running draw analysis...")

    matches = get_today_matches()

    if len(matches) < 40:
        print(f"WARNING: Too few matches — check league coverage (got {len(matches)})")

    candidates = []

    for game in matches:
        try:
            league = game.get("league", {})
            league_id   = league.get("id")
            season      = league.get("season")            # auto-detected
            league_name = ALLOWED_LEAGUES.get(league_id, league.get("name", "Unknown"))

            if league_id not in ALLOWED_LEAGUES or not season:
                continue

            home_id   = game["teams"]["home"]["id"]
            away_id   = game["teams"]["away"]["id"]
            home_name = game["teams"]["home"]["name"]
            away_name = game["teams"]["away"]["name"]

            standings = get_standings(league_id, season)
            if not standings:
                continue

            home = standings.get(home_id)
            away = standings.get(away_id)

            form_h = get_recent_form(home_id, league_id, season)
            form_a = get_recent_form(away_id, league_id, season)
            h2h    = get_h2h(home_id, away_id)

            if not passes_filters(home, away, form_h, form_a, h2h):
                continue

            score = calculate_draw_score(home, away, form_h, form_a, h2h)

            candidates.append({
                "match":  f"{home_name} vs {away_name}",
                "league": league_name,
                "score":  score,
            })

        except Exception as e:
            print(f"[ERROR] Skipping match: {e}")
            continue

    # Split into Strong (≥7) and Backup (=6)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    strong = [c for c in candidates if c["score"] >= 7][:3]   # top 3 only
    backup = [c for c in candidates if c["score"] == 6][:4]   # max 4

    print(f"Strong candidates: {len(strong)}")
    print(f"Backup candidates: {len(backup)}")

    daily_results = format_results(strong, backup)
    return daily_results


# ---------------------------------------------------------
# 9. TELEGRAM COMMAND HANDLERS
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
        "I analyse football matches across selected lower leagues and surface "
        "the strongest draw candidates using standings, form, and head-to-head data.\n\n"
        "📋 Commands:\n\n"
        "🎯 /strongdraws — Today's top draw picks (auto-updated daily at 00:05 UTC)\n"
        "🔍 /testdraws — Run a fresh analysis right now\n"
        "📊 /debugmatches — Fixture counts, detected leagues and samples\n"
        "ℹ️ /start — Show this help menu\n\n"
        "──────────────────────\n"
        "⭐ Picks are scored. Score ≥7 = Strong, =6 = Backup.\n"
        "🏆 Up to 3 Strong + up to 4 Backup picks per day."
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
# 10. UPDATE PROCESSOR (used by webhook)
# ---------------------------------------------------------

async def process_update(update_data: dict):
    async with Application.builder().token(TELEGRAM_TOKEN).build() as application:
        application.add_handler(CommandHandler("start",        start_command))
        application.add_handler(CommandHandler("strongdraws",  strongdraws_command))
        application.add_handler(CommandHandler("testdraws",    testdraws_command))
        application.add_handler(CommandHandler("debugmatches", debugmatches_command))

        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)


# ---------------------------------------------------------
# 11. FLASK APP / ROUTES
# ---------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    data = flask_request.get_json(force=True, silent=True)
    if not data:
        return "Bad request", 400
    asyncio.run(process_update(data))
    return "ok", 200


@app.route("/api/set_webhook", methods=["GET"])
def set_webhook():
    """Call once after deploying to register the webhook with Telegram."""
    async def _set():
        async with Application.builder().token(TELEGRAM_TOKEN).build() as application:
            await application.bot.set_my_commands(BOT_COMMANDS)
            domain = flask_request.host_url.rstrip("/")
            webhook_url = f"{domain}/api/webhook"
            await application.bot.set_webhook(url=webhook_url)
            return webhook_url

    webhook_url = asyncio.run(_set())
    return f"✅ Webhook set to: {webhook_url}", 200


@app.route("/api/run_daily", methods=["GET", "POST"])
def run_daily():
    """Triggered by Vercel Cron at 00:05 UTC daily.
    Resets all caches at the start of every daily run."""
    reset_all_caches()
    result = run_analysis()
    preview = result[:120] if result else "No results"
    return f"✅ Analysis complete: {preview}", 200


# ---------------------------------------------------------
# 12. LOCAL DEV ENTRYPOINT (Replit / direct run)
# ---------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"[INFO] Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
