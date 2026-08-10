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

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO = "NikMag123/news-site"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_NEWS_ON_SITE = 200
MIN_SCORE = 9
MIN_BODY_LENGTH = 250
MAX_BODY_LENGTH = 9000
COPY_FRAGMENT_WORDS = 12

# Статические маркетинговые блоки (безопасные, не генерируются ИИ)
DISCLAIMER_TEXT = "Материал носит информационный характер и не является юридической консультацией. Судебная практика индивидуальна."
CTA_TEXT = "Нужна правовая оценка вашей ситуации? Запишитесь на консультацию к нашим специалистам."

# Перебор архива ВС РФ по ID статей
ARCHIVE_START = 35000  # ~ начало июля 2026

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

TOPIC_KEYWORDS = [
    "недвиж", "квартир", "дом ", "жиль", "жил ", "земл", "земельн", "участк",
    "кадастр", "росреестр", "ипотек", "аренд", "собственност", "долев",
    "многоквартир", "капремонт", "перепланиров", "разрешение на строительство",
    "строительств", "застройщик", "реконструкц", "новострой", "рынок жилья",
    "ввод жилья", "жк ", "жку", "дду", "дольщик", "самострой", "самовольн",
    "жкх", "управляющ", "тсж", "снос", "жилой комплекс", "подряд",
    "сервитут", "градостро", "разрешенн", "охранн зон", "культурн наслед",
    "гараж", "земли сельскохозяйственного назначения", "нестационарн",
    "дизайн интерьер", "отделк", "архитектур", "проектиров",
    "риэлтор", "риелтор", "инженерн",
]

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

OFF_TOPIC_HINTS = [
    "судебн департамент", "организационн обеспеч", "военн суд",
    "госслуж", "чиновник", "финансово-хозяйствен",
    "автомобил", "транспорт", "доминирующ",
    "соцвыплат", "военнослужащ", "погибш", "пенсии", "алимент", "развод", "опек", "экстремист", "хакер", "кибер", "террориз",
    "дтп", "дорожн", "авар",
    "потребител", "покупател", "маркетплейс", "товар",
    "международн связ", "звани судьи", "дисциплинарн",
    "поручительств", "кредитн", "заем",
    "налог", "таможен", "лицензи",
    "уголовн дел", "подсудим", "приговор", "оправдан", "обвиняем",
    "лишени свобод", "условн срок",
    # --- НОВОЕ: уголовные дела ---
    "оправдан", "подсудим", "приговор", "уголовн дел", "обвиняем",
    "лишени свобод", "условн срок", "администраци", "глава администраци",
]

HARD_OFF_TOPIC_HINTS = [
    "коррупц", "антикоррупц", "противодействи", "утрат доверия",
    "сведений о доходах", "декларац", "взятк",
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

FORBIDDEN_OUTPUT_PATTERNS = [
    r"рекоменду[её]тся", r"стоит (?:купить|продать|влож|обратиться)",
    r"удачное время", r"выгодн(?:о|ая)", r"перспективн", r"гарантир",
    r"обязательно привед", r"может привести", r"восстановлени[ея] рынка",
    r"шанс защитить", r"снизит риски", r"избежать (?:всех )?рисков",
    r"юридическ(?:ая|ую) консультац", r"следите за изменениями",
]

# Маппинг для автоматической генерации тегов
TAG_MAPPING = {
    "недвижимость": ["недвиж", "квартир", "дом ", "жил ", "ипотек"],
    "земельные споры": ["земл", "земельн", "участк", "кадастр"],
    "строительство": ["строительств", "застройщик", "разрешение на строительство", "реконструкц"],
    "самострой": ["самострой", "самовольн", "снос"],
    "ЖКХ": ["жкх", "управляющ", "тсж", "капремонт", "многоквартир"],
    "арбитраж": ["арбитраж", "банкротств", "торги", "аукцион"],
    "верховный суд": ["верховный суд"],
    "конституционный суд": ["конституционный суд"],
    "дду": ["дду", "дольщик", "новострой"],
    "собственность": ["собственност", "регистраци прав"],
    "сервитут": ["сервитут"],
    "аренда": ["аренд"],
    "залог": ["залог", "обременен"],
    "сделки": ["сделк", "договор"],
}


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
        tuple(source_words[i:i+n])
        for i in range(len(source_words) - n + 1)
    }
    return any(
        tuple(draft_words[i:i+n]) in source_ngrams
        for i in range(len(draft_words) - n + 1)
    )


