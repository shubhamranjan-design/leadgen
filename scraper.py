from apify_client import ApifyClient


ACTOR_ID = "compass/crawler-google-places"


def map_place(raw: dict) -> dict:
    """Map Apify actor output fields to our internal schema."""
    location = raw.get("location") or {}
    return {
        "place_id": raw.get("placeId"),
        "name": raw.get("title") or raw.get("name", ""),
        "address": raw.get("address"),
        "city": raw.get("city"),
        "state": raw.get("state"),
        "country": raw.get("countryCode"),
        "postal_code": raw.get("postalCode"),
        "phone": raw.get("phone"),
        "website": raw.get("website"),
        "email": raw.get("email"),
        "category": raw.get("categoryName"),
        "categories": raw.get("categories"),
        "rating": raw.get("totalScore"),
        "reviews_count": raw.get("reviewsCount"),
        "latitude": location.get("lat"),
        "longitude": location.get("lng"),
        "google_maps_url": raw.get("url"),
        "image_url": raw.get("imageUrl"),
        "opening_hours": raw.get("openingHours"),
        "price_level": raw.get("price"),
        "description": raw.get("description"),
        "raw_data": raw,
    }


def run_scrape(keyword: str, max_results: int | None, api_key: str) -> dict:
    """Run the Apify Google Maps scraper and return mapped results + cost.

    This is a blocking call — meant to be run in a background task.
    Returns: {"places": [...], "cost_usd": float, "run_id": str}
    """
    client = ApifyClient(api_key)

    run_input = {
        "searchStringsArray": [keyword],
        "language": "en",
        "deeperCityScrape": True,
        "skipClosedPlaces": False,
        "scrapeContacts": True,
        "scrapeImages": False,
        "scrapeReviews": False,
        "scrapeDirectories": False,
        "maxAutomaticZoomOut": 5,
        "countryCode": "in",
        "country": "India",
        "geolocation": {
            "country": "IN",
        },
    }

    if max_results is not None:
        run_input["maxCrawledPlacesPerSearch"] = max_results

    run = client.actor(ACTOR_ID).call(run_input=run_input)

    items = list(
        client.dataset(run["defaultDatasetId"]).iterate_items()
    )

    # Extract cost from run stats
    run_id = run.get("id", "")
    cost_usd = 0.0
    try:
        run_details = client.run(run_id).get()
        usage = run_details.get("usageTotalUsd") or run_details.get("usageUsd")
        if isinstance(usage, (int, float)):
            cost_usd = float(usage)
        elif isinstance(usage, dict):
            cost_usd = sum(v for v in usage.values() if isinstance(v, (int, float)))
        stats = run_details.get("stats", {})
        if cost_usd == 0 and stats.get("computeUnits"):
            # Fallback: estimate from compute units (~$0.25 per CU for paid plans)
            cost_usd = float(stats["computeUnits"]) * 0.25
    except Exception:
        pass

    return {
        "places": [map_place(item) for item in items],
        "cost_usd": round(cost_usd, 6),
        "run_id": run_id,
    }
