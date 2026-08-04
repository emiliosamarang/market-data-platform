import logging
import xml.etree.ElementTree as ET

import anthropic
import requests

from config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)

_FEAR_GREED_URL = "https://api.alternative.me/fng/"

_RSS_FEEDS = {
    "BTC": "https://cointelegraph.com/tags/bitcoin/rss",
    "ETH": "https://cointelegraph.com/tags/ethereum/rss",
    "SOL": "https://cointelegraph.com/tags/solana/rss",
    "XRP": "https://cointelegraph.com/tags/xrp/rss",
    "ADA": "https://cointelegraph.com/tags/cardano/rss",
}
_FALLBACK_FEED = "https://cointelegraph.com/rss"

_SYSTEM_PROMPT = (
    "You are a crypto market analyst. When given news headlines, "
    "reply with only a single decimal number from -1.0 (very bearish) "
    "to +1.0 (very bullish). No explanation."
)


def get_fear_greed() -> int:
    """Returns the Fear & Greed index 0-100. Returns 50 (neutral) on failure."""
    try:
        data = requests.get(_FEAR_GREED_URL, timeout=10).json()["data"][0]
        value = int(data["value"])
        log.info("Fear & Greed Index: %d (%s)", value, data["value_classification"])
        return value
    except Exception:
        log.warning("Fear & Greed fetch failed — defaulting to neutral (50)")
        return 50


def _fetch_headlines(coin: str, max_items: int = 8) -> list[str]:
    feed_url = _RSS_FEEDS.get(coin, _FALLBACK_FEED)
    for url in (feed_url, _FALLBACK_FEED):
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(resp.text)
            titles = []
            for item in root.iter("item"):
                el = item.find("title")
                if el is not None and el.text:
                    titles.append(el.text.strip())
                if len(titles) >= max_items:
                    break
            if titles:
                return titles
        except Exception:
            log.warning("Failed to fetch RSS from %s", url)
    return []


def get_news_sentiment(symbol: str) -> float:
    """
    Returns sentiment score -1.0 to +1.0 for the given symbol using Claude.
    Returns 0.0 (neutral) if no API key or headlines are unavailable.
    """
    if not ANTHROPIC_API_KEY:
        return 0.0

    coin = symbol.replace("USDT", "")
    headlines = _fetch_headlines(coin)

    if not headlines:
        log.info("%s | no headlines found — sentiment neutral", symbol)
        return 0.0

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_content = (
        f"Rate sentiment for {coin} based on these recent headlines:\n"
        + "\n".join(f"- {h}" for h in headlines)
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        score = max(-1.0, min(1.0, float(msg.content[0].text.strip())))
        log.info("%s | news sentiment: %.2f  (headlines: %d)", symbol, score, len(headlines))
        return score
    except Exception:
        log.warning("%s | Claude sentiment call failed — defaulting to neutral", symbol)
        return 0.0
