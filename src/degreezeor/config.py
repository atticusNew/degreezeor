"""Central configuration.

Settings are read from the environment (and an optional `.env` file). No secrets
are committed. Public official APIs accept the shared `DEMO_KEY` (api.data.gov)
which is rate-limited but sufficient for the MVP slice; set real keys via env for
higher throughput.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DZ_", env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql+psycopg://degreezeor:degreezeor@localhost:5432/degreezeor"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Managed Postgres (e.g. Render) provides 'postgres://' or 'postgresql://' without a
        # driver; SQLAlchemy 2.0 needs the explicit psycopg (v3) driver.
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return "postgresql+psycopg://" + v[len("postgres://"):]
            if v.startswith("postgresql://"):
                return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    # --- Official source API keys (DEMO_KEY works for Congress.gov + GovInfo) ---
    congress_api_key: str = "DEMO_KEY"
    govinfo_api_key: str = "DEMO_KEY"
    bls_api_key: str = ""  # optional; raises BLS rate limits
    courtlistener_token: str = ""  # optional; raises CourtListener rate limits

    # --- Immutable raw landing (content-addressed snapshots) ---
    data_dir: Path = REPO_ROOT / "data"

    @property
    def landing_dir(self) -> Path:
        return self.data_dir / "landing"

    # --- Scoring policy ---
    # Confidence gate: below this, the composite is suppressed and the EU is
    # rendered "Insufficient evidence" (never a low score).
    confidence_publish_threshold: Decimal = Decimal("0.60")
    # Deterministic seed pins every score run for bit-reproducibility.
    deterministic_seed: int = 20240607
    # Active methodology version (semver); historical scores remain re-derivable.
    methodology_version: str = "0.3.0"

    # --- Network ---
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 4


settings = Settings()
