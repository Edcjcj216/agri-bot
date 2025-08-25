import os
import logging
from fastapi import FastAPI, Request
import httpx
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tb-webhook")

THINGSBOARD_URL = "https://thingsboard.cloud/api/v1"
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN")  # Device Access Token
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/tb-webhook")
async def tb_webhook(request: Request):
    data = await request.json()
    logger.info(f"Received webhook: {data}")

    hoi = data.get("shared", {}).get("hoi")
    if not hoi:
        return {"error": "No 'hoi' in shared attributes"}

    # gọi Gemini API sinh câu trả lời
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": hoi}]}
        ]
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        gemini_data = r.json()
        logger.info(f"Gemini response: {gemini_data}")

    try:
        answer = gemini_data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        answer = "Xin lỗi, tôi chưa trả lời được."

    # gửi trả lời lên ThingsBoard telemetry
    telemetry_url = f"{THINGSBOARD_URL}/{THINGSBOARD_TOKEN}/telemetry"
    async with httpx.AsyncClient() as client:
        res = await client.post(telemetry_url, json={"answer": answer})
        logger.info(f"Pushed telemetry: {{'answer': '{answer}'}} | Status {res.status_code}")

    return {"answer": answer}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    logger.info("Starting server...")
    uvicorn.run(app, host="0.0.0.0", port=port)
