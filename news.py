import os
import json
import base64
import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from html import unescape
from datetime import datetime
from urllib.parse import urljoin
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO = "NikMag123/news-site"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_NEWS_ON_SITE = 50
MIN_SCORE = 4

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is missing")
if not GH_TOKEN:
    raise SystemExit("GH_TOKEN is missing")

client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CORE_KEYWORDS = [
    "недвиж", "квартир", "дом", "жиль", "жил",
    "земл", "участок", "кадастр", "росреестр", "ипотек",
    "аренд", "собственност", "долев", "многоквартир",
    "капремонт", "перепланиров", "разрешение на строительство",
    "строительств", "застройщик", "реконструкц", "новострой",
    "рынок жилья", "ввод жилья", "сделк", "жк",
    "дду", "дольщик", "самострой", "самовольн", "жкх",
    "управляющ", "тсж", "снос", "права собственности",
    "жилой комплекс", "земельн", "ипотечн", "арбитраж",
]

REGIONAL_KEYWORDS = [
    "краснодар", "сочи", "кубан", "краснодарский край"
]

FEDERAL_KEYWORDS = [
    "российской федерации",
    "федеральный закон",
    "верховный суд",
    "верховный суд российской федерации",
    "конституционный суд российской федерации",
    "правительство российской федерации",
    "минстрой россии",
    "росреестр",
    "минстрой рф",
    "пленум",
    "президиум",
    "обзор судебной практики",
]

IRRELEVANT_HINTS = [
    "спорт", "культура", "кино", "театр", "концерт",
    "погода", "туризм", "школ", "образован", "медицин",
    "авар", "пожар", "кримин", "полици", "шоу",
    "фестиваль", "ремонт дорог", "бензин", "зарплат",
    "наличн", "политик", "отставк",
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
    "оплаты труда",
]

SOURCE_WEIGHTS = {
    "vsrf": 4,
    "rbc": 3,
    "pravo": 2,
}

def clean_text(s):
    return " ".join(unescape((s or "")).split()).strip()

def has_any(text, words):
    return any(w in text for w in words)

def extract_body_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        "div.article__text",
        "div.js-news-text",
        "div[itemprop='articleBody']",
        "article",
        "section.article",
        "div.article",
    ]

    candidates = []

    for selector in selectors:
        for block in soup.select(selector):
            paragraphs = [
                clean_text(p.get_text(" ", strip=True))
                for p in block.find_all("p")
            ]
            paragraphs = [p for p in paragraphs if len(p) > 20]

            if paragraphs:
                text = "\n\n".join(paragraphs)
                if len(text) > 200:
                    candidates.append(text)
                    continue

            text = clean_text(block.get_text(" ", strip=True))
            if len(text) > 300:
                candidates.append(text)

    if candidates:
        return max(candidates, key=len)

    meta_selectors = [
        ('meta[name="description"]', "content"),
        ('meta[property="og:description"]', "content"),
    ]

    for selector, attr in meta_selectors:
        tag = soup.select_one(selector)
        if tag and tag.get(attr):
            text = clean_text(tag.get(attr))
            if text:
                return text

    return ""

def fetch_page_body(url):
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        body = extract_body_from_html(resp.text)
        return body
    except Exception as e:
        print(f"Ошибка загрузки страницы {url}: {e}", flush=True)
        return ""

def fetch_rss_items(url, source_type):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"Ошибка загрузки {url}: {e}", flush=True)
        return []

    results = []
    for item in root.iter("item"):
        title = item.find("title")
        desc = item.find("description")
        link = item.find("link")

        title_text = clean_text(title.text if title is not None else "")
        desc_text = clean_text(desc.text if desc is not None else "")
        link_text = clean_text(link.text if link is not None else "")

        if not title_text:
            continue

        results.append({
            "title": title_text,
            "description": desc_text,
            "source_type": source_type,
            "source_url": link_text,
        })

    return results

def fetch_pravo():
    return fetch_rss_items("https://publication.pravo.gov.ru/api/rss?pageSize=200", "pravo")

def fetch_vsrf():
    pages = [
        "https://vsrf.ru/",
        "https://vsrf.ru/press_center/news/",
        "https://vsrf.ru/documents/",
    ]

    results = []
    seen = set()

    for url in pages:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"Ошибка загрузки ВС РФ {url}: {e}", flush=True)
            continue

        for a in soup.find_all("a", href=True):
            href = clean_text(unescape(a["href"]))
            if "/press_center/news/" not in href and "/documents/" not in href:
                continue

            title_text = clean_text(a.get_text(" ", strip=True))
            if len(title_text) < 20:
                continue

            key = title_text.lower()
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "title": title_text,
                "description": "",
                "source_type": "vsrf",
                "source_url": urljoin(url, href),
            })

    return results

