import json
import os
from datetime import datetime

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "places.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                max_results INTEGER,
                total_results INTEGER DEFAULT 0,
                new_results INTEGER DEFAULT 0,
                apify_run_id TEXT,
                apify_cost_usd REAL DEFAULT 0,
                enrichment_cost_usd REAL DEFAULT 0,
                error_message TEXT,
                suggested_keywords TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id TEXT UNIQUE,
                name TEXT NOT NULL,
                address TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                postal_code TEXT,
                phone TEXT,
                website TEXT,
                email TEXT,
                category TEXT,
                categories TEXT,
                rating REAL,
                reviews_count INTEGER,
                latitude REAL,
                longitude REAL,
                google_maps_url TEXT,
                image_url TEXT,
                opening_hours TEXT,
                price_level TEXT,
                description TEXT,
                enriched_category TEXT,
                enriched_address TEXT,
                raw_data TEXT,
                search_keyword TEXT,
                search_id INTEGER REFERENCES searches(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_places_place_id ON places(place_id);
            CREATE INDEX IF NOT EXISTS idx_places_name_city ON places(name, city);
            CREATE INDEX IF NOT EXISTS idx_places_search_keyword ON places(search_keyword);
            CREATE INDEX IF NOT EXISTS idx_places_search_id ON places(search_id);
        """)

        # Migrations for existing DBs
        try:
            await db.execute("ALTER TABLE searches ADD COLUMN apify_cost_usd REAL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE searches ADD COLUMN enrichment_cost_usd REAL DEFAULT 0")
        except Exception:
            pass

        await db.commit()
    finally:
        await db.close()


async def create_search(keyword: str, max_results: int | None) -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO searches (keyword, max_results, status) VALUES (?, ?, 'pending')",
            (keyword, max_results),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def update_search(search_id: int, **kwargs):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(search_id)
        await db.execute(f"UPDATE searches SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def get_searches():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM searches ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_search(search_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def find_previous_search(keyword: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM searches WHERE keyword = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            (keyword,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def insert_place(place: dict, search_id: int, keyword: str) -> bool:
    """Insert a place with deduplication. Returns True if new, False if duplicate."""
    db = await get_db()
    try:
        place_id = place.get("place_id")

        # Primary dedup: by place_id
        if place_id:
            cursor = await db.execute(
                "SELECT id FROM places WHERE place_id = ?", (place_id,)
            )
            existing = await cursor.fetchone()
            if existing:
                await _merge_update(db, existing["id"], place)
                await db.commit()
                return False

        # Fallback dedup: by normalized name + city
        name = (place.get("name") or "").strip().lower()
        city = (place.get("city") or "").strip().lower()
        if name:
            cursor = await db.execute(
                "SELECT id FROM places WHERE LOWER(TRIM(name)) = ? AND LOWER(TRIM(COALESCE(city, ''))) = ?",
                (name, city),
            )
            existing = await cursor.fetchone()
            if existing:
                await _merge_update(db, existing["id"], place)
                await db.commit()
                return False

        # Insert new
        cols = [
            "place_id", "name", "address", "city", "state", "country", "postal_code",
            "phone", "website", "email", "category", "categories", "rating",
            "reviews_count", "latitude", "longitude", "google_maps_url", "image_url",
            "opening_hours", "price_level", "description", "raw_data",
            "search_keyword", "search_id",
        ]
        place["search_keyword"] = keyword
        place["search_id"] = search_id

        # Serialize JSON fields
        for json_field in ("categories", "opening_hours", "raw_data"):
            if json_field in place and not isinstance(place.get(json_field), str):
                place[json_field] = json.dumps(place[json_field])

        values = [place.get(c) for c in cols]
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)

        await db.execute(
            f"INSERT INTO places ({col_names}) VALUES ({placeholders})", values
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def _merge_update(db: aiosqlite.Connection, existing_id: int, place: dict):
    """Update existing record with any new non-null fields."""
    updatable = [
        "phone", "website", "email", "rating", "reviews_count",
        "image_url", "opening_hours", "price_level", "description",
    ]
    sets = []
    vals = []
    for field in updatable:
        val = place.get(field)
        if val is not None:
            sets.append(f"{field} = COALESCE(?, {field})")
            vals.append(val if isinstance(val, str) else (json.dumps(val) if isinstance(val, (dict, list)) else val))

    if sets:
        vals.append(datetime.utcnow().isoformat())
        vals.append(existing_id)
        await db.execute(
            f"UPDATE places SET {', '.join(sets)}, updated_at = ? WHERE id = ?", vals
        )


async def get_places(
    search_id: int | None = None,
    keyword: str | None = None,
    page: int = 1,
    per_page: int = 50,
    sort_by: str = "created_at",
    sort_dir: str = "DESC",
):
    db = await get_db()
    try:
        conditions = []
        params = []

        if search_id:
            conditions.append("search_id = ?")
            params.append(search_id)
        if keyword:
            conditions.append("search_keyword LIKE ?")
            params.append(f"%{keyword}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        allowed_sorts = {
            "name", "city", "rating", "reviews_count", "category", "created_at",
        }
        if sort_by not in allowed_sorts:
            sort_by = "created_at"
        if sort_dir.upper() not in ("ASC", "DESC"):
            sort_dir = "DESC"

        # Count
        count_cursor = await db.execute(
            f"SELECT COUNT(*) as cnt FROM places {where}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row["cnt"]

        offset = (page - 1) * per_page
        cursor = await db.execute(
            f"SELECT * FROM places {where} ORDER BY {sort_by} {sort_dir} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        )
        rows = await cursor.fetchall()

        return {
            "places": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        }
    finally:
        await db.close()


async def get_places_for_export(search_id: int | None = None, keyword: str | None = None):
    db = await get_db()
    try:
        conditions = []
        params = []
        if search_id:
            conditions.append("search_id = ?")
            params.append(search_id)
        if keyword:
            conditions.append("search_keyword LIKE ?")
            params.append(f"%{keyword}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await db.execute(f"SELECT * FROM places {where} ORDER BY name", params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_places_by_search(search_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM places WHERE search_id = ?", (search_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_place_enrichment(place_id: int, enriched_category: str, enriched_address: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE places SET enriched_category = ?, enriched_address = ?, updated_at = ? WHERE id = ?",
            (enriched_category, enriched_address, datetime.utcnow().isoformat(), place_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_total_costs():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(apify_cost_usd), 0) as total_apify, "
            "COALESCE(SUM(enrichment_cost_usd), 0) as total_enrichment, "
            "COUNT(*) as total_searches "
            "FROM searches WHERE status = 'completed'"
        )
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()


async def delete_search(search_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM places WHERE search_id = ?", (search_id,))
        await db.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        await db.commit()
    finally:
        await db.close()


async def delete_all_places():
    db = await get_db()
    try:
        await db.execute("DELETE FROM places")
        await db.execute("DELETE FROM searches")
        await db.commit()
    finally:
        await db.close()