def generate_tags(title, body):
    """Генерирует теги на основе ключевых слов исходника."""
    text = f"{title} {body}".lower()
    tags = []
    
    for tag, keywords in TAG_MAPPING.items():
        if any(kw in text for kw in keywords):
            tags.append(tag)
    
    return tags


# ---------------------------------------------------------------------------
# Загрузка и извлечение текста
# ---------------------------------------------------------------------------

def source_url_is_article(source_type, url):
    if source_type == "vsrf":
        return bool(re.search(r"/(press_center/news|documents/(all|own))/\d+/?$", url))
    return bool(url)


def extract_title_from_html(html):
    """Извлекает реальный заголовок статьи из HTML."""
    soup = BeautifulSoup(html, "html.parser")
    
    # Способ 1: og:title (самый надёжный — всегда содержит заголовок статьи)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = clean_text(og["content"])
        if len(title) >= 15 and "Официальный сайт" not in title:
            return title
    
    # Способ 2: h1 (но не "Официальный сайт...")
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
        if len(title) >= 15 and "Официальный сайт" not in title:
            return title
    
    # Способ 3: h2
    h2 = soup.find("h2")
    if h2:
        title = clean_text(h2.get_text(" ", strip=True))
        if len(title) >= 15 and "Официальный сайт" not in title:
            return title
    
    # Способ 4: div с классом заголовка новости
    for selector in ["div.news-title", "div.vs-title", "span.news-title",
                     "div[itemprop='headline']", "h3"]:
        el = soup.select_one(selector)
        if el:
            title = clean_text(el.get_text(" ", strip=True))
            if len(title) >= 15 and "Официальный сайт" not in title:
                return title
    
    # Способ 5: <title> с обрезкой мусора
    title_tag = soup.find("title")
    if title_tag:
        title = clean_text(title_tag.get_text(" ", strip=True))
        title = re.sub(r"\s*[—–-]\s*Верховный Суд.*$", "", title)
        title = re.sub(r"^Официальный сайт.*$", "", title)
        if len(title) >= 15:
            return title
    
    return ""


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
                clean_text(p.get_text(" ", strip=True))
                for p in block.find_all("p")
            ]
            paragraphs = [p for p in paragraphs if len(p) >= 35]
            text = "\n\n".join(paragraphs) if paragraphs else clean_text(block.get_text(" ", strip=True))
            if len(text) >= MIN_BODY_LENGTH:
                candidates.append(text)
    
    if not candidates:
        for tag in soup.select("script[type='application/ld+json']"):
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
    """Загружает страницу и извлекает тело статьи + заголовок.
    Возвращает (body, status, real_title)."""
    if not url or not source_url_is_article(source_type, url):
        return "", "not_article_page", ""
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=50)
        response.raise_for_status()
        real_title = extract_title_from_html(response.text)
        body = extract_body_from_html(response.text, source_type)
        if len(body) < MIN_BODY_LENGTH:
            return "", "body_too_short", real_title
        return body[:MAX_BODY_LENGTH], "ok", real_title
    except requests.RequestException as error:
        print(f"Ошибка загрузки страницы {url}: {error}", flush=True)
        return "", "fetch_error", ""


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
    """Собирает статьи ВС РФ: главная страница + перебор архива по ID."""
    results = []
    seen = set()
    
    # --- Часть 1: главная страница ---
    main_count = 0
    try:
        response = requests.get("https://vsrf.ru/", headers=HEADERS, timeout=40)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        for anchor in soup.find_all("a", href=True):
            source_url = urljoin("https://vsrf.ru/", clean_text(unescape(anchor["href"])))
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
            main_count += 1
    except requests.RequestException as error:
        print(f"Ошибка загрузки главной ВС РФ: {error}", flush=True)
    
    # --- Часть 2: перебор архива с автоопределением верхней границы ---
    archive_found = 0
    consecutive_miss = 0
    article_id = ARCHIVE_START
    total_checked = 0
    
    while consecutive_miss < 100:
        total_checked += 1
        if total_checked % 20 == 0:
            print(
                f"  Архив: проверено {total_checked}, найдено {archive_found}, "
                f"текущий ID {article_id}",
                flush=True,
            )
        
        pattern = f"https://vsrf.ru/press_center/news/{article_id}/"
        key = pattern.lower()
        
        if key not in seen:
            try:
                head = requests.head(
                    pattern, headers=HEADERS, timeout=5, allow_redirects=True
                )
                if head.status_code == 200:
                    consecutive_miss = 0
                    seen.add(key)
                    results.append({
                        "title": f"Материал ВС РФ №{article_id}",
                        "description": "",
                        "source_type": "vsrf",
                        "source_url": pattern,
                    })
                    archive_found += 1
                else:
                    consecutive_miss += 1
            except requests.RequestException:
                consecutive_miss += 1
        
        article_id += 1
    
    print(
        f"ВС РФ: главная={main_count}, архив={archive_found}, "
        f"всего={len(results)}, верхний ID={article_id-1}",
        flush=True,
    )
    
    return results


