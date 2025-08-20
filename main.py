from fastapi import FastAPI
from pydantic import BaseModel
import requests
import os

app = FastAPI()

# Config
THINGSBOARD_TOKEN = os.getenv("TB_TOKEN", "66dd31thvta4gx1l781q")
THINGSBOARD_URL   = f"https://thingsboard.cloud/api/v1/{THINGSBOARD_TOKEN}/telemetry"
AI_API_URL        = os.getenv("AI_API_URL", "https://your-ai-service/predict")


class ESP32Data(BaseModel):
    temperature: float
    humidity: float


@app.get("/")
async def root():
    return {"status": "ok", "message": "Agri-Bot service is live 🚀"}


@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    # Gọi AI API
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

    # Push telemetry lên ThingsBoard
    payload = {"prediction": prediction, "advice": advice}

    try:
        r = requests.post(
            THINGSBOARD_URL,
            json=payload,  # đây là key fix
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10
        )
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "msg": f"Push ThingsBoard fail: {e}"}

    return {"status": "ok", "prediction": prediction, "advice": advice}
