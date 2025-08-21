import os
import time
import json
import asyncio
import traceback
import logging
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# LOGGING (chi tiết)
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("agri-bot")


def trunc(obj: Any, n: int = 400) -> str:
    """Thu gọn string để log gọn, tránh spam."""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s if len(s) <= n else f"{s[:n]}... (len={len(s)})"


# =========================
# CONFIG
# =========================
# ThingsBoard DEMO device (rau muống Hồ Chí Minh)
DEMO_TOKEN = os.getenv("DEMO_TOKEN", "66dd31thvta4gx1l781q")  # <-- thay bằng token của device DEMO nếu cần
THINGSBOARD_TELEMETRY_URL = f"https://thingsboard.cloud/api/v1/{DEMO_TOKEN}/telemetry"

# Tenant API để đọc last telemetry (cần tài khoản Tenant)
TB_API_URL = os.getenv("TB_API_URL", "https://thingsboard.cloud/api")
TB_TENANT_USER = os.getenv("TB_TENANT_USER", "")  # ví dụ: tenant@yourdomain.com
TB_TENANT_PASS = os.getenv("TB_TENANT_PASS", "")
TB_DEVICE_ID = os.getenv("TB_DEVICE_ID", "56b87360-7d80-11f0-bb1d-31e5940e37d2")  # id của device DEMO

# AI (Hugging Face Inference API)
HF_API_KEY = os.getenv("HF_API_KEY")
HF_MODEL = os.getenv("HF_MODEL", "google/flan-t5-small")

# Ngữ cảnh cây trồng
CROP = os.getenv("CROP", "Rau muống")
LOCATION = os.getenv("LOCATION", "Hồ Chí Minh, VN")

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Agri-Bot DEMO")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# MODELS
# =========================
class ESP32Data(BaseModel):
    temperature: float
    humidity: float
    battery: Optional[float] = None


# =========================
# GLOBAL STATE (giữ bản đọc gần nhất từ ESP32 thật)
# =========================
latest_data: Optional[ESP32Data] = None