# ---------------------------------------------------------------------------
# Тематический отбор
# ---------------------------------------------------------------------------

def classify_item(title, description, body, source_type):
    text = f"{title}{description}{body}".lower()
    
    topic_hits = sum(k in text for k in TOPIC_KEYWORDS)
    context_hits = sum(k in text for k in CONTEXT_KEYWORDS)
    region_hits = sum(k in text for k in REGIONAL_KEYWORDS)
    federal_hits = sum(k in text for k in FEDERAL_KEYWORDS)
    irrelevant_hits = sum(k in text for k in IRRELEVANT_HINTS)
    off_topic_hits = sum(k in text for k in OFF_TOPIC_HINTS)
    hard_off_hits = sum(k in text for k in HARD_OFF_TOPIC_HINTS)
    hard_block_hits = sum(k in text for k in HARD_BLOCK_HINTS)
    
    if hard_off_hits:
        return False, 0, "off_topic_hard"
    
    if hard_block_hits and not topic_hits:
        return False, 0, "hard_block"
    
    if not topic_hits:
        return False, 0, "not_real_estate_or_construction"
    
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
# Модель
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

АНТИКОПИРОВАНИЕ: НЕ используй ни одной последовательности из 6 и более слов подряд из исходника. Полностью меняй структуру и порядок предложений, а не только отдельные слова.

ДОПОЛНИТЕЛЬНО: В конце основного текста добавь ОДНО предложение "процессуальный контекст":
- Оно должно описывать ТОЛЬКО стадию дела или процессуальное действие из исходника.
- НЕ давать советов, НЕ рекомендовать действия.
- Использовать формулировки: "Дело направлено на новое рассмотрение...", 
  "Спор находится на стадии...", "Ранее аналогичные дела рассматривались..."

Примеры ПРАВИЛЬНЫХ формулировок:
✓ "Дело возвращено в апелляционную инстанцию для повторного рассмотрения."
✓ "Спор о признании права собственности на самовольную постройку прошел три инстанции."

Примеры НЕПРАВИЛЬНЫХ формулировок:
✗ "Владельцам самостроев стоит проверить документы." ← СОВЕТ
✗ "Это решение поможет защитить права собственников." ← ОЦЕНКА

Если фактов недостаточно для содержательной заметки, верни approved=false.

{retry_instruction}

Исходный заголовок: {item['title']}

Исходный материал:

{body}

