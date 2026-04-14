import json

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"


def _extract_cost(data: dict) -> float:
    """Extract cost from OpenRouter response. Returns cost in USD."""
    # OpenRouter returns usage in the response
    usage = data.get("usage", {})
    # Some models return cost directly in the response headers or body
    # OpenRouter includes generation cost info
    total_cost = 0.0

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Try to get cost from the response directly (OpenRouter includes this)
    if "cost" in data:
        return float(data["cost"])

    # Gemini 2.5 Flash Lite pricing on OpenRouter: ~$0.075/1M input, ~$0.3/1M output
    total_cost = (prompt_tokens * 0.075 / 1_000_000) + (completion_tokens * 0.3 / 1_000_000)
    return total_cost


async def enrich_places(places: list[dict], api_key: str) -> dict:
    """Batch-enrich places with AI for address normalization and category refinement.

    Returns: {"results": [...], "cost_usd": float}
    """
    results = []
    total_cost = 0.0
    batch_size = 20

    for i in range(0, len(places), batch_size):
        batch = places[i : i + batch_size]
        batch_input = [
            {
                "id": p["id"],
                "name": p.get("name", ""),
                "address": p.get("address", ""),
                "city": p.get("city", ""),
                "category": p.get("category", ""),
            }
            for p in batch
        ]

        prompt = f"""Given these business entries from Google Maps, for each entry:
1. Normalize the address into a clean, structured format (street, city, state, postal code, country)
2. Refine the category into a more specific industry category useful for lead generation

Return a JSON array with objects containing: id, enriched_address, enriched_category
Only return the JSON array, no other text.

Input:
{json.dumps(batch_input, indent=2)}"""

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
                data = response.json()

                total_cost += _extract_cost(data)

                content = data["choices"][0]["message"]["content"]
                # Strip markdown code fences if present
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content.rsplit("```", 1)[0]
                content = content.strip()

                enriched = json.loads(content)
                results.extend(enriched)
        except Exception as e:
            # Enrichment failures are non-fatal — skip this batch
            print(f"Enrichment batch failed: {e}")
            continue

    return {"results": results, "cost_usd": round(total_cost, 6)}


async def suggest_keywords(keyword: str, results_count: int, requested: int | None, api_key: str) -> list[str]:
    """Use AI to suggest related search keywords when results fall short."""
    if not api_key:
        return []

    prompt = f"""The user searched for "{keyword}" on Google Maps and got {results_count} results{f' but wanted {requested}' if requested else ''}.

Suggest 5 more specific or related search keywords that would help find additional, different businesses in the same domain. Make them specific enough to find new results that the original keyword missed.

Return only a JSON array of strings, no other text.
Example: ["steel factories in South Delhi", "metal fabrication units in Noida"]"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
            return json.loads(content.strip())
    except Exception as e:
        print(f"Keyword suggestion failed: {e}")
        return []
