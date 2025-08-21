import os
import json
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, Request
import httpx

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI()

AI_TOKEN = os.getenv("AI_TOKEN", "")
TB_TOKEN = os.getenv("TB_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))

logging.info(f"=== SERVICE START ===")
logging.info(f"AI_TOKEN: {AI_TOKEN if AI_TOKEN else '<<EMPTY>>'}")
logging.info(f"TB_TOKEN: {TB_TOKEN if TB_TOKEN else '<<EMPTY>>'}")
logging.info(f"PORT: {PORT}")

THINGSBOARD_URL = f"https://demo.thingsboard.io/api/v1/{TB_TOKEN}/telemetry" if TB_TOKEN else None
AI_URL = "https://api.openai.com/v1/chat/completions"  # giả định bạn gọi AI từ OpenAI

# ===================== AI CALL =====================
async def call_ai_api(sensor_data, weather_data):
    if not AI_TOKEN:
        logging.warning("AI_TOKEN KHÔNG CÓ — bỏ qua gọi AI")
        return None

    headers = {
        "Authorization": f"Bearer {AI_TOKEN}",
        "Content-Type": "application/json"
    }

    prompt = (
        f"Dữ liệu cảm biến: {sensor_data}. "
        f"Thời tiết: {weather_data}. "
        "Viết 1 câu dự báo và 1 câu gợi ý chăm sóc."
    )

    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            logging.info(f"AI ▶ {prompt}")
            r = await client.post(AI_URL, headers=headers, json=payload)
            logging.info(f"AI ◀ status={r.status_code}")
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"]
            else:
                logging.error(f"AI trả về lỗi {r.status_code}: {r.text}")
        except Exception as e:
            logging.exception(f"AI call exception: {e}")
    return None

# ===================== GỬI THINGSBOARD =====================
async def post_to_thingsboard(data: dict):
    if not TB_TOKEN or not THINGSBOARD_URL:
        logging.warning("TB_TOKEN KHÔNG CÓ — bỏ qua đẩy TB")
        return

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(THINGSBOARD_URL, json=data)
            if r.status_code == 200:
                logging.info("Đẩy TB OK")
            else:
                logging.error(f"Đẩy TB lỗi {r.status_code}: {r.text}")
        except Exception as e:
            logging.exception(f"Đẩy TB exception: {e}")

# ===================== API TỪ ESP32 =====================
@app.post("/esp32-data")
async def esp32_data(req: Request):
    payload = await req.json()
    logging.info(f"ESP32 data: {payload}")

    # ----------- Giả lập dữ liệu thời tiết -----------
    wx_hien_tai = {
        "temp": 27.2,
        "humidity": 84,
        "desc": "Nhiều mây",
        "iso": datetime.utcnow().isoformat()
    }
    wx_gio_tiep_theo = [
        {"hour": 12, "temp": 26.7, "humidity": 88, "desc": "Giông"},
        {"hour": 13, "temp": 26.3, "humidity": 93, "desc": "Giông"},
        {"hour": 14, "temp": 26.7, "humidity": 88, "desc": "Mưa rào nhẹ"},
        {"hour": 15, "temp": 27.3, "humidity": 81, "desc": "Mưa rào nhẹ"},
        {"hour": 16, "temp": 27.4, "humidity": 77, "desc": "Nhiều mây"},
        {"hour": 17, "temp": 27.2, "humidity": 77, "desc": "Nhiều mây"},
    ]
    wx_ngay_mai = {
        "temp_min": 23.6,
        "temp_max": 32.9,
        "desc": "Nhiều mây"
    }

    # ----------- Tách 6 giờ tiếp theo thành 6 key riêng -----------
    next_hours = {f"wx_gio_{i+1}": wx_gio_tiep_theo[i] for i in range(len(wx_gio_tiep_theo))}

    # ----------- Gọi AI -----------
    sensor_desc = f"Nhiệt độ {payload.get('temperature')}°C, độ ẩm {payload.get('humidity')}%, pin {payload.get('battery')}%. Cây: {payload.get('crop')}"
    weather_desc = f"Hiện tại {wx_hien_tai['desc']} {wx_hien_tai['temp']}°C, Ngày mai {wx_ngay_mai['desc']} {wx_ngay_mai['temp_min']}–{wx_ngay_mai['temp_max']}°C"
    ai_text = await call_ai_api(sensor_desc, weather_desc)

    # ----------- Gom dữ liệu đẩy ThingsBoard -----------
    tb_data = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "temperature": payload.get("temperature"),
        "humidity": payload.get("humidity"),
        "battery": payload.get("battery"),
        "crop": payload.get("crop"),
        "location": f"{payload.get('lat')},{payload.get('lon')}",
        "wx_hien_tai": wx_hien_tai,
        "wx_ngay_mai": wx_ngay_mai,
        **next_hours
    }
    if ai_text:
        tb_data["advice"] = ai_text

    await post_to_thingsboard(tb_data)
    return {"status": "ok", "ai_text": ai_text}

# ===================== AUTO LOOP (NẾU CẦN) =====================
async def auto_loop():
    while True:
        # Có thể thêm logic tự gọi AI hoặc ThingsBoard mỗi X giây
        await asyncio.sleep(60)

# ===================== CHẠY APP =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
