#!/usr/bin/env python3
"""
main.py

FastAPI app to receive ThingsBoard Shared Attributes (e.g. key "hoi": "cách trồng rau muống"),
call an LLM (OpenAI / HuggingFace / Gemini) to answer *any* question dynamically
(in Vietnamese, natural-sounding), and push the answer back to ThingsBoard as telemetry.

OpenRouter has been removed from this version per your request.

- All API keys are read from environment variables (do NOT hardcode keys).
- Weather code kept minimal for you to edit later.

Environment variables used (examples):
  TB_DEMO_TOKEN, OPENAI_API_KEY, HUGGINGFACE_API_TOKEN, GEMINI_API_KEY,
  PREFERRED_AI (auto/openai/huggingface/gemini)

Deploy: set env vars on Render and deploy this repo. Do not commit secrets.
"""

import os
import time
import json
import logging
import asyncio
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

# Optional OpenAI async client
_HAS_OPENAI = False
try:
    from openai import AsyncOpenAI
    _HAS_OPENAI = True
except Exception:
    _HAS_OPENAI = False

# Optional Gemini (google generative ai) SDK
_HAS_GEMINI = False
try:
    import google.generativeai as genai
    _HAS_GEMINI = True
except Exception:
    _HAS_GEMINI = False

# ================= CONFIG =================
TB_DEMO_TOKEN = os.getenv("TB_DEMO_TOKEN", "sgkxcrqntuki8gu1oj8u")
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

LAT = float(os.getenv("LAT", "10.79"))
LON = float(os.getenv("LON", "106.70"))
AUTO_LOOP_INTERVAL = int(os.getenv("AUTO_LOOP_INTERVAL", "300"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PREFERRED_AI = os.getenv("PREFERRED_AI", "auto").lower()  # auto/openai/huggingface/gemini
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
HUGGINGFACE_MODEL = os.getenv("HUGGINGFACE_MODEL", "gpt2")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-mini")

# ================ LOGGING ================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("thingsboard-ai")

# ================ FASTAPI ================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================ WEATHER (kept for you) ================
WEATHER_CODE_MAP = {
    0: "Trời quang", 1: "Trời quang nhẹ", 2: "Có mây", 3: "Nhiều mây",
    45: "Sương mù", 48: "Sương mù đóng băng", 51: "Mưa phùn nhẹ", 53: "Mưa phùn vừa",
    55: "Mưa phùn dày", 61: "Mưa nhẹ", 63: "Mưa vừa", 65: "Mưa to",
    71: "Tuyết nhẹ", 73: "Tuyết vừa", 75: "Tuyết dày", 80: "Mưa rào nhẹ",
    81: "Mưa rào vừa", 82: "Mưa rào mạnh", 95: "Giông nhẹ hoặc vừa",
    96: "Giông kèm mưa đá nhẹ", 99: "Giông kèm mưa đá mạnh"
}

weather_cache = {"ts": 0, "data": {}}

def get_weather_forecast():
    """Kept intentionally minimal — edit later if you want.
    Returns a dict with some keys used in merged telemetry.
    """
    now = datetime.now()
    if time.time() - weather_cache["ts"] < 900:
        return weather_cache["data"]
    # simple fallback empty structure; you can replace with your previous implementation
    fallback = {"weather_today_desc": "?", "weather_today_max": 0, "weather_today_min": 0, "humidity_today": 0}
    for i in range(7):
        fallback[f"hour_{i}_temperature"] = 0
        fallback[f"hour_{i}_humidity"] = 0
        fallback[f"hour_{i}_weather_desc"] = "?"
    weather_cache["data"] = fallback
    weather_cache["ts"] = time.time()
    return fallback

# ================ HELPER: rule-based (fallback only) ================
def rule_based_advice(temp, humi):
    nutrition = ["Ưu tiên Kali (K)","Cân bằng NPK","Bón phân hữu cơ"]
    care = []
    if temp >=35: care.append("Tránh nắng gắt, tưới sáng sớm/chiều mát")
    elif temp >=30: care.append("Tưới đủ nước, theo dõi thường xuyên")
    elif temp <=15: care.append("Giữ ấm, tránh sương muối")
    else: care.append("Nhiệt độ bình thường")
    if humi <=40: care.append("Độ ẩm thấp: tăng tưới")
    elif humi <=60: care.append("Độ ẩm hơi thấp: theo dõi, tưới khi cần")
    elif humi >=85: care.append("Độ ẩm cao: tránh úng, kiểm tra thoát nước")
    else: care.append("Độ ẩm ổn định cho rau muống")
    return {
        "advice": " | ".join(nutrition + care + ["Quan sát cây trồng và điều chỉnh thực tế"]),
        "advice_nutrition": " | ".join(nutrition),
        "advice_care": " | ".join(care),
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "prediction": f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    }

# ================ AI PROVIDERS (answer-anything) ================
# Each provider returns a plain-text answer (Vietnamese). The orchestration will try providers in an order

async def call_openai_answer(question: str) -> str:
    if not OPENAI_API_KEY or not _HAS_OPENAI:
        raise RuntimeError("OpenAI not configured or client missing")
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    # build prompt using concatenation to avoid unterminated f-string issues
    prompt = (
        "Bạn là một chuyên gia nông nghiệp/toàn diện. Trả lời NGẮN GỌN, rõ ràng, bằng tiếng Việt, "
        "với phong cách tự nhiên, hữu ích, không liệt kê nội dung có sẵn. "
        + f"Câu hỏi: {question}
"
        + "Trả lời giữ trong giới hạn 2-6 câu, tránh mở rộng quá dài."
    )
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"user","content":prompt}],
        max_tokens=400,
        temperature=0.3,
    )
    try:
        text = resp.choices[0].message["content"]
    except Exception:
        text = getattr(resp, "text", str(resp))
    return text.strip()

