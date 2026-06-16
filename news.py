import os
import json
import base64
import requests
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO = "NikMag123/news-site"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_NEWS_ON_SITE = 50
MIN_SCORE = 5

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is missing")
if not GH_TOKEN:
    raise SystemExit("GH_TOKEN is missing")

client = OpenAI(api_key=OPENAI_API_KEY)

TOPIC_KEYWORDS = [
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

GEO_ALLOWED = [
    "краснодар",
    "сочи",
    "кубан",
    "краснодарский край"
]

DOC_KEYWORDS = [
    "федеральный закон",
    "постановление правительства",
    "постановление",
    "приказ",
    "распоряжение",
    "пленум",
    "верховный суд",
    "конституционный суд",
    "обзор судебной практики",
    "разъяснение",
    "решение суда",
    "определение суда",
    "кадастровой стоимости",
    "росреестр",
    "минстрой",
    "минэкономразвития"
]

IRRELEVANT_HINTS = [
    "спорт", "культура", "кино", "театр", "концерт",
    "погода", "туризм", "школ", "образован", "медицин",
    "авар", "пожар", "кримин", "полици", "шоу",
    "гастр", "праздник", "фестиваль", "ремонт дорог"
]

def clean_text(s):
    return " ".join(unescape((s or "")).split()).strip()

def has_any(text, words):
    return any(w in text for w in words)

def score_item(title, desc, source_type):
    text = (title + " " + desc).lower()
    score = 0
    reasons = []

    topic_hits = sum(1 for kw in TOPIC_KEYWORDS if kw in text)
    if topic_hits:
        score += min(6, topic_hits * 2)
        reasons.append(f"topics:{topic_hits}")

    geo_hits = sum(1 for kw in GEO_ALLOWED if kw in text)
    if geo_hits:
        score += 3
        reasons.append("geo")

    doc_hits = sum(1 for kw in DOC_KEYWORDS if kw in text)
    if doc_hits:
        score += min(4, doc_hits)
        reasons.append(f"docs:{doc_hits}")

    if source_type == "law":
        score += 2
        reasons.append("source:law")
    else:
        score += 1
        reasons.append("source:news")

    if has_any(text, ["верховн", "пленум", "судебн", "обзор практик", "разъяснен", "определен", "решени"]):
        score += 2
        reasons.append("court")

    if has_any(text, ["федеральный закон", "постановлен", "приказ", "распоряжен", "указ"]):
        score += 1
        reasons.append("legal_doc")

    if has_any(text, IRRELEVANT_HINTS):
        score -= 3
        reasons.append("irrelevant")

    if len(text) < 40:
        score -= 2
        reasons.append("short")

    return score, reasons

def fetch_rss(url, source_type):
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as e:
        print(f"Ошибка загрузки {url}: {e}", flush=True)
        return []

    results = []

    for item in root.iter("item"):
        title = item.find("title")
        desc = item.find("description")

        title_text = clean_text(title.text if title is not None else "")
        desc_text = clean_text(desc.text if desc is not None else "")

        if not title_text:
            continue

        score, reasons = score_item(title_text, desc_text, source_type)

        results.append({
            "title": title_text,
            "description": desc_text,
            "source_type": source_type,
            "score": score,
            "reasons": reasons
        })

    return results

def get_existing_news():
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return [], ""

        data = resp.json()
        if "content" in data:
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content), data.get("sha", "")
    except Exception as e:
        print(f"Ошибка чтения news.json: {e}", flush=True)

    return [], ""

def rewrite_one(item):
    source_type = item["source_type"]
    prompt = f"""
Ты пишешь аккуратный, живой и полезный юридический рерайт для сайта юриста по недвижимости.

Исходный материал:
Заголовок: {item["title"]}
Описание: {item.get("description", "")}

Правила:
1. Не выдумывай факты, цифры, регионы, последствия и источники.
2. Не добавляй Сочи, Краснодарский край или любой другой регион, если этого нет в исходнике.
3. Не делай вид, что новость точно про недвижимость, если это не видно из текста.
4. Если текст сухой, можно добавить короткое нейтральное пояснение, но без фантазий.
5. Текст обязательно должен состоять из 3 фраз в таком порядке:
   - что произошло
   - что это значит
   - кому это может быть важно
6. Пиши просто, по-человечески, но профессионально.
7. Не превращай текст в рекламу.
8. Сохраняй смысл исходника.
9. Верни только JSON без markdown и без пояснений.

Формат ответа строго такой:
{{
  "title": "...",
  "text": "..."
}}
"""

    try:
        result = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Ты аккуратный юридический редактор. Возвращай только JSON."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"}
        )

        raw = result.choices[0].message.content.strip()
        parsed = json.loads(raw)

        title = clean_text(parsed.get("title", ""))
        text = clean_text(parsed.get("text", ""))

        if not title or not text:
            raise ValueError("Empty title/text from model")

        return {
            "source": source_type,
            "title": title,
            "text": text,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_title": item["title"]
        }

    except Exception as e:
        print(f"Ошибка GPT для '{item['title']}': {e}", flush=True)

        fallback_text = clean_text(item.get("description", ""))
        if not fallback_text:
            fallback_text = f"Поступил новый материал: {clean_text(item['title'])}"

        return {
            "source": source_type,
            "title": item["title"],
            "text": fallback_text,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_title": item["title"]
        }

def save_to_github(news_list):
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    content_json = json.dumps(news_list, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content_json.encode("utf-8")).decode("utf-8")

    sha = ""
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            sha = resp.json().get("sha", "")
    except Exception:
        pass

    payload = {
        "message": "Update news",
        "content": encoded
    }

    if sha:
        payload["sha"] = sha

    resp = requests.put(url, headers=headers, json=payload, timeout=20)

    if resp.status_code in (200, 201):
        print("GitHub updated", flush=True)
    else:
        print(f"GitHub error: {resp.status_code} {resp.text}", flush=True)

def main():
    existing, _ = get_existing_news()

    existing_titles = {
        n.get("source_title", "").lower()
        for n in existing
        if n.get("source_title")
    }

    print(f"На сайте сейчас: {len(existing)}", flush=True)

    laws = fetch_rss("http://publication.pravo.gov.ru/api/rss?pageSize=200", "law")
    news = fetch_rss("https://lenta.ru/rss/news", "news")

    print(f"Найдено: laws={len(laws)} news={len(news)}", flush=True)

    candidates = []
    for item in laws + news:
        if item["title"].lower() not in existing_titles:
            candidates.append(item)

    if not candidates:
        print("Нет новых материалов", flush=True)
        return

    candidates.sort(key=lambda x: (x["score"], 1 if x["source_type"] == "law" else 0), reverse=True)

    best = candidates[0]
    print(
        f"Выбрано: score={best['score']} source={best['source_type']} title={best['title']}",
        flush=True
    )
    print(f"Причины: {', '.join(best['reasons'])}", flush=True)

    if best["score"] < MIN_SCORE:
        print("Нет материалов с достаточным качеством", flush=True)
        return

    article = rewrite_one(best)
    updated = [article] + existing
    updated = updated[:MAX_NEWS_ON_SITE]

    save_to_github(updated)
    print("Done!", flush=True)

if __name__ == "__main__":
    main()