Верни только JSON:
{{
    "approved": true,
    "title": "новый нейтральный заголовок без сенсационности",
    "text": "от 3 до 10 предложений; только изложение фактов + одно предложение процессуального контекста в конце",
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

- НЕ придумывай собственных критериев отказа. Отказ возможен ТОЛЬКО по пунктам 1-3 ниже.

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
    
    if any(re.search(p, text, re.IGNORECASE) for p in FORBIDDEN_OUTPUT_PATTERNS):
        return False, "forbidden_advice_or_forecast"
    
    if has_long_copied_fragment(body, f"{title}{text}"):
        return False, "copied_fragment"
    
    return True, "ok"


def check_duplicate(article, existing_news):
    """Проверяет, не дублирует ли заметка уже опубликованную по существу."""
    if not existing_news:
        return False, "ok"
    
    published = []
    for item in existing_news[:30]:
        t = item.get("title", "")
        x = item.get("text", "")[:200]
        published.append(f"- {t}: {x}")
    published_block = "\n".join(published)
    
    prompt = f"""
Ты проверяешь, является ли новая заметка ДУБЛЕМ уже опубликованной.

Дубль = ОДНО И ТО ЖЕ конкретное дело/событие: совпадают ВСЕ три признака одновременно:
1) Те же стороны (те же истец, ответчик, орган)
2) Тот же предмет спора (тот же объект, тот же договор, тот же акт)
3) Те же обстоятельства (та же сумма, тот же регион, тот же эпизод)

НЕ дубль (даже если тема похожа):
- Два разных дела о земельных участках в разных регионах → НЕ дубль
- Два разных банкротства разных компаний → НЕ дубль
- Два разных спора о ЖКХ с разными управляющими компаниями → НЕ дубль
- Дела с похожей правовой квалификацией, но разными сторонами → НЕ дубль

Если сомневаешься — ставь duplicate=false. Лучше опубликовать похожую новость,
чем пропустить уникальную.

Новая заметка:

Заголовок: {article['title']}

Текст: {article['text']}

Уже опубликованные:

{published_block}

Верни только JSON:
{{"duplicate": true, "reason": "конкретно какое дело и почему совпадают все 3 признака"}}
или
{{"duplicate": false, "reason": "ok"}}
"""
    
    try:
        result = ask_model([
            {"role": "system", "content": "Ты проверяешь дубли. Дубль = одно и то же дело. Возвращай только JSON."},
            {"role": "user", "content": prompt},
        ])
        return bool(result.get("duplicate")), clean_text(result.get("reason", ""))
    except Exception as error:
        print(f"Ошибка проверки дублей: {error}", flush=True)
        return False, "check_error"


def rewrite_one(item, body):
    try:
        retry_note = ""
        last_status = "model_rejected_or_empty"
        
        for _ in range(4):
            draft = create_draft(item, body, retry_note)
            valid, reason = validate_draft(body, draft)
            
            if not valid:
                last_status = reason
            else:
                audit = audit_draft(item, body, draft)
                if audit.get("approved"):
                    tags = generate_tags(item['title'], body)
                    
                    return {
                        "source": "law",
                        "title": clean_text(draft["title"]),
                        "text": clean_text(draft["text"]),
                        "tags": tags,
                        "disclaimer": DISCLAIMER_TEXT,
                        "cta": CTA_TEXT,
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "source_name": SOURCE_NAMES[item["source_type"]],
                        "source_title": item["title"],
                        "source_url": item["source_url"],
                    }, "ok"
                
                last_status = f"audit_failed:{clean_text(audit.get('reason', ''))}"
            
            if last_status == "copied_fragment":
                retry_note = (
                    "предыдущий вариант слишком близко повторяет исходник: перепиши ПОЛНОСТЬЮ "
                    "другими конструкциями, поменяй порядок фактов и длину предложений, "
                    "не оставляй ни одного оборота из 6+ слов подряд из источника"
                )
            elif last_status.startswith("audit_failed"):
                retry_note = (
                    f"предыдущий вариант отклонен ({last_status}); убери спорную фразу и "
                    "перепиши заметку короче и своими словами"
                )
            else:
                break
        
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
        
        body, status, real_title = fetch_page_body(item["source_url"], item["source_type"])
        if status != "ok":
            stats[status] += 1
            continue
        
        # Заменяем заголовок-заглушку на реальный (для архивных статей)
        if real_title and item["title"].startswith("Материал ВС РФ"):
            item["title"] = real_title
        
        ok, score, reason = classify_item(
            item["title"], item["description"], body, item["source_type"]
        )
        if not ok:
            stats[reason] += 1
            continue
        
        item["body"] = body
        item["score"] = score
        candidates.append(item)
    
    candidates.sort(key=lambda x: x["score"], reverse=True)
    stats["suitable"] = len(candidates)
    
    if candidates:
        print("Кандидаты после тематического отбора:", flush=True)
        for item in candidates[:10]:
            print(
                f"  score={item['score']} source={item['source_type']} title={item['title']}",
                flush=True,
            )
    
    article = None
    for item in candidates:
        candidate_article, status = rewrite_one(item, item["body"])
        if not candidate_article:
            stats[status] += 1
            continue
        
        is_dup, dup_reason = check_duplicate(candidate_article, existing)
        if is_dup:
            print(f"  Дубль: {candidate_article['title']} ({dup_reason})", flush=True)
            stats["duplicate_content"] += 1
            continue
        
        article = candidate_article
        break
    
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
