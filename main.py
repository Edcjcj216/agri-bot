# main.py
import os
import json
import random
import logging
import asyncio
from datetime import datetime
from fastapi import FastAPI
import httpx
from geopy.geocoders import Nominatim

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

# ================== CONFIG ==================
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard device token
if not TB_TOKEN:
    logger.warning("⚠️ TB_TOKEN chưa được cấu hình! Chỉ log locally.")

CROP = "rau muống"
LATITUDE = 10.806094263669602
LONGITUDE = 106.75222004270555

_last_push = {"ok": False, "status": None, "body": None, "time": None}

geolocator = Nominatim(user_agent="agri-bot-geocoder")

# ================== APP ==================
app = FastAPI()

# ================== AI ADVICE GENERATOR ==================
def generate_ai_advice(crop: str):
    temp_now = round(24 + random.uniform(-2, 4), 1)
    humidity_now = round(60 + random.uniform(-10, 20), 1)
    days_ago = random.randint(1, 3)
    temp_diff = round(temp_now - (24 + random.uniform(-2, 4)), 1)
    advice = (
        f"Hôm nay {crop} cần chăm sóc. "
        f"Nhiệt độ {temp_now}°C, độ ẩm {humidity_now}%. "
        f"So với {days_ago} ngày trước, nhiệt độ {'cao hơn' if temp_diff>0 else 'thấp hơn'} {abs(temp_diff)}°C, "
        f"hãy điều chỉnh tưới nước và bón phân hợp lý."
    )
    return advice

# ================== GET ADDRESS ==================
def get_address(lat, lon):
    try:
        location = geolocator.reverse((lat, lon), exactly_one=True, timeout=10)
        return location.address if location else None
    except Exception as e:
        logger.warning(f"⚠️ Failed to get address: {e}")
        return None

# ================== PUSH TO THINGSBOARD ==================
async def push_to_tb(payload: dict):
    global _last_push
    advice_text = generate_ai_advice(CROP)
    address = get_address(LATITUDE, LONGITUDE)

    send_payload = {
        "advice_text": advice_text,
        "_ts": int(datetime.utcnow().timestamp() * 1000),
        "location": {"lat": LATITUDE, "lon": LONGITUDE},
        "address": address
    }

    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

    if not TB_TOKEN:
        logger.info(f"[LOCAL] {send_payload}")
        _last_push.update({"ok": False, "status": "no_token", "body": None, "time": datetime.utcnow().isoformat()})
        return

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=send_payload, timeout=10)
            status = resp.status_code
            _last_push.update({"ok": 200 <= status < 300, "status": status, "body": send_payload, "time": datetime.utcnow().isoformat()})
            logger.info(f"✅ Sent to ThingsBoard: {send_payload}")
        except Exception as e:
            logger.exception(f"❌ Failed to push telemetry: {e}")
            _last_push.update({"ok": False, "status": "exception", "body": str(e), "time": datetime.utcnow().isoformat()})

# ================== QUICK TEST 10 PAYLOAD ==================
async def push_10_quick():
    logger.info("🚀 Quick test: push 10 payloads immediately for crop rau muống")
    for i in range(10):
        await push_to_tb({})
        await asyncio.sleep(0.2)

# ================== AUTO-SEND LOOP ==================
async def auto_send_loop(interval_sec: int = 300):
    logger.info(f"🚀 Auto-send loop started. Interval: {interval_sec}s")
    while True:
        await push_to_tb({})
        await asyncio.sleep(interval_sec)

# ================== STARTUP ==================
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(push_10_quick())       # push 10 payload đầu tiên ngay khi deploy
    asyncio.create_task(auto_send_loop(300))   # auto-send 5 phút/lần

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last-push")
def last_push():
    return _last_push

# ================== MAIN ==================
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("PORT", 10000))
    logger.info(f"Starting server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
