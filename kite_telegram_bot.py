import os
import json
from flask import Flask, request
from kiteconnect import KiteConnect
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Load environment variables from .env (only needed locally, Render will ignore)
load_dotenv()

# 🔑 Read secrets from environment variables
API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TOKENS_FILE = "tokens.json"

app = Flask(__name__)

# ---------------- TOKEN MANAGEMENT ---------------- #
def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, default=str)

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    return None

def get_kite_client():
    tokens = load_tokens()
    kite = KiteConnect(api_key=API_KEY)
    if tokens and "access_token" in tokens:
        kite.set_access_token(tokens["access_token"])
        return kite
    return None

# ---------------- FLASK CALLBACK ---------------- #
@app.route("/callback")
def callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return "Missing request_token", 400

    kite = KiteConnect(api_key=API_KEY)
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        save_tokens(data)
        return "✅ Login successful! You can now use Telegram bot."
    except Exception as e:
        return f"❌ Error: {str(e)}"

# ---------------- TELEGRAM BOT COMMANDS ---------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Use /login to authenticate Zerodha, or /portfolio to view holdings."
    )

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()
    await update.message.reply_text(
        f"🔑 Please login here:\n{login_url}\n\n"
        "After logging in, Zerodha will redirect you back automatically."
    )

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kite = get_kite_client()
    if not kite:
        await update.message.reply_text("⚠️ Session expired. Please /login again.")
        return

    try:
        holdings = kite.holdings()
        if not holdings:
            await update.message.reply_text("📭 No holdings in portfolio.")
            return

        # Build a nice table-like message
        message = "📊 *Portfolio Snapshot:*\n\n"
        message += "`Symbol     Qty   AvgPrice   LTP    PnL`\n"
        message += "`----------------------------------------`\n"

        for h in holdings:
            pnl = (h["last_price"] - h["average_price"]) * h["quantity"]
            color = "🟢" if pnl >= 0 else "🔴"
            message += (
                f"`{h['tradingsymbol']:<10} {h['quantity']:<4} "
                f"{h['average_price']:<9.2f} {h['last_price']:<6.2f} {pnl:<.2f}` {color}\n"
            )

        await update.message.reply_text(message, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching portfolio: {str(e)}")

# ---------------- MAIN ---------------- #
def main():
    # Start Telegram bot
    app_bot = Application.builder().token(BOT_TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("login", login))
    app_bot.add_handler(CommandHandler("portfolio", portfolio))

    # Run Flask + Telegram bot together
    import threading
    threading.Thread(target=lambda: app.run(port=5000)).start()
    app_bot.run_polling()

if __name__ == "__main__":
    main()
