import os
import time
import json
import logging
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ================== CONFIG ==================
TB_DEMO_TOKEN = "pk94asonfacs6mbeuutg"  # Device DEMO token mới
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEMO_TOKEN}/telemetry"

TB_TENANT_USER = os.getenv("TB_TENANT_USER", "")
TB_TENANT_PASS = os.getenv("TB_TENANT_PASS", "")

AI_API_URL = os.getenv("AI_API_URL", "https://api-inference.huggingface.co/models/gpt2")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ================== FASTAPI ==================
app = FastAPI()

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# ================== HELPERS ==================
def call_ai_api(data: dict) -> dict:
    try:
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        payload = {"inputs": f"Nhiệt độ {data['temperature']}°C, độ ẩm {data['humidity']}%"}
        logger.info(f"AI ▶ {payload}")
        r = requests.post(AI_API_URL, headers=headers, json=payload, timeout=20)
        logger.info(f"AI ◀ status={r.status_code} text={r.text[:200]}")
        if r.status_code == 200:
            return {
                "prediction": payload["inputs"],
                "advice": "Theo dõi cây trồng, tưới nước đều, bón phân cân đối."
            }
    except Exception as e:
        logger.error(f"AI API error: {e}")
    return {"prediction": "N/A", "advice": "N/A"}


def send_to_thingsboard(data: dict):
    try:
        logger.info(f"TB ▶ {data}")
        r = requests.post(TB_DEVICE_URL, json=data, timeout=10)
        logger.info(f"TB ◀ status={r.status_code} text={r.text}")
    except Exception as e:
        logger.error(f"ThingsBoard push error: {e}")

# ================== API ROUTES ==================
@app.get("/")
def root():
    return {"status": "running", "demo_token": TB_DEMO_TOKEN[:4] + "***"}

@app.post("/esp32-data")
def receive_data(data: SensorData):
    logger.info(f"ESP32 ▶ {data.dict()}")
    ai_result = call_ai_api(data.dict())
    merged = data.dict() | ai_result
    send_to_thingsboard(merged)
    return {"received": data.dict(), "pushed": merged}

@app.get("/debug/test-push")
def test_push():
    sample = {"temperature": 29.5, "humidity": 72, "battery": 95,
              "prediction": "Nhiệt độ 29.5°C, độ ẩm 72%",
              "advice": "Tưới thêm nước nhẹ buổi sáng."}
    send_to_thingsboard(sample)
    return {"test": "pushed", "data": sample}

@app.get("/last-telemetry")
def last_telemetry(keys: str = "temperature,humidity,battery,prediction,advice"):
    if not TB_TENANT_USER or not TB_TENANT_PASS:
        return JSONResponse(status_code=400, content={"error": "Missing TB_TENANT_USER/PASS env vars"})

    try:
        # 1. Login tenant
        login_url = "https://thingsboard.cloud/api/auth/login"
        r = requests.post(login_url, json={"username": TB_TENANT_USER, "password": TB_TENANT_PASS}, timeout=10)
        r.raise_for_status()
        token = r.json()["token"]
        headers = {"X-Authorization": f"Bearer {token}"}

        # 2. Get deviceId by token
        device_url = f"https://thingsboard.cloud/api/device/token/{TB_DEMO_TOKEN}"
        r = requests.get(device_url, headers=headers, timeout=10)
        r.raise_for_status()
        device_id = r.json()["id"]["id"]

        # 3. Get latest telemetry
        ts_url = f"https://thingsboard.cloud/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries?keys={keys}"
        r = requests.get(ts_url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Last telemetry error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# ================== AUTO LOOP (5min) ==================
import threading

def auto_loop():
    while True:
        try:
            # Fake ESP32 data (có thể thay bằng đọc queue/thật)
            sample = {"temperature": 30.1, "humidity": 69.2, "battery": 90}
            logger.info(f"[AUTO] ESP32 ▶ {sample}")
            ai_result = call_ai_api(sample)
            merged = sample | ai_result
            send_to_thingsboard(merged)
        except Exception as e:
            logger.error(f"AUTO loop error: {e}")
        time.sleep(300)  # 5 phút

threading.Thread(target=auto_loop, daemon=True).start()
