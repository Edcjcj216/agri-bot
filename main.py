from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"
AI_API_URL      = "https://your-ai-service/predict"  # Thay bằng AI thật

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

# ================== ROUTES ==================
@app.get("/")
def home():
    return {"message": "Agri-Bot service is running 🚀"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    """
    Nhận dữ liệu từ ESP32 → gọi AI API → push lên ThingsBoard
    """
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

    payload = {"prediction": prediction, "advice": advice}

    try:
        requests.post(
            THINGSBOARD_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10
        )
    except Exception as e:
        return {"status": "error", "msg": f"Push ThingsBoard fail: {e}"}

    return {"status": "ok", "prediction": prediction, "advice": advice}

# ================== BACKGROUND TASK ==================
async def push_periodic_ai():
    """
    Job tự động 5 phút gọi AI API → push dữ liệu lên ThingsBoard
    """
    while True:
        try:
            # Gọi AI API với dữ liệu giả/demo hoặc trung bình
            ai_resp = requests.post(
                AI_API_URL,
                json={"temperature": 30, "humidity": 70},  # hoặc trung bình/gợi ý
                timeout=10
            )
            ai_resp.raise_for_status()
            ai_json = ai_resp.json()
            prediction = ai_json.get("prediction", "Nhiệt độ trung bình 30°C, độ ẩm 70%")
            advice     = ai_json.get("advice", "Theo dõi cây trồng, tưới nước đều, bón phân cân đối")
        except Exception as e:
            prediction = "Nhiệt độ trung bình 30°C, độ ẩm 70%"
            advice     = f"(Fallback) Không gọi được AI API: {str(e)}"

        payload = {"prediction": prediction, "advice": advice}

        try:
            requests.post(
                THINGSBOARD_URL,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=10
            )
            print("✅ Auto pushed AI telemetry:", payload)
        except Exception as e:
            print("❌ Error pushing periodic AI telemetry:", e)

        await asyncio.sleep(300)  # 5 phút

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(push_periodic_ai())

# ================== RUN LOCAL ==================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
