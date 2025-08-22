import os
import json
import logging
import requests
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
from pathlib import Path
import pprint
import random

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN")  # Demo device token

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

LOG_FILE = Path("/tmp/tb_payloads.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ================== AI CLIENTS ==================
async def ask_openai(prompt: str) -> str:
    if not OPENAI_KEY:
        raise ValueError("Missing OPENAI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 200},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def ask_openrouter(prompt: str) -> str:
    if not OPENROUTER_KEY:
        raise ValueError("Missing OPENROUTER_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "HTTP-Referer": "https://github.com/your/repo", "X-Title": "Agri-Bot"},
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 200},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def ask_hf(prompt: str) -> str:
    if not HF_KEY:
        raise ValueError("Missing HUGGINGFACE_API_TOKEN")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api-inference.huggingface.co/models/facebook/blenderbot-400M-distill",
            headers={"Authorization": f"Bearer {HF_KEY}"},
            json={"inputs": prompt},
        )
        r.raise_for_status()
        data = r.json()
        return data[0]["generated_text"].strip()

async def ask_gemini(prompt: str) -> str:
    if not GEMINI_KEY:
        raise ValueError("Missing GEMINI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

async def get_ai_advice(prompt: str) -> str:
    for fn in [ask_openai, ask_openrouter, ask_gemini, ask_hf]:
        try:
            return await fn(prompt)
        except Exception as e:
            logging.warning(f"AI provider failed: {e}")
    return "Xin lỗi, hiện tại hệ thống AI không khả dụng."

# ================== THINGSBOARD ==================
def push_to_tb(data: dict):
    tb_token = os.getenv("TB_DEMO_TOKEN")
    if not tb_token:
        logging.error("❌ ThingsBoard token chưa được cấu hình!")
        return
    url = f"{TB_URL}/{tb_token}/telemetry"
    data["_ts"] = int(datetime.utcnow().timestamp() * 1000)
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {data}")
    except Exception as e:
        logging.error(f"❌ Failed to push telemetry: {e}")

def log_payload_to_file(payload: dict):
    timestamp = datetime.utcnow().isoformat()
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception as e:
        logging.error(f"❌ Failed to write payload to file: {e}")

# ================== ENDPOINTS ==================
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    try:
        body = await req.json()
        logging.info("📩 Got TB webhook payload:")
        logging.info(pprint.pformat(body, width=120))
        log_payload_to_file(body)

        shared = body.get("shared", {})
        hoi = shared.get("hoi", "Hãy đưa ra lời khuyên nông nghiệp.")
        crop = shared.get("crop", "cây trồng")
        location = shared.get("location", "Hồ Chí Minh")

        prompt = f"Người dùng hỏi: {hoi}\nCây trồng: {crop}\nVị trí: {location}\nHãy trả lời ngắn gọn, thực tế, dễ hiểu."

        advice_text = await get_ai_advice(prompt)

        push_to_tb({"advice_text": advice_text})

        return {"status": "ok", "advice_text": advice_text}

    except Exception as e:
        logging.error(f"❌ Error handling webhook: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

@app.get("/")
def root():
    return {"status": "running"}

# ================== AUTO SIMULATE ESP32 PAYLOAD ==================
def generate_fake_sensor():
    return {
        "shared": {
            "hoi": random.choice([
                "cách trồng rau muống",
                "tưới nước cho cà chua",
                "bón phân cho lúa"
            ]),
            "crop": random.choice(["rau muống", "cà chua", "lúa"]),
            "location": "Hồ Chí Minh",
            "temperature": round(random.uniform(24, 32), 1),
            "humidity": round(random.uniform(60, 90), 1),
            "battery": round(random.uniform(3.5, 4.2), 2)
        }
    }

def send_fake_payload():
    payload = generate_fake_sensor()
    logging.info("📤 Auto-sending fake ESP32 payload:")
    logging.info(pprint.pformat(payload, width=120))
    log_payload_to_file(payload)
    try:
        requests.post(f"http://localhost:10000/tb-webhook", json=payload, timeout=10)
    except Exception as e:
        logging.error(f"❌ Failed auto-send payload: {e}")

scheduler.add_job(send_fake_payload, "interval", minutes=5)
