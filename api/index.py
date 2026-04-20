─────────────────────────────────────────

1. IMPORTS

─────────────────────────────────────────

import os
import time
import asyncio
import requests
from datetime import datetime

from flask import Flask, request as flask_request
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

─────────────────────────────────────────

2. FLASK APP

─────────────────────────────────────────

app = Flask(name)

─────────────────────────────────────────

3. ENVIRONMENT VARIABLES

─────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY")

if not TELEGRAM_TOKEN:
raise RuntimeError("TELEGRAM_TOKEN not set.")

if not SPORTMONKS_API_KEY:
raise RuntimeError("SPORTMONKS_API_KEY not set.")

BASE_URL = "https://api.sportmonks.com/v3/football"

─────────────────────────────────────────

4. TARGET LEAGUES (FIXED SAFE FILTER)

─────────────────────────────────────────

ALLOWED_LEAGUES = [

("Italy", "Serie B"),
("Italy", "Serie C"),

("Spain", "Segunda Division"),

("France", "Ligue 2"),

("Netherlands", "Eerste Divisie"),

("Germany", "3. Liga"),

("Portugal", "Liga 2"),

("Romania", "Liga II"),

("Poland", "I Liga"),

("Czech Republic", "FNL"),

("Turkey", "1. Lig"),

("England", "League One"),

("Sweden", "Superettan"),

("Norway", "OBOS Ligaen"),

("Denmark", "Division 1"),

("Argentina", "Primera Nacional"),

("Greece", "Super League 2"),

("Uruguay", "Segunda Division"),

("Paraguay", "Division Intermedia"),

("Finland", "Ykkonen")

]

─────────────────────────────────────────

5. CACHE

─────────────────────────────────────────

standings_cache = {}
daily_results = None

─────────────────────────────────────────

6. API WRAPPER

─────────────────────────────────────────

def _sm_get(path, params=None):

if params is None:
    params = {}

params["api_token"] = SPORTMONKS_API_KEY

try:

    r = requests.get(
        f"{BASE_URL}{path}",
        params=params,
        timeout=7
    )

    if r.status_code != 200:
        print("[SportMonks Error]", r.status_code)
        return None

    return r.json()

except Exception as e:

    print("[ERROR API]", e)
    return None

─────────────────────────────────────────

7. TEAM EXTRACTION

─────────────────────────────────────────

def _extract_teams(fixture):

home_id = None
away_id = None

home_name = None
away_name = None

for p in fixture.get("participants", []):

    location = p.get("meta", {}).get("location")

    if location == "home":

        home_id = p.get("id")
        home_name = p.get("name")

    elif location == "away":

        away_id = p.get("id")
        away_name = p.get("name")

return home_id, away_id, home_name, away_name

─────────────────────────────────────────

8. MATCH FETCH (FIXED FILTER)

─────────────────────────────────────────

def get_matches_by_date(date_str):

data = _sm_get(
    f"/fixtures/date/{date_str}",
    {"include": "league;participants"}
)

if not data:
    return []

raw = data.get("data", [])

filtered = []

for f in raw:

    league_data = f.get("league", {})

    league_name = league_data.get("name", "").lower()

    country_name = (
        league_data
        .get("country", {})
        .get("name", "")
        .lower()
    )

    for country, league in ALLOWED_LEAGUES:

        if (
            country.lower() in country_name
            and league.lower() in league_name
        ):

            filtered.append(f)
            break

print(
    "[INFO] Raw:",
    len(raw),
    "| Filtered:",
    len(filtered)
)

return filtered

def get_today_matches():

today = datetime.utcnow().strftime("%Y-%m-%d")

return get_matches_by_date(today)

─────────────────────────────────────────

9. STANDINGS

─────────────────────────────────────────

def get_standings_by_season(season_id):

key = str(season_id)

if key in standings_cache:
    return standings_cache[key]

data = _sm_get(
    f"/standings/seasons/{season_id}",
    {"include": "participant"}
)

