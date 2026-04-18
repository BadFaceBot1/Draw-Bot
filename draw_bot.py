import os
import requests
import time
from datetime import datetime, time as dtime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ─────────────────────────────────────────
# TOKENS (FROM RAILWAY ENV VARIABLES)
# ─────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("8578968957:AAFf9gs1w3npCbqqhbVipkzGeH1bHBM8EqU")
API_KEY = os.getenv("3ba2576116ed4ce20b0bb5e0d8d28249")

BASE_URL = "https://v3.football.api-sports.io"

HEADERS = {
    "x-apisports-key": API_KEY
}

# ─────────────────────────────────────────
# LOWER + DRAW-FRIENDLY LEAGUES
# ─────────────────────────────────────────

ALLOWED_LEAGUES = [

    # Brazil
    71,   # Serie B
    72,   # Serie C

    # Argentina
    128,  # Primera Nacional

    # Colombia
    239,  # Primera B

    # Mexico
    262,  # Liga Expansion MX

    # Chile
    266,  # Primera B

    # Peru
    281,  # Liga 2

    # Paraguay
    289,  # Division Intermedia

    # Uruguay
    292,  # Segunda Division

    # EUROPE LOWER LEAGUES

    # Sweden
    113,  # Superettan

    # Norway
    104,  # OBOS Ligaen

    # Finland
    244,  # Ykkonen

    # Denmark
    119,  # Division 1

    # Netherlands
    89,   # Eerste Divisie

    # Germany
    78,   # 3. Liga
]

# ─────────────────────────────────────────
# CACHES (CRITICAL FOR API LIMIT)
# ─────────────────────────────────────────

standings_cache = {}
form_cache = {}
h2h_cache = {}
team_stats_cache = {}

daily_results = None
last_run_date = None

# ─────────────────────────────────────────
# API FUNCTIONS
# ─────────────────────────────────────────

def get_today_matches():

    today = datetime.today().strftime('%Y-%m-%d')

    try:

        r = requests.get(
            f"{BASE_URL}/fixtures?date={today}",
            headers=HEADERS,
            timeout=10
        )

        data = r.json()

        return data.get("response", [])

    except Exception as e:

        print(f"[ERROR] fixtures: {e}")

        return []


def get_standings(league_id, season):

    key = f"{league_id}_{season}"

    if key in standings_cache:
        return standings_cache[key]

    try:

        r = requests.get(
            f"{BASE_URL}/standings?league={league_id}&season={season}",
            headers=HEADERS,
            timeout=10
        )

        data = r.json()

        groups = data.get("response", [])

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

                "goal_diff": team["goalsDiff"]

            }

        standings_cache[key] = result

        return result

    except Exception as e:

        print(f"[ERROR] standings {league_id}: {e}")

        return None


