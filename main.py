from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any
import requests, asyncio, uvicorn, random

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

# Model input từ ESP32
class ESP32Data(BaseModel):
    temperature: float
    humidity: float

# ========== ROUTES ==========
@app.get("/")
def home():
    return {"message": "Agri-Bot service is running 🚀"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    """
    Nhận dữ liệu từ ESP32 → xử lý AI → push lên ThingsBoard
    """
    prediction = f"Nhiệt độ {data.temperature}°C, độ ẩm {data.humidity}%"
    advice = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

    payload = {
        "prediction": prediction,
        "advice": advice
    }
    try:
        r = requests.post(
            THINGSBOARD_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ========== BACKGROUND TASK ==========
async def push_periodic():
    """
    Job tự động 5 phút push dữ liệu dự báo lên ThingsBoard
    """
    while True:
        # Fake data random để demo
        temp = random.randint(28, 35)
        humi = random.randint(60, 80)
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"

        payload = {"prediction": prediction, "advice": advice}

        try:
            requests.post(
                THINGSBOARD_URL,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            print("✅ Auto pushed telemetry:", payload)
        except Exception as e:
            print("❌ Error pushing periodic telemetry:", e)

        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(push_periodic())

# ========== RUN LOCAL ==========
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
