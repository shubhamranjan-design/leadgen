from pydantic import BaseModel


class ConfigRequest(BaseModel):
    apify_key: str
    openrouter_key: str = ""


class SearchRequest(BaseModel):
    keyword: str
    max_results: int | None = None  # None = no cap, get everything


class ConfigStatus(BaseModel):
    apify_configured: bool
    openrouter_configured: bool
