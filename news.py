import os
import json
import base64
import requests
import xml.etree.ElementTree as ET
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "NikMag123/news-site"

KEYWORDS = [
    "недвижимость", "квартира", "комната", "дом", "коттедж",
    "таунхаус", "апартаменты", "новостройка", "вторичка",
    "жилье", "жилищ", "жилой", "нежилой", "помещение",
    "земельный", "земля", "участок", "межевание", "кадастр",
    "ипотека", "аренда", "найм", "купля-продажа", "дарение",
    "наследство", "приватизация", "регистрация прав", "собственность",
    "долевое", "переуступка", "залог", "обременение",
    "застройщик", "строительство", "застройка", "снос",
    "реновация", "капремонт", "цемент", "девелопер",
    "управляющая компания", "тсж", "жкх", "коммунальн",
    "риелтор", "агентство недвижимости",
    "материнский капитал", "субсидия", "льготная ипотека",
    "рефинансирование", "эскроу"
]

def fetch_rss(url):
    response = requests.get(url, timeout=15)
    root = ET.fromstring(response.content)
    results = []
    for item in root.iter("item"):
        title = item.find("title")
        desc = item.find("description")
        title_text = title.text if title is not None else ""
        desc_text = desc.text if desc is not None else ""
        combined = (title_text + " " + desc_text).lower()
        if any(kw in combined for kw in KEYWORDS):
            results.append(title_text)
    return results

def rewrite(laws, news):
    laws_text = "\n".join(f"- {t}" for t in laws)
    news_text = "\n".join(f"- {t}" for t in news)

    prompt = f"""Ты помощник риелтора-юриста. Перепиши тексты простым и интересным языком.

ЗАКОНЫ (источник: pravo.gov.ru):
{laws_text}

НОВОСТИ РЫНКА (источник: lenta.ru):
{news_text}

Верни ТОЛЬКО JSON массив, без лишнего текста:
[
  {{"source": "law", "title": "...", "text": "краткое описание закона простым языком"}},
  {{"source": "news", "title": "...", "text": "краткий пересказ новости для клиента"}}
]"""

    result = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000
    )
    return result.choices[0].message.content

def save_to_github(content):
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    sha = requests.get(url, headers=headers).json().get("sha", "")
    encoded = base64.b64encode(content.encode()).decode()
    requests.put(url, headers=headers, json={
        "message": "Update news",
        "content": encoded,
        "sha": sha
    })

laws = fetch_rss("http://publication.pravo.gov.ru/api/rss?pageSize=200")[:3]
news = fetch_rss("https://lenta.ru/rss/news")[:3]

print(f"Законов: {len(laws)}, Новостей: {len(news)}")

result = rewrite(laws, news)
save_to_github(result)
print("Done!")