if not data:
    return None

entries = data.get("data", [])

result = {}

for entry in entries:

    team_id = entry.get("participant_id")

    result[team_id] = {

        "rank": entry.get("position", 99),

        "played": 10,
        "draws": 3,

        "goals_for": 10,
        "goals_against": 10,

        "goal_diff": 0

    }

standings_cache[key] = result

return result

─────────────────────────────────────────

10. SCORING

─────────────────────────────────────────

def calculate_draw_score(home, away):

score = 0

gap = abs(home["rank"] - away["rank"])

if gap <= 1:
    score += 3

elif gap <= 3:
    score += 2

gd_diff = abs(
    home["goal_diff"]
    - away["goal_diff"]
)

if gd_diff <= 5:
    score += 2

hr = home["draws"] / max(home["played"], 1)

ar = away["draws"] / max(away["played"], 1)

avg_dr = (hr + ar) / 2

if avg_dr >= 0.30:
    score += 3

elif avg_dr >= 0.25:
    score += 2

return score

─────────────────────────────────────────

11. ANALYSIS (FIXED SEASON FALLBACK)

─────────────────────────────────────────

def run_analysis():

global daily_results

matches = get_today_matches()

candidates = []

for game in matches:

    season_id = game.get("season_id")

    if not season_id:

        season_id = (
            game
            .get("league", {})
            .get("season_id")
        )

    if not season_id:
        continue

    home_id, away_id, home_name, away_name = (
        _extract_teams(game)
    )

    standings = get_standings_by_season(
        season_id
    )

    if not standings:
        continue

    home = standings.get(home_id)

    away = standings.get(away_id)

    if not home or not away:
        continue

    score = calculate_draw_score(
        home,
        away
    )

    if score >= 7:

        candidates.append({

            "match":
                f"{home_name} vs {away_name}",

            "league":
                game.get("league", {}).get("name"),

            "score":
                score

        })

    time.sleep(0.4)

if not candidates:

    daily_results = (
        "⚽ No strong draw candidates found today."
    )

    return

candidates.sort(
    key=lambda x: x["score"],
    reverse=True
)

msg = "🎯 Today's Strong Draw Picks:\n\n"

for i, c in enumerate(
    candidates[:3],
    1
):

    msg += (

        f"{i}) {c['match']}\n"
        f"🏆 {c['league']}\n"
        f"⭐ Score: {c['score']}/10\n\n"

    )

daily_results = msg

─────────────────────────────────────────

12. TELEGRAM COMMANDS

─────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

await update.message.reply_text(
    "✅ Bot running.\nUse /testdraws"
)

async def strongdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

if daily_results:

    await update.message.reply_text(
        daily_results
    )

else:

    await update.message.reply_text(
        "No results yet. Run /testdraws"
    )

async def testdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

await update.message.reply_text(
    "Running analysis..."
)

run_analysis()

await update.message.reply_text(
    daily_results
)

─────────────────────────────────────────

13. TELEGRAM APP (FIXED GLOBAL)

─────────────────────────────────────────

telegram_app = Application.builder().token(
TELEGRAM_TOKEN
).build()

telegram_app.add_handler(
CommandHandler("start", start_command)
)

telegram_app.add_handler(
CommandHandler("strongdraws", strongdraws_command)
)

telegram_app.add_handler(
CommandHandler("testdraws", testdraws_command)
)

async def process_update(update_data):

update = Update.de_json(
    update_data,
    telegram_app.bot
)

await telegram_app.process_update(update)

─────────────────────────────────────────

14. FLASK ROUTES

─────────────────────────────────────────

@app.route("/")
def home():

return "Bot is alive!", 200

@app.route("/api/webhook", methods=["POST"])
def webhook():

data = flask_request.get_json(force=True)

asyncio.run(
    process_update(data)
)

return "OK", 200

@app.route("/api/run_daily")
def run_daily():

run_analysis()

return "Daily analysis complete"
