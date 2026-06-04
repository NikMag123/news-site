import os
import json
import base64
import requests
import xml.etree.ElementTree as ET
from openai import OpenAI
from datetime import datetime

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "NikMag123/news-site"

MAX_NEWS_ON_SITE = 50

KEYWORDS = [
    "недвижимость", "квартира", "дом", "жилье",
    "земля", "участок", "кадастр", "ипотека",
    "аренда", "собственность", "строительство",
    "застройщик", "жкх", "коммунальн",
    "управляющая компания", "тсж",
    "капремонт", "субсидия",
    "льготная ипотека", "материнский капитал",
    "регистрация прав", "росреестр",
    "долевое строительство"
]

# Разрешенные регионы
ALLOWED_REGIONS = [
    "краснодар",
    "сочи",
    "кубан",
    "краснодарский край"
]

# Нежелательные регионы
BLOCKED_REGIONS = [
    "алтай",
    "карел",
    "буряти",
    "удмурт",
    "якут",
    "чуваш",
    "татарстан",
    "омск",
    "новосибирск",
    "дагестан",
    "челябинск",
    "хакаси",
    "иркут"
]

def is_relevant(combined_text):
    combined_text = combined_text.lower()

    # Блокируем ненужные регионы
    if any(region in combined_text for region in BLOCKED_REGIONS):
        return False

    keyword_matches = sum(
        1 for kw in KEYWORDS
        if kw in combined_text
    )

    # Если есть Краснодарский край/Сочи — пропускаем
    if any(region in combined_text for region in ALLOWED_REGIONS):
        return keyword_matches >= 1

    # Для остальных регионов только реально важные темы
    strong_keywords = [
        "ипотека",
        "недвижимость",
        "кадастр",
        "росреестр",
        "собственность",
        "земля",
        "участок",
        "аренда",
        "строительство"
    ]

    strong_matches = sum(
        1 for kw in strong_keywords
        if kw in combined_text
    )

    return strong_matches >= 2


def fetch_rss(url):
    try:
        response = requests.get(url, timeout=15)
        root = ET.fromstring(response.content)
    except Exception as e:
        print(f"Ошибка загрузки {url}: {e}")
        return []

    results = []

    for item in root.iter("item"):
        title = item.find("title")
        desc = item.find("description")

        title_text = title.text if title is not None else ""
        desc_text = desc.text if desc is not None else ""

        combined = (
            title_text + " " + desc_text
        ).lower()

        if is_relevant(combined):
            results.append({
                "title": title_text,
                "description": desc_text
            })

    return results


def get_existing_news():
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"

    headers = {
        "Authorization": f"token {GH_TOKEN}"
    }

    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        data = resp.json()

        if "content" in data:
            content = base64.b64decode(
                data["content"]
            ).decode("utf-8")

            return json.loads(content), data.get("sha", "")

    except Exception as e:
        print(f"Ошибка чтения news.json: {e}")

    return [], ""


def rewrite_one(item, source_type):

    source_label = (
        "pravo.gov.ru"
        if source_type == "law"
        else "lenta.ru"
    )

    prompt = f"""
Ты — юрист-копирайтер для сайта юриста по недвижимости в Сочи.

Исходный материал:
Заголовок: {item['title']}
Описание: {item.get('description', '')}

ВАЖНО:

1. Не пиши про малозначимые региональные новости.
2. Игнорируй:
- мусорные тарифы
- ЖКУ регионов
- локальные коммунальные программы
- если они не влияют на рынок недвижимости

3. Разрешены:
- недвижимость
- земля
- ипотека
- кадастр
- собственность
- аренда
- строительство
- ЖКХ если это важно собственникам

4. Особенно важны:
- Краснодарский край
- Сочи
- федеральные законы

Напиши полезную статью для сайта юриста.

Ответ СТРОГО JSON:

{{
"title":"...",
"text":"..."
}}
"""

    try:

        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=1800
        )

        raw = result.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]

        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]

        parsed = json.loads(raw)

        parsed["source"] = source_type
        parsed["date"] = datetime.now().strftime("%Y-%m-%d")

        # Храним исходный заголовок
        parsed["source_title"] = item["title"]

        return parsed

    except Exception as e:

        print(f"Ошибка GPT: {e}")

        return {
            "source": source_type,
            "title": item["title"],
            "text": item.get("description", ""),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_title": item["title"]
        }


def save_to_github(news_list):

    url = f"https://api.github.com/repos/{REPO}/contents/news.json"

    headers = {
        "Authorization": f"token {GH_TOKEN}"
    }

    content_json = json.dumps(
        news_list,
        ensure_ascii=False,
        indent=2
    )

    encoded = base64.b64encode(
        content_json.encode("utf-8")
    ).decode("utf-8")

    sha = ""

    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        if resp.status_code == 200:
            sha = resp.json().get("sha", "")

    except Exception:
        pass

    payload = {
        "message": "Update news",
        "content": encoded,
    }

    if sha:
        payload["sha"] = sha

    resp = requests.put(
        url,
        headers=headers,
        json=payload,
        timeout=15
    )

    if resp.status_code in (200, 201):
        print("GitHub обновлен!")
    else:
        print(f"Ошибка GitHub: {resp.status_code}")


# === MAIN ===

existing, _ = get_existing_news()

# Антидубли
existing_titles = {
    n.get("source_title", "").lower()
    for n in existing
}

print(f"На сайте сейчас: {len(existing)}")


laws = fetch_rss(
    "http://publication.pravo.gov.ru/api/rss?pageSize=200"
)

news = fetch_rss(
    "https://lenta.ru/rss/news"
)

print(f"Найдено: laws={len(laws)} news={len(news)}")

new_item = None
new_source = None


for item in laws:

    if item["title"].lower() not in existing_titles:
        new_item = item
        new_source = "law"
        break


if not new_item:

    for item in news:

        if item["title"].lower() not in existing_titles:
            new_item = item
            new_source = "news"
            break


if not new_item:

    print("Нет новых материалов")

else:

    print(f"Публикуем: {new_item['title']}")

    article = rewrite_one(
        new_item,
        new_source
    )

    updated = [article] + existing

    updated = updated[:MAX_NEWS_ON_SITE]

    save_to_github(updated)

    print("Done!")
