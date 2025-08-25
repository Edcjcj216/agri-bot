import os
import logging
from datetime import datetime
import httpx
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="INFO:tb-webhook:%(message)s")
logger = logging.getLogger("tb-webhook")

app = FastAPI()

# Lấy token & API key từ environment (Render Env Vars)
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not DEVICE_TOKEN:
    logger.warning("DEVICE_TOKEN chưa được cấu hình!")
if not GEMINI_KEY:
    logger.warning("GEMINI_KEY chưa được cấu hình!")


@app.on_event("startup")
async def startup_event():
    logger.info("Starting server...")
    # Gửi ping khi server khởi động để dễ kiểm tra trên ThingsBoard
    await push_telemetry({"startup_ping": datetime.utcnow().isoformat()})


@app.get("/")
async def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Nhận Shared Attributes từ Rule Chain.
    Expect JSON như:
    {
      "hoi": "xin chào bạn"
    }
    """
    data = await request.json()
    logger.info(f"Webhook received: {data}")

    hoi = data.get("hoi")
    if not hoi:
        await push_telemetry({"status": "no question"})
        return {"msg": "no question"}

    # Gọi Gemini để sinh câu trả lời
    advice = await get_gemini_advice(hoi)

    # Gửi kết quả lên ThingsBoard qua telemetry
    await push_telemetry({"advice_text": advice})
    return {"advice": advice}


async def get_gemini_advice(question: str) -> str:
    """
    Gọi Google Gemini API sinh câu trả lời từ văn bản.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": f"Trả lời ngắn gọn: {question}"}]}
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            data = res.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            logger.info(f"Gemini response: {text}")
            return text
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return "Không thể sinh câu trả lời ngay lúc này."


async def push_telemetry(payload: dict):
    """
    Gửi dữ liệu telemetry lên ThingsBoard.
    """
    url = f"https://thingsboard.cloud/api/v1/{DEVICE_TOKEN}/telemetry"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload)
            logger.info(f"Pushed telemetry: {payload} | Status {res.status_code}")
    except Exception as e:
        logger.error(f"Telemetry push error: {e}")
