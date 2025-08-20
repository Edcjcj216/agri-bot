from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, requests, asyncio, uvicorn, traceback, time

# ================== CONFIG ==================
THINGSBOARD_URL = "https://thingsboard.cloud/api/v1/66dd31thvta4gx1l781q/telemetry"

# Providers (all optional)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")   # OpenRouter key (optional)
OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")           # Gemini / Google GenAI key (optional)
# Default URL is a plausible placeholder — change to your real endpoint if needed.
GEMINI_API_URL = os.getenv("GEMINI_API_URL", "https://gemini.googleapis.com/v1/models/gemma-3-27b-it:predict")

HF_API_KEY = os.getenv("HF_API_KEY")                   # Hugging Face token (optional)
HF_MODEL = os.getenv("HF_MODEL", "google/flan-t5-small")

# ================== FASTAPI APP ==================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== GLOBAL ==================
latest_data = {"temperature": None, "humidity": None}
DEFAULT_TEMP = 30
DEFAULT_HUMI = 70

# ================== MODEL ==================
class ESP32Data(BaseModel):
    temperature: float
    humidity: float

# ================== ROUTES ==================
@app.get("/")
def root():
    return {
        "message": "Agri-Bot service is running 🚀",
        "openrouter": bool(OPENROUTER_API_KEY),
        "gemini": bool(GEMINI_API_KEY),
        "huggingface": bool(HF_API_KEY),
    }

@app.post("/esp32-data")
async def receive_esp32(data: ESP32Data):
    latest_data["temperature"] = data.temperature
    latest_data["humidity"] = data.humidity

    prediction, advice = call_ai_with_fallback(data.temperature, data.humidity)
    payload = {"prediction": prediction, "advice": advice}
    push_thingsboard(payload)

    return {"status": "ok", "latest_data": data.dict(), "prediction": prediction, "advice": advice}

# ================== AI PROVIDERS ==================
def call_openrouter(temp: float, humi: float, prompt: str, timeout: int = 10) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OpenRouter API key not configured")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    resp = requests.post(OPENROUTER_API_URL, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # robust parsing
    text_output = data.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(data, dict) else ""
    if not text_output:
        text_output = data.get("text") or data.get("content") or ""
    return str(text_output or "").strip()

def call_gemini(prompt: str, timeout: int = 15) -> str:
    """
    Simple Gemini call using GEMINI_API_URL + GEMINI_API_KEY.
    NOTE: real Google/Vertex setups often require OAuth2 access token (service account) or specific endpoint format.
    If your setup needs a different auth flow, tell mình và mình sẽ thêm code lấy access token from service account.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini API key not configured")

    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json"
    }

    # Two common payload shapes — try a couple to increase chance of success.
    # 1) Vertex-like / generativelanguage: {"prompt": {"text": "..."}}
    payload_options = [
        {"prompt": {"text": prompt}},
        {"input": [{"role": "user", "content": prompt}]},           # shape used earlier in convo
        {"instances": [{"content": prompt}]},                      # another possible shape
        {"input": prompt},                                         # generic
    ]

    last_exc = None
    for payload in payload_options:
        try:
            resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=timeout)
            # if unauthorized / not found, will raise below
            resp.raise_for_status()
            data = resp.json()
            # Try common response shapes
            # Vertex-like: {'candidates': [{'content': '...'}], 'output': [...]}
            text = ""
            if isinstance(data, dict):
                # check several keys
                if "candidates" in data and isinstance(data["candidates"], list) and len(data["candidates"])>0:
                    # candidate may have 'content' or 'output'
                    cand = data["candidates"][0]
                    if isinstance(cand, dict):
                        text = cand.get("content") or cand.get("output") or cand.get("text") or ""
                    else:
                        text = str(cand)
                # older shapes
                text = text or data.get("output") or data.get("text") or data.get("content") or ""
                # some responses have nested structure
                if not text and "results" in data and isinstance(data["results"], list) and len(data["results"])>0:
                    r0 = data["results"][0]
                    if isinstance(r0, dict):
                        text = r0.get("content") or r0.get("output") or ""
            elif isinstance(data, list) and len(data)>0:
                first = data[0]
                if isinstance(first, dict):
                    text = first.get("content") or first.get("text") or ""
                else:
                    text = str(first)
            if text:
                return str(text).strip()
            # else try next payload shape
        except Exception as e:
            last_exc = e
            # continue to try next payload shape
            continue

    # If all payload shapes failed, raise last exception to be handled by caller
    raise RuntimeError(f"Gemini call failed (tried multiple payload shapes). Last error: {last_exc}")

def call_huggingface(prompt: str, timeout: int = 30) -> str:
    if not HF_API_KEY:
        raise RuntimeError("HF API key not configured")
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {
        "Authorization": f"Bearer {HF_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    body = {"inputs": prompt, "options": {"wait_for_model": True}}
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    out = resp.json()

    # parse flexible shapes
    text = ""
    if isinstance(out, dict):
        text = out.get("generated_text") or out.get("text") or out.get("output") or ""
    elif isinstance(out, list) and len(out) > 0:
        first = out[0]
        if isinstance(first, dict):
            text = first.get("generated_text") or first.get("text") or ""
        else:
            text = str(first)
    else:
        text = str(out)
    return (text or "").strip()

# ================== FALLBACK LOGIC ==================
def call_ai_with_fallback(temp: float, humi: float):
    prompt = f"Dự báo nông nghiệp: nhiệt độ {temp}°C, độ ẩm {humi}%. Viết 1 prediction ngắn (1 câu) và 1 advice ngắn (1-2 câu)."
    prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"

    # 1) OpenRouter
    if OPENROUTER_API_KEY:
        try:
            start = time.time()
            text = call_openrouter(temp, humi, prompt)
            print(f"✅ OpenRouter OK (took {time.time()-start:.2f}s)")
            if text:
                return prediction, text
        except Exception as e:
            print("⚠️ OpenRouter failed:", e)
            traceback.print_exc()

    # 2) Gemini
    if GEMINI_API_KEY and GEMINI_API_URL:
        try:
            start = time.time()
            text = call_gemini(prompt)
            print(f"✅ Gemini OK (took {time.time()-start:.2f}s)")
            if text:
                return prediction, text
        except Exception as e:
            print("⚠️ Gemini failed:", e)
            traceback.print_exc()

    # 3) Hugging Face
    if HF_API_KEY:
        try:
            start = time.time()
            text = call_huggingface(prompt)
            print(f"✅ HuggingFace OK (took {time.time()-start:.2f}s, model={HF_MODEL})")
            if text:
                return prediction, text
        except Exception as e:
            print("⚠️ HuggingFace failed:", e)
            traceback.print_exc()

    # 4) Local fallback
    advice = "Theo dõi cây trồng, tưới nước đều, bón phân cân đối"
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
        traceback.print_exc()

# ================== BACKGROUND TASK ==================
async def periodic_ai_loop():
    while True:
        temp = latest_data.get("temperature") or DEFAULT_TEMP
        humi = latest_data.get("humidity") or DEFAULT_HUMI

        prediction, advice = call_ai_with_fallback(temp, humi)
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
