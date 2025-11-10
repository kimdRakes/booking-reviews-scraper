import json
import logging
import os
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger("booking_reviews_scraper.exporter")

def _derive_paths(base_output_path: str, formats: List[str]) -> Dict[str, str]:
    root, ext = os.path.splitext(base_output_path)
    paths: Dict[str, str] = {}

    for fmt in formats:
        fmt_lower = fmt.lower()
        if fmt_lower == "json":
            paths["json"] = base_output_path if ext == ".json" or not ext else f"{root}.json"
        elif fmt_lower == "csv":
            paths["csv"] = base_output_path if ext == ".csv" or not ext else f"{root}.csv"
        elif fmt_lower in {"xlsx", "excel"}:
            paths["xlsx"] = base_output_path if ext in {".xlsx", ".xls"} or not ext else f"{root}.xlsx"

    return paths

def _flatten_review(hotel_stats: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}

    # Hotel-level columns (some aggregated)
    flat["hotelStats.totalReviews"] = hotel_stats.get("totalReviews")

    scores = hotel_stats.get("scores") or {}
    if isinstance(scores, dict):
        for key, entry in scores.items():
            if isinstance(entry, dict):
                flat[f"hotelStats.scores.{key}"] = entry.get("score")

    # Core review fields
    flat["score"] = review.get("score")
    flat["reviewDate"] = review.get("reviewDate")
    flat["title"] = review.get("title")
    flat["positiveContent"] = review.get("positiveContent")
    flat["negativeContent"] = review.get("negativeContent")
    flat["language"] = review.get("language")

    # Guest info
    guest = review.get("guest") or {}
    if isinstance(guest, dict):
        flat["guest.name"] = guest.get("name")
        flat["guest.country"] = guest.get("country")
        flat["guest.type"] = guest.get("type")

    # Booking info
    booking = review.get("booking") or {}
    if isinstance(booking, dict):
        flat["booking.roomType"] = booking.get("roomType")
        flat["booking.checkIn"] = booking.get("checkIn")
        flat["booking.checkOut"] = booking.get("checkOut")
        flat["booking.nights"] = booking.get("nights")
        flat["booking.customerType"] = booking.get("customerType")

    # Photos
    photos = review.get("photos") or []
    if isinstance(photos, list):
        flat["photos"] = ";".join(str(p) for p in photos)

    return flat

def export_dataset(
    hotel_stats: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    base_output_path: str,
    formats: List[str],
) -> None:
    if not reviews:
        logger.warning("No reviews to export. Still writing empty JSON for schema consistency.")

    paths = _derive_paths(base_output_path, formats)
    if not paths:
        raise ValueError(f"No valid output formats requested: {formats}")

    os.makedirs(os.path.dirname(list(paths.values())[0]) or ".", exist_ok=True)

    # JSON export
    if "json" in paths:
        json_path = paths["json"]
        logger.info("Writing JSON output to %s", json_path)
        payload = []
        for r in reviews:
            item = {
                "hotelStats": hotel_stats,
                **{
                    k: v
                    for k, v in r.items()
                    if k not in {"hotelStats"}
                },
            }
            payload.append(item)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    # CSV/XLSX export use flattened rows
    if "csv" in paths or "xlsx" in paths:
        logger.info("Flattening reviews for tabular export.")
        rows = [_flatten_review(hotel_stats, r) for r in reviews]
        df = pd.DataFrame(rows)

        if "csv" in paths:
            csv_path = paths["csv"]
            logger.info("Writing CSV output to %s", csv_path)
            df.to_csv(csv_path, index=False)

        if "xlsx" in paths:
            xlsx_path = paths["xlsx"]
            logger.info("Writing Excel output to %s", xlsx_path)
            df.to_excel(xlsx_path, index=False)