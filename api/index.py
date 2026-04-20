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
API_KEY = os.getenv("FOOTBALL_KEY") or os.getenv("API_FOOTBALL_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is not set.")

if not API_KEY:
    raise RuntimeError("FOOTBALL_KEY (or API_FOOTBALL_KEY) environment variable is not set.")

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# ─────────────────────────────────────────
# 4. ALLOWED LEAGUES
# ─────────────────────────────────────────

ALLOWED_LEAGUES = {
    # Italy
    137: "Serie B (Italy)",
    72:  "Serie C (Italy)",
    # Spain
    141: "Segunda Division (Spain)",
    # France
    65:  "Ligue 2 (France)",
    # Netherlands
    89:  "Eerste Divisie (Netherlands)",
    # Germany
    78:  "3. Liga (Germany)",
    # Portugal
    94:  "Liga 2 (Portugal)",
    # Romania
    284: "Liga II (Romania)",
    # Poland
    106: "I Liga (Poland)",
    # Czech Republic
    345: "FNL (Czech Republic)",
    # Turkey
    203: "1. Lig (Turkey)",
    # England
    41:  "League One (England)",
    # Sweden
    113: "Superettan (Sweden)",
    # Norway
    104: "OBOS Ligaen (Norway)",
    # Denmark
    119: "Division 1 (Denmark)",
    # Argentina
    128: "Primera Nacional (Argentina)",
    # Greece
    210: "Super League 2 (Greece)",
    # Uruguay
    292: "Segunda Division (Uruguay)",
    # Paraguay
    289: "Division Intermedia (Paraguay)",
    # Finland
    244: "Ykkonen (Finland)",
}

# ─────────────────────────────────────────
# 5. IN-MEMORY CACHE
# (Shared across warm invocations on the same instance)
# ─────────────────────────────────────────

standings_cache = {}
form_cache = {}
h2h_cache = {}
daily_results = None

# ─────────────────────────────────────────
# 6. API FUNCTIONS
# ─────────────────────────────────────────


def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"{BASE_URL}/fixtures?date={today}",
            headers=HEADERS,
            timeout=10,
        )
        return r.json().get("response", [])
    except Exception as e:
        print(f"[ERROR] get_today_matches: {e}")
        return []


def get_standings(league_id, season):
    key = f"{league_id}_{season}"
    if key in standings_cache:
        return standings_cache[key]
    try:
        r = requests.get(
            f"{BASE_URL}/standings?league={league_id}&season={season}",
            headers=HEADERS,
            timeout=10,
        )
        groups = r.json().get("response", [])
        if not groups:
            return None
        standings = groups[0]["league"]["standings"][0]
        result = {}
        for team in standings:
            tid = team["team"]["id"]
            result[tid] = {
                "rank": team["rank"],
                "played": team["all"]["played"],
                "draws": team["all"]["draw"],
                "goals_for": team["all"]["goals"]["for"],
                "goals_against": team["all"]["goals"]["against"],
                "goal_diff": team["goalsDiff"],
            }
        standings_cache[key] = result
        return result
    except Exception as e:
        print(f"[ERROR] get_standings league={league_id}: {e}")
        return None


def get_recent_form(team_id, league_id, season):
    key = f"{team_id}_{league_id}"
    if key in form_cache:
        return form_cache[key]
    try:
        r = requests.get(
            f"{BASE_URL}/fixtures?team={team_id}&league={league_id}&season={season}&last=5",
            headers=HEADERS,
            timeout=10,
        )
        fixtures = r.json().get("response", [])
        results = []
        for game in fixtures:
            hg = game["goals"]["home"]
            ag = game["goals"]["away"]
            if hg is None or ag is None:
                continue
            if hg == ag:
                results.append("D")
            elif hg > ag:
                results.append("W" if team_id == game["teams"]["home"]["id"] else "L")
            else:
                results.append("W" if team_id == game["teams"]["away"]["id"] else "L")
        form_cache[key] = results
        return results
    except Exception as e:
        print(f"[ERROR] get_recent_form team={team_id}: {e}")
        return []


def get_h2h(home_id, away_id):
    key = f"{home_id}_{away_id}"
    if key in h2h_cache:
        return h2h_cache[key]
    try:
        r = requests.get(
            f"{BASE_URL}/fixtures/headtohead?h2h={home_id}-{away_id}&last=5",
            headers=HEADERS,
            timeout=10,
        )
        fixtures = r.json().get("response", [])
        total = len(fixtures)
        draws = sum(
            1
            for g in fixtures
            if g["goals"]["home"] is not None
            and g["goals"]["away"] is not None
            and g["goals"]["home"] == g["goals"]["away"]
        )
        result = {"total": total, "draw_rate": draws / total if total > 0 else 0}
        h2h_cache[key] = result
        return result
    except Exception as e:
        print(f"[ERROR] get_h2h {home_id}-{away_id}: {e}")
        return None


