"""Typed application settings. Every knob is an env var prefixed HGC_."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="HGC_", extra="ignore", frozen=True
    )

    # --- runtime -------------------------------------------------------------
    env: Literal["local", "test", "staging", "prod"] = "local"
    service_name: str = "hydrogeochem"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- storage -------------------------------------------------------------
    database_url: str = "postgresql+psycopg://hgc:hgc@localhost:5432/hgc"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # --- PHREEQC -------------------------------------------------------------
    phreeqc_database_dir: Path = Path("/opt/phreeqc/database")
    phreeqc_default_database: str = "phreeqc.dat"
    phreeqc_allowed_databases: Annotated[tuple[str, ...], NoDecode] = (
        "phreeqc.dat",
        "wateq4f.dat",
        "llnl.dat",
        "pitzer.dat",
        "minteq.v4.dat",
    )
    phreeqc_workers: int = 4
    phreeqc_timeout_s: float = 20.0
    phreeqc_max_tasks_per_child: int = 200
    phreeqc_max_input_bytes: int = 64_000
    phreeqc_child_memory_mb: int = 1024

    # Runs estimated to finish under this deadline are executed inline; the rest
    # are queued. Keeps the API responsive without forcing polling on trivial work.
    sync_run_deadline_s: float = 5.0

    # --- upstreams (USGS Water Data for the Nation) --------------------------
    # OGC monitoring-locations API for site search; Samples Data API for chemistry.
    # These replace the retired NWISWeb/WaterServices site service and the Water Quality Portal.
    wdfn_ogc_base_url: str = "https://api.waterdata.usgs.gov/ogcapi/v0"
    samples_base_url: str = "https://api.waterdata.usgs.gov/samples-data"
    # The USGS Water Data APIs run behind api.data.gov, which rate-limits by IP without a key.
    # Register a free key at https://api.data.gov/signup and set HGC_USGS_API_KEY for high limits.
    usgs_api_key: str = ""
    http_timeout_s: float = 30.0
    http_max_retries: int = 3
    http_user_agent: str = "hydrogeochem/1.0 (+https://example.org/hydrogeochem)"
    cache_ttl_sites_s: int = 86_400
    cache_ttl_results_s: int = 21_600

    # --- security ------------------------------------------------------------
    api_keys: Annotated[tuple[str, ...], NoDecode] = ()
    jwt_secret: SecretStr = SecretStr("change-me")
    cors_origins: Annotated[tuple[str, ...], NoDecode] = ("http://localhost:8501",)
    rate_limit_per_minute: int = 60
    rate_limit_runs_per_minute: int = 10

    @field_validator("api_keys", "cors_origins", "phreeqc_allowed_databases", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(item.strip() for item in v.split(",") if item.strip())
        return v

    @field_validator("phreeqc_workers")
    @classmethod
    def _sane_workers(cls, v: int) -> int:
        if not 1 <= v <= 64:
            raise ValueError("phreeqc_workers must be between 1 and 64")
        return v

    @property
    def testing(self) -> bool:
        return self.env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
