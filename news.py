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

CORE_KEYWORDS = [
    "недвиж", "квартир", "дом", "жиль", "жил",
    "земл", "участок", "кадастр", "росреестр", "ипотек",
    "аренд", "собственност", "долев", "многоквартир",
    "капремонт", "перепланиров", "разрешение на строительство",
    "строительств", "застройщик", "реконструкц"
]

SECONDARY_KEYWORDS = [
    "жкх", "коммунальн", "собственник", "арендатор",
    "регистрация прав", "права собственности", "жилое помещение"
]

GEO_ALLOWED = [
    "краснодар",
    "сочи",
    "кубан",
    "краснодарский край"
]

FEDERAL_SCOPE_MARKERS = [
    "российской федерации",
    "федеральный закон",
    "верховный суд российской федерации",
    "конституционный суд российской федерации",
    "правительство российской федерации",
    "минстрой россии",
    "росреестр",
]

IRRELEVANT_HINTS = [
    "спорт", "культура", "кино", "театр", "концерт",
    "погода", "туризм", "школ", "образован", "медицин",
    "авар", "пожар", "кримин", "полици", "шоу",
    "фестиваль", "ремонт дорог"
]

HARD_BLOCK_HINTS = [
    "нормативных затрат",
    "нормативные затраты",
    "должностных окладов",
    "бюджетных учреждений",
    "государственных учреждений",
    "исполнительных органов",
    "министерства труда",
    "социального развития",
    "ветеринарии",
    "финансов",
    "бюджет",
    "бюджетирование",
    "казнач",
    "зарплат",
    "оплаты труда"
]

def clean_text(s):
    return " ".join(unescape((s or "")).split()).strip()

def has_any(text, words):
    return any(w in text for w in words)

def classify_item(title, desc, source_type):
    text = (title + " " + desc).lower()

    core_hits = sum(1 for kw in CORE_KEYWORDS if kw in text)
    secondary_hits = sum(1 for kw in SECONDARY_KEYWORDS if kw in text)
    geo_hits = sum(1 for kw in GEO_ALLOWED if kw in text)
    federal_hits = sum(1 for kw in FEDERAL_SCOPE_MARKERS if kw in text)
    irrelevant_hits = sum(1 for kw in IRRELEVANT_HINTS if kw in text)
    hard_block_hits = [kw for kw in HARD_BLOCK_HINTS if kw in text]

    reasons = []

    regional_words = [
        "области", "республики", "края", "округа", "автономного округа",
        "республика ", "область ", "край ", "округ "
    ]

    if source_type == "law":
        is_regional = has_any(text, regional_words)
        if geo_hits == 0 and is_regional:
            return False, 0, ["regional_not_allowed"]
        if geo_hits == 0 and federal_hits == 0:
            return False, 0, ["no_krasnodar_and_not_federal"]
        if geo_hits == 0 and core_hits == 0:
            return False, 0, ["federal_but_not_real_estate"]

    if source_type == "news":
        if core_hits == 0 and geo_hits == 0:
            return False, 0, ["not_relevant"]

    if hard_block_hits and geo_hits == 0 and federal_hits == 0:
        return False, 0, ["hard_block"]

    score = 0
    score += core_hits * 3
    score += secondary_hits
    score += geo_hits * 4
    score += federal_hits * 2

    if source_type == "law":
        score += 1

    if irrelevant_hits:
        score -= 3

    if has_any(text, ["верховн", "пленум", "судебн", "разъяснен", "определен", "решени"]):
        score += 2

    if has_any(text, ["федеральный закон", "постановлен", "приказ", "распоряжен", "указ"]):
        score += 1

    reasons.extend([
        f"core:{core_hits}",
        f"secondary:{secondary_hits}",
        f"geo:{geo_hits}",
        f"federal:{federal_hits}"
    ])

    if irrelevant_hits:
        reasons.append("irrelevant")
    if hard_block_hits:
        reasons.append("hard_block_hint")

    if score < MIN_SCORE:
        return False, score, reasons

    return True, score, reasons

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

        ok, score, reasons = classify_item(title_text, desc_text, source_type)

        results.append({
            "title": title_text,
            "description": desc_text,
            "source_type": source_type,
            "score": score,
            "reasons": reasons,
            "ok": ok
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
    prompt = f"""
Ты — юридический редактор сайта юриста по недвижимости в Краснодарском крае и Сочи.

Сделай не сухой пересказ, а короткий живой юридический комментарий к материалу.

Исходный материал:
Заголовок: {item["title"]}
Описание: {item.get("description", "")}

Правила:
1. Не выдумывай факты, цифры, регионы, последствия и источники.
2. Не добавляй Сочи, Краснодарский край, недвижимость или практические выводы, если этого нет в исходнике.
3. Не делай текст рекламным.
4. Не используй канцелярский стиль.
5. Можно добавить только нейтральное пояснение и практический смысл.
6. Текст должен состоять из 4 коротких фраз:
   - что произошло;
   - что это значит на практике;
   - кому это может быть важно;
   - какой практический вывод можно сделать.
7. Если тема слабая, не усиливай её искусственно, а пиши сдержанно.
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
            "source": item["source_type"],
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
            "source": item["source_type"],
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
        if item["title"].lower() not in existing_titles and item["ok"]:
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

    article = rewrite_one(best)
    updated = [article] + existing
    updated = updated[:MAX_NEWS_ON_SITE]

    save_to_github(updated)
    print("Done!", flush=True)

if __name__ == "__main__":
    main()
