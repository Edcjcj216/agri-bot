from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"

# ================== FASTAPI APP ==================
app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== GLOBAL ==================
latest_data = {"temperature": None, "humidity": None}

# ================== MODEL ==================
class ESP32Data(BaseModel):
    temperature: float
    humidity: float

# ================== CHECK ENV ==================
AI_API_KEY = os.getenv("AI_API_KEY")
if not AI_API_KEY:
    raise ValueError("⚠️ AI_API_KEY chưa được cấu hình trong biến môi trường")

AI_API_URL = "https://api.openai.com/v1/gemini/predict"

# ================== ROUTES ==================
@app.get("/")
def home():
    return {"message": "Agri-Bot service is running 🚀"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    latest_data["temperature"] = data.temperature
    latest_data["humidity"]    = data.humidity

    prediction, advice = call_ai(data.temperature, data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI HELPER ==================
def call_ai(temp: float, humi: float):
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(
            AI_API_URL,
            headers=headers,
            json={"temperature": temp, "humidity": humi},
            timeout=10
        )
        resp.raise_for_status()
        ai_json = resp.json()
        prediction = ai_json.get("prediction", f"Nhiệt độ {temp}°C, độ ẩm {humi}%")
        advice     = ai_json.get("advice", "Theo dõi cây trồng, tưới nước đều, bón phân cân đối")
    except Exception as e:
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice     = f"(Fallback) Không gọi được AI API: {str(e)}"
    return prediction, advice

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
        temp = latest_data.get("temperature", 30)
        humi = latest_data.get("humidity", 70)
        prediction, advice = call_ai(temp, humi)
        push_thingsboard({"prediction": prediction, "advice": advice})
        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

# ================== RUN LOCAL ==================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
