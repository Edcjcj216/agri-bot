from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"
GEMINI_API_URL = "https://gemini.googleapis.com/v1/models/gemma-3-27b-it:predict"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # Lấy từ biến môi trường

if not GEMINI_API_KEY:
    raise ValueError("⚠️ GEMINI_API_KEY chưa được cấu hình trong biến môi trường")

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

    prediction, advice = call_gemini(data.temperature, data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI HELPER ==================
def call_gemini(temp: float, humi: float):
    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"Dự báo nông nghiệp: nhiệt độ {temp}°C, độ ẩm {humi}%. Đưa ra advice ngắn gọn."

    payload = {"input": [{"role": "user", "content": prompt}]}

    try:
        resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        ai_json = resp.json()
        text_output = ai_json.get("output", [{}])[0].get("content", "")
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice = text_output or "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"
    except Exception as e:
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice = f"(Fallback) Không gọi được AI Gemini: {str(e)}"

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
        temp = latest_data.get("temperature") or DEFAULT_TEMP
        humi = latest_data.get("humidity") or DEFAULT_HUMI

        prediction, advice = call_gemini(temp, humi)
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
