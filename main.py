import asyncio
import csv
import io
import json
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse

from config import config
from database import (
    create_search,
    delete_all_places,
    delete_search,
    find_previous_search,
    get_places,
    get_places_by_search,
    get_places_for_export,
    get_search,
    get_searches,
    get_total_costs,
    init_db,
    insert_place,
    update_place_enrichment,
    update_search,
)
from enrichment import enrich_places, suggest_keywords
from models import ConfigRequest, ConfigStatus, SearchRequest
from scraper import run_scrape

app = FastAPI(title="Google Places Scraper")


@app.on_event("startup")
async def startup():
    await init_db()


# --- Static files ---

@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")


# --- Config ---

@app.post("/api/config")
async def save_config(req: ConfigRequest):
    config.apify_key = req.apify_key
    config.openrouter_key = req.openrouter_key
    return {"status": "ok"}


@app.get("/api/config/status")
async def config_status():
    return ConfigStatus(
        apify_configured=config.apify_configured,
        openrouter_configured=config.openrouter_configured,
    )


# --- Search ---

@app.post("/api/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks):
    if not config.apify_configured:
        return {"error": "Apify API key not configured"}

    # Check for previous search with same keyword
    previous = await find_previous_search(req.keyword)
    warning = None
    if previous:
        warning = {
            "message": f"This keyword was already searched on {previous['created_at']}",
            "total_results": previous["total_results"],
            "new_results": previous["new_results"],
        }

    search_id = await create_search(req.keyword, req.max_results)
    background_tasks.add_task(_run_search_task, search_id, req.keyword, req.max_results)

    return {"search_id": search_id, "status": "pending", "warning": warning}


async def _run_search_task(search_id: int, keyword: str, max_results: int | None):
    try:
        await update_search(search_id, status="running")

        # Run the blocking Apify call in a thread
        result = await asyncio.to_thread(
            run_scrape, keyword, max_results, config.apify_key
        )

        places = result["places"]
        apify_cost = result["cost_usd"]
        run_id = result["run_id"]

        total = len(places)
        new_count = 0

        for place in places:
            is_new = await insert_place(place, search_id, keyword)
            if is_new:
                new_count += 1

        # Check for shortfall and suggest keywords
        suggested = None
        if max_results and total < max_results and config.openrouter_configured:
            suggestions = await suggest_keywords(
                keyword, total, max_results, config.openrouter_key
            )
            if suggestions:
                suggested = json.dumps(suggestions)

        await update_search(
            search_id,
            status="completed",
            total_results=total,
            new_results=new_count,
            apify_run_id=run_id,
            apify_cost_usd=apify_cost,
            suggested_keywords=suggested,
            completed_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        await update_search(
            search_id,
            status="failed",
            error_message=str(e),
            completed_at=datetime.utcnow().isoformat(),
        )


@app.get("/api/searches")
async def list_searches():
    return await get_searches()


@app.get("/api/searches/{search_id}")
async def get_search_detail(search_id: int):
    search = await get_search(search_id)
    if not search:
        return {"error": "Search not found"}
    return search


# --- Costs ---

@app.get("/api/costs")
async def get_costs():
    return await get_total_costs()


# --- Places ---

@app.get("/api/places")
async def list_places(
    search_id: int | None = Query(None),
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("DESC"),
):
    return await get_places(search_id, keyword, page, per_page, sort_by, sort_dir)


@app.get("/api/places/export")
async def export_places(
    search_id: int | None = Query(None),
    keyword: str | None = Query(None),
):
    places = await get_places_for_export(search_id, keyword)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Name", "Address", "City", "State", "Country", "Postal Code",
        "Phone", "Website", "Email", "Category", "Rating", "Reviews",
        "Google Maps URL", "Enriched Category", "Enriched Address",
    ])
    for p in places:
        writer.writerow([
            p.get("name"), p.get("address"), p.get("city"), p.get("state"),
            p.get("country"), p.get("postal_code"), p.get("phone"),
            p.get("website"), p.get("email"), p.get("category"),
            p.get("rating"), p.get("reviews_count"), p.get("google_maps_url"),
            p.get("enriched_category"), p.get("enriched_address"),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=places_export.csv"},
    )


# --- Enrichment ---

@app.post("/api/enrich/{search_id}")
async def enrich_search(search_id: int):
    if not config.openrouter_configured:
        return {"error": "OpenRouter API key not configured"}

    places = await get_places_by_search(search_id)
    if not places:
        return {"error": "No places found for this search"}

    enrichment_result = await enrich_places(places, config.openrouter_key)
    enriched_items = enrichment_result["results"]
    enrichment_cost = enrichment_result["cost_usd"]

    updated = 0
    for item in enriched_items:
        await update_place_enrichment(
            item["id"],
            item.get("enriched_category", ""),
            item.get("enriched_address", ""),
        )
        updated += 1

    # Store enrichment cost on the search record
    search = await get_search(search_id)
    existing_enrichment_cost = search.get("enrichment_cost_usd") or 0
    await update_search(
        search_id,
        enrichment_cost_usd=existing_enrichment_cost + enrichment_cost,
    )

    return {
        "status": "ok",
        "enriched_count": updated,
        "enrichment_cost_usd": enrichment_cost,
    }


# --- Cleanup ---

@app.delete("/api/searches/{search_id}")
async def remove_search(search_id: int):
    await delete_search(search_id)
    return {"status": "ok"}


@app.delete("/api/places")
async def clear_all():
    await delete_all_places()
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
