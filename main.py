# ================== main.py ==================
from fastapi import FastAPI, Request
import requests, json, os, asyncio
import time
import threading

app = FastAPI()

# ================== CONFIG ==================
DEMO_DEVICE_TOKEN = "66dd31thvta4gx1l781q"
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1"

AI_API_URL = "https://api.example.com/predict"  # Thay bằng API thật
AI_API_KEY = os.getenv("AI_API_KEY")            # hoặc đặt trực tiếp

# ================== AI cache 5 phút ==================
last_ai_update = 0
ai_cache = {"prediction": "", "advice": ""}

def get_ai_prediction(payload):
    global last_ai_update, ai_cache
    current_time = time.time()
    # chỉ gọi AI API nếu đã quá 5 phút
    if current_time - last_ai_update > 300 or not ai_cache["prediction"]:
        try:
            ai_resp = requests.post(
                AI_API_URL,
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json=payload,
                timeout=10
            )
            ai_resp.raise_for_status()
            ai_result = ai_resp.json()
            ai_cache["prediction"] = ai_result.get("prediction", f"Nhiệt độ {payload['temperature']}°C, độ ẩm {payload['humidity']}%")
            ai_cache["advice"] = ai_result.get("advice", "Theo dõi cây trồng, tưới nước đều, bón phân cân đối")
        except Exception:
            ai_cache["prediction"] = f"Nhiệt độ {payload['temperature']}°C, độ ẩm {payload['humidity']}%"
            ai_cache["advice"] = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"
        last_ai_update = current_time
    return ai_cache

# ================== REST endpoint nhận dữ liệu ESP32 thật ==================
@app.post("/esp32-data")
async def receive_esp32(request: Request):
    data = await request.json()

    temperature = data.get("temperature")
    humidity    = data.get("humidity")
    battery     = data.get("battery")
    crop        = data.get("crop", "Rau muống")
    location    = data.get("location", "Ho Chi Minh,VN")

    ai_payload = {
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        "crop": crop,
        "location": location
    }

    # Lấy prediction/advice từ cache
    ai_result = get_ai_prediction(ai_payload)

    telemetry = {
        "prediction": ai_result["prediction"],
        "advice": ai_result["advice"]
    }

    # Gửi lên DEMO device
    try:
        resp = requests.post(
            f"{THINGSBOARD_URL}/{DEMO_DEVICE_TOKEN}/telemetry",
            headers={"Content-Type": "application/json"},
            data=json.dumps(telemetry),
            timeout=5
        )
        resp.raise_for_status()
        print(f"[{time.strftime('%H:%M:%S')}] ✅ Telemetry DEMO sent from ESP32")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ❌ Error sending telemetry: {e}")
        return {"status": "fail", "error": str(e), "telemetry": telemetry}

    return {"status": "ok", "telemetry": telemetry}

# ================== Demo loop gửi dữ liệu random 5 phút ==================
async def send_demo_loop():
    import random
    while True:
        temperature = round(random.uniform(25, 32),1)
        humidity    = round(random.uniform(50, 80),1)
        ai_payload = {
            "temperature": temperature,
            "humidity": humidity,
            "battery": 3.7,
            "crop": "Rau muống",
            "location": "Ho Chi Minh,VN"
        }
        ai_result = get_ai_prediction(ai_payload)

        telemetry = {
            "prediction": ai_result["prediction"],
            "advice": ai_result["advice"]
        }

        try:
            requests.post(
                f"{THINGSBOARD_URL}/{DEMO_DEVICE_TOKEN}/telemetry",
                headers={"Content-Type": "application/json"},
                data=json.dumps(telemetry),
                timeout=5
            )
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Demo telemetry sent")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Error: {e}")
        await asyncio.sleep(300)

# ================== Khởi chạy server + demo loop ==================
if __name__ == "__main__":
    # Start loop demo telemetry trong background
    loop = asyncio.get_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(send_demo_loop()), daemon=True).start()

    # Start FastAPI server trên Render
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)
