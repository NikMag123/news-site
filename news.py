import os
import json
import base64
import requests
import xml.etree.ElementTree as ET
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "NikMag123/news-site"
MAX_NEWS_ON_SITE = 50  # сколько новостей хранить на сайте

KEYWORDS = [
    "недвижимость", "квартира", "комната", "дом", "коттедж",
    "таунхаус", "апартаменты", "новостройка", "вторичка",
    "жилье", "жилищ", "жилой", "нежилой", "помещение",
    "земельный", "земля", "участок", "межевание", "кадастр",
    "ипотека", "аренда", "найм", "купля-продажа", "дарение",
    "наследство", "приватизация", "регистрация прав", "собственность",
    "долевое", "переуступка", "залог", "обременение",
    "застройщик", "строительство", "застройка", "снос",
    "реновация", "капремонт", "девелопер",
    "управляющая компания", "тсж", "жкх", "коммунальн",
    "риелтор", "агентство недвижимости",
    "материнский капитал", "субсидия", "льготная ипотека",
    "рефинансирование", "эскроу"
]


def fetch_rss(url):
    """Получаем заголовки из RSS-ленты, фильтруем по ключевым словам."""
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
        combined = (title_text + " " + desc_text).lower()
        matches = sum(1 for kw in KEYWORDS if kw in combined)
        if matches >= 2:
            results.append({"title": title_text, "description": desc_text})
    return results


def get_existing_news():
    """Получаем текущий news.json из GitHub."""
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        if "content" in data:
            content = base64.b64decode(data["content"]).decode("utf-8")
            # Убираем markdown-обертку если есть
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
            content = content.strip()
            return json.loads(content), data.get("sha", "")
    except Exception as e:
        print(f"Ошибка чтения news.json: {e}")
    return [], ""


def rewrite_one(item, source_type):
    """Рерайт ОДНОЙ новости — развернутая статья для сайта юриста."""
    source_label = "pravo.gov.ru" if source_type == "law" else "lenta.ru"

    prompt = f"""Ты — юрист-копирайтер, пишущий статьи для сайта юриста по недвижимости в Сочи.

Исходный материал ({source_label}):
Заголовок: {item['title']}
Описание: {item.get('description', '')}

Напиши развернутую новостную статью для сайта юриста (4-6 абзацев, 200-300 слов):

1. Придумай цепляющий заголовок, понятный обычным людям
2. В первом абзаце — суть изменения простым языком
3. Во втором — подробности: что конкретно изменилось
4. В третьем — кого это касается (собственники, покупатели, арендаторы и т.д.)
5. В четвертом — практические советы: что делать обычному человеку
6. Если уместно — как это затрагивает рынок недвижимости Сочи и Краснодарского края

Пиши живым языком, без канцелярита. Текст должен быть полезным и интересным.

ВАЖНО: Ответь СТРОГО в формате JSON без каких-либо обёрток, без markdown:
{{"title": "заголовок статьи", "text": "текст статьи (используй HTML-теги: <p> для абзацев, <strong> для выделения)"}}"""

    try:
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        raw = result.choices[0].message.content.strip()

        # Убираем markdown-обертку если GPT её добавил
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        parsed = json.loads(raw)
        parsed["source"] = source_type
        return parsed

    except Exception as e:
        print(f"Ошибка GPT: {e}")
        return {
            "source": source_type,
            "title": item["title"],
            "text": f"<p>{item.get('description', item['title'])}</p>"
        }


def save_to_github(news_list):
    """Сохраняем news.json на GitHub (чистый JSON, без обёрток)."""
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"token {GH_TOKEN}"}

    content_json = json.dumps(news_list, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content_json.encode("utf-8")).decode("utf-8")

    # Получаем sha текущего файла
    sha = ""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
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

    resp = requests.put(url, headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        print("GitHub обновлен!")
    else:
        print(f"Ошибка GitHub: {resp.status_code}")


# === ОСНОВНАЯ ЛОГИКА ===

# 1. Получаем существующие новости
existing, _ = get_existing_news()
existing_titles = {n.get("title", "") for n in existing}
print(f"На сайте сейчас: {len(existing)} новостей")

# 2. Собираем свежие материалы
laws = fetch_rss("http://publication.pravo.gov.ru/api/rss?pageSize=200")
news = fetch_rss("https://lenta.ru/rss/news")
print(f"Найдено: законов={len(laws)}, новостей={len(news)}")

# 3. Выбираем ОДНУ новую запись, которой ещё нет на сайте
new_item = None
new_source = None

# Сначала ищем среди законов
for item in laws:
    if item["title"] not in existing_titles:
        new_item = item
        new_source = "law"
        break

# Если новых законов нет — берем новость
if not new_item:
    for item in news:
        if item["title"] not in existing_titles:
            new_item = item
            new_source = "news"
            break

if not new_item:
    print("Нет новых материалов — пропускаем")
else:
    # 4. Делаем рерайт одной статьи
    print(f"Обрабатываем: {new_item['title'][:60]}...")
    article = rewrite_one(new_item, new_source)

    from datetime import datetime
    article["date"] = datetime.now().strftime("%Y-%m-%d")

    # 5. Добавляем в начало списка, обрезаем до MAX_NEWS_ON_SITE
    updated = [article] + existing
    updated = updated[:MAX_NEWS_ON_SITE]

    # 6. Сохраняем на GitHub
    save_to_github(updated)
    print(f"Опубликовано: {article['title']}")

print("Done!")
