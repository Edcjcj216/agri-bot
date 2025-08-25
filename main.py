# main.py
import os
import logging
import asyncio
from typing import Any, Dict

import requests
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============== CONFIG ==============
TB_BASE_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")  # ThingsBoard device token (bắt buộc để push telemetry)

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
COHERE_KEY = os.getenv("COHERE_API_KEY")
DEEPAI_KEY = os.getenv("DEEPAI_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")

# Cohere optional import (không bắt buộc cài)
try:
    from cohere import Client as CohereClient
except Exception:
    CohereClient = None

# ============== LOGGING ==============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI()
scheduler = AsyncIOScheduler()
scheduler.start()

cohere_client = CohereClient(COHERE_KEY) if (CohereClient and COHERE_KEY) else None
last_telemetry: Dict[str, Any] = {}

# ============== UTILS ==============
def limit_to_3_sentences(text: str) -> str:
    """Giới hạn 1–3 câu, nhận diện .?! và dọn khoảng trắng."""
    if not text:
        return ""
    import re
    txt = " ".join(str(text).split())
    parts = re.split(r"(?<=[\.!\?])\s+", txt)
    parts = [p.strip() for p in parts if p.strip()]
    out = " ".join(parts[:3])
    if out and out[-1] not in ".!?":
        out += "."
    return out

def push_to_tb(data: dict):
    """Push telemetry lên ThingsBoard qua device token v1 API."""
    global last_telemetry
    if not TB_TOKEN:
        logging.error("❌ TB_TOKEN chưa được set. Bỏ qua push telemetry.")
        return
    url = f"{TB_BASE_URL}/{TB_TOKEN}/telemetry"
    try:
        r = requests.post(url, json=data, timeout=15)
        r.raise_for_status()
        logging.info("✅ Sent to ThingsBoard: %s", data)
        last_telemetry = data
    except Exception as e:
        logging.exception("❌ Failed to push telemetry to ThingsBoard: %s", e)

# ============== AI PROVIDERS (async) ==============
async def ask_gemini(prompt: str) -> str:
    if not GEMINI_KEY:
        raise ValueError("Missing GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        j = r.json()
        # best effort parse
        try:
            return j["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            return str(j)

async def ask_cohere(prompt: str) -> str:
    if not COHERE_KEY or not cohere_client:
        raise ValueError("Missing COHERE_API_KEY or cohere package not installed")
    loop = asyncio.get_event_loop()
    def _call():
        resp = cohere_client.generate(model="xlarge", prompt=prompt, max_tokens=200)
        return resp.generations[0].text.strip()
    return await loop.run_in_executor(None, _call)

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
        j = r.json()
        return (j.get("output") or j.get("id") or str(j)).strip()

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
        j = r.json()
        if isinstance(j, list) and j and isinstance(j[0], dict) and "generated_text" in j[0]:
            return j[0]["generated_text"].strip()
        return str(j)

async def get_ai_advice(prompt: str) -> str:
    """Thử các provider theo thứ tự; trả 1–3 câu; không ném lỗi ra ngoài."""
    providers = [ask_gemini, ask_cohere, ask_deepai, ask_hf]
    for fn in providers:
        try:
            res = await fn(prompt)
            if res:
                return limit_to_3_sentences(res)
        except Exception as e:
            logging.warning("AI provider %s failed: %s", fn.__name__, e)
    return "Xin lỗi, hiện tại hệ thống AI tạm thời không khả dụng."

# ============== FASTAPI ENDPOINTS ==============
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/last-push")
def get_last_push():
    return {"last_telemetry": last_telemetry}

@app.post("/tb-webhook")
async def tb_webhook(req: Request):
    """
    Nhận body từ Rule Chain/Webhook server.
    Hỗ trợ các dạng:
    {
      "shared": { "hoi": "...", "crop": "...", "location": "...", "advice_text": "..." }
    }
    hoặc top-level.
    """
    # 1) Parse JSON an toàn
    try:
        body = await req.json()
    except Exception:
        logging.exception("❌ tb-webhook: Invalid JSON")
        return JSONResponse(status_code=400, content={"status": "error", "error": "Invalid JSON"})

    logging.info("🔎 tb-webhook body: %s", body)

    # 2) Normalize -> shared dict
    shared = {}
    if isinstance(body, dict):
        if isinstance(body.get("shared"), dict):
            shared = body["shared"]
        elif isinstance(body.get("data"), dict):
            shared = body["data"]
        else:
            shared = body
    # ensure strings
    def _s(val: Any) -> str:
        return (val or "").__str__().strip()

    hoi = _s(shared.get("hoi") or body.get("hoi"))
    crop = _s(shared.get("crop") or body.get("crop"))
    location = _s(shared.get("location") or body.get("location"))
    advice_from_body = _s(shared.get("advice_text") or body.get("advice_text"))

    logging.info("💬 Câu hỏi nhận được: %r | crop=%r | location=%r", hoi, crop, location)

    # 3) Tạo prompt + quyết định có gọi AI không
    if not hoi and not advice_from_body:
        advice_text = "Xin hãy gửi câu hỏi cụ thể (ví dụ: 'cách trồng rau muống')."
    elif advice_from_body:
        # ưu tiên dùng advice đã có trong body (khỏi gọi AI)
        advice_text = limit_to_3_sentences(advice_from_body)
    else:
        prompt = (
            f"Người dùng hỏi: {hoi}\n"
            f"Cây trồng: {crop}\n"
            f"Vị trí: {location}\n\n"
            "Hãy trả lời NGAY lập tức, ngắn gọn 1–3 câu, thực tế, dễ hiểu cho nông dân. "
            "KHÔNG hỏi lại hay yêu cầu thêm thông tin."
        )
        try:
            advice_text = await get_ai_advice(prompt)
        except Exception:
            logging.exception("❌ tb-webhook: get_ai_advice failed")
            advice_text = "Xin lỗi, hiện tại hệ thống AI tạm thời không khả dụng."

    # 4) Push telemetry lên ThingsBoard (an toàn, không văng 500)
    try:
        push_to_tb({"advice_text": advice_text})
    except Exception:
        logging.exception("❌ tb-webhook: push_to_tb raised exception")

    # 5) Trả response cho caller
    return JSONResponse(status_code=200, content={"status": "ok", "advice_text": advice_text})

# ============== SCHEDULER ==============
async def scheduled_push_async():
    prompt = "Cập nhật tổng quan nông nghiệp tự động cực ngắn, 1–3 câu."
    try:
        advice_text = await get_ai_advice(prompt)
    except Exception:
        logging.exception("❌ scheduled_push_async: get_ai_advice failed")
        advice_text = "Tin nhanh nông nghiệp: duy trì tưới tiêu ổn định và kiểm tra sâu bệnh định kỳ."
    push_to_tb({"advice_text": advice_text})
    logging.info("⏱️ Scheduled push completed.")

def scheduled_push():
    asyncio.create_task(scheduled_push_async())

scheduler.add_job(scheduled_push, "interval", minutes=5, id="agri_auto_push")

# ============== STARTUP HOOK ==============
@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Agri-Bot AI service started.")
    try:
        await scheduled_push_async()
    except Exception:
        logging.exception("❌ Initial scheduled push failed")