# =========================
# HUGGING FACE CALL (log chi tiết)
# =========================
def call_huggingface(prompt: str, timeout: int = 30) -> str:
    if not HF_API_KEY:
        raise RuntimeError("HF_API_KEY chưa được cấu hình trong biến môi trường.")

    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {
        "Authorization": f"Bearer {HF_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"inputs": prompt, "options": {"wait_for_model": True}}

    logger.info(f"HF ▶ POST {url} body={trunc(body)}")
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    logger.info(f"HF ◀ status={resp.status_code} text={trunc(resp.text)}")
    resp.raise_for_status()

    out = resp.json()
    # Chuẩn hóa output (tùy model)
    if isinstance(out, list) and out:
        first = out[0]
        if isinstance(first, dict):
            return first.get("generated_text") or first.get("text") or str(first)
        return str(first)
    if isinstance(out, dict):
        return out.get("generated_text") or out.get("text") or json.dumps(out, ensure_ascii=False)
    return str(out)


# =========================
# AI LOGIC
# =========================
def build_advice(temp: float, humi: float) -> (str, str):
    prediction = f"Nhiệt độ {temp}°C, độ ẩm {humi}%"
    prompt = (
        f"Dự báo nông nghiệp: Nhiệt độ {temp}°C, độ ẩm {humi}% tại {LOCATION}, cây {CROP}. "
        f"Viết ngắn gọn 1 câu dự báo và 1-2 câu gợi ý chăm sóc (ưu tiên thực tế, ngắn gọn)."
    )

    if HF_API_KEY:
        try:
            t0 = time.time()
            text = call_huggingface(prompt)
            logger.info(f"HF ✅ took={time.time()-t0:.2f}s output={trunc(text)}")
            if text:
                return prediction, text.strip()
        except Exception:
            logger.exception("HF ❌ lỗi khi gọi API")

    # Fallback đơn giản nếu AI lỗi/không cấu hình
    advice = (
        "Theo dõi ẩm độ đất; giữ ẩm đều, tưới vào sáng sớm/chiều mát. "
        "Bón phân cân đối, tránh úng/khô đột ngột."
    )
    return prediction, advice


# =========================
# THINGSBOARD: PUSH TELEMETRY (log chi tiết)
# =========================
def push_thingsboard(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = THINGSBOARD_TELEMETRY_URL
    try:
        logger.info(f"TB ▶ POST {url} payload={trunc(payload)}")
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        logger.info(f"TB ◀ status={resp.status_code} text={trunc(resp.text)}")
        return {"status_code": resp.status_code, "text": resp.text}
    except Exception:
        logger.exception("TB ❌ lỗi khi push telemetry")
        return {"error": "exception", "trace": traceback.format_exc()}


# =========================
# THINGSBOARD: READ LAST TELEMETRY (log chi tiết)
# =========================
def tb_login() -> str:
    if not TB_TENANT_USER or not TB_TENANT_PASS:
        raise RuntimeError("Chưa cấu hình TB_TENANT_USER/TB_TENANT_PASS để đọc last telemetry.")
    url = f"{TB_API_URL}/auth/login"
    body = {"username": TB_TENANT_USER, "password": TB_TENANT_PASS}
    logger.info(f"TB ▶ POST {url} body={trunc(body)}")
    resp = requests.post(url, json=body, timeout=15)
    logger.info(f"TB ◀ status={resp.status_code} text={trunc(resp.text)}")
    resp.raise_for_status()
    return resp.json()["token"]


def tb_get_last_timeseries(device_id: str, jwt: str, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    url = f"{TB_API_URL}/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
    params = {}
    if keys:
        params["keys"] = ",".join(keys)
    logger.info(f"TB ▶ GET {url} params={params}")
    resp = requests.get(url, headers={"X-Authorization": f"Bearer {jwt}"}, params=params, timeout=15)
    logger.info(f"TB ◀ status={resp.status_code} text={trunc(resp.text)}")
    resp.raise_for_status()
    return resp.json()


# =========================
# ROUTES
# =========================
@app.get("/")
@app.head("/")
def root():
    masked_token = DEMO_TOKEN[:4] + "***" + DEMO_TOKEN[-3:] if DEMO_TOKEN else None
    return {
        "message": "Agri-Bot DEMO running 🚀",
        "huggingface_configured": bool(HF_API_KEY),
        "thingsboard": {
            "telemetry_url": THINGSBOARD_TELEMETRY_URL,
            "demo_token_masked": masked_token,
            "device_id": TB_DEVICE_ID,
            "api_url": TB_API_URL,
        },
    }


@app.post("/esp32-data")
def receive_esp32(data: ESP32Data):
    global latest_data
    latest_data = data
    logger.info(f"ESP32 ▶ received data={data.json(ensure_ascii=False)}")

    # Gọi AI
    prediction, advice = build_advice(data.temperature, data.humidity)

    payload = {
        "temperature": data.temperature,
        "humidity": data.humidity,
        "battery": data.battery,
        "prediction": prediction,
        "advice": advice,
    }

    tb_res = push_thingsboard(payload)

    return {
        "status": "ok",
        "received": data.dict(),
        "prediction": prediction,
        "advice": advice,
        "tb_result": tb_res,
    }


@app.get("/last-telemetry")
def last_telemetry(keys: Optional[str] = Query(default="temperature,humidity,battery,prediction,advice")):
    try:
        jwt = tb_login()
        key_list = [k.strip() for k in keys.split(",") if k.strip()] if keys else None
        data = tb_get_last_timeseries(TB_DEVICE_ID, jwt, key_list)
        return {"status": "ok", "last_telemetry": data}
    except Exception as e:
        logger.exception("❌ Lỗi khi đọc last telemetry")
        return {"status": "error", "message": str(e)}


@app.get("/debug/test-push")
def debug_test_push():
    # Gửi mẫu dữ liệu để test nhanh đường push lên ThingsBoard
    sample = {
        "temperature": 30.5,
        "humidity": 70.2,
        "battery": 95,
        "prediction": "Nhiệt độ 30.5°C, độ ẩm 70.2%",
        "advice": "Giữ ẩm đều; tưới sáng sớm/chiều mát; tránh úng."
    }
    res = push_thingsboard(sample)
    return {"status": "ok", "sent": sample, "tb_result": res}


# =========================
# BACKGROUND TASK: mỗi 5 phút gửi lại dự báo dựa trên dữ liệu ESP32 thật gần nhất
# =========================
async def periodic_ai_loop():
    while True:
        await asyncio.sleep(300)  # 5 phút
        try:
            if latest_data:
                logger.info("⏳ Loop 5 phút ▶ tạo dự báo từ dữ liệu ESP32 gần nhất…")
                prediction, advice = build_advice(latest_data.temperature, latest_data.humidity)
                payload = {
                    "temperature": latest_data.temperature,
                    "humidity": latest_data.humidity,
                    "battery": latest_data.battery,
                    "prediction": prediction,
                    "advice": advice,
                }
                push_thingsboard(payload)
            else:
                logger.info("⏳ Loop 5 phút ⟹ chưa có dữ liệu ESP32 để gửi.")
        except Exception:
            logger.exception("❌ Lỗi trong loop 5 phút")


@app.on_event("startup")
async def on_startup():
    logger.info("🚀 Application startup")
    asyncio.create_task(periodic_ai_loop())


# =========================
# RUN UVICORN (Render friendly)
# =========================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting Uvicorn on 0.0.0.0:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port)