def get_recent_form(team_id, league_id, season):

    key = f"{team_id}_{league_id}"

    if key in form_cache:
        return form_cache[key]

    try:

        r = requests.get(
            f"{BASE_URL}/fixtures?team={team_id}&league={league_id}&season={season}&last=5",
            headers=HEADERS,
            timeout=10
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
                if team_id == game["teams"]["home"]["id"]:
                    results.append("W")
                else:
                    results.append("L")

            else:

                if team_id == game["teams"]["away"]["id"]:
                    results.append("W")
                else:
                    results.append("L")

        form_cache[key] = results

        return results

    except Exception as e:

        print(f"[ERROR] form {team_id}: {e}")

        return []


def get_h2h(home_id, away_id):

    key = f"{home_id}_{away_id}"

    if key in h2h_cache:
        return h2h_cache[key]

    try:

        r = requests.get(
            f"{BASE_URL}/fixtures/headtohead?h2h={home_id}-{away_id}&last=5",
            headers=HEADERS,
            timeout=10
        )

        fixtures = r.json().get("response", [])

        total = len(fixtures)

        draws = sum(
            1 for g in fixtures
            if g["goals"]["home"] == g["goals"]["away"]
        )

        result = {

            "total": total,
            "draw_rate": draws / total if total > 0 else 0

        }

        h2h_cache[key] = result

        return result

    except Exception as e:

        print(f"[ERROR] h2h {home_id}-{away_id}: {e}")

        return None


# ─────────────────────────────────────────
# DRAW SCORING MODEL
# ─────────────────────────────────────────

def calculate_draw_score(home, away, form_h, form_a, h2h):

    score = 0

    # Position Gap
    gap = abs(home["rank"] - away["rank"])

    if gap <= 1:
        score += 3
    elif gap <= 3:
        score += 2

    # Goal Difference Similarity
    gd_diff = abs(home["goal_diff"] - away["goal_diff"])

    if gd_diff <= 5:
        score += 2

    # Draw Rate
    hr = home["draws"] / max(home["played"], 1)
    ar = away["draws"] / max(away["played"], 1)

    avg_dr = (hr + ar) / 2

    if avg_dr >= 0.30:
        score += 3
    elif avg_dr >= 0.25:
        score += 2

    # Recent Form
    form_draws = form_h.count("D") + form_a.count("D")

    if form_draws >= 3:
        score += 2

    # H2H
    if h2h and h2h["draw_rate"] >= 0.30:
        score += 2

    return score


# ─────────────────────────────────────────
# DAILY ANALYSIS ENGINE
# ─────────────────────────────────────────

async def scheduled_daily_analysis(context: ContextTypes.DEFAULT_TYPE):

    global daily_results
    global last_run_date

    today = datetime.today().date()

    print("Running daily analysis...")

    matches = get_today_matches()

    candidates = []

    for game in matches:

        league_id = game["league"]["id"]

        if league_id not in ALLOWED_LEAGUES:
            continue

        season = game["league"]["season"]

        home_id = game["teams"]["home"]["id"]
        away_id = game["teams"]["away"]["id"]

        home_name = game["teams"]["home"]["name"]
        away_name = game["teams"]["away"]["name"]

        standings = get_standings(
            league_id,
            season
        )

        if not standings:
            continue

        home = standings.get(home_id)
        away = standings.get(away_id)

        if not home or not away:
            continue

        form_h = get_recent_form(
            home_id,
            league_id,
            season
        )

        form_a = get_recent_form(
            away_id,
            league_id,
            season
        )

        h2h = get_h2h(
            home_id,
            away_id
        )

        score = calculate_draw_score(
            home,
            away,
            form_h,
            form_a,
            h2h
        )

        if score >= 7:

            candidates.append({

                "match":
                    f"{home_name} vs {away_name}",

                "league":
                    game["league"]["name"],

                "score":
                    score

            })

        time.sleep(0.4)

    if not candidates:

        daily_results = "No strong draw candidates today."

        return

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    top3 = candidates[:3]

    msg = "🎯 Today's Strong Draw Picks:\n\n"

    for i, c in enumerate(top3, 1):

        msg += (

            f"{i}) {c['match']}\n"
            f"🏆 {c['league']}\n"
            f"⭐ Score: {c['score']}/10\n\n"

        )

    daily_results = msg
    last_run_date = today

    print("Daily analysis completed.")


# ─────────────────────────────────────────
# TELEGRAM COMMANDS
# ─────────────────────────────────────────

async def strongdraws_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if daily_results:

        await update.message.reply_text(
            daily_results
        )

    else:

        await update.message.reply_text(
            "No results yet. Wait for daily analysis."
        )


# ─────────────────────────────────────────
# MAIN ENTRY
# ─────────────────────────────────────────

if __name__ == "__main__":

    app = ApplicationBuilder().token(
        TELEGRAM_TOKEN
    ).build()

    app.add_handler(
        CommandHandler(
            "strongdraws",
            strongdraws_command
        )
    )

    job_queue = app.job_queue

    job_queue.run_daily(

        scheduled_daily_analysis,

        time=dtime(
            hour=0,
            minute=5
        )

    )

    print("Bot running...")

    app.run_polling()
