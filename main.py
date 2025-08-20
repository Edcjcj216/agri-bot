from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # optional

GEMINI_API_URL = "https://gemini.googleapis.com/v1/models/gemma-3-27b-it:predict"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # optional

# ================== FASTAPI APP ==================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== GLOBAL ==================
latest_data = {"temperature": None, "humidity": None}
DEFAULT_TEMP = 30
DEFAULT_HUMI = 70

# ================== MODEL ==================
class ESP32Data(BaseModel):
    temperature: float
    humidity: float

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"message": "Agri-Bot service is running 🚀"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    latest_data["temperature"] = data.temperature
    latest_data["humidity"] = data.humidity

    prediction, advice = call_ai(temp=data.temperature, humi=data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI HELPER ==================
def call_ai(temp: float, humi: float):
    """Tự động fallback: OpenRouter -> Gemini -> default"""
    prompt = f"Dự báo nông nghiệp: nhiệt độ {temp}°C, độ ẩm {humi}%. Advice ngắn gọn."
    prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"

    # 1️⃣ Thử OpenRouter nếu có key
    if OPENROUTER_API_KEY:
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        try:
            resp = requests.post(
                OPENROUTER_API_URL,
                headers=headers,
                json={"model": "gpt-4o-mini", "messages":[{"role":"user","content":prompt}], "temperature":0.7},
                timeout=10
            )
            resp.raise_for_status()
            ai_json = resp.json()
            text_output = ai_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            if text_output:
                return prediction, text_output
        except Exception as e:
            print("⚠️ OpenRouter failed:", e)

    # 2️⃣ Thử Gemini nếu có key
    if GEMINI_API_KEY:
        headers = {"Authorization": f"Bearer {GEMINI_API_KEY}", "Content-Type": "application/json"}
        payload = {"input":[{"role":"user","content":prompt}]}
        try:
            resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            ai_json = resp.json()
            text_output = ai_json.get("output", [{}])[0].get("content", "")
            if text_output:
                return prediction, text_output
        except Exception as e:
            print("⚠️ Gemini failed:", e)

    # 3️⃣ Fallback mặc định
    return prediction, "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

# ================== THINGSBOARD HELPER ==================
def push_thingsboard(payload: dict):
    try:
        requests.post(
            THINGSBOARD_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10
        )
        print("✅ Pushed telemetry:", payload)
    except Exception as e:
        print("❌ Error pushing telemetry:", e)

# ================== BACKGROUND TASK ==================
async def periodic_ai_loop():
    while True:
        temp = latest_data.get("temperature") or DEFAULT_TEMP
        humi = latest_data.get("humidity") or DEFAULT_HUMI

        prediction, advice = call_ai(temp, humi)
        payload = {"prediction": prediction, "advice": advice}
        push_thingsboard(payload)

        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

# ================== RUN ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