def fetch_rbc_kuban():
    url = "https://kuban.rbc.ru/krasnodar/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"Ошибка загрузки RBC Краснодар: {e}", flush=True)
        return []

    results = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = clean_text(unescape(a["href"]))
        if "/krasnodar/" not in href and "kuban.plus.rbc.ru/news/" not in href:
            continue

        title_text = clean_text(a.get_text(" ", strip=True))
        if len(title_text) < 20:
            continue

        key = title_text.lower()
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "title": title_text,
            "description": "",
            "source_type": "rbc",
            "source_url": urljoin(url, href),
        })

    return results

def classify_item(title, desc, source_type):
    text = (title + " " + desc).lower()

    core_hits = sum(1 for kw in CORE_KEYWORDS if kw in text)
    region_hits = sum(1 for kw in REGIONAL_KEYWORDS if kw in text)
    federal_hits = sum(1 for kw in FEDERAL_KEYWORDS if kw in text)
    irrelevant_hits = sum(1 for kw in IRRELEVANT_HINTS if kw in text)
    hard_block_hits = [kw for kw in HARD_BLOCK_HINTS if kw in text]
    source_weight = SOURCE_WEIGHTS.get(source_type, 1)

    reasons = []

    if hard_block_hits and core_hits == 0 and region_hits == 0 and federal_hits == 0:
        return False, 0, ["hard_block"]

    if source_type == "rbc":
        if core_hits == 0:
            return False, 0, ["rbc_not_real_estate"]

    elif source_type == "pravo":
        if core_hits == 0:
            return False, 0, ["pravo_not_real_estate"]

    score = 0
    score += core_hits * 3
    score += region_hits * 3
    score += federal_hits * 2
    score += source_weight

    if has_any(text, ["верховн", "пленум", "судебн", "разъяснен", "определен", "решени"]):
        score += 2

    if has_any(text, ["ипотек", "новостро", "застройщ", "кадастр", "росреестр", "земл", "аренд", "капремонт"]):
        score += 1

    if irrelevant_hits:
        score -= 3

    reasons.extend([
        f"core:{core_hits}",
        f"region:{region_hits}",
        f"federal:{federal_hits}",
        f"source:{source_type}",
    ])

    if irrelevant_hits:
        reasons.append("irrelevant")
    if hard_block_hits:
        reasons.append("hard_block_hint")

    if score < MIN_SCORE:
        return False, score, reasons

    return True, score, reasons

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
    desc = item.get("description", "") or ""
    body = ""
    if item.get("source_url"):
        body = fetch_page_body(item["source_url"])

    if len(body) < 120:
        body = desc

    if len(body) > 7000:
        body = body[:7000] + " ..."

    prompt = f"""
    Ты — юридический редактор сайта по недвижимости и строительству в России.

Сделай не сухой пересказ, а короткий живой юридический комментарий к материалу.

Исходный материал:
Заголовок: {item["title"]}
Короткое описание: {desc}
Полный текст статьи: {body}

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
            "source": "law" if item["source_type"] in ("pravo", "vsrf") else "news",
            "title": title,
            "text": text,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_title": item["title"],
            "source_url": item.get("source_url", ""),
        }

    except Exception as e:
        print(f"Ошибка GPT для '{item['title']}': {e}", flush=True)

        fallback_text = clean_text(desc or body)
        if not fallback_text:
            fallback_text = f"Поступил новый материал: {clean_text(item['title'])}"

        return {
            "source": "law" if item["source_type"] in ("pravo", "vsrf") else "news",
            "title": item["title"],
            "text": fallback_text,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_title": item["title"],
            "source_url": item.get("source_url", ""),
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
        "content": encoded,
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

    laws = fetch_pravo()
    rbc = fetch_rbc_kuban()
    vsrf = fetch_vsrf()

    print(f"Найдено: pravo={len(laws)} rbc={len(rbc)} vsrf={len(vsrf)}", flush=True)

    candidates = []
    for item in (vsrf + rbc + laws):
        title_key = item["title"].lower()
        if title_key in existing_titles:
            continue
        ok, score, reasons = classify_item(item["title"], item.get("description", ""), item["source_type"])
        if not ok:
            continue
        item["score"] = score
        item["reasons"] = reasons
        candidates.append(item)

    if not candidates:
        print("Нет новых материалов", flush=True)
        return

    candidates.sort(
        key=lambda x: (
            x["score"],
            3 if x["source_type"] == "vsrf" else 2 if x["source_type"] == "rbc" else 1 if x["source_type"] == "pravo" else 0
        ),
        reverse=True
    )

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
