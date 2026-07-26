import base64
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from openai import OpenAI


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO = "NikMag123/news-site"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_NEWS_ON_SITE = 50
MIN_SCORE = 4
MIN_BODY_LENGTH = 350
MAX_BODY_LENGTH = 9000

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is missing")
if not GH_TOKEN:
    raise SystemExit("GH_TOKEN is missing")

client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JuristSochiNewsBot/1.0; +https://juristsochi.ru/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CORE_KEYWORDS = [
    "недвиж", "квартир", "дом", "жиль", "земл", "участок", "кадастр",
    "росреестр", "ипотек", "аренд", "собственност", "долев", "многоквартир",
    "капремонт", "перепланиров", "разрешение на строительство", "строительств",
    "застройщик", "реконструкц", "новострой", "рынок жилья", "ввод жилья",
    "сделк", "жк", "дду", "дольщик", "самострой", "самовольн", "жкх",
    "управляющ", "тсж", "снос", "жилой комплекс", "арбитраж", "подряд",
    "сервитут", "градостро", "разрешенн", "охранн", "культурн наслед",
    "гараж", "земли сельскохозяйственного назначения", "изъяти", "залог",
    "банкротств", "аукцион", "торги", "нестационарн",
]

REGIONAL_KEYWORDS = ["краснодар", "сочи", "кубан", "краснодарский край"]
FEDERAL_KEYWORDS = [
    "российской федерации", "федеральный закон", "верховный суд",
    "конституционный суд", "правительство российской федерации", "минстрой",
    "росреестр", "обзор судебной практики",
]
IRRELEVANT_HINTS = [
    "спорт", "культура", "кино", "театр", "концерт", "погода", "туризм",
    "школ", "образован", "медицин", "авар", "пожар", "кримин", "полици",
    "фестиваль", "ремонт дорог", "бензин", "зарплат", "отставк",
]
HARD_BLOCK_HINTS = [
    "нормативных затрат", "должностных окладов", "бюджетных учреждений",
    "государственных учреждений", "ветеринарии", "казнач", "оплаты труда",
]
SOURCE_WEIGHTS = {"vsrf": 4, "pravo": 3}
SOURCE_NAMES = {
    "vsrf": "Верховный Суд Российской Федерации",
    "pravo": "Официальный интернет-портал правовой информации",
}

# These phrases usually signal a recommendation, forecast, or unsupported conclusion.
FORBIDDEN_OUTPUT_PATTERNS = [
    r"рекоменду[её]тся", r"стоит (?:купить|продать|влож|обратиться)",
    r"удачное время", r"выгодн(?:о|ая)", r"перспективн", r"гарантир",
    r"обязательно привед", r"может привести", r"восстановлени[ея] рынка",
    r"шанс защитить", r"снизит риски", r"избежать (?:всех )?рисков",
    r"юридическ(?:ая|ую) консультац", r"следите за изменениями",
]


def clean_text(value):
    return " ".join(unescape(value or "").split()).strip()


def has_any(text, words):
    return any(word in text for word in words)


def words(text):
    return re.findall(r"[а-яa-z0-9]+", (text or "").lower())


def sentence_count(text):
    return len([part for part in re.split(r"[.!?]+", text) if part.strip()])


def has_long_copied_fragment(source, draft, n=6):
    source_words = words(source)
    draft_words = words(draft)
    source_ngrams = {
        tuple(source_words[index:index + n])
        for index in range(len(source_words) - n + 1)
    }
    return any(
        tuple(draft_words[index:index + n]) in source_ngrams
        for index in range(len(draft_words) - n + 1)
    )


def source_url_is_article(source_type, url):
    if source_type == "vsrf":
        return bool(re.search(r"/(press_center/news|documents/(all|own))/\d+/?$", url))
    return bool(url)


def extract_body_from_html(html, source_type):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, nav, header, footer, aside, form, noscript"):
        tag.decompose()

    selectors = {
        "vsrf": ["div.js-news-text", "div[itemprop='articleBody']", "article"],
        "pravo": ["div[itemprop='articleBody']", "article", "main"],
    }.get(source_type, ["div[itemprop='articleBody']", "article", "main"])

    candidates = []
    for selector in selectors:
        for block in soup.select(selector):
            paragraphs = [
                clean_text(paragraph.get_text(" ", strip=True))
                for paragraph in block.find_all("p")
            ]
            paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 35]
            text = "\n\n".join(paragraphs) if paragraphs else clean_text(block.get_text(" ", strip=True))
            if len(text) >= MIN_BODY_LENGTH:
                candidates.append(text)

    if not candidates:
        json_ld = soup.select("script[type='application/ld+json']")
        for tag in json_ld:
            try:
                data = json.loads(tag.get_text(strip=True))
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    text = clean_text(item.get("articleBody", ""))
                    if len(text) >= MIN_BODY_LENGTH:
                        candidates.append(text)

    if not candidates:
        return ""

    # The longest candidate is normally the main article after navigation is removed.
    return max(candidates, key=len)


