import os
import time
import asyncio
import requests
from datetime import datetime

from flask import Flask, request as flask_request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. Initialize Flask
app = Flask(__name__)

# 2. Environment Variables & Constants
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY")
BASE_URL = "https://api.sportmonks.com/v3/football"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://yourdomain.com/api/webhook

if not TELEGRAM_TOKEN or not SPORTMONKS_API_KEY:
    print("CRITICAL: Missing environment variables")
    exit(1)

if not WEBHOOK_URL:
    print("WARNING: WEBHOOK_URL not set. Webhook registration will be skipped.")

ALLOWED_LEAGUES = [
    ("Italy", "Serie B"), ("Italy", "Serie C"), ("Spain", "Segunda Division"),
    ("France", "Ligue 2"), ("Netherlands", "Eerste Divisie"), ("Germany", "3. Liga"),
    ("Portugal", "Liga 2"), ("Romania", "Liga II"), ("Poland", "I Liga"),
    ("Czech Republic", "FNL"), ("Turkey", "1. Lig"), ("England", "League One"),
    ("Sweden", "Superettan"), ("Norway", "OBOS Ligaen"), ("Denmark", "Division 1"),
    ("Argentina", "Primera Nacional"), ("Greece", "Super League 2"),
    ("Uruguay", "Segunda Division"), ("Paraguay", "Division Intermedia"),
    ("Finland", "Ykkonen")
]

# 3. Cache & Globals
standings_cache = {}
daily_results = None

# 4. Telegram Bot Setup
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# 5. Helper Functions
def _sm_get(path, params=None):
    if params is None:
        params = {}
    params["api_token"] = SPORTMONKS_API_KEY
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=7)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[ERROR API] {e}")
        return None

def _extract_teams(fixture):
    home_id, away_id, home_name, away_name = None, None, None, None
    for p in fixture.get("participants", []):
        loc = p.get("meta", {}).get("location")
        if loc == "home":
            home_id, home_name = p.get("id"), p.get("name")
        elif loc == "away":
            away_id, away_name = p.get("id"), p.get("name")
    return home_id, away_id, home_name, away_name

def get_standings_by_season(season_id):
    key = str(season_id)
    if key in standings_cache:
        return standings_cache[key]
    data = _sm_get(f"/standings/seasons/{season_id}", {"include": "participant;details"})
    if not data or not data.get("data"):
        return None
    
    result = {}
    for entry in data.get("data", []):
        team_id = entry.get("participant_id")
        if not team_id:
            continue
        details = entry.get("details", [])
        
        def get_val(tid):
            for d in details:
                if d.get("type_id") == tid:
                    return int(d.get("value", 0))
            return 0
            
        wins, draws, losses = get_val(46), get_val(47), get_val(48)
        played = max(wins + draws + losses, 1)
        gf, ga = get_val(52), get_val(53)
        
        result[team_id] = {
            "rank": entry.get("position", 99),
            "played": played,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": gf,
            "goals_against": ga,
            "goal_diff": gf - ga
        }
    standings_cache[key] = result
    return result

def run_analysis():
    global daily_results
    today = datetime.utcnow().strftime("%Y-%m-%d")
    data = _sm_get(f"/fixtures/date/{today}", {"include": "league;participants"})
    if not data:
        daily_results = "⚽ No fixtures data available today."
        return
    
    candidates = []
    for game in data.get("data", []):
        league_data = game.get("league", {})
        l_name = league_data.get("name", "").lower()
        c_name = league_data.get("country", {}).get("name", "").lower()
        
        match_league = False
        for country, league in ALLOWED_LEAGUES:
            if country.lower() in c_name and league.lower() in l_name:
                match_league = True
                break
        
        if not match_league:
            continue
        
        season_id = game.get("season_id") or league_data.get("season_id")
        if not season_id:
            continue
        
        h_id, a_id, h_name, a_name = _extract_teams(game)
        if not all([h_id, a_id, h_name, a_name]):
            continue
            
        st = get_standings_by_season(season_id)
        if not st:
            continue
        
        home, away = st.get(h_id), st.get(a_id)
        if not home or not away:
            continue
        
        # FIXED: Better scoring logic for draw candidates
        # Prefer teams with similar standings and high draw rates
        rank_gap = abs(home["rank"] - away["rank"])
        home_draw_rate = home["draws"] / max(home["played"], 1)
        away_draw_rate = away["draws"] / max(away["played"], 1)
        avg_draw_rate = (home_draw_rate + away_draw_rate) / 2
        
        # Score based on: small rank gap + high draw rate in league
        score = 0
        if rank_gap <= 3:
            score += 2
        if rank_gap <= 6:
            score += 1
        if avg_draw_rate >= 0.25:
            score += 2
        if avg_draw_rate >= 0.20:
            score += 1
        
        if score >= 3:  # Adjusted threshold
            candidates.append({
                "match": f"{h_name} vs {a_name}",
                "league": league_data.get("name"),
                "score": score,
                "rank_gap": rank_gap,
                "draw_rate": f"{avg_draw_rate*100:.1f}%"
            })
        time.sleep(0.1)

    if not candidates:
        daily_results = "⚽ No strong draw candidates found today."
    else:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        msg = "🎯 Today's Draw Picks:\n\n"
        for i, c in enumerate(candidates[:3], 1):
            msg += f"{i}) {c['match']}\n🏆 {c['league']}\n📊 Rank Gap: {c['rank_gap']} | Draw Rate: {c['draw_rate']}\n⭐ Score: {c['score']}\n\n"
        daily_results = msg

# 6. Telegram Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot running. Use /testdraws or /strongdraws")

async def strongdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(daily_results or "No results. Run /testdraws first")

async def testdraws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Running analysis...")
    # Run analysis in thread pool to avoid blocking
    await asyncio.to_thread(run_analysis)
    await update.message.reply_text(daily_results or "Analysis complete but no candidates found.")

telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("strongdraws", strongdraws_command))
telegram_app.add_handler(CommandHandler("testdraws", testdraws_command))

# 7. Flask Routes
@app.route("/")
def home():
    return "Bot is alive!", 200

@app.route("/api/webhook", methods=["POST"])
def webhook():
    if flask_request.method == "POST":
        try:
            update = Update.de_json(flask_request.get_json(force=True), telegram_app.bot)
            asyncio.run(telegram_app.process_update(update))
            return "OK", 200
        except Exception as e:
            print(f"[ERROR Webhook] {e}")
            return "Error", 500
    return "Invalid method", 400

# 8. Startup: Register webhook with Telegram
async def setup_webhook():
    if WEBHOOK_URL:
        try:
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
            print(f"✅ Webhook registered: {WEBHOOK_URL}")
        except Exception as e:
            print(f"⚠️ Webhook registration failed: {e}")
    else:
        print("⚠️ No WEBHOOK_URL set. Using polling mode (not recommended for production).")

# Run on startup
if __name__ == "__main__":
    # Register webhook when app starts
    asyncio.run(setup_webhook())
    # Start Flask server
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