# ─────────────────────────────────────────
# 7. SCORING FUNCTION
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
# 8. ANALYSIS FUNCTION
# ─────────────────────────────────────────


def run_analysis():
    global daily_results

    print("[INFO] Running draw analysis...")

    matches = get_today_matches()
    candidates = []

    for game in matches:
        try:
            league_id = game["league"]["id"]
            if league_id not in ALLOWED_LEAGUES:
                continue

            season = game["league"]["season"]
            home_id = game["teams"]["home"]["id"]
            away_id = game["teams"]["away"]["id"]
            home_name = game["teams"]["home"]["name"]
            away_name = game["teams"]["away"]["name"]
            league_name = game["league"]["name"]

            standings = get_standings(league_id, season)
            if not standings:
                continue

            home = standings.get(home_id)
            away = standings.get(away_id)
            if not home or not away:
                continue

            form_h = get_recent_form(home_id, league_id, season)
            form_a = get_recent_form(away_id, league_id, season)
            h2h = get_h2h(home_id, away_id)

            score = calculate_draw_score(home, away, form_h, form_a, h2h)

            if score >= 7:
                candidates.append({
                    "match": f"{home_name} vs {away_name}",
                    "league": league_name,
                    "score": score,
                })

            time.sleep(0.4)

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
# 9. TELEGRAM COMMAND HANDLERS
# ─────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start",        "Show the welcome message and command guide"),
    BotCommand("strongdraws",  "View today's top draw picks"),
    BotCommand("testdraws",    "Run a fresh analysis right now"),
    BotCommand("debugmatches", "See how many matches are available today"),
    BotCommand("health",       "Check if the bot is online"),
]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Welcome to the Strong Draw Predictor Bot!\n\n"
        "I analyse football matches across lower leagues and identify the strongest draw candidates using stats like standings, form, and head-to-head records.\n\n"
        "📋 Available Commands:\n\n"
        "🎯 /strongdraws — View today's top draw picks\n\n"
        "🔍 /testdraws — Run a fresh analysis right now\n\n"
        "📊 /debugmatches — Show how many matches are scheduled today and how many fall inside the supported leagues\n\n"
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
        await update.message.reply_text("📊 Fetching today's matches...")
        matches = get_today_matches()
        total = len(matches)
        allowed = [m for m in matches if m["league"]["id"] in ALLOWED_LEAGUES]
        allowed_count = len(allowed)

        msg = (
            f"📊 Debug — Today's Matches\n\n"
            f"Total matches today: {total}\n"
            f"Matches in allowed leagues: {allowed_count}\n\n"
        )

        if allowed:
            msg += "⚽ Sample matches (up to 5):\n\n"
            for m in allowed[:5]:
                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]
                league = m["league"]["name"]
                msg += f"• {home} vs {away}\n  🏆 {league}\n\n"
        else:
            msg += "No matches found in allowed leagues today."

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is alive and running.")


# ─────────────────────────────────────────
# 10. CORE UPDATE PROCESSOR
# ─────────────────────────────────────────


async def process_update(update_data: dict):
    async with Application.builder().token(TELEGRAM_TOKEN).build() as application:
        application.add_handler(CommandHandler("start",        start_command))
        application.add_handler(CommandHandler("strongdraws",  strongdraws_command))
        application.add_handler(CommandHandler("testdraws",    testdraws_command))
        application.add_handler(CommandHandler("debugmatches", debugmatches_command))
        application.add_handler(CommandHandler("health",       health_command))

        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)


# ─────────────────────────────────────────
# 11. FLASK ROUTES
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
    return "ok", 200


@app.route("/api/set_webhook", methods=["GET"])
def set_webhook():
    """
    Call this endpoint once after deploying to register the webhook with Telegram.
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
    Trigger this endpoint via Vercel Cron (e.g. daily at 00:05) to run the analysis.
    Add to vercel.json:
    {
      "crons": [{ "path": "/api/run_daily", "schedule": "5 0 * * *" }]
    }
    """
    run_analysis()
    return f"✅ Analysis complete: {daily_results[:80] if daily_results else 'No results'}", 200

