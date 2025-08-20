from fastapi import FastAPI
from pydantic import BaseModel
import requests
import os

app = FastAPI()

# ================== CONFIG ==================
THINGSBOARD_TOKEN = os.getenv("TB_TOKEN", "66dd31thvta4gx1l781q")
THINGSBOARD_URL   = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"

# Giả lập AI API (bạn thay url này = service AI thật của bạn)
AI_API_URL = os.getenv("AI_API_URL", "https://your-ai-service/predict")


class ESP32Data(BaseModel):
    temperature: float
    humidity: float


@app.get("/")
async def root():
    return {"status": "ok", "message": "Agri-Bot service is live 🚀"}


@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    """
    Nhận dữ liệu từ ESP32 → gọi AI API → push prediction/advice lên ThingsBoard
    """

    # --- Gọi AI API ---
    try:
        ai_resp = requests.post(
            AI_API_URL,
            json={"temperature": data.temperature, "humidity": data.humidity},
            timeout=10
        )
        ai_resp.raise_for_status()
        ai_json = ai_resp.json()

        prediction = ai_json.get("prediction", f"Nhiệt độ {data.temperature}°C, độ ẩm {data.humidity}%")
        advice     = ai_json.get("advice", "Theo dõi cây trồng, tưới nước đều, bón phân cân đối")

    except Exception as e:
        prediction = f"Nhiệt độ {data.temperature}°C, độ ẩm {data.humidity}%"
        advice     = f"(Fallback) Không gọi được AI API: {str(e)}"

    # --- Push lên ThingsBoard ---
    payload = {"prediction": prediction, "advice": advice}

    try:
        tb_resp = requests.post(
            THINGSBOARD_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        tb_resp.raise_for_status()
    except Exception as e:
        return {"status": "error", "msg": f"Push ThingsBoard fail: {e}"}

    return {"status": "ok", "prediction": prediction, "advice": advice}
