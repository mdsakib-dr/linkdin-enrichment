"""Centralized configuration loaded from environment variables (.env)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env once at import time. Real environment variables take precedence.
load_dotenv()


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    apify_token: str
    database_url: str
    search_queries: List[str] = field(default_factory=list)
    # Company names for the company-employees actor. When set, the pipeline
    # finds people working at these companies instead of running text searches.
    companies: List[str] = field(default_factory=list)
    # Specific LinkedIn profile URLs to enrich directly (no search step).
    profiles: List[str] = field(default_factory=list)
    max_leads: int = 100
    max_budget_usd: float = 5.00
    # When False, skip the enrichment actor and build leads directly from the
    # search results (name/headline/company/title/location — no email/phone).
    # Useful on the free Apify plan, where the enrich actor blocks API runs.
    enrich_profiles: bool = True
    # When True, look up a verified business email per lead via the atomus
    # profile-search actor (name -> LinkedIn URL/email). Paid Apify plans only.
    find_email: bool = False

    # Optional post-search location filters. The search actor treats location
    # words as relevance keywords, not hard filters, so profiles from other
    # regions leak in. These drop any profile whose current location doesn't
    # match. filter_country is an ISO-3166 alpha-2 code (e.g. "BD"); filter_city
    # is a case-insensitive substring (e.g. "dhaka"). None = no filtering.
    filter_country: str | None = None
    filter_city: str | None = None

    # Cost model constants (documented assumptions).
    cost_per_search_page: float = 0.10          # $ per search results page
    cost_per_company_profile: float = 0.008     # $8 per 1000 employees => $0.008 each
    cost_per_enriched_profile: float = 0.01     # $10 per 1000 profiles => $0.01 each
    cost_per_atomus_profile: float = 0.006      # $6 per 1000 profiles returned
    cost_per_atomus_email: float = 0.006        # $6 per 1000 emails found

    @classmethod
    def load(
        cls,
        *,
        queries_override: List[str] | None = None,
        companies_override: List[str] | None = None,
        profiles_override: List[str] | None = None,
        max_leads_override: int | None = None,
        max_budget_override: float | None = None,
        enrich_override: bool | None = None,
        find_email_override: bool | None = None,
        filter_country_override: str | None = None,
        filter_city_override: str | None = None,
    ) -> "Config":
        """Build a Config from env vars, applying optional CLI overrides.

        Raises ConfigError with an actionable message if anything required is
        missing or malformed.
        """
        errors: List[str] = []

        apify_token = os.getenv("APIFY_TOKEN", "").strip()
        if not apify_token:
            errors.append(
                "APIFY_TOKEN is not set. Add it to your .env "
                "(get one at https://console.apify.com/account/integrations)."
            )

        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            errors.append(
                "DATABASE_URL is not set. Add a Postgres connection string to your .env, e.g. "
                "postgresql+psycopg2://user:password@localhost:5432/linkedin_leads"
            )

        # Search queries: CLI override wins, otherwise parse env.
        if queries_override:
            search_queries = [q.strip() for q in queries_override if q.strip()]
        else:
            raw_queries = os.getenv("SEARCH_QUERIES", "")
            search_queries = [q.strip() for q in raw_queries.split(",") if q.strip()]

        # Companies (company-employees mode): CLI override wins, else COMPANIES.
        if companies_override:
            companies = [c.strip() for c in companies_override if c.strip()]
        else:
            raw_companies = os.getenv("COMPANIES", "")
            companies = [c.strip() for c in raw_companies.split(",") if c.strip()]

        # Direct profile URLs (enrich-only mode): CLI override wins, else PROFILES.
        if profiles_override:
            profiles = [p.strip() for p in profiles_override if p.strip()]
        else:
            raw_profiles = os.getenv("PROFILES", "")
            profiles = [p.strip() for p in raw_profiles.split(",") if p.strip()]

        if not search_queries and not companies and not profiles:
            errors.append(
                "No search queries, companies or profiles provided. Set SEARCH_QUERIES in "
                ".env, or pass --queries / --company / --profile on the command line."
            )

        # Numeric config with validation.
        max_leads = cls._parse_int(
            "MAX_LEADS", max_leads_override, default=100, errors=errors
        )
        max_budget_usd = cls._parse_float(
            "MAX_BUDGET_USD", max_budget_override, default=5.00, errors=errors
        )

        # Enrichment toggle: CLI override wins, otherwise parse env (default on).
        if enrich_override is not None:
            enrich_profiles = enrich_override
        else:
            enrich_profiles = os.getenv("ENRICH_PROFILES", "true").strip().lower() not in (
                "0", "false", "no", "off",
            )

        # Email lookup toggle: CLI override wins, otherwise FIND_EMAIL (default off).
        if find_email_override is not None:
            find_email = find_email_override
        else:
            find_email = os.getenv("FIND_EMAIL", "false").strip().lower() in (
                "1", "true", "yes", "on",
            )

        # Location filters: CLI override wins, else env. Empty string => None.
        raw_country = (
            filter_country_override
            if filter_country_override is not None
            else os.getenv("FILTER_COUNTRY", "")
        ).strip()
        filter_country = raw_country.upper() or None

        raw_city = (
            filter_city_override
            if filter_city_override is not None
            else os.getenv("FILTER_CITY", "")
        ).strip()
        filter_city = raw_city or None

        if errors:
            raise ConfigError(
                "Configuration error(s):\n  - " + "\n  - ".join(errors)
            )

        return cls(
            apify_token=apify_token,
            database_url=database_url,
            search_queries=search_queries,
            companies=companies,
            profiles=profiles,
            max_leads=max_leads,
            max_budget_usd=max_budget_usd,
            enrich_profiles=enrich_profiles,
            find_email=find_email,
            filter_country=filter_country,
            filter_city=filter_city,
        )

    @staticmethod
    def _parse_int(
        env_key: str, override: int | None, *, default: int, errors: List[str]
    ) -> int:
        if override is not None:
            value = override
        else:
            raw = os.getenv(env_key, str(default)).strip()
            try:
                value = int(raw)
            except ValueError:
                errors.append(f"{env_key} must be an integer, got {raw!r}.")
                return default
        if value <= 0:
            errors.append(f"{env_key} must be a positive integer, got {value}.")
            return default
        return value

    @staticmethod
    def _parse_float(
        env_key: str, override: float | None, *, default: float, errors: List[str]
    ) -> float:
        if override is not None:
            value = override
        else:
            raw = os.getenv(env_key, str(default)).strip()
            try:
                value = float(raw)
            except ValueError:
                errors.append(f"{env_key} must be a number, got {raw!r}.")
                return default
        if value <= 0:
            errors.append(f"{env_key} must be a positive number, got {value}.")
            return default
        return value
