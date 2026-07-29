import requests
import sqlite3
from datetime import datetime, timedelta

API_URL = "https://api.example.com/articles"

# Only keep articles from the last 6 months
cutoff_date = datetime.today() - timedelta(days=180)

# -----------------------------
# Step 1: Extract Data from API
# -----------------------------
response = requests.get(API_URL)

if response.status_code != 200:
    raise Exception("API request failed")

articles = response.json()

# -----------------------------
# Step 2: Process & Transform
# -----------------------------
clean_articles = []

for article in articles:

    headline = article.get("headline", "").strip()
    section = article.get("section", "").strip()
    url = article.get("url", "").strip()
    date_str = article.get("published_date")

    # Skip incomplete records
    if not headline or not section or not url or not date_str:
        continue

    date = datetime.strptime(date_str, "%Y-%m-%d")

    # Only recent articles
    if date < cutoff_date:
        continue

    clean_articles.append({
        "headline": headline,
        "section": section,
        "date": date_str,
        "url": url
    })

print(f"Validated {len(clean_articles)} articles")

# -----------------------------
# Step 3: Store in SQL Database
# -----------------------------
conn = sqlite3.connect("articles.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS articles (
    headline TEXT,
    section TEXT,
    date TEXT,
    url TEXT
)
""")

cursor.executemany("""
INSERT INTO articles
VALUES (:headline, :section, :date, :url)
""", clean_articles)

conn.commit()

# -----------------------------
# Step 4: Analyze with SQL
# -----------------------------
cursor.execute("""
SELECT section, COUNT(*)
FROM articles
GROUP BY section
ORDER BY COUNT(*) DESC;
""")

print("\nArticles by Section")

for row in cursor.fetchall():
    print(row)

conn.close()
