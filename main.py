from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, requests, asyncio, uvicorn, traceback, time

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"
HF_API_KEY = os.getenv("HF_API_KEY")                   # REQUIRED for AI
HF_MODEL = os.getenv("HF_MODEL", "google/flan-t5-small")

# ================== APP ==================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

latest_data = {"temperature": None, "humidity": None}
DEFAULT_TEMP = 30
DEFAULT_HUMI = 70

class ESP32Data(BaseModel):
    temperature: float
    humidity: float

@app.get("/")
def root():
    return {"message": "Agri-Bot running 🚀", "huggingface": bool(HF_API_KEY)}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    latest_data["temperature"] = data.temperature
    latest_data["humidity"] = data.humidity

    prediction, advice = get_advice(data.temperature, data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== HF CALL ==================
def call_huggingface(prompt: str, timeout: int = 30) -> str:
    if not HF_API_KEY:
        raise RuntimeError("HF_API_KEY not set")
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}", "Content-Type": "application/json"}
    body = {"inputs": prompt, "options": {"wait_for_model": True}}
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    out = resp.json()
    # parse flexibly
    if isinstance(out, list) and len(out)>0:
        first = out[0]
        if isinstance(first, dict):
            return first.get("generated_text") or first.get("text") or str(first)
        return str(first)
    if isinstance(out, dict):
        return out.get("generated_text") or out.get("text") or str(out)
    return str(out)

# ================== MAIN LOGIC (HF -> LOCAL) ==================
def get_advice(temp: float, humi: float):
    prompt = f"Dự báo nông nghiệp: nhiệt độ {temp}°C, độ ẩm {humi}%. Viết 1 prediction ngắn (1 câu) và 1 advice ngắn (1-2 câu)."
    prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"

    # Try HF if available
    if HF_API_KEY:
        try:
            start = time.time()
            text = call_huggingface(prompt)
            print(f"✅ HF OK (took {time.time()-start:.2f}s)")
            if text:
                return prediction, text.strip()
        except Exception as e:
            print("⚠️ HuggingFace failed:", e)
            traceback.print_exc()

    # Local fallback
    advice = "Theo dõi cây trồng, tưới nước vừa phải, bón phân cân đối."
    return prediction, advice

# ================== THINGSBOARD ==================
def push_thingsboard(payload: dict):
    try:
        requests.post(THINGSBOARD_URL, json=payload, headers={"Content-Type":"application/json; charset=utf-8"}, timeout=10)
        print("✅ Pushed telemetry:", payload)
    except Exception as e:
        print("❌ Error pushing telemetry:", e)
        traceback.print_exc()

# ================== BACKGROUND LOOP ==================
async def periodic_ai_loop():
    while True:
        temp = latest_data.get("temperature") or DEFAULT_TEMP
        humi = latest_data.get("humidity") or DEFAULT_HUMI
        prediction, advice = get_advice(temp, humi)
        push_thingsboard({"prediction": prediction, "advice": advice})
        await asyncio.sleep(300)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_ai_loop())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
