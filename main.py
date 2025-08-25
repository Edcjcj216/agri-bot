import os
import logging
import requests
import httpx
import asyncio
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cohere import Client as CohereClient

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")

COHERE_KEY = os.getenv("COHERE_API_KEY")
DEEPAI_KEY = os.getenv("DEEPAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = AsyncIOScheduler()
scheduler.start()

# ================== INIT AI CLIENT ==================
cohere_client = CohereClient(COHERE_KEY) if COHERE_KEY else None

# ================== LAST TELEMETRY ==================
last_telemetry = {}

# ================== AI PROVIDER FUNCTIONS ==================
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

async def ask_cohere(prompt: str) -> str:
    if not cohere_client:
        raise ValueError("Missing COHERE_API_KEY")
    loop = asyncio.get_event_loop()
    def call_cohere():
        response = cohere_client.generate(model="xlarge", prompt=prompt, max_tokens=200)
        return response.generations[0].text.strip()
    return await loop.run_in_executor(None, call_cohere)

async def ask_deepai(prompt: str) -> str:
    if not DEEPAI_KEY:
        raise ValueError("Missing DEEPAI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.deepai.org/api/text-generator",
            headers={"api-key": DEEPAI_KEY},
            data={"text": prompt},
        )
        r.raise_for_status()
        return r.json().get("output", "").strip()

# ================== AI ADVICE STRICT ==================
async def get_ai_advice_strict(prompt: str) -> str:
    for fn in [ask_gemini, ask_cohere, ask_deepai, ask_hf]:
        try:
            resp = await fn(prompt)
            if resp.strip():  # Chỉ trả nếu có nội dung
                return resp.strip()
        except Exception as e:
            logging.warning(f"AI provider failed: {e}")
    # Nếu tất cả provider fail, trả lời cố định dựa trên câu hỏi
    return f"Xin lỗi, hiện tại hệ thống AI không khả dụng để trả lời: '{prompt.strip()}'"

# ================== PUSH THINGSBOARD ==================
def push_to_tb(data: dict):
    global last_telemetry
    url = f"{TB_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        logging.info(f"✅ Sent to ThingsBoard: {data}")
        last_telemetry = data
    except Exception as e:
        logging.error(f"❌ Failed to push telemetry: {e}")

# ================== FASTAPI ENDPOINTS ==================
@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    body = await req.json()
    shared = body.get("shared", {})
    hoi = shared.get("hoi", "")
    crop = shared.get("crop", "")
    location = shared.get("location", "")

    logging.info(f"💬 Câu hỏi nhận được: {hoi}")

    prompt = f"""
Người dùng hỏi: {hoi}
Cây trồng: {crop}
Vị trí: {location}

Hãy trả lời NGAY lập tức, ngắn gọn, thực tế, dễ hiểu cho nông dân. 
Chỉ 1 đoạn văn duy nhất, KHÔNG hỏi lại hay yêu cầu thêm thông tin.
"""
    advice_text = await get_ai_advice_strict(prompt)
    push_to_tb({"advice_text": advice_text})
    return {"status": "ok", "advice_text": advice_text}

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last-push")
def get_last_push():
    return {"last_telemetry": last_telemetry}

# ================== SCHEDULER 5 PHÚT PUSH THINGSBOARD ==================
async def scheduled_push_async():
    prompt = "Cập nhật lời khuyên nông nghiệp tự động"
    advice_text = await get_ai_advice_strict(prompt)
    push_to_tb({"advice_text": advice_text})
    logging.info("⏱️ Scheduled push completed.")

def scheduled_push():
    asyncio.create_task(scheduled_push_async())

scheduler.add_job(scheduled_push, 'interval', minutes=5)

# ================== STARTUP ==================
@app.on_event("startup")
async def init():
    logging.info("🚀 Agri-Bot AI service started, waiting for ThingsBoard...")
    try:
        await scheduled_push_async()
    except Exception as e:
        logging.error(f"❌ Initial push failed: {e}")
