import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    apify_key: str = ""
    openrouter_key: str = ""

    @property
    def apify_configured(self) -> bool:
        return bool(self.apify_key)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_key)


config = AppConfig(
    apify_key=os.environ.get("APIFY_API_KEY", ""),
    openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
)
