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

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_DEMO_TOKEN", "your_tb_token_here")

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = BackgroundScheduler()
scheduler.start()

# File tạm lưu payload ThingsBoard
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
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

async def ask_openrouter(prompt: str) -> str:
    if not OPENROUTER_KEY:
        raise ValueError("Missing OPENROUTER_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "HTTP-Referer": "https://github.com/your/repo",
                "X-Title": "Agri-Bot",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
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
    return "Xin lỗi, hiện tại hệ thống AI không khả dụng. Vui lòng thử lại sau hoặc cấu hình API key."

# ================== THINGSBOARD ==================
def push_to_tb(data: dict):
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    data["_ts"] = int(datetime.utcnow().timestamp() * 1000)
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {data}")
    except Exception as e:
        logging.error(f"❌ Failed to push telemetry: {e}")

def log_payload_to_file(payload: dict):
    """Lưu payload ThingsBoard vào file tạm /tmp/tb_payloads.log"""
    timestamp = datetime.utcnow().isoformat()
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {json.dumps(payload, ensure_ascii=False)}\n")
    except Exception as e:
        logging.error(f"❌ Failed to write payload to file: {e}")

# ================== ENDPOINTS ==================
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    logging.info("📩 Got TB webhook payload:")
    logging.info(pprint.pformat(body, width=120))
    log_payload_to_file(body)  # lưu payload vào file tạm

    shared = body.get("shared", {})
    hoi = shared.get("hoi", "Hãy đưa ra lời khuyên nông nghiệp.")
    crop = shared.get("crop", "cây trồng")
    location = shared.get("location", "Hồ Chí Minh")

    prompt = f"""
Người dùng hỏi: {hoi}
Cây trồng: {crop}
Vị trí: {location}

Hãy trả lời ngắn gọn, thực tế, dễ hiểu cho nông dân. 
Chỉ cần đưa ra 1 đoạn văn duy nhất.
"""

    advice_text = await get_ai_advice(prompt)
    push_to_tb({"advice_text": advice_text})
    return {"status": "ok", "advice_text": advice_text}

@app.get("/")
def root():
    return {"status": "running"}

# ================== STARTUP ==================
@app.on_event("startup")
def init():
    logging.info("🚀 Agri-Bot AI debug mode started, waiting for ThingsBoard...")
    logging.info(f"📂 Payload log file: {LOG_FILE}")
