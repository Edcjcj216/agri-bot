from fastapi import FastAPI, Query
import requests
import os
from openai import OpenAI
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("Thi?u OPENAI_API_KEY. Hãy d?t key trong Render ? Environment.")

if not WEATHER_API_KEY:
    raise ValueError("Thi?u WEATHER_API_KEY. Hãy d?t key trong Render ? Environment.")

client = OpenAI(api_key=OPENAI_API_KEY)

@app.get("/")
def home():
    return JSONResponse(
        content={"message": "AI Bot Nông nghi?p dang ch?y!"},
        media_type="application/json; charset=utf-8"
    )

@app.head("/")
async def healthcheck():
    return PlainTextResponse("OK", media_type="text/plain; charset=utf-8")

@app.get("/advise")
def advise(crop: str = Query(...), location: str = Query(...)):
    weather_url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?q={location}&appid={WEATHER_API_KEY}&units=metric&lang=vi"
    )
    weather_data = requests.get(weather_url).json()
    if "list" not in weather_data:
        return JSONResponse(
            content={"error": "Không l?y du?c d? li?u th?i ti?t."},
            media_type="application/json; charset=utf-8"
        )
    forecast = weather_data["list"][0]
    temp = forecast["main"]["temp"]
    desc = forecast["weather"][0]["description"]

    prompt = (
        f"Tôi là chuyên gia nông nghi?p. V?i cây {crop} ? {location}, "
        f"nhi?t d? {temp}°C và th?i ti?t {desc}, "
        "hãy dua ra g?i ý dinh du?ng và cham sóc phù h?p trong tu?n t?i."
    )

    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "B?n là chuyên gia nông nghi?p."},
            {"role": "user", "content": prompt},
        ],
    )
    advice = completion.choices[0].message.content

    return JSONResponse(
        content={
            "crop": crop,
            "location": location,
            "temperature": temp,
            "weather": desc,
            "advice": advice,
        },
        media_type="application/json; charset=utf-8"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
