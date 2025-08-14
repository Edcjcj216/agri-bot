from fastapi import FastAPI, Query
import requests
import os
import openai

app = FastAPI()

# Lấy API key từ biến môi trường Railway
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
openai.api_key = OPENAI_API_KEY

@app.get("/")
def home():
    return {"message": "AI Bot Nông nghiệp đang chạy!"}

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
    # 1. Lấy dữ liệu thời tiết
    weather_url = f"https://api.openweathermap.org/data/2.5/forecast?q={location}&appid={WEATHER_API_KEY}&units=metric&lang=vi"
    weather_data = requests.get(weather_url).json()

    if "list" not in weather_data:
        return {"error": "Không lấy được dữ liệu thời tiết."}
    forecast = weather_data["list"][0]
    temp = forecast["main"]["temp"]
    desc = forecast["weather"][0]["description"]

    # 2. Gọi AI để phân tích
    prompt = f"""
    Tôi là chuyên gia nông nghiệp. Với cây {crop} ở {location}, nhiệt độ {temp}°C và thời tiết {desc},
    hãy đưa ra gợi ý dinh dưỡng và chăm sóc phù hợp trong tuần tới.
    """
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "system", "content": "Bạn là chuyên gia nông nghiệp."},
                  {"role": "user", "content": prompt}]
    )

    advice = completion.choices[0].message["content"]
    return {
        "crop": crop,
        "location": location,
        "temperature": temp,
        "weather": desc,
        "advice": advice
    }
