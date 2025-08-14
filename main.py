from fastapi import FastAPI, Query, HTTPException, Depends, Request
import httpx
import os
import logging
from openai import OpenAI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from cachetools import TTLCache
import json
from fastapi.security import APIKeyHeader

# 1. CẤU HÌNH NÂNG CAO
# ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 2. CACHE HIỆU NĂNG
# ==================
weather_cache = TTLCache(maxsize=1000, ttl=3600)  # Cache 1 giờ
advice_cache = TTLCache(maxsize=500, ttl=1800)    # Cache 30 phút

# 3. BẢO MẬT API
# ==============
api_key_header = APIKeyHeader(name="X-API-KEY")

def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key != os.getenv("API_SECRET_KEY"):
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key

app = FastAPI(
    title="AgriBot Pro API",
    description="API thông minh cho nông nghiệp với AI và dữ liệu thời tiết",
    version="3.1",
    docs_url="/api-docs",
    redoc_url=None,
    openapi_tags=[{
        "name": "Agriculture",
        "description": "Các endpoint tư vấn nông nghiệp"
    }]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=json.loads(os.getenv("ALLOWED_ORIGINS", "[]")),
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. MODELS NÂNG CẤP
# ==================
class WeatherData(BaseModel):
    temp: float = Field(..., example=27.5)
    humidity: int = Field(..., example=65)
    description: str = Field(..., example="mây rải rác")
    icon: str = Field(..., example="04d")

class AdviceResponse(BaseModel):
    metadata: dict
    data: dict

# 5. ENDPOINTS NÂNG CẤP
# =====================
@app.get("/", include_in_schema=False)
async def home():
    return {"status": "running", "version": app.version, "timestamp": datetime.utcnow()}

@app.get("/health", tags=["Monitoring"])
async def health_check():
    """Endpoint kiểm tra tình trạng hệ thống"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/advise", response_model=AdviceResponse, tags=["Agriculture"])
async def get_advice(
    request: Request,
    crop: str = Query(..., min_length=2, max_length=50, example="lúa"),
    location: str = Query(..., min_length=2, max_length=100, example="Hà Nội"),
    _: str = Depends(get_api_key)
):
    """Nhận tư vấn nông nghiệp dựa trên cây trồng và địa điểm"""
    cache_key = f"{crop}_{location}"
    
    # Kiểm tra cache
    if cache_key in advice_cache:
        logger.info(f"Returning cached advice for {cache_key}")
        return advice_cache[cache_key]

    try:
        # 1. Lấy dữ liệu thời tiết (có cache)
        weather_info = await get_weather_data(location)
        
        # 2. Gọi OpenAI API
        advice = await generate_ai_advice(crop, location, weather_info)
        
        response = {
            "metadata": {
                "model": "gpt-3.5-turbo",
                "weather_source": "OpenWeatherMap",
                "timestamp": datetime.utcnow().isoformat()
            },
            "data": {
                "crop": crop,
                "location": location,
                "weather": weather_info,
                "advice": advice
            }
        }
        
        # Lưu vào cache
        advice_cache[cache_key] = response
        return JSONResponse(response)

    except Exception as e:
        logger.error(f"Error in get_advice: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

# 6. HELPER FUNCTIONS
# ===================
async def get_weather_data(location: str) -> dict:
    """Lấy dữ liệu thời tiết với cache"""
    if location in weather_cache:
        return weather_cache[location]

    async with httpx.AsyncClient(timeout=TIMEOUT_CONFIG) as client:
        response = await client.get(
            WEATHER_API_URL,
            params={
                "q": location,
                "appid": os.getenv("WEATHER_API_KEY"),
                "units": "metric",
                "lang": "vi"
            }
        )
        response.raise_for_status()
        data = response.json()
        
        if not data.get("list"):
            raise HTTPException(status_code=404, detail="Weather data not found")
        
        forecast = data["list"][0]
        weather_info = {
            "temp": forecast["main"]["temp"],
            "humidity": forecast["main"]["humidity"],
            "description": forecast["weather"][0]["description"],
            "icon": forecast["weather"][0]["icon"]
        }
        
        weather_cache[location] = weather_info
        return weather_info

async def generate_ai_advice(crop: str, location: str, weather: dict) -> str:
    """Tạo lời khuyên từ AI"""
    prompt = f"""
    Là chuyên gia nông nghiệp, hãy đưa ra lời khuyên cụ thể về:
    - Cây trồng: {crop}
    - Địa điểm: {location}
    - Thời tiết: {weather['description']}, {weather['temp']}°C, độ ẩm {weather['humidity']}%
    
    Yêu cầu:
    1. Ngắn gọn (150-200 từ)
    2. Có cấu trúc rõ ràng
    3. Bao gồm cảnh báo rủi ro
    4. Ngôn ngữ tự nhiên, thân thiện
    """
    
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Bạn là chuyên gia nông nghiệp 20 năm kinh nghiệm."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7,
        max_tokens=350
    )
    
    return response.choices[0].message.content

# 7. CẤU HÌNH SERVER
# ===================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true",
        timeout_keep_alive=30,
        log_config="log.ini"
    )