def fetch_page_body(url, source_type):
    if not url or not source_url_is_article(source_type, url):
        return "", "not_article_page"
    try:
        response = requests.get(url, headers=HEADERS, timeout=25)
        response.raise_for_status()
        body = extract_body_from_html(response.text, source_type)
        if len(body) < MIN_BODY_LENGTH:
            return "", "body_too_short"
        return body[:MAX_BODY_LENGTH], "ok"
    except requests.RequestException as error:
        print(f"Ошибка загрузки страницы {url}: {error}", flush=True)
        return "", "fetch_error"


def fetch_rss_items(url, source_type):
    root = None
    for attempt in range(1, 3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=40)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            break
        except Exception as error:
            print(f"Ошибка загрузки RSS {url}, попытка {attempt}/2: {error}", flush=True)
            if attempt == 1:
                time.sleep(3)
    if root is None:
        return []

    results = []
    for item in root.iter("item"):
        title = item.find("title")
        description = item.find("description")
        link = item.find("link")
        title_text = clean_text(title.text if title is not None else "")
        link_text = clean_text(link.text if link is not None else "")
        if title_text and link_text:
            results.append({
                "title": title_text,
                "description": clean_text(description.text if description is not None else ""),
                "source_type": source_type,
                "source_url": link_text,
            })
    return results


def fetch_pravo():
    return fetch_rss_items("https://publication.pravo.gov.ru/api/rss?pageSize=200", "pravo")


def fetch_vsrf():
    # The category pages load entries dynamically. The current materials are
    # present in the server-rendered markup of the main page.
    pages = ["https://vsrf.ru/"]
    results = []
    seen = set()
    for page_url in pages:
        try:
            response = requests.get(page_url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as error:
            print(f"Ошибка загрузки ВС РФ {page_url}: {error}", flush=True)
            continue

        for anchor in soup.find_all("a", href=True):
            source_url = urljoin(page_url, clean_text(unescape(anchor["href"])))
            if not source_url_is_article("vsrf", source_url):
                continue
            title = clean_text(anchor.get_text(" ", strip=True))
            key = source_url.lower()
            if len(title) < 20 or key in seen:
                continue
            seen.add(key)
            results.append({
                "title": title,
                "description": "",
                "source_type": "vsrf",
                "source_url": source_url,
            })
    return results


def classify_item(title, description, body, source_type):
    text = f"{title} {description} {body}".lower()
    core_hits = sum(keyword in text for keyword in CORE_KEYWORDS)
    region_hits = sum(keyword in text for keyword in REGIONAL_KEYWORDS)
    federal_hits = sum(keyword in text for keyword in FEDERAL_KEYWORDS)
    irrelevant_hits = sum(keyword in text for keyword in IRRELEVANT_HINTS)
    hard_block_hits = sum(keyword in text for keyword in HARD_BLOCK_HINTS)

    if hard_block_hits and not core_hits and not region_hits and not federal_hits:
        return False, 0, "hard_block"
    if not core_hits:
        return False, 0, "not_real_estate_or_construction"

    score = core_hits * 3 + region_hits * 3 + federal_hits * 2 + SOURCE_WEIGHTS[source_type]
    if has_any(text, ["верховн", "судебн", "разъяснен", "определен", "решени"]):
        score += 2
    if irrelevant_hits:
        score -= 3
    if score < MIN_SCORE:
        return False, score, "low_relevance_score"
    return True, score, "ok"


def get_existing_news():
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code != 200:
            return []
        data = response.json()
        return json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except Exception as error:
        print(f"Ошибка чтения news.json: {error}", flush=True)
        return []


def ask_model(messages):
    result = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0,
        max_tokens=1400,
        response_format={"type": "json_object"},
    )
    return json.loads(result.choices[0].message.content.strip())


