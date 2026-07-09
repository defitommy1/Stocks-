"""
NGX (Nigerian Exchange) Daily Tracker
--------------------------------------
Sends a daily Telegram digest covering:
  1. Price snapshot for your watchlist (best-effort scrape — see note below)
  2. Recent news mentioning your watchlist companies
  3. New/upcoming listing announcements on NGX

This does NOT give buy/sell recommendations. It surfaces data and news so
you can make your own call.

NOTE ON PRICES: NGX doesn't offer a free live-data API, so this pulls from
a public NGX market data page. Site layouts change over time — if the price
section stops working, the news sections will keep working regardless.
"""

import os
import re
import json
import time
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"

WATCHLIST = {
    "GTCO":       ["GTCO", "Guaranty Trust", "GTBank"],
    "ZENITHBANK": ["Zenith Bank", "ZENITHBANK"],
    "ACCESSCORP": ["Access Holdings", "Access Bank", "ACCESSCORP"],
    "UBA":        ["United Bank for Africa", "UBA"],
    "MTNN":       ["MTN Nigeria", "MTNN"],
    "AIRTELAFRI": ["Airtel Africa", "AIRTELAFRI"],
    "DANGCEM":    ["Dangote Cement", "DANGCEM"],
    "BUACEMENT":  ["BUA Cement", "BUACEMENT"],
    "BUAFOODS":   ["BUA Foods", "BUAFOODS"],
    "NESTLE":     ["Nestle Nigeria", "NESTLE"],
    "SEPLAT":     ["Seplat Energy", "SEPLAT"],
    "NB":         ["Nigerian Breweries", " NB "],
}

NEWS_FEEDS = [
    "https://nairametrics.com/feed/",
    "https://businessday.ng/feed/",
]

LISTING_KEYWORDS = [
    "IPO", "initial public offering", "listing on NGX", "lists on NGX",
    "NGX admits", "to list on the exchange", "debut on NGX",
    "Nigerian Exchange listing", "new listing",
]

HISTORY_FILE = "ngx_history.json"


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return {"seen_links": []}


def save_history(history):
    history["seen_links"] = history["seen_links"][-500:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def fetch_price_snapshot():
    try:
        resp = requests.get("https://ngxpulse.ng/", timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PersonalTracker/1.0)"
        })
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        results = {}
        for ticker in WATCHLIST:
            match = re.search(
                rf"{ticker}\s+₦?N?([\d,]+\.\d+)\s+([+-]?[\d.]+)\s*%", text
            )
            if match:
                results[ticker] = {"price": match.group(1), "change": match.group(2)}
        return results
    except Exception as e:
        print(f"[warn] price snapshot failed: {e}")
        return {}


def fetch_company_news(history):
    hits = {ticker: [] for ticker in WATCHLIST}
    cutoff = datetime.now(timezone.utc) - timedelta(days=1, hours=6)

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[warn] feed failed {feed_url}: {e}")
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            if not link or link in history["seen_links"]:
                continue

            published = entry.get("published_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title} {summary}"

            for ticker, aliases in WATCHLIST.items():
                if any(alias.lower() in full_text.lower() for alias in aliases):
                    hits[ticker].append({"title": title, "link": link})
                    history["seen_links"].append(link)

    return hits


def fetch_listing_news(history):
    hits = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=1, hours=6)

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            if not link or link in history["seen_links"]:
                continue

            published = entry.get("published_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title} {summary}"

            if any(kw.lower() in full_text.lower() for kw in LISTING_KEYWORDS):
                hits.append({"title": title, "link": link})
                history["seen_links"].append(link)

    return hits


def get_ai_digest_note(prices, news_hits, listings):
    if not ANTHROPIC_API_KEY:
        return None

    lines = []
    for ticker, snap in prices.items():
        lines.append(f"{ticker}: N{snap['price']} ({snap['change']}%)")
    for ticker, articles in news_hits.items():
        for a in articles:
            lines.append(f"News - {ticker}: {a['title']}")
    for l in listings:
        lines.append(f"Listing news: {l['title']}")

    if not lines:
        return None

    prompt = (
        "Here is today's raw data on Nigerian stock market watchlist "
        "companies (prices and news headlines):\n\n" + "\n".join(lines) +
        "\n\nWrite a short (3-4 sentence) neutral summary of what stands "
        "out today. Do not recommend buying or selling anything — just "
        "summarize the notable movements and news in plain English."
    )
    try:
        resp = requests.post(
            CLAUDE_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 250,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return resp.json()["content"][0]["text"].strip()
    except Exception:
        return None


def send_telegram(text):
    for i in range(0, len(text), 3800):
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[i:i + 3800],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        time.sleep(1)


def main():
    print("Starting NGX daily scan...")
    history = load_history()

    prices = fetch_price_snapshot()
    news_hits = fetch_company_news(history)
    listings = fetch_listing_news(history)

    has_news = any(news_hits[t] for t in news_hits)
    if not prices and not has_news and not listings:
        send_telegram("🇳🇬 NGX Daily Update: No price data or new headlines today.")
        save_history(history)
        return

    lines = ["🇳🇬 <b>NGX Daily Update</b>\n"]

    if prices:
        lines.append("<b>Price snapshot:</b>")
        for ticker, snap in prices.items():
            arrow = "🟢" if not snap["change"].startswith("-") else "🔴"
            lines.append(f"{arrow} {ticker}: ₦{snap['price']} ({snap['change']}%)")
        lines.append("")
    else:
        lines.append("<i>Price snapshot unavailable today (source page may have changed).</i>\n")

    if has_news:
        lines.append("<b>Company news:</b>")
        for ticker, articles in news_hits.items():
            for a in articles:
                lines.append(f"• <b>{ticker}</b>: {a['title']}\n  {a['link']}")
        lines.append("")

    if listings:
        lines.append("<b>New listing / IPO news:</b>")
        for l in listings:
            lines.append(f"• {l['title']}\n  {l['link']}")
        lines.append("")

    ai_note = get_ai_digest_note(prices, news_hits, listings)
    if ai_note:
        lines.append(f"<b>Summary:</b>\n{ai_note}")

    send_telegram("\n".join(lines))
    save_history(history)
    print("Done.")


if __name__ == "__main__":
    main()
