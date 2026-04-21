# ─────────────────────────────────────────
# 1. IMPORTS
# ─────────────────────────────────────────

import os
import time
import asyncio
import requests
from datetime import datetime

from flask import Flask, request as flask_request
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────
# 2. FLASK APP
# ─────────────────────────────────────────

app = Flask(__name__)

# ─────────────────────────────────────────
# 3. ENVIRONMENT VARIABLES
# ─────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is not set.")

if not SPORTMONKS_API_KEY:
    raise RuntimeError("SPORTMONKS_API_KEY environment variable is not set.")

BASE_URL = "https://api.sportmonks.com/v3/football"
HEADERS = {
    "Authorization": SPORTMONKS_API_KEY,
}

# ─────────────────────────────────────────
# 4. ALLOWED LEAGUE NAMES
# SportMonks IDs differ from API-Football,
# so we filter by league name instead.
# ─────────────────────────────────────────

ALLOWED_LEAGUES = {
    # Italy
    387: "Serie B (Italy)",
    781:  "Serie C (Italy)",
    # Spain
    567: "Segunda Division (Spain)",
    # France
    302: "Ligue 2 (France)",
    # Netherlands
    639: "Eerste Divisie (Netherlands)",
    # Germany
    85: "3. Liga (Germany)",
    # Portugal
    490: "Liga 2 (Portugal)",
    # Poland
    483: "I Liga (Poland)",
    # Czech Republic
    244: "FNL (Czech Republic)",
    # Turkey
    603: "1. Lig (Turkey)",
    # England
    20:  "League One (England)",
    # Sweden
    465: "Superettan (Sweden)",
    # Norway
    447: "OBOS Ligaen (Norway)",
    # Denmark
    274: "Division 1 (Denmark)",
    
}
# ─────────────────────────────────────────
# 5. IN-MEMORY CACHE
# ─────────────────────────────────────────

standings_cache = {}
form_cache = {}
h2h_cache = {}
daily_results = None

# ─────────────────────────────────────────
# 6. SPORTMONKS API HELPERS
# ─────────────────────────────────────────


def _sm_get(path, extra_params=None, timeout=7):
    """Single wrapper for all SportMonks GET requests."""
    params = {"api_token": SPORTMONKS_API_KEY}
    if extra_params:
        params.update(extra_params)
    try:
        r = requests.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params,
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[SportMonks API Error] {r.status_code} for {path}")
            return None
        return r.json()
    except Exception as e:
        print(f"[ERROR] _sm_get({path}): {e}")
        return None


def _extract_teams(fixture):
    """Return (home_id, away_id, home_name, away_name) from a fixture's participants."""
    home_id = home_name = away_id = away_name = None
    for p in fixture.get("participants", []):
        loc = p.get("meta", {}).get("location")
        if loc == "home":
            home_id = p["id"]
            home_name = p["name"]
        elif loc == "away":
            away_id = p["id"]
            away_name = p["name"]
    return home_id, away_id, home_name, away_name


def _extract_score(fixture):
    """Return (home_goals, away_goals) for a fixture using the CURRENT score."""
    home_goals = away_goals = None
    for s in fixture.get("scores", []):
        if s.get("description") == "CURRENT":
            side = s.get("participant") or s.get("score", {}).get("participant")
            goals = s.get("score", {}).get("goals")
            if side == "home":
                home_goals = goals
            elif side == "away":
                away_goals = goals
    return home_goals, away_goals


def _detail_value(details, type_id, default=0):
    """Extract a stat value from a standings details array by type_id."""
    for d in details:
        if d.get("type_id") == type_id:
            try:
                return int(d.get("value", default))
            except (ValueError, TypeError):
                return default
    return default


# ─────────────────────────────────────────
# 7. API FUNCTIONS
# ─────────────────────────────────────────


def get_matches_by_date(date_str):
    """Fetch fixtures for a date and filter to allowed leagues."""
    print(f"[INFO] Fetching fixtures for: {date_str}")
    data = _sm_get(f"/fixtures/date/{date_str}", {
        "include": "league;participants",
    })
    if not data:
        return []
    raw = data.get("data", [])
    filtered = [
        f for f in raw
        if f.get("league", {}).get("name") in ALLOWED_LEAGUE_NAMES
    ]
    print(f"[INFO] Total fixtures: {len(raw)} | Filtered: {len(filtered)}")
    return filtered


def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return get_matches_by_date(today)


