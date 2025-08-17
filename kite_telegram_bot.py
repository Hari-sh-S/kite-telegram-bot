# kite_telegram_bot.py
import os
import json
from datetime import datetime, timedelta
from threading import Thread

from flask import Flask, request
from kiteconnect import KiteConnect
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- CONFIG -----------------
API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIRECT_URL = os.getenv("REDIRECT_URL", "http://127.0.0.1:5000/callback")
TOKENS_FILE = "tokens.json"
PORT = int(os.getenv("PORT", 5000))

if not (API_KEY and API_SECRET and TELEGRAM_TOKEN):
    print("⚠️ Missing environment variables! Please set API_KEY, API_SECRET, TELEGRAM_TOKEN.")

kite = None

# ---------------- Token Helpers -----------------
def save_tokens(tokens: dict):
    data = tokens.copy()
    if "expires_at" in data and isinstance(data["expires_at"], datetime):
        data["expires_at"] = data["expires_at"].isoformat()
    data["saved_at"] = datetime.now().isoformat()
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f)

def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return None
    with open(TOKENS_FILE, "r") as f:
        data = json.load(f)
    if "saved_at" in data:
        try:
            data["saved_at"] = datetime.fromisoformat(data["saved_at"])
        except Exception:
            data["saved_at"] = datetime.now() - timedelta(days=1)
    return data

# ---------------- Kite Client -----------------
def kite_client_with_token(access_token: str):
    k = KiteConnect(api_key=API_KEY)
    k.set_access_token(access_token)
    return k

def is_access_token_valid(access_token: str) -> bool:
    try:
        k = kite_client_with_token(access_token)
        k.profile()
        return True
    except Exception:
        return False

def ensure_tokens_valid() -> bool:
    global kite
    saved = load_tokens()
    if not saved:
        return False
    access_token = saved.get("access_token")
    if access_token and is_access_token_valid(access_token):
        kite = kite_client_with_token(access_token)
        return True
    refresh_token = saved.get("refresh_token")
    if not refresh_token:
        return False
    try:
        kite_base = KiteConnect(api_key=API_KEY)
        newdata = kite_base.renew_access_token(refresh_token, api_secret=API_SECRET)
        merged = {**saved, **newdata}
        save_tokens(merged)
        kite = kite_client_with_token(merged["access_token"])
        return True
    except Exception as e:
        print("Token refresh failed:", e)
        return False

# ---------------- Flask Callback -----------------
app = Flask(__name__)

@app.route("/callback")
def callback():
    req_token = request.args.get("request_token")
    if not req_token:
        return "❌ No request_token received. Login failed.", 400
    try:
        kite_base = KiteConnect(api_key=API_KEY)
        session = kite_base.generate_session(req_token, api_secret=API_SECRET)
        save_tokens(session)
        return "✅ Login success! You can return to Telegram and use /snapshot."
    except Exception as e:
        return f"❌ Error creating session: {e}", 500

# ---------------- Telegram Commands -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Use /login to authenticate Kite, then /snapshot to view portfolio."
    )

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kite_base = KiteConnect(api_key=API_KEY)
    login_url = kite_base.login_url()
    login_url += f"&redirect_uri={REDIRECT_URL}"
    await update.message.reply_text(f"🔐 Click to login to Kite:\n{login_url}")

def format_portfolio_table(holdings):
    header = "📌 Portfolio Snapshot\n\n"
    rows = []
    rows.append(f"{'Symbol':<10} {'Qty':>5} {'Avg':>10} {'LTP':>10} {'P&L':>12}")
    rows.append("-"*52)
    total_pnl = 0.0
    for h in holdings:
        sym = h.get("tradingsymbol", "N/A")
        qty = h.get("quantity", 0)
        avg = h.get("average_price", 0.0) or 0.0
        ltp = h.get("last_price", 0.0) or 0.0
        try:
            pnl = (ltp - avg) * qty
        except Exception:
            pnl = 0.0
        total_pnl += pnl
        emoji = "🟢" if pnl >= 0 else "🔴"
        rows.append(f"{sym:<10} {qty:>5} {avg:>10.2f} {ltp:>10.2f} {emoji} {pnl:>9.2f}")
    rows.append("-"*52)
    tot_emoji = "🟢" if total_pnl >= 0 else "🔴"
    rows.append(f"{'TOTAL':<10} {'':>5} {'':>10} {'':>10} {tot_emoji} {total_pnl:>9.2f}")
    return header + "<pre>" + "\n".join(rows) + "</pre>"

async def snapshot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_tokens_valid():
        await update.message.reply_text("⚠️ Session missing/expired. Please use /login and complete the flow.")
        return
    try:
        holdings = kite.holdings() or []
        if not holdings:
            await update.message.reply_text("📭 No holdings found.")
            return
        html_msg = format_portfolio_table(holdings)
        await update.message.reply_text(html_msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching portfolio: {e}")

# ---------------- Run Flask + Telegram -----------------
def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def main():
    Thread(target=run_flask, daemon=True).start()
    if load_tokens():
        print("Attempting to validate or refresh saved tokens at startup...")
        if ensure_tokens_valid():
            print("Tokens valid/refreshed at startup.")
        else:
            print("Saved tokens invalid/refresh failed. Use /login.")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("snapshot", snapshot_cmd))
    print("Bot started. Waiting for Telegram commands.")
    application.run_polling()

if __name__ == "__main__":
    main()
