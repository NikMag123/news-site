import os
import json
import requests
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
GH_TOKEN = os.environ["GH_TOKEN"]
REPO = "NikMag123/news-site"

def get_news():
    url = "https://newsapi.org/v2/top-headlines?country=ru&apiKey=demo"
    # Используем RSS как альтернативу
    rss_url = "https://lenta.ru/rss/news"
    response = requests.get(rss_url)
    return response.text[:3000]

def rewrite_news(raw):
    result = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": f"Перепиши 3 новости из этого RSS кратко и по-русски в формате JSON массива с полями title и text:\n{raw}"
        }]
    )
    return result.choices[0].message.content

def save_to_github(content):
    url = f"https://api.github.com/repos/{REPO}/contents/news.json"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    
    get = requests.get(url, headers=headers)
    sha = get.json().get("sha", "")
    
    import base64
    encoded = base64.b64encode(content.encode()).decode()
    
    requests.put(url, headers=headers, json={
        "message": "Update news",
        "content": encoded,
        "sha": sha
    })

raw = get_news()
news = rewrite_news(raw)
save_to_github(news)
print("Done!")