def get_standings_by_season(season_id):
    """
    Fetch league standings for a SportMonks season_id.
    Returns dict mapping team_id → stats, or None on failure.

    SportMonks standings detail type_ids:
      129 = played, 130 = wins, 131 = draws, 132 = losses,
      133 = goals_for, 134 = goals_against, 179 = goal_difference
    """
    key = str(season_id)
    if key in standings_cache:
        return standings_cache[key]
    data = _sm_get(f"/standings/seasons/{season_id}", {
        "include": "participant;details",
    })
    if not data:
        return None
    entries = data.get("data", [])
    if not entries:
        return None
    result = {}
    for entry in entries:
        tid = entry.get("participant_id")
        if not tid:
            continue
        details = entry.get("details", [])
        played = _detail_value(details, 129)
        draws = _detail_value(details, 131)
        goals_for = _detail_value(details, 133)
        goals_against = _detail_value(details, 134)
        goal_diff = _detail_value(details, 179, goals_for - goals_against)
        result[tid] = {
            "rank": entry.get("position", 99),
            "played": played,
            "draws": draws,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "goal_diff": goal_diff,
        }
    standings_cache[key] = result
    return result


def get_recent_form(team_id):
    """Return last-5 results as list of 'W'/'D'/'L' for a team."""
    key = str(team_id)
    if key in form_cache:
        return form_cache[key]
    data = _sm_get(f"/fixtures/teams/{team_id}", {
        "include": "participants;scores",
        "last": 5,
    })
    if not data:
        form_cache[key] = []
        return []
    fixtures = data.get("data", [])
    results = []
    for game in fixtures:
        hg, ag = _extract_score(game)
        if hg is None or ag is None:
            continue
        home_id, _, _, _ = _extract_teams(game)
        if hg == ag:
            results.append("D")
        elif hg > ag:
            results.append("W" if team_id == home_id else "L")
        else:
            results.append("L" if team_id == home_id else "W")
    form_cache[key] = results
    return results


def get_h2h(home_id, away_id):
    """Return H2H stats (total, draw_rate) for last 5 meetings."""
    key = f"{home_id}_{away_id}"
    if key in h2h_cache:
        return h2h_cache[key]
    data = _sm_get(f"/fixtures/head-to-head/{home_id}/{away_id}", {
        "include": "scores",
        "last": 5,
    })
    if not data:
        return None
    fixtures = data.get("data", [])
    total = len(fixtures)
    draws = sum(
        1 for g in fixtures
        if None not in _extract_score(g) and _extract_score(g)[0] == _extract_score(g)[1]
    )
    result = {"total": total, "draw_rate": draws / total if total > 0 else 0}
    h2h_cache[key] = result
    return result


# ─────────────────────────────────────────
# 8. SCORING FUNCTION  (unchanged)
# ─────────────────────────────────────────


def calculate_draw_score(home, away, form_h, form_a, h2h):
    score = 0

    gap = abs(home["rank"] - away["rank"])
    if gap <= 1:
        score += 3
    elif gap <= 3:
        score += 2

    gd_diff = abs(home["goal_diff"] - away["goal_diff"])
    if gd_diff <= 5:
        score += 2

    hr = home["draws"] / max(home["played"], 1)
    ar = away["draws"] / max(away["played"], 1)
    avg_dr = (hr + ar) / 2
    if avg_dr >= 0.30:
        score += 3
    elif avg_dr >= 0.25:
        score += 2

    form_draws = form_h.count("D") + form_a.count("D")
    if form_draws >= 3:
        score += 2

    if h2h and h2h["draw_rate"] >= 0.30:
        score += 2

    return score


# ─────────────────────────────────────────
# 9. ANALYSIS FUNCTION
# ─────────────────────────────────────────


def run_analysis():
    global daily_results

    print("[INFO] Running draw analysis...")

    matches = get_today_matches()
    candidates = []

    for game in matches:
        try:
            league_name = game.get("league", {}).get("name", "")
            season_id = game.get("season_id")
            home_id, away_id, home_name, away_name = _extract_teams(game)

            if not home_id or not away_id or not season_id:
                continue

            standings = get_standings_by_season(season_id)
            if not standings:
                continue

            home = standings.get(home_id)
            away = standings.get(away_id)
            if not home or not away:
                continue

            form_h = get_recent_form(home_id)
            time.sleep(0.5)
            form_a = get_recent_form(away_id)
            time.sleep(0.5)
            h2h = get_h2h(home_id, away_id)
            time.sleep(0.5)

            score = calculate_draw_score(home, away, form_h, form_a, h2h)

            if score >= 7:
                candidates.append({
                    "match": f"{home_name} vs {away_name}",
                    "league": league_name,
                    "score": score,
                })

        except Exception as e:
            print(f"[ERROR] Skipping match: {e}")
            continue

    if not candidates:
        daily_results = "⚽ No strong draw candidates found today."
        print("[INFO] Analysis done. No candidates.")
        return

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top3 = candidates[:3]

    msg = "🎯 Today's Strong Draw Picks:\n\n"
    for i, c in enumerate(top3, 1):
        msg += (
            f"{i}) {c['match']}\n"
            f"🏆 {c['league']}\n"
            f"⭐ Score: {c['score']}/10\n\n"
        )

    daily_results = msg
    print(f"[INFO] Analysis done. {len(candidates)} candidates found.")


