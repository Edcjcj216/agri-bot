from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"
AI_API_URL = "https://api.openai.com/v1/gemini/predict"
AI_API_KEY = "AIzaSyDvHhwey-dlCtCGrUCGsrDoYVl3XlBQ8I8"  # Hardcode trực tiếp

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

# ================== ROUTES ==================
@app.get("/")
def root():
    return {"message": "Agri-Bot service is running 🚀"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    """
    Nhận dữ liệu ESP32 → gọi AI → push ThingsBoard
    """
    # Lưu dữ liệu mới nhất
    latest_data["temperature"] = data.temperature
    latest_data["humidity"] = data.humidity

    # Gọi AI
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
    """
    Mỗi 5 phút gọi AI với dữ liệu ESP32 mới nhất → push ThingsBoard
    """
    while True:
        temp = latest_data.get("temperature", 30)
        humi = latest_data.get("humidity", 70)

        prediction, advice = call_ai(temp, humi)
        payload = {"prediction": prediction, "advice": advice}
        push_thingsboard(payload)

        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

# ================== RUN LOCAL / Render ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
