import logging
import random
from fastapi import FastAPI, Request
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import uvicorn

# --- Cấu hình logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# --- Cấu hình ThingsBoard ---
TB_TOKEN = "pk94asonfacs6mbeuutg"  # Token cố định cho thiết bị
TB_URL = f"http://thingsboard.cloud/api/v1/{TB_TOKEN}/telemetry"

# --- Tạo ứng dụng FastAPI ---
app = FastAPI()

# --- Hàm gửi dữ liệu lên ThingsBoard ---
def send_to_thingsboard(payload: dict):
    try:
        logger.info(f"[TB ▶] Payload: {payload}")
        resp = requests.post(TB_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            logger.info(f"[TB ◀] OK {resp.status_code}")
        else:
            logger.warning(f"[TB ◀] LỖI {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"[TB] EXCEPTION: {e}")

# --- Hàm tạo dữ liệu mẫu ---
def generate_sample_data():
    temperature = round(random.uniform(25, 35), 1)
    humidity = round(random.uniform(60, 80), 1)
    battery = random.randint(50, 100)
    payload = {
        # --- Dữ liệu cảm biến ---
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        # --- Thông tin dự báo và cây trồng ---
        "plant_type": "Rau muống",  # loại cây trồng
        "location_name": "10.80609,106.75222",  # vị trí hiện tại (ví dụ toạ độ)
        "weather_now_desc": "Nhiều mây",  # thời tiết hiện tại
        "weather_now_temp": temperature,
        "weather_now_humidity": humidity,
        # --- Lời khuyên mẫu ---
        "advice_nutrition": "Ưu tiên Kali (K) | Cân bằng NPK | Bón phân hữu cơ",
        "advice_care": "Tưới đủ nước, theo dõi thường xuyên | Độ ẩm ổn định cho rau muống",
        "advice_note": "Quan sát cây trồng và điều chỉnh thực tế",
        "advice": " | ".join([
            "Ưu tiên Kali (K)",
            "Cân bằng NPK",
            "Bón phân hữu cơ",
            "Tưới đủ nước, theo dõi thường xuyên",
            "Độ ẩm ổn định cho rau muống",
            "Quan sát cây trồng và điều chỉnh thực tế"
        ]),
        "prediction": f"Nhiệt độ {temperature}°C, độ ẩm {humidity}%"
    }
    return payload

# --- Job định kỳ gửi dữ liệu mẫu ---
def job_send_sample():
    logger.info("[JOB] Gửi dữ liệu mẫu định kỳ lên ThingsBoard...")
    payload = generate_sample_data()
    send_to_thingsboard(payload)

# --- Lịch chạy APScheduler ---
scheduler = BackgroundScheduler()
scheduler.add_job(job_send_sample, 'interval', minutes=5)  # gửi 5 phút/lần
scheduler.start()

# --- API nhận dữ liệu từ ESP32 ---
@app.post("/telemetry")
async def receive_telemetry(req: Request):
    data = await req.json()
    logger.info(f"[ESP32 ▶] Dữ liệu nhận: {data}")
    send_to_thingsboard(data)
    return {"status": "OK"}

# --- Kiểm tra API ---
@app.get("/")
async def root():
    return {"message": "AgriBot server đang chạy. Dữ liệu được gửi lên ThingsBoard định kỳ mỗi 5 phút."}

# --- Chạy server ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)