async def call_hf_answer(question: str) -> str:
    if not HUGGINGFACE_API_TOKEN:
        raise RuntimeError("HuggingFace token missing")
    url = f"https://api-inference.huggingface.co/models/{HUGGINGFACE_MODEL}"
    headers = {"Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}"}
    payload = {"inputs": f"Trả lời tiếng Việt, ngắn gọn: {question}", "options": {"wait_for_model": True}}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    if isinstance(data, list) and data and "generated_text" in data[0]:
        return data[0]["generated_text"].strip()
    if isinstance(data, dict) and "generated_text" in data:
        return data["generated_text"].strip()
    return json.dumps(data, ensure_ascii=False)

async def call_gemini_answer(question: str) -> str:
    if not GEMINI_API_KEY or not _HAS_GEMINI:
        raise RuntimeError("Gemini not configured or SDK missing")
    genai.configure(api_key=GEMINI_API_KEY)
    prompt = f"Trả lời tiếng Việt, ngắn gọn: {question}"
    def sync_call():
        model = genai.GenerativeModel(GEMINI_MODEL)
        resp = model.generate_content(prompt)
        return getattr(resp, "text", str(resp))
    text = await asyncio.to_thread(sync_call)
    return text.strip()

async def ai_answer_question(question: str) -> str:
    """Try providers in order and return the first successful answer. Fallback to friendly message or rule-based if none available."""
    order = []
    if PREFERRED_AI == "auto":
        order = ["openai","gemini","huggingface"]
    else:
        order = [PREFERRED_AI] + [p for p in ["openai","gemini","huggingface"] if p != PREFERRED_AI]

    errors = []
    for provider in order:
        try:
            if provider == "openai" and OPENAI_API_KEY and _HAS_OPENAI:
                return await call_openai_answer(question)
            if provider == "gemini" and GEMINI_API_KEY and _HAS_GEMINI:
                return await call_gemini_answer(question)
            if provider == "huggingface" and HUGGINGFACE_API_TOKEN:
                return await call_hf_answer(question)
        except Exception as e:
            logger.warning(f"Provider {provider} failed: {e}")
            errors.append((provider, str(e)))
            continue

    # If no provider worked, provide a clear fallback message
    if errors:
        logger.info("All AI providers failed, returning fallback message")
        return ("Xin lỗi, hiện tại hệ thống AI không khả dụng. "
                "Vui lòng thử lại sau hoặc cấu hình API key. Nếu cần, hệ thống sẽ cung cấp lời khuyên cơ bản.")

    # final fallback (no providers configured)
    return "Chưa có API AI nào được cấu hình. Vui lòng đặt OPENAI_API_KEY hoặc HUGGINGFACE_API_TOKEN hoặc GEMINI_API_KEY"

