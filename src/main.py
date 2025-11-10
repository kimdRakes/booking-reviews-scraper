import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import requests

# Ensure local packages are importable when running as "python src/main.py"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.append(CURRENT_DIR)

from extractors.booking_parser import BookingReviewParser  # type: ignore
from extractors.pagination_handler import PaginationHandler  # type: ignore
from outputs.dataset_exporter import export_dataset  # type: ignore

logger = logging.getLogger("booking_reviews_scraper")

def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

def load_settings(settings_path: str) -> Dict[str, Any]:
    if not os.path.exists(settings_path):
        logger.warning("Settings file %s not found. Using in-memory defaults.", settings_path)
        return {
            "user_agent": "BookingReviewsScraper/1.0 (+https://bitbash.dev)",
            "timeout": 15,
            "max_retries": 3,
            "backoff_factor": 0.5,
            "default_max_items": 250,
            "output": {
                "path": "data/output.sample.json",
                "formats": ["json"],
            },
            "proxy": {
                "http": None,
                "https": None,
            },
        }

    with open(settings_path, "r", encoding="utf-8") as f:
        return json.load(f)

def create_http_session(settings: Dict[str, Any]) -> requests.Session:
    session = requests.Session()

    headers = {
        "User-Agent": settings.get(
            "user_agent",
            "BookingReviewsScraper/1.0 (+https://bitbash.dev)",
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    session.headers.update(headers)

    proxies = settings.get("proxy") or {}
    session.proxies.update(
        {
            k: v
            for k, v in proxies.items()
            if v  # only set non-empty proxies
        }
    )

    return session

def parse_input_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}

    if not os.path.exists(path):
        raise FileNotFoundError(f"Input config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("Input config must be a JSON object at the root level.")

    return data

def merge_config(
    settings: Dict[str, Any],
    input_cfg: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}

    # Hotel URL priority: CLI > input config
    hotel_url = args.hotel_url or input_cfg.get("hotelUrl") or input_cfg.get("hotel_url")
    if not hotel_url:
        raise ValueError("A Booking.com hotel URL must be provided via --hotel-url or input JSON (hotelUrl).")
    cfg["hotel_url"] = hotel_url

    max_items = (
        args.max_items
        if args.max_items is not None
        else input_cfg.get("maxItems")
        if input_cfg.get("maxItems") is not None
        else settings.get("default_max_items", 250)
    )
    cfg["max_items"] = int(max_items)

    language = args.language or input_cfg.get("language")
    cfg["language"] = language

    output_cfg = settings.get("output", {})
    output_path = args.output or input_cfg.get("outputPath") or output_cfg.get("path") or "data/output.sample.json"
    cfg["output_path"] = output_path

    formats = None
    if args.formats:
        formats = [fmt.strip().lower() for fmt in args.formats.split(",") if fmt.strip()]
    elif isinstance(input_cfg.get("formats"), list):
        formats = [str(fmt).lower() for fmt in input_cfg.get("formats")]
    else:
        formats = output_cfg.get("formats", ["json"])

    if not formats:
        formats = ["json"]

    cfg["formats"] = formats

    cfg["settings"] = settings
    return cfg

def scrape_reviews(
    session: requests.Session,
    hotel_url: str,
    max_items: int,
    language: Optional[str],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    parser = BookingReviewParser(language_hint=language)
    paginator = PaginationHandler(
        session=session,
        timeout=settings.get("timeout", 15),
        max_retries=settings.get("max_retries", 3),
        backoff_factor=settings.get("backoff_factor", 0.5),
    )

    all_reviews: List[Dict[str, Any]] = []
    hotel_stats: Optional[Dict[str, Any]] = None

    logger.info("Starting scrape for %s", hotel_url)
    page_count = 0

    try:
        for page_html in paginator.iter_pages(hotel_url):
            page_count += 1
            logger.info("Parsing page %d", page_count)

            parsed_stats, page_reviews = parser.parse(page_html)

            if parsed_stats and not hotel_stats:
                hotel_stats = parsed_stats

            for r in page_reviews:
                all_reviews.append(r)
                if len(all_reviews) >= max_items:
                    logger.info("Reached max_items limit (%d). Stopping pagination.", max_items)
                    raise StopIteration()

    except StopIteration:
        logger.debug("Pagination stopped after reaching max_items.")
    except Exception as exc:
        logger.error("Unexpected error during scraping: %s", exc, exc_info=True)

    hotel_stats = hotel_stats or {
        "totalReviews": len(all_reviews),
        "scores": {},
    }

    logger.info("Scraping complete. Collected %d reviews.", len(all_reviews))

    return {
        "hotelStats": hotel_stats,
        "reviews": all_reviews,
    }

def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Booking.com hotel reviews scraper",
    )
    parser.add_argument(
        "--input",
        help="Path to JSON input config (contains hotelUrl, maxItems, formats, etc.)",
    )
    parser.add_argument(
        "--hotel-url",
        help="Booking.com hotel URL (overrides hotelUrl in input JSON).",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        help="Maximum number of reviews to fetch.",
    )
    parser.add_argument(
        "--language",
        help="Preferred review language code (e.g., en, nl, fr).",
    )
    parser.add_argument(
        "--output",
        help="Output base file path (extension added based on formats).",
    )
    parser.add_argument(
        "--formats",
        help="Comma-separated list of output formats: json,csv,xlsx",
    )
    parser.add_argument(
        "--settings",
        default=os.path.join(CURRENT_DIR, "config", "settings.json"),
        help="Path to settings.json file.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (use -vv for debug).",
    )

    args = parser.parse_args()
    configure_logging(args.verbose)

    try:
        settings = load_settings(args.settings)
        input_cfg = parse_input_config(args.input)
        cfg = merge_config(settings, input_cfg, args)
    except Exception as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    session = create_http_session(cfg["settings"])

    scrape_result = scrape_reviews(
        session=session,
        hotel_url=cfg["hotel_url"],
        max_items=cfg["max_items"],
        language=cfg["language"],
        settings=cfg["settings"],
    )

    output_path = cfg["output_path"]
    formats = cfg["formats"]

    ensure_parent_dir(output_path)

    try:
        export_dataset(
            hotel_stats=scrape_result["hotelStats"],
            reviews=scrape_result["reviews"],
            base_output_path=output_path,
            formats=formats,
        )
    except Exception as exc:
        logger.error("Failed to export dataset: %s", exc)
        sys.exit(1)

    logger.info("All done. Output written to base path: %s", output_path)

if __name__ == "__main__":
    main()