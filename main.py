from fastapi import FastAPI, Query
import requests
import os
from openai import OpenAI
from fastapi.responses import PlainTextResponse

app = FastAPI()

# Lấy API key từ biến môi trường Render
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("Thiếu OPENAI_API_KEY. Hãy đặt key trong Render → Environment.")

if not WEATHER_API_KEY:
    raise ValueError("Thiếu WEATHER_API_KEY. Hãy đặt key trong Render → Environment.")

client = OpenAI(api_key=OPENAI_API_KEY)

@app.get("/")
def home():
    return {"message": "AI Bot Nông nghiệp đang chạy!"}

# Healthcheck cho UptimeRobot
@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK")

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
    # 1. Lấy dữ liệu thời tiết
    weather_url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?q={location}&appid={WEATHER_API_KEY}&units=metric&lang=vi"
    )
    weather_data = requests.get(weather_url).json()

    if "list" not in weather_data:
        return {"error": "Không lấy được dữ liệu thời tiết."}

    forecast = weather_data["list"][0]
    temp = forecast["main"]["temp"]
    desc = forecast["weather"][0]["description"]

    # 2. Gọi AI để phân tích
    prompt = (
        f"Tôi là chuyên gia nông nghiệp. Với cây {crop} ở {location}, "
        f"nhiệt độ {temp}°C và thời tiết {desc}, "
        "hãy đưa ra gợi ý dinh dưỡng và chăm sóc phù hợp trong tuần tới."
    )

    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Bạn là chuyên gia nông nghiệp."},
            {"role": "user", "content": prompt},
        ],
    )

    advice = completion.choices[0].message.content

    return {
        "crop": crop,
        "location": location,
        "temperature": temp,
        "weather": desc,
        "advice": advice,
    }

# Cho phép chạy cục bộ hoặc Render
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))  # Render sẽ set PORT tự động
    uvicorn.run("main:app", host="0.0.0.0", port=port)
