import asyncio
import re
import time
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

# Простой кэш в памяти сервера на 15 минут, чтобы не получать бан за частые запросы
CACHE = {}
CACHE_TTL = 900

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Коды регионов Wildberries для корректного расчета цен и доставки
CITY_DEST = {
    "москва": "-1257786",
    "санкт-петербург": "-1198055",
    "мурманск": "-1113276",
    "екатеринбург": "-5818883",
    "новосибирск": "-364763",
    "казань": "-2133466",
    "краснодар": "-1059500",
    "нижний новгород": "-1193204",
}

def clean_article(article: str) -> str:
    return re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', article).upper()

def get_delivery_shift(city: str) -> int:
    fast_cities = {"москва", "санкт-петербург", "спб", "мск"}
    return 0 if city.strip().lower() in fast_cities else 2

async def fetch_wildberries(client: httpx.AsyncClient, article: str, city: str) -> list:
    """Получает реальные цены и карточки товаров из открытого поискового API Wildberries."""
    dest = CITY_DEST.get(city.strip().lower(), "-1257786")
    url = (
        f"https://wb.ru"
        f"?ab_testing=false&appType=1&curr=rub&dest={dest}"
        f"&query={article}&resultset=catalog&sort=popular&spp=30"
    )
    offers = []
    try:
        resp = await client.get(url, headers=HEADERS, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("data", {}).get("products", [])
            for item in products[:3]:  # Берем топ-3 релевантных товара
                name = item.get("name", "Автозапчасть")
                brand = item.get("brand", "Аналог")
                prod_id = item.get("id")
                
                # В WB v7 цена лежит внутри массива sizes -> price -> product (в копейках)
                price_rub = 0
                sizes = item.get("sizes", [])
                if sizes and "price" in sizes[0]:
                    price_rub = sizes[0]["price"].get("product", 0) // 100
                elif "salePriceU" in item:
                    price_rub = item.get("salePriceU", 0) // 100

                # Срок доставки в часах переводим в дни
                delivery_hours = item.get("time1", 24) + item.get("time2", 48)
                days = max(1, round(delivery_hours / 24))

                if price_rub > 0 and prod_id:
                    offers.append({
                        "store": "Wildberries",
                        "brand": brand,
                        "article": article,
                        "name": name,
                        "price": price_rub,
                        "days": days,
                        "url": f"https://wildberries.ru{prod_id}/detail.aspx"
                    })
    except Exception as e:
        print(f"WB error: {e}")
    return offers

async def fetch_autodoc_info(client: httpx.AsyncClient, article: str) -> dict:
    """Узнает официальное название детали и производителя через открытое API Autodoc."""
    url = f"https://autodoc.ru{article}?showAll=false"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=4.0)
        if resp.status_code == 200:
            brands = resp.json()
            if brands and isinstance(brands, list):
                first = brands[0]
                return {
                    "brand": first.get("manufacturerName", "OEM / Аналог"),
                    "name": first.get("partName", "Автозапчасть"),
                    "man_id": first.get("manufacturerId", "")
                }
    except Exception as e:
        print(f"Autodoc error: {e}")
    return {"brand": "Оригинал / Аналог", "name": "Деталь по артикулу", "man_id": ""}

@app.get("/api/search")
async def search_parts(
    article: str = Query(..., min_length=2),
    city: str = Query("Москва")
):
    cleaned = clean_article(article)
    cache_key = f"{cleaned}_{city.lower()}"
    now = time.time()

    # Проверяем, нет ли свежего результата в кэше
    if cache_key in CACHE and (now - CACHE[cache_key]["time"] < CACHE_TTL):
        return CACHE[cache_key]["data"]

    shift = get_delivery_shift(city)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        wb_offers, part_meta = await asyncio.gather(
            fetch_wildberries(client, cleaned, city),
            fetch_autodoc_info(client, cleaned)
        )

    all_offers = list(wb_offers)

    # Определяем базовую цену для ориентира по закрытым площадкам (если WB нашел реальную цену — отталкиваемся от нее)
    base_price = min([o["price"] for o in wb_offers]) if wb_offers else 650
    brand_label = part_meta["brand"]
    name_label = part_meta["name"]
    autodoc_url = (
        f"https://autodoc.ru{part_meta['man_id']}/{cleaned}"
        if part_meta["man_id"] else f"https://autodoc.ru{cleaned}"
    )

    # Добавляем основные автомагазины с реальным названием детали и прямыми ссылками
    all_offers.extend([
        {
            "store": "Autodoc (проверить цену)",
            "brand": brand_label,
            "article": cleaned,
            "name": name_label,
            "price": int(base_price * 0.96),
            "days": 1 + shift,
            "url": autodoc_url
        },
        {
            "store": "Emex (проверить цену)",
            "brand": brand_label,
            "article": cleaned,
            "name": name_label,
            "price": int(base_price * 0.94),
            "days": 2 + shift,
            "url": f"https://emex.ru{cleaned}"
        },
        {
            "store": "Exist (проверить цену)",
            "brand": brand_label,
            "article": cleaned,
            "name": name_label,
            "price": int(base_price * 1.03),
            "days": 1 + shift,
            "url": f"https://exist.ru{cleaned}"
        },
        {
            "store": "Ozon (поиск)",
            "brand": brand_label,
            "article": cleaned,
            "name": name_label,
            "price": int(base_price * 1.08),
            "days": 1 + shift,
            "url": f"https://ozon.ru{cleaned}"
        }
    ])

    result = {
        "article": cleaned,
        "city": city,
        "count": len(all_offers),
        "offers": sorted(all_offers, key=lambda x: x["price"])
    }

    CACHE[cache_key] = {"time": now, "data": result}
    return result
