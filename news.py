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
MIN_SCORE = 8
MIN_BODY_LENGTH = 250
MAX_BODY_LENGTH = 9000
COPY_FRAGMENT_WORDS = 12

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is missing")
if not GH_TOKEN:
    raise SystemExit("GH_TOKEN is missing")

client = OpenAI(api_key=OPENAI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JuristSochiNewsBot/1.0; +https://juristsochi.ru/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Ключевые слова
# ---------------------------------------------------------------------------

# Узкие маркеры темы. Попадание ХОТЯ БЫ ОДНОГО обязательно для прохода.
TOPIC_KEYWORDS = [
    "недвиж", "квартир", "дом ", "жиль", "земл", "участок", "кадастр",
    "росреестр", "ипотек", "аренд", "собственност", "долев", "многоквартир",
    "капремонт", "перепланиров", "разрешение на строительство", "строительств",
    "застройщик", "реконструкц", "новострой", "рынок жилья", "ввод жилья",
    "жк ", "дду", "дольщик", "самострой", "самовольн", "жкх",
    "управляющ", "тсж", "снос", "жилой комплекс", "подряд",
    "сервитут", "градостро", "разрешенн", "охранн зон", "культурн наслед",
    "гараж", "земли сельскохозяйственного назначения", "нестационарн",
    "дизайн интерьер", "отделк", "архитектур", "проектиров",
    "риэлтор", "риелтор", "инженерн",
]

# Юридический контекст. Повышает оценку, но САМ ПО СЕБЕ проход не даёт.
CONTEXT_KEYWORDS = [
    "банкротств", "залог", "торги", "аукцион", "арбитраж", "изъяти",
    "обременен", "регистраци прав", "сделк",
]

REGIONAL_KEYWORDS = ["краснодар", "сочи", "кубан", "краснодарский край"]

FEDERAL_KEYWORDS = [
    "российской федерации", "федеральный закон", "верховный суд",
    "конституционный суд", "правительство российской федерации", "минстрой",
    "росреестр", "обзор судебной практики",
]

IRRELEVANT_HINTS = [
    "спорт", "культур", "кино", "театр", "концерт", "погода", "туризм",
    "школ", "образован", "медицин", "авар", "пожар", "кримин", "полици",
    "фестиваль", "ремонт дорог", "бензин", "зарплат", "отставк",
]

# Явные маркеры ЧУЖИХ тем (судебная реформа, кадры, авто).
OFF_TOPIC_HINTS = [
    "судебн департамент", "организационн обеспеч", "утрат доверия",
    "военн суд", "госслуж", "чиновник", "финансово-хозяйствен",
    "автомобил", "транспорт",
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

# Фразы, которые сигнализируют рекомендацию, прогноз или неподтверждённый вывод.
FORBIDDEN_OUTPUT_PATTERNS = [
    r"рекоменду[её]тся", r"стоит (?:купить|продать|влож|обратиться)",
    r"удачное время", r"выгодн(?:о|ая)", r"перспективн", r"гарантир",
    r"обязательно привед", r"может привести", r"восстановлени[ея] рынка",
    r"шанс защитить", r"снизит риски", r"избежать (?:всех )?рисков",
    r"юридическ(?:ая|ую) консультац", r"следите за изменениями",
]


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def clean_text(value):
    return " ".join(unescape(value or "").split()).strip()


def has_any(text, words):
    return any(word in text for word in words)


def words(text):
    return re.findall(r"[а-яa-z0-9]+", (text or "").lower())


def sentence_count(text):
    return len([part for part in re.split(r"[.!?]+", text) if part.strip()])


def has_long_copied_fragment(source, draft, n=COPY_FRAGMENT_WORDS):
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


# ---------------------------------------------------------------------------
# Загрузка и извлечение текста
# ---------------------------------------------------------------------------

def source_url_is_article(source_type, url):
    if source_type == "vsrf":
        return bool(re.search(r"/(press_center/news|documents/(all|own))/\d+/?$", url))
    return bool(url)


def extract_body_from_html(html, source_type):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, nav, header, footer, aside, form, noscript"):
        tag.decompose()

    selectors = {
        "vsrf": [
            "div.vs-content div.vs-text", "div.js-news-text",
            "div.news-detail", "div[itemprop='articleBody']",
            "article", "main",
        ],
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


# ---------------------------------------------------------------------------
# Тематический отбор
# ---------------------------------------------------------------------------

def classify_item(title, description, body, source_type):
    text = f"{title} {description} {body}".lower()
    topic_hits = sum(keyword in text for keyword in TOPIC_KEYWORDS)
    context_hits = sum(keyword in text for keyword in CONTEXT_KEYWORDS)
    region_hits = sum(keyword in text for keyword in REGIONAL_KEYWORDS)
    federal_hits = sum(keyword in text for keyword in FEDERAL_KEYWORDS)
    irrelevant_hits = sum(keyword in text for keyword in IRRELEVANT_HINTS)
    off_topic_hits = sum(keyword in text for keyword in OFF_TOPIC_HINTS)
    hard_block_hits = sum(keyword in text for keyword in HARD_BLOCK_HINTS)

    if hard_block_hits and not topic_hits:
        return False, 0, "hard_block"

    # Обязательное условие: материал вообще про недвижимость / строительство.
    if not topic_hits:
        return False, 0, "not_real_estate_or_construction"

    # Слабая тема + явные маркеры чужой темы = отклон.
    if off_topic_hits and topic_hits <= 1:
        return False, 0, "off_topic_subject"

    score = (
        topic_hits * 3
        + context_hits * 1
        + region_hits * 3
        + federal_hits * 2
        + SOURCE_WEIGHTS[source_type]
    )
    if has_any(text, ["верховн", "судебн", "разъяснен", "определен", "решени"]):
        score += 2
    if irrelevant_hits:
        score -= 3
    if off_topic_hits:
        score -= 2
    if score < MIN_SCORE:
        return False, score, "low_relevance_score"
    return True, score, "ok"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

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


def save_to_github(news_list):
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    content = base64.b64encode(
        json.dumps(news_list, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
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


# ---------------------------------------------------------------------------
# Модель: генерация, аудит, валидация
# ---------------------------------------------------------------------------

def ask_model(messages, temperature=0):
    result = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=1400,
        response_format={"type": "json_object"},
    )
    return json.loads(result.choices[0].message.content.strip())


def create_draft(item, body, retry_note=""):
    retry_instruction = f"\nДополнительное требование: {retry_note}\n" if retry_note else ""
    prompt = f"""
Ты готовишь короткую информационную заметку для российского сайта о недвижимости и строительстве.
Используй ТОЛЬКО факты из исходного материала. Не используй общие знания.
Не давай рекомендации, прогнозы, оценку выгодности сделки, обещания результата в суде или юридические консультации.
Не добавляй регионы, даты, цифры, законы, участников спора или последствия, которых нет в материале.
Если в исходнике есть выводы суда, оценочные суждения, слова о «важности», «балансе интересов», «доверии» — НЕ переноси их. Излагай только процессуальные факты: кто, к кому, какой иск/спор, что решили инстанции, куда направлено дело.
Не копируй предложения или крупные фрагменты исходника. Устойчивые названия органов, актов и юридических терминов допустимы.
Если фактов недостаточно для содержательной заметки, верни approved=false.
{retry_instruction}
Исходный заголовок: {item['title']}
Исходный материал:
{body}

Верни только JSON:
{{
  "approved": true,
  "title": "новый нейтральный заголовок без сенсационности",
  "text": "от 3 до 10 предложений; только изложение фактов, БЕЗ единого вывода, оценки или интерпретации",
  "facts": ["3-8 кратких проверяемых фактов из исходного материала"]
}}
"""
    return ask_model([
        {"role": "system", "content": "Ты аккуратный редактор. Возвращай только JSON."},
        {"role": "user", "content": prompt},
    ], temperature=0.2)


def audit_draft(item, body, draft):
    prompt = f"""
Ты проверяешь ТОЛЬКО одно: не добавила ли модель-генератор в черновик информацию,
которой НЕТ в исходном материале. Это единственная цель проверки.

КРИТИЧЕСКИ ВАЖНО — чего НЕ надо делать:
- Черновик — это КРАТКАЯ заметка. Она НЕ обязана включать все факты исходника.
  Отсутствие в черновике каких-то фактов, деталей, участников или эпизодов из исходника —
  это НЕ ошибка и НЕ причина отказа. НЕ проверяй полноту.
- НЕ проверяй стиль, «точность пересказа» или «достаточность» описания действий органов.
  Переформулировка своими словами — это норма, а не неточность.
- НЕ придумывай собственных критериев отказа. Отказ возможен ТОЛЬКО по пунктам 1–3 ниже.

Одобри (approved=true), если выполнены ВСЕ три пункта:
1. Каждый факт, цифра, регион, дата, имя, орган и правовое последствие, которые ЕСТЬ
   в черновике, действительно присутствуют в исходном материале (переформулировка допустима).
2. В черновике нет совета, рекомендации, прогноза, обещания результата в суде
   или юридической консультации.
3. Черновик не копирует дословно длинные фрагменты исходника
   (устойчивые официальные названия, реквизиты актов и юридические термины копированием НЕ считаются).

Отклони (approved=false) ТОЛЬКО при нарушении пункта 1, 2 или 3.
В reason укажи КОНКРЕТНУЮ фразу из черновика, которой нет в исходнике,
или конкретное нарушение пункта 2/3. Не отклоняй за краткость, за пропущенные факты,
за субъективное впечатление о «неточности» или «неполноте».

Если сомневаешься между одобрить и отклонить — ОДОБРЯЙ.

Исходный материал:
{body}

Черновик:
Заголовок: {draft.get('title', '')}
Текст: {draft.get('text', '')}
Факты: {json.dumps(draft.get('facts', []), ensure_ascii=False)}

Верни только JSON: {{"approved": true, "reason": "ok"}}
или {{"approved": false, "reason": "конкретная фраза черновика и почему её нет в исходнике"}}.
"""
    return ask_model([
        {"role": "system", "content": "Ты фактчекер. Ищешь только домыслы генератора сверх исходника. Возвращай только JSON."},
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
        retry_note = ""
        last_status = "model_rejected_or_empty"
        for _ in range(3):
            draft = create_draft(item, body, retry_note)
            valid, reason = validate_draft(body, draft)
            if not valid:
                last_status = reason
            else:
                audit = audit_draft(item, body, draft)
                if audit.get("approved"):
                    return {
                        "source": "law",
                        "title": clean_text(draft["title"]),
                        "text": clean_text(draft["text"]),
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "source_name": SOURCE_NAMES[item["source_type"]],
                        "source_title": item["title"],
                        "source_url": item["source_url"],
                    }, "ok"
                last_status = f"audit_failed:{clean_text(audit.get('reason', ''))}"

            if last_status != "copied_fragment" and not last_status.startswith("audit_failed"):
                break
            retry_note = (
                f"предыдущий вариант отклонен ({last_status}); перепиши заметку иначе, "
                "короче и своими словами, без переноса структуры и фраз источника"
            )
        return None, last_status
    except Exception as error:
        print(f"Ошибка модели для '{item['title']}': {error}", flush=True)
        return None, "model_error"


# ---------------------------------------------------------------------------
# Основной конвейер
# ---------------------------------------------------------------------------

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

    if candidates:
        print("Кандидаты после тематического отбора:", flush=True)
        for item in candidates[:5]:
            print(
                f"  score={item['score']} source={item['source_type']} title={item['title']}",
                flush=True,
            )

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
