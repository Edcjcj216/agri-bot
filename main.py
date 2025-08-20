from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests, asyncio, uvicorn

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"
OPENROUTER_API_KEY = "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"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# ================== APP ==================
app = FastAPI()
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
    latest_data["temperature"] = data.temperature
    latest_data["humidity"] = data.humidity

    prediction, advice = call_ai(data.temperature, data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI HELPER ==================
def call_ai(temp: float, humi: float):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"Dự báo nông nghiệp: Nhiệt độ {temp}°C, độ ẩm {humi}%. Hãy trả về prediction và advice ngắn gọn."
    try:
        resp = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=10
        )
        resp.raise_for_status()
        ai_json = resp.json()
        content = ai_json["choices"][0]["message"]["content"]
        # Giả sử response dạng: "prediction: ... ; advice: ..."
        parts = content.split(";")
        prediction = parts[0].replace("prediction:", "").strip() if len(parts) > 0 else f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice = parts[1].replace("advice:", "").strip() if len(parts) > 1 else "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"
    except Exception as e:
        prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
        advice = f"(Fallback) Không gọi được AI OpenRouter: {str(e)}"
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
