# main.py
import os
import json
import random
import logging
from datetime import datetime
from fastapi import FastAPI
import asyncio
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot")

app = FastAPI()
TB_TOKEN = os.getenv("TB_TOKEN")  # Device token từ Render env
PORT = int(os.getenv("PORT", 10000))

# Crop + hành động (action) tự sinh logic
CROPS = ["rau muống", "cà chua", "lúa"]
ACTIONS = {
    "rau muống": ["tưới nước", "bón phân hữu cơ", "tỉa lá già"],
    "cà chua": ["tưới nước", "bón phân NPK", "phòng sâu bệnh", "hỗ trợ ra hoa"],
    "lúa": ["bón phân đợt 1", "bón phân đợt 2", "tỉa lá", "phòng sâu"]
}

_last_push = {"ok": False, "status": None, "body": None, "time": None}
_sent_pairs = set()  # tránh lặp crop+action

def generate_payload():
    attempts = 0
    while attempts < 20:
        crop = random.choice(CROPS)
        action = random.choice(ACTIONS[crop])
        key = (crop, action)
        if key not in _sent_pairs:
            _sent_pairs.add(key)
            question = f"{action} cho {crop}"
            return {"shared": {"crop": crop, "hoi": question}}
        attempts += 1
    # Nếu lặp lại nhiều lần, reset sent_pairs
    _sent_pairs.clear()
    crop = random.choice(CROPS)
    action = random.choice(ACTIONS[crop])
    question = f"{action} cho {crop}"
    _sent_pairs.add((crop, action))
    return {"shared": {"crop": crop, "hoi": question}}

def make_advice_text(shared: dict) -> str:
    crop = shared.get("crop", "unknown")
    hoi = shared.get("hoi", "")
    return f"AI advice placeholder for crop {crop} — question: {hoi}"

async def push_to_tb(payload: dict):
    global _last_push
    if not TB_TOKEN:
        logger.warning("⚠️ TB_TOKEN chưa được cấu hình! Chỉ log locally.")
        _last_push.update({"ok": False, "status": "no_token", "body": None, "time": datetime.utcnow().isoformat()})
        return

    advice_text = make_advice_text(payload["shared"])
    send_payload = {"advice_text": advice_text, "_ts": int(datetime.utcnow().timestamp() * 1000)}
    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=send_payload, timeout=10)
            status = resp.status_code
            body_text = resp.text
            _last_push.update({"ok": 200 <= status < 300, "status": status, "body": body_text, "time": datetime.utcnow().isoformat()})
            logger.info(f"✅ Sent to ThingsBoard: {send_payload}")
        except Exception as e:
            logger.exception(f"❌ Failed to push telemetry: {e}")
            _last_push.update({"ok": False, "status": "exception", "body": str(e), "time": datetime.utcnow().isoformat()})

async def auto_send_loop(interval_sec: int = 300):
    logger.info(f"🚀 Auto-send loop started. Interval: {interval_sec}s")
    while True:
        payload = generate_payload()
        logger.info(f"🚀 Auto-generated payload at {datetime.utcnow().isoformat()}")
        logger.info(json.dumps(payload, ensure_ascii=False))
        await push_to_tb(payload)
        await asyncio.sleep(interval_sec)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_send_loop(interval_sec=300))

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last-push")
def last_push():
    """Return last push status for debugging (no secrets)."""
    return _last_push

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