def create_draft(item, body):
    prompt = f"""
Ты готовишь короткую информационную заметку для российского сайта о недвижимости и строительстве.

Используй ТОЛЬКО факты из исходного материала. Не используй общие знания.
Не давай рекомендации, прогнозы, оценку выгодности сделки, обещания результата в суде или юридические консультации.
Не добавляй регионы, даты, цифры, законы, участников спора или последствия, которых нет в материале.
Не копируй фразы из источника длиннее четырех слов подряд.
Если фактов недостаточно для содержательной заметки, верни approved=false.

Исходный заголовок: {item['title']}
Исходный материал:
{body}

Верни только JSON:
{{
  "approved": true,
  "title": "новый нейтральный заголовок без сенсационности",
  "text": "от 3 до 10 содержательных предложений; только изложение фактов и максимум один нейтральный вывод, прямо следующий из фактов",
  "facts": ["3-8 кратких проверяемых фактов из исходного материала"]
}}
"""
    return ask_model([
        {"role": "system", "content": "Ты аккуратный редактор. Возвращай только JSON."},
        {"role": "user", "content": prompt},
    ])


def audit_draft(item, body, draft):
    prompt = f"""
Проверь черновик информационной заметки по исходному материалу.
Одобри только если каждый существенный факт черновика прямо подтверждается исходным текстом.
Отклони, если есть совет, прогноз, обещание результата, неподтвержденный вывод, добавленный регион, дата, цифра, участник или правовое последствие.
Отклони, если текст почти копирует исходник.

Исходный материал:
{body}

Черновик:
Заголовок: {draft.get('title', '')}
Текст: {draft.get('text', '')}
Факты: {json.dumps(draft.get('facts', []), ensure_ascii=False)}

Верни только JSON: {{"approved": true, "reason": "ok"}}
или {{"approved": false, "reason": "краткая причина"}}.
"""
    return ask_model([
        {"role": "system", "content": "Ты строгий фактчекер юридических новостей. Возвращай только JSON."},
        {"role": "user", "content": prompt},
    ])


def validate_draft(body, draft):
    title = clean_text(draft.get("title", ""))
    text = clean_text(draft.get("text", ""))
    facts = draft.get("facts", [])
    if not draft.get("approved") or not title or not text or not isinstance(facts, list):
        return False, "model_rejected_or_empty"
    if not 3 <= sentence_count(text) <= 10:
        return False, "wrong_sentence_count"
    if len(text) < 280 or len(text) > 1800:
        return False, "wrong_text_length"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in FORBIDDEN_OUTPUT_PATTERNS):
        return False, "forbidden_advice_or_forecast"
    if has_long_copied_fragment(body, f"{title} {text}"):
        return False, "copied_fragment"
    return True, "ok"


def rewrite_one(item, body):
    try:
        draft = create_draft(item, body)
        valid, reason = validate_draft(body, draft)
        if not valid:
            return None, reason
        audit = audit_draft(item, body, draft)
        if not audit.get("approved"):
            return None, f"audit_failed:{clean_text(audit.get('reason', ''))}"
        return {
            "source": "law",
            "title": clean_text(draft["title"]),
            "text": clean_text(draft["text"]),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "source_name": SOURCE_NAMES[item["source_type"]],
            "source_title": item["title"],
            "source_url": item["source_url"],
        }, "ok"
    except Exception as error:
        print(f"Ошибка модели для '{item['title']}': {error}", flush=True)
        return None, "model_error"


def save_to_github(news_list):
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    content = base64.b64encode(json.dumps(news_list, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update news", "content": content}
    try:
        current = requests.get(url, headers=headers, timeout=20)
        if current.status_code == 200:
            payload["sha"] = current.json().get("sha", "")
        response = requests.put(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        print("GitHub updated", flush=True)
    except requests.RequestException as error:
        print(f"GitHub error: {error}", flush=True)


def main():
    existing = get_existing_news()
    existing_urls = {item.get("source_url", "") for item in existing if item.get("source_url")}
    stats = Counter()
    raw_items = fetch_vsrf() + fetch_pravo()
    stats["found"] = len(raw_items)
    candidates = []

    for item in raw_items:
        if item["source_url"] in existing_urls:
            stats["already_published"] += 1
            continue
        body, status = fetch_page_body(item["source_url"], item["source_type"])
        if status != "ok":
            stats[status] += 1
            continue
        ok, score, reason = classify_item(item["title"], item["description"], body, item["source_type"])
        if not ok:
            stats[reason] += 1
            continue
        item["body"] = body
        item["score"] = score
        candidates.append(item)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    stats["suitable"] = len(candidates)
    article = None
    for item in candidates:
        article, status = rewrite_one(item, item["body"])
        if article:
            break
        stats[status] += 1

    print("Статистика обработки:", flush=True)
    for key, value in sorted(stats.items()):
        print(f"  {key}: {value}", flush=True)

    if not article:
        print("Нет материала, прошедшего проверку. news.json не изменен.", flush=True)
        return

    save_to_github([article] + existing[:MAX_NEWS_ON_SITE - 1])
    print(f"Опубликовано: {article['title']}", flush=True)


if __name__ == "__main__":
    main()
