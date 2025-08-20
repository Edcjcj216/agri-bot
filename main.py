from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, asyncio, uvicorn, os

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"

# AI Gemini (OpenAI)
AI_API_URL = "https://api.openai.com/v1/chat/completions"  # endpoint chuẩn hiện tại
AI_MODEL   = "gpt-4o-mini"  # ví dụ model có thể dùng
AI_API_KEY = os.getenv("AI_API_KEY")  # bắt buộc set qua biến môi trường

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
def home():
    return {"message": "Agri-Bot service is running"}  # plain text

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    # Lưu dữ liệu mới nhất
    latest_data["temperature"] = data.temperature
    latest_data["humidity"]    = data.humidity

    # Gọi AI
    prediction, advice = call_ai(data.temperature, data.humidity)

    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI HELPER ==================
def call_ai(temp: float, humi: float):
    # fallback nếu biến môi trường chưa set
    if not AI_API_KEY:
        return (
            f"Nhiệt độ {temp}°C, độ ẩm {humi}%",
            "(Fallback) AI_API_KEY chưa cấu hình"
        )

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"Dự đoán tình trạng cây trồng với nhiệt độ {temp}°C và độ ẩm {humi}%. Gợi ý cách chăm sóc."

    try:
        resp = requests.post(
            AI_API_URL,
            headers=headers,
            json={
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100
            },
            timeout=10
        )
        resp.raise_for_status()
        ai_json = resp.json()
        content = ai_json["choices"][0]["message"]["content"]

        # Giản lược, ví dụ tách prediction/advice từ content
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice     = content
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
        temp = latest_data.get("temperature") or 30
        humi = latest_data.get("humidity") or 70

        prediction, advice = call_ai(temp, humi)
        payload = {"prediction": prediction, "advice": advice}
        push_thingsboard(payload)

        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

# ================== RUN LOCAL ==================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
