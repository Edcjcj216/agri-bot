import time
import logging
import requests
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Token ThingsBoard DEMO device
TB_DEVICE_TOKEN = "pk94asonfacs6mbeuutg"
TB_DEVICE_URL = f"https://thingsboard.cloud/api/v1/{TB_DEVICE_TOKEN}/telemetry"

class SensorData(BaseModel):
    temperature: float
    humidity: float
    battery: float

# Giả lập gọi AI API (thay bằng API thực tế của bạn)
def call_ai_api(sensor: dict):
    return {
        "prediction": "Sâu bệnh nhẹ",
        "advice": "Nên kiểm tra lá non kỹ lưỡng",
        "advice_nutrition": "Bón thêm phân kali",
        "advice_care": "Tưới thêm nước buổi sáng",
        "ghi_chu": "Theo dõi trong 3 ngày tới"
    }

@app.post("/esp32-data")
def receive_data(data: SensorData):
    sensor = data.dict()
    ai_result = call_ai_api(sensor)

    ts = int(time.time() * 1000)
    values = {
        "temperature": sensor["temperature"],
        "humidity": sensor["humidity"],
        "battery": sensor["battery"],
        "prediction": ai_result.get("prediction"),
        "advice": ai_result.get("advice"),
        "advice_nutrition": ai_result.get("advice_nutrition"),
        "advice_care": ai_result.get("advice_care"),
        "ghi_chu": ai_result.get("ghi_chu"),
    }

    body = [{"ts": ts, "values": values}]
    try:
        r = requests.post(TB_DEVICE_URL, json=body, timeout=10)
        logger.info(f"TB push status={r.status_code} text={r.text}")
        return {"pushed": values, "tb_status": r.status_code}
    except Exception as e:
        logger.error(f"Error pushing to TB: {e}")
        return {"error": str(e)}
