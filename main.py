import os
import logging
import requests
import httpx
import asyncio
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cohere import Client as CohereClient

# ================== CONFIG ==================
TB_URL = "https://thingsboard.cloud/api/v1"
TB_TOKEN = os.getenv("TB_TOKEN")

COHERE_KEY = os.getenv("COHERE_API_KEY")
DEEPAI_KEY = os.getenv("DEEPAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
HF_KEY = os.getenv("HUGGINGFACE_API_TOKEN")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
scheduler = AsyncIOScheduler()
scheduler.start()

# ================== INIT AI CLIENT ==================
cohere_client = CohereClient(COHERE_KEY) if COHERE_KEY else None

# ================== LAST TELEMETRY ==================
last_telemetry = {}

# ================== AI PROVIDER FUNCTIONS ==================
async def ask_hf(prompt: str) -> str:
    if not HF_KEY:
        raise ValueError("Missing HUGGINGFACE_API_TOKEN")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api-inference.huggingface.co/models/facebook/blenderbot-400M-distill",
            headers={"Authorization": f"Bearer {HF_KEY}"},
            json={"inputs": prompt},
        )
        r.raise_for_status()
        data = r.json()
        return data[0]["generated_text"].strip()

async def ask_gemini(prompt: str) -> str:
    if not GEMINI_KEY:
        raise ValueError("Missing GEMINI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

async def ask_cohere(prompt: str) -> str:
    if not cohere_client:
        raise ValueError("Missing COHERE_API_KEY")
    loop = asyncio.get_event_loop()
    def call_cohere():
        response = cohere_client.generate(model="xlarge", prompt=prompt, max_tokens=200)
        return response.generations[0].text.strip()
    return await loop.run_in_executor(None, call_cohere)

async def ask_deepai(prompt: str) -> str:
    if not DEEPAI_KEY:
        raise ValueError("Missing DEEPAI_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.deepai.org/api/text-generator",
            headers={"api-key": DEEPAI_KEY},
            data={"text": prompt},
        )
        r.raise_for_status()
        return r.json().get("output", "").strip()

# ================== AI ADVICE STRICT ==================
async def get_ai_advice_strict(prompt: str, hoi: str) -> str:
    for fn in [ask_gemini, ask_cohere, ask_deepai, ask_hf]:
        try:
            resp = await fn(prompt)
            if resp.strip():
                return resp.strip()
        except Exception as e:
            logging.warning(f"AI provider failed: {e}")
    return f"Xin lỗi, hiện tại hệ thống AI không khả dụng để trả lời câu hỏi: '{hoi}'"

# ================== HELPER: LIMIT OUTPUT 1-3 CÂU ==================
def limit_to_3_sentences(text: str) -> str:
    sentences = text.replace("\n", " ").split(". ")
    limited = ". ".join(sentences[:3]).strip()
    if not limited.endswith("."):
        limited += "."
    return limited

# ================== PUSH THINGSBOARD ==================
def push_to_tb(data: dict):
    global last_telemetry
    advice_text = data.get("advice_text", "")
    advice_text = limit_to_3_sentences(advice_text)
    data["advice_text"] = advice_text

    url = f"{TB
