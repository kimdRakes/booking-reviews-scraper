import logging
import time
from typing import Generator, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("booking_reviews_scraper.pagination")

class PaginationHandler:
    """
    Handles pagination over Booking.com hotel review pages.

    This class relies on either:
    - <a rel="next" ...> links, or
    - pagination links with "Next" in their text or aria-label.
    """

    def __init__(
        self,
        session: requests.Session,
        timeout: int = 15,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        self.session = session
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def iter_pages(self, start_url: str) -> Generator[str, None, None]:
        """
        Yield HTML for each page starting from start_url until there is
        no "next page" link or an error occurs.
        """
        current_url = start_url
        visited_urls = set()

        while current_url and current_url not in visited_urls:
            visited_urls.add(current_url)
            logger.debug("Fetching page: %s", current_url)
            html = self._fetch(current_url)
            if not html:
                logger.warning("Empty response for %s, stopping pagination.", current_url)
                break

            yield html

            try:
                next_url = self._find_next_page_url(html, current_url)
            except Exception as exc:
                logger.debug("Error while resolving next page: %s", exc, exc_info=True)
                next_url = None

            if not next_url:
                logger.info("No further pages detected after %s.", current_url)
                break

            current_url = next_url

    def _fetch(self, url: str) -> Optional[str]:
        last_exception: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code >= 400:
                    logger.warning("HTTP %s while requesting %s", resp.status_code, url)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                last_exception = exc
                sleep_time = self.backoff_factor * (2 ** (attempt - 1))
                logger.warning(
                    "Request attempt %d/%d failed for %s: %s. Retrying in %.1fs",
                    attempt,
                    self.max_retries,
                    url,
                    exc,
                    sleep_time,
                )
                time.sleep(sleep_time)

        logger.error("All retries failed for %s: %s", url, last_exception)
        return None

    def _find_next_page_url(self, html: str, current_url: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")

        # Strategy 1: <a rel="next" ...>
        next_link = soup.find("a", rel="next")
        if next_link and next_link.get("href"):
            href = next_link["href"]
            next_url = urljoin(current_url, href)
            logger.debug("Next page from rel=next: %s", next_url)
            return next_url

        # Strategy 2: pagination controls with aria-label
        for a in soup.find_all("a"):
            aria = (a.get("aria-label") or "").strip().lower()
            text = (a.get_text(strip=True) or "").lower()
            if "next" in aria or "next" in text:
                href = a.get("href")
                if href:
                    next_url = urljoin(current_url, href)
                    logger.debug("Next page from aria/text match: %s", next_url)
                    return next_url

        # Strategy 3: data-testid based
        next_btn = soup.find(attrs={"data-testid": "review-paginator-next"})
        if next_btn and next_btn.name == "a" and next_btn.get("href"):
            href = next_btn["href"]
            next_url = urljoin(current_url, href)
            logger.debug("Next page from data-testid: %s", next_url)
            return next_url

        return None