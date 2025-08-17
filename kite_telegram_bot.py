# kite_telegram_bot.py
import os
import json
from datetime import datetime, timedelta
from threading import Thread

from flask import Flask, request
from kiteconnect import KiteConnect
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------- CONFIG (from environment) -----------------
API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# If you want to store a custom redirect url env as well:
REDIRECT_URL = os.getenv("REDIRECT_URL")  # optional; must match Kite app settings

TOKENS_FILE = "tokens.json"
PORT = int(os.getenv("PORT", 5000))

if not (API_KEY and API_SECRET and TELEGRAM_TOKEN):
    print("⚠️ WARNING: some environment variables are missing (KITE_API_KEY, KITE_API_SECRET, TELEGRAM_BOT_TOKEN).")
    # still allow startup so Render will show logs; requests will error until envs are set.

# ----------------- KITE CLIENT helper -----------------
def kite_client_with_token(access_token: str):
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)
    return kite

# ----------------- Token storage helpers -----------------
def _to_serializable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (datetime, )):
            out[k] = v.isoformat()
        else:
            out[k] = v
    out["saved_at"] = datetime.now().isoformat()
    return out

def save_tokens(tokens: dict):
    with open(TOKENS_FILE, "w") as f:
        json.dump(_to_serializable(tokens), f)

def load_tokens():
    if not os.path.exists(TOKENS_FILE):
        return None
    with open(TOKENS_FILE, "r") as f:
        data = json.load(f)
    # convert saved_at back to datetime for age checks
    if "saved_at" in data:
        try:
            data["saved_at"] = datetime.fromisoformat(data["saved_at"])
        except Exception:
            data["saved_at"] = datetime.now() - timedelta(days=1)
    return data

# ----------------- Validate / refresh tokens -----------------
def is_access_token_valid(access_token: str) -> bool:
    try:
        kite = kite_client_with_token(access_token)
        kite.profile()  # quick test; will throw if invalid
        return True
    except Exception:
        return False

def ensure_tokens_valid() -> bool:
    """
    Returns True if we can use the Kite API (loads or refreshes tokens).
    Side-effect: sets a valid access token on a global kite_client used below.
    """
    saved = load_tokens()
    if not saved:
        return False

    # Try existing access token
    access_token = saved.get("access_token")
    if access_token and is_access_token_valid(access_token):
        # set global kite for use
        global kite
        kite = kite_client_with_token(access_token)
        return True

    # Try refresh using refresh_token (use renew_access_token)
    refresh_token = saved.get("refresh_token")
    if not refresh_token:
        return False

    try:
        kite_base = KiteConnect(api_key=API_KEY)
        # renew_access_token is the method to refresh; SDK name may be renew_access_token
        newdata = kite_base.renew_access_token(refresh_token, api_secret=API_SECRET)
        # newdata should contain new access_token (and maybe new refresh_token)
        merged = {**saved, **newdata}
        save_tokens(merged)
        # set global kite
        kite = kite_client_with_token(merged["access_token"])
        return True
    except Exception as e:
        print("Token refresh failed:", e)
        return False

# Initialize global kite (may be set by ensure_tokens_valid)
kite = None

# ----------------- Flask callback for login -----------------
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
        return "✅ Login success. You can return to Telegram and use /snapshot."
    except Exception as e:
        return f"❌ Error creating session: {e}", 500

# ----------------- Telegram command handlers -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! Use /login to authenticate Kite, then /snapshot to view portfolio.")

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kite_base = KiteConnect(api_key=API_KEY)
    # kite_base.login_url() uses redirect set in Kite dev console.
    login_url = kite_base.login_url()
    # If you set REDIRECT_URL env and want to enforce it append param (ensure it exactly matches Kite app)
    if REDIRECT_URL:
        login_url = login_url + f"&redirect_uri={REDIRECT_URL}"
    await update.message.reply_text("🔐 Click to login to Kite:\n" + login_url)

def _format_table_html(holdings):
    # Build a monospace table inside <pre> tags and color P&L with emoji green/red
    header = "📌 Portfolio Snapshot\n\n"
    rows = []
    rows.append(f"{'Symbol':<10} {'Qty':>5} {'Avg':>10} {'LTP':>10} {'P&L':>12}")
    rows.append("-" * 52)
    total_pnl = 0.0
    for h in holdings:
        sym = h.get("tradingsymbol", "N/A")
        qty = h.get("quantity", 0)
        avg = h.get("average_price", 0.0) or 0.0
        ltp = h.get("last_price", 0.0) or 0.0
        try:
            pnl = (float(ltp) - float(avg)) * float(qty)
        except Exception:
            pnl = 0.0
        total_pnl += pnl
        emoji = "🟢" if pnl >= 0 else "🔴"
        rows.append(f"{sym:<10} {qty:>5} {avg:>10.2f} {ltp:>10.2f} {emoji} {pnl:>9.2f}")
    rows.append("-" * 52)
    tot_emoji = "🟢" if total_pnl >= 0 else "🔴"
    rows.append(f"{'TOTAL':<10} {'':>5} {'':>10} {'':>10} {tot_emoji} {total_pnl:>9.2f}")
    # join in a preformatted block
    pre = "<pre>" + "\n".join(rows) + "</pre>"
    return header + pre

async def snapshot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = ensure_tokens_valid()
    if not ok:
        await update.message.reply_text("⚠️ Session missing / expired. Please use /login and complete the flow.")
        return

    try:
        holdings = kite.holdings() or []
        if not holdings:
            await update.message.reply_text("📭 No holdings found.")
            return
        html_msg = _format_table_html(holdings)
        await update.message.reply_text(html_msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching portfolio: {e}")

# ----------------- Run Flask + Telegram together -----------------
def run_flask():
    # Bind to host 0.0.0.0 and the PORT Render gives (or 5000 locally)
    app.run(host="0.0.0.0", port=PORT)

def main():
    # Start Flask as a background thread so Render can receive the redirect.
    Thread(target=run_flask, daemon=True).start()

    # Try to load or refresh tokens at startup (helpful to auto-connect)
    if load_tokens():
        print("Attempting to validate or refresh saved tokens at startup...")
        if ensure_tokens_valid():
            print("Tokens valid/ refreshed at startup.")
        else:
            print("Saved tokens invalid/refresh failed. Use /login.")

    # Start Telegram bot (blocking)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("snapshot", snapshot_cmd))

    print("Bot started. Waiting for Telegram commands.")
    application.run_polling()

if __name__ == "__main__":
    main()
