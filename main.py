# main_test_quick.py
import os
import json
import random
import logging
from datetime import datetime
import asyncio
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agri-bot-test")

# Token từ env
TB_TOKEN = os.getenv("TB_TOKEN")
if not TB_TOKEN:
    raise RuntimeError("⚠️ TB_TOKEN chưa được cấu hình!")

# Crop + hành động (action)
CROPS = ["rau muống", "cà chua", "lúa"]
ACTIONS = {
    "rau muống": ["tưới nước", "bón phân hữu cơ", "tỉa lá già"],
    "cà chua": ["tưới nước", "bón phân NPK", "phòng sâu bệnh", "hỗ trợ ra hoa"],
    "lúa": ["bón phân đợt 1", "bón phân đợt 2", "tỉa lá", "phòng sâu"]
}

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
    advice_text = make_advice_text(payload["shared"])
    send_payload = {"advice_text": advice_text, "_ts": int(datetime.utcnow().timestamp() * 1000)}
    url = f"https://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=send_payload, timeout=10)
            status = resp.status_code
            logger.info(f"✅ Sent ({status}): {send_payload}")
        except Exception as e:
            logger.exception(f"❌ Failed to push telemetry: {e}")

async def push_10_payloads_quick():
    logger.info("🚀 Starting quick test: push 10 payloads immediately")
    for i in range(10):
        payload = generate_payload()
        logger.info(f"🚀 Payload {i+1}: {json.dumps(payload, ensure_ascii=False)}")
        await push_to_tb(payload)
        await asyncio.sleep(0.2)  # small delay để không bị rate limit

if __name__ == "__main__":
    asyncio.run(push_10_payloads_quick())
    logger.info("✅ Quick test finished. Check ThingsBoard Latest Telemetry.")
