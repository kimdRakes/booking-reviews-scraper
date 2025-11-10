import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("booking_reviews_scraper.booking_parser")

def _safe_text(node) -> str:
    if not node:
        return ""
    return " ".join(node.get_text(strip=True).split())

class BookingReviewParser:
    """
    Parser for Booking.com hotel review pages.

    The HTML structure of Booking.com can change over time, so this parser
    is intentionally defensive and uses multiple strategies for each field.
    """

    def __init__(self, language_hint: Optional[str] = None) -> None:
        self.language_hint = language_hint

    def parse(self, html: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        soup = BeautifulSoup(html, "lxml")

        hotel_stats = self._extract_hotel_stats(soup)
        reviews = self._extract_reviews(soup)

        # If totalReviews unknown, derive from count
        if hotel_stats.get("totalReviews") is None:
            hotel_stats["totalReviews"] = len(reviews)

        return hotel_stats, reviews

    # --------------------------------------------------------------------- #
    # Hotel-level statistics
    # --------------------------------------------------------------------- #
    def _extract_hotel_stats(self, soup: BeautifulSoup) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "totalReviews": None,
            "scores": {},
        }

        # Strategy 1: Look for JSON-LD containing aggregateRating
        try:
            for script in soup.find_all("script", type="application/ld+json"):
                data = json.loads(script.string or "{}")
                if isinstance(data, list):
                    for item in data:
                        self._populate_stats_from_jsonld(item, stats)
                else:
                    self._populate_stats_from_jsonld(data, stats)
        except Exception as exc:
            logger.debug("Failed to parse JSON-LD: %s", exc)

        # Strategy 2: Booking-specific DOM elements
        # Total reviews (e.g., "Based on 263 reviews")
        if stats.get("totalReviews") is None:
            total_node = soup.find(attrs={"data-testid": "review-score-subtitle"})
            if total_node:
                text = _safe_text(total_node)
                stats["totalReviews"] = self._extract_int_from_text(text)

        # Category scores: often displayed as cards with labels
        for score_block in soup.select('[data-testid="review-subscore"]'):
            label_node = score_block.find(attrs={"data-testid": "review-subscore-title"})
            score_node = score_block.find(attrs={"data-testid": "review-subscore-value"})
            label = _safe_text(label_node)
            score_text = _safe_text(score_node)
            if not label or not score_text:
                continue
            try:
                score_val = float(score_text.replace(",", "."))
            except ValueError:
                continue

            key = self._normalize_score_label(label)
            stats["scores"][key] = {
                "score": score_val,
                "translation": label,
                "bounds": {
                    "lower": None,
                    "higher": None,
                },
            }

        return stats

    def _populate_stats_from_jsonld(self, data: Dict[str, Any], stats: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return

        data_type = data.get("@type") or data.get("@graph", [{}])[0].get("@type")
        if isinstance(data_type, list):
            data_type = data_type[0]

        if str(data_type).lower() not in {"hotel", "lodgingbusiness"}:
            return

        aggregate = data.get("aggregateRating") or {}
        if isinstance(aggregate, dict):
            review_count = aggregate.get("reviewCount") or aggregate.get("ratingCount")
            if review_count and stats.get("totalReviews") is None:
                try:
                    stats["totalReviews"] = int(review_count)
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def _extract_int_from_text(text: str) -> Optional[int]:
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    @staticmethod
    def _normalize_score_label(label: str) -> str:
        return (
            label.strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )

    # --------------------------------------------------------------------- #
    # Review-level data
    # --------------------------------------------------------------------- #
    def _extract_reviews(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        reviews: List[Dict[str, Any]] = []

        # Booking often wraps each review card
        review_cards = soup.select('[data-testid="review-card"]')
        if not review_cards:
            # Fallback: look for common class names
            review_cards = soup.select('.review_list_new_item_block, [itemprop="review"]')

        for card in review_cards:
            try:
                review = self._parse_single_review(card)
                if review:
                    reviews.append(review)
            except Exception as exc:
                logger.debug("Failed to parse review card: %s", exc, exc_info=True)

        return reviews

    def _parse_single_review(self, card) -> Optional[Dict[str, Any]]:
        # Overall score
        score = self._parse_score(card)

        # Title
        title = self._parse_title(card)

        # Review date
        review_date = self._parse_review_date(card)

        # Positive and negative content
        positive, negative = self._parse_review_text(card)

        # Language
        language = self._parse_language(card)

        # Guest information
        guest = self._parse_guest_info(card)

        # Booking information
        booking = self._parse_booking_info(card)

        # Photos
        photos = self._parse_photos(card)

        if not (positive or negative or title):
            # Ignore empty cards
            return None

        return {
            "score": score,
            "reviewDate": review_date,
            "title": title,
            "positiveContent": positive,
            "negativeContent": negative,
            "language": language or self.language_hint,
            "guest": guest,
            "booking": booking,
            "photos": photos,
        }

    def _parse_score(self, card) -> Optional[float]:
        score_node = card.find(attrs={"data-testid": "review-score"})
        if not score_node:
            # Alternative structures
            score_node = card.find(class_="bui-review-score__badge") or card.find(itemprop="ratingValue")

        text = _safe_text(score_node)
        if not text:
            return None
        text = text.replace(",", ".")
        try:
            return float(text)
        except ValueError:
            return None

    def _parse_title(self, card) -> str:
        title_node = card.find(attrs={"data-testid": "review-title"})
        if not title_node:
            title_node = card.find("h3") or card.find(class_="review-title")

        return _safe_text(title_node)

    def _parse_review_date(self, card) -> Optional[int]:
        # Some Booking pages include machine-readable timestamps
        time_node = card.find("time")
        if time_node and time_node.has_attr("datetime"):
            # Try to convert ISO date to UNIX timestamp
            import datetime as _dt

            dt_val = time_node["datetime"]
            try:
                dt = _dt.datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except Exception:
                pass

        # Fallback: Booking often has spans with "Reviewed: 26 August 2022"
        date_node = card.find(attrs={"data-testid": "review-date"})
        text = _safe_text(date_node)
        if not text:
            return None

        # We leave date as None when we can't parse reliably, because
        # mapping arbitrary localized dates to timestamps is error-prone
        return None

    def _parse_review_text(self, card) -> Tuple[str, str]:
        positive = ""
        negative = ""

        positive_node = card.find(attrs={"data-testid": "review-positive-text"})
        if positive_node:
            positive = _safe_text(positive_node)

        negative_node = card.find(attrs={"data-testid": "review-negative-text"})
        if negative_node:
            negative = _safe_text(negative_node)

        # Fallback: generic paragraphs
        if not (positive or negative):
            paragraphs = [p for p in card.find_all("p") if _safe_text(p)]
            if paragraphs:
                if len(paragraphs) == 1:
                    positive = _safe_text(paragraphs[0])
                else:
                    positive = _safe_text(paragraphs[0])
                    negative = " ".join(_safe_text(p) for p in paragraphs[1:])

        return positive, negative

    def _parse_language(self, card) -> Optional[str]:
        # Booking uses lang attribute or data-review-language
        if card.has_attr("lang"):
            return card["lang"]

        lang_attr = card.get("data-review-language")
        if lang_attr:
            return lang_attr

        # Sometimes language can be inferred from HTML tag
        html = card.find_parent("html")
        if html is not None and html.has_attr("lang"):
            return html["lang"]

        return None

    def _parse_guest_info(self, card) -> Dict[str, Any]:
        guest: Dict[str, Any] = {
            "name": None,
            "country": None,
            "type": None,
        }

        name_node = card.find(attrs={"data-testid": "reviewer-name"})
        if not name_node:
            name_node = card.find(class_="bui-avatar-block__title")
        guest["name"] = _safe_text(name_node) or None

        country_node = card.find(attrs={"data-testid": "reviewer-country"})
        if not country_node:
            country_node = card.find(class_="bui-avatar-block__subtitle")
        guest["country"] = _safe_text(country_node) or None

        # Guest type: "Family with young children", "Couple", etc.
        type_node = card.find(attrs={"data-testid": "reviewer-type"})
        if type_node:
            guest["type"] = _safe_text(type_node) or None

        return guest

    def _parse_booking_info(self, card) -> Dict[str, Any]:
        booking: Dict[str, Any] = {
            "roomType": None,
            "checkIn": None,
            "checkOut": None,
            "nights": None,
            "customerType": None,
        }

        # Room type might be in a span with an icon/label
        room_node = card.find(attrs={"data-testid": "review-room-type"})
        if room_node:
            booking["roomType"] = _safe_text(room_node) or None

        # Booking summary block might include check-in/out text
        stay_node = card.find(attrs={"data-testid": "review-stay-date"})
        if stay_node:
            text = _safe_text(stay_node)
            # Text is usually descriptive (e.g., "Stayed 2 nights in August 2022")
            # We won't attempt full parsing here, but we can derive nights.
            nights = self._extract_int_from_text(text)
            if nights is not None:
                booking["nights"] = nights

        customer_type_node = card.find(attrs={"data-testid": "review-customer-type"})
        if customer_type_node:
            booking["customerType"] = _safe_text(customer_type_node) or None

        return booking

    def _parse_photos(self, card) -> List[str]:
        photos: List[str] = []
        for img in card.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            try:
                parsed = urlparse(src)
                if not parsed.scheme or not parsed.netloc:
                    continue
                photos.append(src)
            except Exception:
                continue
        # Deduplicate while preserving order
        seen = set()
        unique_photos = []
        for p in photos:
            if p in seen:
                continue
            seen.add(p)
            unique_photos.append(p)
        return unique_photos