"""Lightweight web access for the agent: fetch a URL's readable text.

For pages that need real JS rendering, Playwright is included in
requirements.txt as an optional upgrade path -- swap fetch_url's
implementation for a Playwright page.goto() call if you run into sites that
don't work with a plain HTTP GET.
"""
import requests
from bs4 import BeautifulSoup

from src.utils.logger import get_logger

log = get_logger("browser_tool")

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ZELIAAgent/1.0"}


def fetch_url(url: str, max_chars: int = 6000) -> dict:
    log.info("Fetching %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return {"ok": True, "text": text[:max_chars]}
