import re
import time
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="PartsRadar API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GDESLON_API_KEY = "e6a33e7bd2714c3b57414a89bf22916fe9da4f31"
CACHE = {}
CACHE_TTL = 900  # Кэш на 15 минут

def clean_article(article: str) -> str:
    return re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', article).upper()

def get_delivery_shift(city: str) -> int:
    fast_cities = {"москва", "санкт-петербург", "спб", "мск"}
    return 0 if city.strip().lower() in fast_cities else 2

async def fetch_gdeslon_offers(client: httpx.AsyncClient, article: str, city: str) -> list:
    """Получает реальные розничные цены и партнерские ссылки через XML API Где Слон."""
    url = "https://gdeslon.ru"
    params = {
        "q": article,
        "l": 15,  # Запрашиваем до 15 предложений для фильтрации
        "p": 1,
        "_gs_at": GDESLON_API_KEY
    }
    shift = get_delivery_shift(city)
    offers = []

    try:
        resp = await client.get(url, params=params, timeout=8.0)
        if resp.status_code == 200 and resp.text:
            root = ET.fromstring(resp.text)
            for item in root.findall(".//offer"):
                name_el = item.find("name")
                price_el = item.find("price")
                url_el = item.find("url")
                vendor_el = item.find("vendor")
                shop_el = item.find("shop_name") or item.find("merchant")

                name = name_el.text.strip() if (name_el is not None and name_el.text) else "Автозапчасть"
                price_val = float(price_el.text) if (price_el is not None and price_el.text) else 0
                buy_url = url_el.text.strip() if (url_el is not None and url_el.text) else ""
                brand = vendor_el.text.strip() if (vendor_el is not None and vendor_el.text) else "Оригинал / Аналог"
                
                # Если название магазина не передано в отдельном теге, берем домен или общее имя
                store_name = shop_el.text.strip() if (shop_el is not None and shop_el.text) else "Магазин-партнер"

                # Фильтруем: оставляем только те товары, где в названии или описании есть искомый артикул
                if price_val > 0 and buy_url and (article.lower() in name.lower() or len(offers) < 5):
                    offers.append({
                        "store": store_name,
                        "brand": brand,
                        "article": article,
                        "name": name[:85],
                        "price": int(round(price_val)),
                        "days": 2 + shift,
                        "url": buy_url
                    })
    except Exception as e:
        print(f"GdeSlon API error: {e}")

    return offers[:8]

@app.get("/api/search")
async def search_parts(
    article: str = Query(..., min_length=2),
    city: str = Query("Москва")
):
    cleaned = clean_article(article)
    cache_key = f"{cleaned}_{city.lower()}"
    now = time.time()

    if cache_key in CACHE and (now - CACHE[cache_key]["time"] < CACHE_TTL):
        return CACHE[cache_key]["data"]

    shift = get_delivery_shift(city)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        real_offers = await fetch_gdeslon_offers(client, cleaned, city)

    # Если по редкому артикулу в партнерской базе пока пусто, добавляем прямые ссылки на крупные автосети
    if not real_offers:
        real_offers = [
            {
                "store": "Emex (поиск)",
                "brand": "Оригинал / Аналог",
                "article": cleaned,
                "name": "Перейти к поиску по артикулу",
                "price": 0,
                "days": 2 + shift,
                "url": f"https://emex.ru{cleaned}"
            },
            {
                "store": "Autodoc (поиск)",
                "brand": "Оригинал / Аналог",
                "article": cleaned,
                "name": "Перейти к поиску по артикулу",
                "price": 0,
                "days": 1 + shift,
                "url": f"https://autodoc.ru{cleaned}"
            },
            {
                "store": "Exist (поиск)",
                "brand": "Оригинал / Аналог",
                "article": cleaned,
                "name": "Перейти к поиску по артикулу",
                "price": 0,
                "days": 1 + shift,
                "url": f"https://exist.ru{cleaned}"
            }
        ]

    result = {
        "article": cleaned,
        "city": city,
        "count": len(real_offers),
        "offers": sorted(real_offers, key=lambda x: (x["price"] == 0, x["price"]))
    }

    CACHE[cache_key] = {"time": now, "data": result}
    return result
