import os
import time
import requests
import traceback
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
# Token DEMO device (rau muống Hồ Chí Minh)
DEMO_TOKEN = os.getenv("DEMO_TOKEN", "kfj6183wtsdijxu3z4yx")
THINGSBOARD_URL = f"https://thingsboard.cloud/api/v1/{DEMO_TOKEN}/telemetry"

# Hugging Face AI API
HF_API_KEY = os.getenv("HF_API_KEY")                  # cần set trong Render
HF_MODEL = os.getenv("HF_MODEL", "google/flan-t5-small")

CROP = "Rau muống"
LOCATION = "Hồ Chí Minh, VN"

# =========================
# FASTAPI
# =========================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# MODEL DỮ LIỆU ESP32
# =========================
class ESP32Data(BaseModel):
    temperature: float
    humidity: float
    battery: float | None = None

# =========================
# GLOBAL STATE
# =========================
latest_data: ESP32Data | None = None

# =========================
# HUGGING FACE CALL
# =========================
def call_huggingface(prompt: str, timeout: int = 30) -> str:
    if not HF_API_KEY:
        raise RuntimeError("HF_API_KEY chưa set")
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}", "Content-Type": "application/json"}
    body = {"inputs": prompt, "options": {"wait_for_model": True}}
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    out = resp.json()
    if isinstance(out, list) and len(out) > 0:
        first = out[0]
        if isinstance(first, dict):
            return first.get("generated_text") or first.get("text") or str(first)
        return str(first)
    if isinstance(out, dict):
        return out.get("generated_text") or out.get("text") or str(out)
    return str(out)

# =========================
# AI LOGIC
# =========================
def get_advice(temp: float, humi: float):
    prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    prompt = f"Dự báo nông nghiệp: Nhiệt độ {temp}°C, độ ẩm {humi}% tại {LOCATION}, cây {CROP}. Viết ngắn gọn dự báo và gợi ý chăm sóc."
    if HF_API_KEY:
        try:
            start = time.time()
            text = call_huggingface(prompt)
            print(f"✅ HuggingFace trả về sau {time.time()-start:.2f}s")
            if text:
                return prediction, text.strip()
        except Exception as e:
            print("⚠️ Hugging Face lỗi:", e)
            traceback.print_exc()
    # fallback cứng
    advice = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối."
    return prediction, advice

# =========================
# PUSH TELEMETRY LÊN DEMO DEVICE
# =========================
def push_thingsboard(payload: dict):
    try:
        requests.post(
            THINGSBOARD_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10
        )
        print(f"✅ Đã gửi lên ThingsBoard DEMO device: {payload}")
    except Exception as e:
        print("❌ Lỗi khi gửi lên ThingsBoard:", e)
        traceback.print_exc()

# =========================
# ROUTES
# =========================
@app.get("/")
def root():
    return {"message": "Agri-Bot DEMO running 🚀", "huggingface": bool(HF_API_KEY)}

@app.post("/esp32-data")
def receive_esp32(data: ESP32Data):
    global latest_data
    latest_data = data
    prediction, advice = get_advice(data.temperature, data.humidity)

    payload = {
        "temperature": data.temperature,
        "humidity": data.humidity,
        "battery": data.battery,
        "prediction": prediction,
        "advice": advice
    }

    # Gửi kết quả lên DEMO device ngay lập tức
    push_thingsboard(payload)

    return {"status": "ok", "received": data.dict(),
            "prediction": prediction, "advice": advice}

# =========================
# BACKGROUND TASK: mỗi 5 phút gửi lại dự báo dựa trên dữ liệu ESP32 thật
# =========================
async def periodic_ai_loop():
    while True:
        await asyncio.sleep(300)  # 5 phút
        if latest_data:
            print("⏳ Tạo dự báo định kỳ từ dữ liệu ESP32 thật...")
            prediction, advice = get_advice(latest_data.temperature, latest_data.humidity)
            payload = {
                "temperature": latest_data.temperature,
                "humidity": latest_data.humidity,
                "battery": latest_data.battery,
                "prediction": prediction,
                "advice": advice
            }
            push_thingsboard(payload)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

# =========================
# RUN UVICORN (Render friendly)
# =========================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