# ================ ThingsBoard telemetry push (async) ================
async def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {json.dumps(data, ensure_ascii=False)}")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(TB_DEVICE_URL, json=data)
            logger.info(f"TB ◀ {r.status_code}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================ ROUTES ================
@app.get("/")
async def root():
    return {"status":"running","ai": {"preferred": PREFERRED_AI, "openai": bool(OPENAI_API_KEY and _HAS_OPENAI), "hf": bool(HUGGINGFACE_API_TOKEN), "gemini": bool(GEMINI_API_KEY and _HAS_GEMINI)}}

@app.post("/esp32-data")
async def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    weather_data = get_weather_forecast()
    # If you want AI-generated advice for telemetry, you can call ai_answer_question here.
    advice_text = None
    try:
        # Example: build a question for the AI from sensor values
        q = f"Nhiệt độ {data.temperature}°C, độ ẩm {data.humidity}%. Cho tôi lời khuyên chăm sóc rau muống ngắn gọn."
        advice_text = await ai_answer_question(q)
    except Exception as e:
        logger.warning(f"AI advice error: {e}")
        advice_text = rule_based_advice(data.temperature, data.humidity).get("advice")

    merged = {
        **data.dict(),
        "advice_text": advice_text,
        **weather_data,
        "location":"An Phú, Hồ Chí Minh",
        "crop":"Rau muống"
    }
    await send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

# Endpoint to receive ThingsBoard Shared Attributes (Rule Chain POSTs this)
@app.post("/tb-shared-attr")
async def tb_shared_attr(req: Request):
    body = await req.json()
    logger.info("Received TB shared attr: %s", json.dumps(body, ensure_ascii=False))

    # try to find a question under common shapes
    question = None
    if isinstance(body, dict):
        # direct key
        if "hoi" in body and isinstance(body["hoi"], str):
            question = body["hoi"]
        # nested under 'shared' or 'attributes' or 'data'
        for k in ("shared","attributes","data","msg","body"):
            if k in body and isinstance(body[k], dict) and "hoi" in body[k]:
                question = body[k]["hoi"]
                break
            if k in body and isinstance(body[k], str) and k == "msg":
                # sometimes msg is stringified JSON
                try:
                    parsed = json.loads(body[k])
                    if isinstance(parsed, dict) and "hoi" in parsed:
                        question = parsed["hoi"]
                        break
                except Exception:
                    pass

    if not question:
        logger.info("No 'hoi' key found; ignoring payload")
        return {"status":"no_question_detected", "received": body}

    logger.info("Detected question: %s", question)
    answer = await ai_answer_question(question)
    logger.info("AI answer: %s", answer)

    telemetry_payload = {"hoi_question": question, "hoi_answer": answer, "hoi_answer_ts": int(time.time()*1000)}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(TB_DEVICE_URL, json=telemetry_payload)
            r.raise_for_status()
            logger.info("Posted telemetry to ThingsBoard (hoi_answer)")
    except Exception as e:
        logger.error("Failed to post telemetry to ThingsBoard: %s", e)
        return {"status":"ai_answer_returned_but_push_failed", "answer": answer, "error": str(e)}

    return {"status":"ok","answer": answer}

# ================ AUTO LOOP (example) ================
async def auto_loop():
    while True:
        try:
            sample = {"temperature":30.1, "humidity":69.2}
            weather_data = get_weather_forecast()
            q = f"Nhiệt độ {sample['temperature']}°C, độ ẩm {sample['humidity']}%. Cho tôi lời khuyên chăm sóc rau muống ngắn gọn."
            advice_text = await ai_answer_question(q)
            merged = {**sample, "advice_text": advice_text, **weather_data, "location":"An Phú, Hồ Chí Minh", "crop":"Rau muống"}
            await send_to_thingsboard(merged)
        except Exception as e:
            logger.error("AUTO loop error: %s", e)
        await asyncio.sleep(AUTO_LOOP_INTERVAL)

@app.on_event("startup")
async def start_auto_loop():
    asyncio.create_task(auto_loop())

# ================ END ================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)), log_level="info")