# ─────────────────────────────────────────
# 10. TELEGRAM COMMAND HANDLERS
# ─────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start",        "Show the welcome message and command guide"),
    BotCommand("strongdraws",  "View today's top draw picks (updated at 00:05)"),
    BotCommand("testdraws",    "Run a fresh analysis right now"),
    BotCommand("debugmatches", "Today's match count with league breakdown"),
    BotCommand("rawfixtures",  "Raw fixture list for today — no filter"),
    BotCommand("health",       "Check if the bot is online"),
]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Welcome to the Strong Draw Predictor Bot!\n\n"
        "I analyse football matches across lower leagues and identify the strongest draw candidates using stats like standings, form, and head-to-head records.\n\n"
        "📋 Available Commands:\n\n"
        "🎯 /strongdraws — View today's top draw picks (updated daily at 00:05)\n\n"
        "🔍 /testdraws — Run a fresh analysis right now\n\n"
        "📊 /debugmatches — Today's fixture count and active leagues\n\n"
        "📡 /rawfixtures — Raw fixture list for today, no filter\n\n"
        "❤️ /health — Check if the bot is online and responding\n\n"
        "ℹ️ /start — Show this help menu\n\n"
        "──────────────────────\n"
        "🏆 Supported leagues span South America and Europe — lower divisions known for higher draw rates.\n\n"
        "⭐ Picks are scored out of 10. Only matches scoring 7 or above are shown."
    )
    await update.message.reply_text(msg)


async def strongdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if daily_results:
        await update.message.reply_text(daily_results)
    else:
        await update.message.reply_text(
            "📊 No results yet. Use /testdraws to run the analysis now."
        )


async def testdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Running full analysis now, please wait...")
    run_analysis()
    if daily_results:
        await update.message.reply_text(daily_results)
    else:
        await update.message.reply_text("⚽ No strong draw candidates found.")


async def debugmatches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        await update.message.reply_text(f"📊 Fetching matches for {today} (UTC)...")

        matches = get_matches_by_date(today)
        total = len(matches)

        msg = (
            f"📊 Debug — Today's Matches\n\n"
            f"📅 Date used: {today} (UTC)\n"
            f"✅ Matches in allowed leagues: {total}\n\n"
        )

        if matches:
            msg += "⚽ Sample matches (up to 5):\n\n"
            for m in matches[:5]:
                home_id, away_id, home_name, away_name = _extract_teams(m)
                league = m.get("league", {}).get("name", "Unknown")
                msg += f"• {home_name} vs {away_name}\n  🏆 {league}\n\n"

            seen_leagues = []
            for m in matches:
                ln = m.get("league", {}).get("name", "")
                if ln and ln not in seen_leagues:
                    seen_leagues.append(ln)
                if len(seen_leagues) == 5:
                    break
            msg += "🌍 Leagues represented (up to 5):\n"
            for ln in seen_leagues:
                msg += f"  • {ln}\n"
        else:
            msg += "No matches found in allowed leagues today."

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching debug info: {e}")


async def rawfixtures_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        await update.message.reply_text(f"📡 Fetching all fixtures for {today} (UTC)...")

        data = _sm_get(f"/fixtures/date/{today}", {"include": "league;participants"})
        raw = data.get("data", []) if data else []
        total = len(raw)

        msg = f"📅 Date: {today}\n📊 Total Fixtures: {total}\n\n"

        if raw:
            for f in raw[:5]:
                home_id, away_id, home_name, away_name = _extract_teams(f)
                league = f.get("league", {}).get("name", "Unknown")
                msg += f"⚽ {home_name} vs {away_name}\n🏆 {league}\n\n"
        else:
            msg += "❌ No fixtures returned from API."

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and running.")


# ─────────────────────────────────────────
# 11. CORE UPDATE PROCESSOR
# ─────────────────────────────────────────


async def process_update(update_data: dict):
    async with Application.builder().token(TELEGRAM_TOKEN).build() as application:
        application.add_handler(CommandHandler("start",        start_command))
        application.add_handler(CommandHandler("strongdraws",  strongdraws_command))
        application.add_handler(CommandHandler("testdraws",    testdraws_command))
        application.add_handler(CommandHandler("debugmatches", debugmatches_command))
        application.add_handler(CommandHandler("rawfixtures",  rawfixtures_command))
        application.add_handler(CommandHandler("health",       health_command))

        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)


# ─────────────────────────────────────────
# 12. FLASK ROUTES
# ─────────────────────────────────────────


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    data = flask_request.get_json(force=True)
    if not data:
        return "Bad request", 400
    asyncio.run(process_update(data))
    return "Webhook received", 200


@app.route("/api/set_webhook", methods=["GET"])
def set_webhook():
    """
    Call once after deploying to register the webhook with Telegram.
    Example: GET https://your-vercel-app.vercel.app/api/set_webhook
    """
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
    """
    Trigger via Vercel Cron at 00:05 UTC daily.
    vercel.json: {"crons": [{"path": "/api/run_daily", "schedule": "5 0 * * *"}]}
    """
    run_analysis()
    return f"✅ Analysis complete: {daily_results[:80] if daily_results else 'No results'}", 200
