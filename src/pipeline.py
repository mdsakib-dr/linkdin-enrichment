"""Pipeline: search -> enrich -> save, with cost/budget tracking."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import sessionmaker

from .apify_client import ApifyClient, ApifyError, extract_profile_url, normalize_linkedin_url
from .config import Config
from .db import session_scope, upsert_leads

logger = logging.getLogger(__name__)

ENRICH_BATCH_SIZE = 50
ENRICH_CONCURRENCY = 3


class BudgetExceeded(Exception):
    """Raised when a planned action would push estimated cost over the cap."""


@dataclass
class RunSummary:
    total_searched: int = 0        # unique profile URLs collected from search
    filtered_out: int = 0          # profiles dropped by location filter
    total_enriched: int = 0        # profiles successfully enriched
    total_failed_enrich: int = 0   # profiles that failed enrichment
    total_saved: int = 0           # rows upserted
    with_email: int = 0
    with_phone: int = 0
    estimated_cost: float = 0.0
    budget_hit: bool = False
    errors: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "",
            "=" * 48,
            "  RUN SUMMARY",
            "=" * 48,
            f"  Profiles found (search):   {self.total_searched}",
            f"  Filtered out (location):   {self.filtered_out}",
            f"  Profiles enriched:         {self.total_enriched}",
            f"  Enrichment failures:       {self.total_failed_enrich}",
            f"  Leads saved (upserted):    {self.total_saved}",
            f"  Leads with email:          {self.with_email}",
            f"  Leads with phone:          {self.with_phone}",
            f"  Estimated total cost:      ${self.estimated_cost:.2f}",
        ]
        if self.budget_hit:
            lines.append("  NOTE: budget cap reached — run stopped early.")
        if self.errors:
            lines.append(f"  Non-fatal errors:          {len(self.errors)}")
        lines.append("=" * 48)
        return "\n".join(lines)


class CostTracker:
    """Thread-safe running estimate of spend against a hard budget cap."""

    def __init__(self, config: Config):
        self._cfg = config
        self._cost = 0.0
        self._lock = threading.Lock()

    @property
    def cost(self) -> float:
        with self._lock:
            return self._cost

    def would_exceed(self, additional: float) -> bool:
        with self._lock:
            return (self._cost + additional) > self._cfg.max_budget_usd

    def add(self, amount: float) -> float:
        with self._lock:
            self._cost += amount
            return self._cost


class Pipeline:
    def __init__(
        self,
        config: Config,
        client: ApifyClient,
        session_factory: sessionmaker,
    ):
        self._cfg = config
        self._client = client
        self._session_factory = session_factory
        self._cost = CostTracker(config)
        self._search_items: Dict[str, Dict[str, Any]] = {}

    # ---- step 1: search ----------------------------------------------------

    def _search(self, summary: RunSummary) -> Dict[str, str]:
        """Collect unique {url: query} until MAX_LEADS or budget reached.

        Also stashes the raw search item per URL in self._search_items so the
        search-only (no-enrich) path can build leads without a second API call.
        """
        collected: Dict[str, str] = {}
        self._search_items: Dict[str, Dict[str, Any]] = {}

        if self._cfg.profiles:
            return {
                normalize_linkedin_url(u): "direct"
                for u in self._cfg.profiles[: self._cfg.max_leads]
            }

        if self._cfg.companies:
            return self._search_company_employees(summary)

        for query in self._cfg.search_queries:
            if len(collected) >= self._cfg.max_leads:
                break

            # Each search "page" costs cost_per_search_page. Guard the budget
            # before spending on the next query.
            if self._cost.would_exceed(self._cfg.cost_per_search_page):
                logger.warning(
                    "Skipping remaining searches — budget cap $%.2f would be "
                    "exceeded (current estimate $%.2f).",
                    self._cfg.max_budget_usd,
                    self._cost.cost,
                )
                summary.budget_hit = True
                break

            remaining = self._cfg.max_leads - len(collected)
            try:
                items = self._client.search_profiles(query, max_results=remaining)
            except (ApifyError, Exception) as exc:  # noqa: BLE001 - log and continue
                msg = f"Search failed for {query!r}: {exc}"
                logger.error(msg)
                summary.errors.append(msg)
                continue

            # Cost: charge per page. Estimate pages from item count (~25/page),
            # minimum one page since we made the call.
            pages = max(1, (len(items) + 24) // 25)
            new_cost = self._cost.add(pages * self._cfg.cost_per_search_page)
            logger.info(
                "Search %r: %d items (~%d page(s)); running cost $%.2f",
                query, len(items), pages, new_cost,
            )

            for item in items:
                url = extract_profile_url(item)
                if not url:
                    continue
                if not self._location_matches(item):
                    summary.filtered_out += 1
                    continue
                if url not in collected:
                    collected[url] = query
                    self._search_items[url] = item
                if len(collected) >= self._cfg.max_leads:
                    break

        summary.total_searched = len(collected)
        if summary.filtered_out:
            logger.info(
                "Filtered out %d profile(s) not matching location "
                "(country=%s, city=%s).",
                summary.filtered_out,
                self._cfg.filter_country or "any",
                self._cfg.filter_city or "any",
            )
        logger.info("Collected %d unique profile URLs from search.", len(collected))
        return collected

    def _search_company_employees(self, summary: RunSummary) -> Dict[str, str]:
        """Collect employees of the configured companies (company mode).

        Runs the harvestapi company-employees actor once for all companies,
        then applies the same location filtering/dedup as the text search.
        """
        collected: Dict[str, str] = {}
        self._search_items = {}

        # Pre-flight budget check: assume the full requested batch is returned.
        projected = self._cfg.max_leads * self._cfg.cost_per_company_profile
        if self._cost.would_exceed(projected):
            logger.warning(
                "Skipping company search — %d profiles (~$%.2f) would exceed "
                "budget $%.2f.",
                self._cfg.max_leads, projected, self._cfg.max_budget_usd,
            )
            summary.budget_hit = True
            summary.total_searched = 0
            return collected

        try:
            items = self._client.search_company_employees(
                self._cfg.companies, max_results=self._cfg.max_leads
            )
        except Exception as exc:  # noqa: BLE001 - log and continue
            msg = f"Company employees search failed: {exc}"
            logger.error(msg)
            summary.errors.append(msg)
            items = []

        new_cost = self._cost.add(
            len(items) * self._cfg.cost_per_company_profile
        )
        logger.info(
            "Company employees (%s): %d items; running cost $%.2f",
            ", ".join(self._cfg.companies), len(items), new_cost,
        )

        for item in items:
            url = extract_profile_url(item)
            if not url:
                continue
            if not self._location_matches(item):
                summary.filtered_out += 1
                continue
            if url not in collected:
                collected[url] = self._company_label(item) or ", ".join(
                    self._cfg.companies
                )
                self._search_items[url] = item
            if len(collected) >= self._cfg.max_leads:
                break

        summary.total_searched = len(collected)
        if summary.filtered_out:
            logger.info(
                "Filtered out %d profile(s) not matching location "
                "(country=%s, city=%s).",
                summary.filtered_out,
                self._cfg.filter_country or "any",
                self._cfg.filter_city or "any",
            )
        logger.info(
            "Collected %d unique profile URLs from company employees.", len(collected)
        )
        return collected

    @staticmethod
    def _company_label(item: Dict[str, Any]) -> Optional[str]:
        """Best-effort current company name for a company-employees item."""
        positions = item.get("currentPosition")
        if isinstance(positions, list) and positions and isinstance(positions[0], dict):
            name = positions[0].get("companyName")
            if isinstance(name, str) and name.strip():
                return name.strip()
        for key in ("companyName", "company"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _location_matches(self, item: Dict[str, Any]) -> bool:
        """Return True if the profile's current location passes the configured
        country/city filters. Profiles with no location data are kept only when
        no filter is set; a filter present + missing data => excluded."""
        country_filter = self._cfg.filter_country
        city_filter = self._cfg.filter_city
        if not country_filter and not city_filter:
            return True

        loc = item.get("location")
        if not isinstance(loc, dict):
            return False

        parsed = loc.get("parsed") if isinstance(loc.get("parsed"), dict) else {}

        if country_filter:
            code = (loc.get("countryCode") or parsed.get("countryCode") or "")
            if code.upper() != country_filter:
                return False

        if city_filter:
            # Match city against the structured city plus the human-readable
            # location text, so "Greater Dhaka" / "Dhaka, Bangladesh" all pass.
            haystacks = [
                parsed.get("city"),
                parsed.get("text"),
                loc.get("linkedinText"),
            ]
            needle = city_filter.lower()
            if not any(
                isinstance(h, str) and needle in h.lower() for h in haystacks
            ):
                return False

        return True

    # ---- step 2 + 3: enrich and save --------------------------------------

    def _enrich_and_save(
        self, url_to_query: Dict[str, str], summary: RunSummary
    ) -> None:
        urls = list(url_to_query.keys())
        batches = [
            urls[i : i + ENRICH_BATCH_SIZE]
            for i in range(0, len(urls), ENRICH_BATCH_SIZE)
        ]
        total_batches = len(batches)
        if not total_batches:
            return

        # Pre-flight budget check: enrichment cost for all collected profiles.
        # We still guard per-batch inside the worker in case cost creeps up.
        logger.info(
            "Enriching %d profiles in %d batch(es), concurrency %d.",
            len(urls), total_batches, ENRICH_CONCURRENCY,
        )

        with ThreadPoolExecutor(max_workers=ENRICH_CONCURRENCY) as executor:
            future_to_idx = {}
            for idx, batch in enumerate(batches, start=1):
                future = executor.submit(
                    self._process_batch, idx, total_batches, batch, url_to_query
                )
                future_to_idx[future] = idx

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                except BudgetExceeded as exc:
                    logger.warning(str(exc))
                    summary.budget_hit = True
                    continue
                except Exception as exc:  # noqa: BLE001
                    msg = f"Batch {idx} failed: {exc}"
                    logger.error(msg)
                    summary.errors.append(msg)
                    continue

                summary.total_enriched += result["enriched"]
                summary.total_failed_enrich += result["failed"]
                summary.total_saved += result["saved"]
                summary.with_email += result["with_email"]
                summary.with_phone += result["with_phone"]

    def _process_batch(
        self,
        idx: int,
        total: int,
        batch_urls: List[str],
        url_to_query: Dict[str, str],
    ) -> Dict[str, int]:
        """Enrich one batch and upsert results in a single transaction."""
        batch_cost = len(batch_urls) * self._cfg.cost_per_enriched_profile
        if self._cost.would_exceed(batch_cost):
            raise BudgetExceeded(
                f"Batch {idx}/{total} skipped — enriching {len(batch_urls)} "
                f"profiles (${batch_cost:.2f}) would exceed budget "
                f"${self._cfg.max_budget_usd:.2f} (current ${self._cost.cost:.2f})."
            )

        logger.info("Enriching batch %d/%d (%d profiles)...", idx, total, len(batch_urls))
        items = self._client.enrich_profiles(batch_urls)
        self._cost.add(batch_cost)

        rows: List[dict] = []
        enriched = 0
        with_email = 0
        with_phone = 0

        # Map enriched items back to their input URL where possible.
        for item in items:
            row = self._map_lead(item, url_to_query)
            if row is None:
                continue
            enriched += 1
            if row.get("email"):
                with_email += 1
            if row.get("mobile_number"):
                with_phone += 1
            rows.append(row)

        failed = len(batch_urls) - enriched
        if failed > 0:
            logger.warning(
                "Batch %d/%d: %d profile(s) had no usable enrichment result.",
                idx, total, failed,
            )

        saved = 0
        if rows:
            with session_scope(self._session_factory) as session:
                saved = upsert_leads(session, rows)
            logger.info("Batch %d/%d: saved %d lead(s).", idx, total, saved)

        return {
            "enriched": enriched,
            "failed": max(0, failed),
            "saved": saved,
            "with_email": with_email,
            "with_phone": with_phone,
        }

    # ---- search-only path (no enrichment) ---------------------------------

    def _save_from_search(
        self, items_by_url: Dict[str, Dict[str, Any]], url_to_query: Dict[str, str],
        summary: RunSummary,
    ) -> None:
        """Build leads straight from search results, skipping the enrich actor.

        The harvestapi search actor already returns rich profile data (name,
        headline, current position, location). It does not return contact info,
        so email/phone will be empty. This keeps the free Apify plan usable.
        """
        rows: List[dict] = []
        for url, item in items_by_url.items():
            row = self._map_search_lead(url, item, url_to_query)
            if row is None:
                continue
            rows.append(row)
            if row.get("email"):
                summary.with_email += 1
            if row.get("mobile_number"):
                summary.with_phone += 1

        summary.total_enriched = len(rows)
        if rows:
            with session_scope(self._session_factory) as session:
                summary.total_saved = upsert_leads(session, rows)
            logger.info("Saved %d lead(s) from search results.", summary.total_saved)
        else:
            logger.warning("No usable leads could be mapped from search results.")

    def _map_search_lead(
        self, url: str, item: Dict[str, Any], url_to_query: Dict[str, str]
    ) -> Optional[dict]:
        """Map a raw harvestapi search item to a Lead row dict."""
        full_name = self._first(item, "fullName", "name")
        if not full_name:
            first = self._first(item, "firstName")
            last = self._first(item, "lastName")
            full_name = " ".join(p for p in (first, last) if p) or None

        headline = self._first(item, "headline", "occupation")

        # currentPosition is a list of position dicts; take the first.
        company = None
        job_title = None
        positions = item.get("currentPosition")
        if isinstance(positions, list) and positions and isinstance(positions[0], dict):
            pos = positions[0]
            company = pos.get("companyName") or None
            job_title = pos.get("position") or None
        # Fallbacks to flat keys if the actor shape ever changes.
        company = company or self._first(item, "companyName", "company")
        job_title = job_title or self._first(item, "jobTitle", "title") or headline

        # location is a nested dict on the search actor; fall back to flat string.
        location = None
        loc = item.get("location")
        if isinstance(loc, dict):
            location = loc.get("linkedinText") or (
                loc.get("parsed", {}).get("text") if isinstance(loc.get("parsed"), dict) else None
            )
        elif isinstance(loc, str):
            location = loc

        return {
            "linkedin_url": url,
            "full_name": full_name if isinstance(full_name, str) else None,
            "headline": headline if isinstance(headline, str) else None,
            "job_title": job_title if isinstance(job_title, str) else None,
            "company_name": company if isinstance(company, str) else None,
            "location": location if isinstance(location, str) else None,
            "email": self._extract_email(item),
            "mobile_number": self._extract_phone(item),
            "raw_data": item,
            "search_query": url_to_query.get(url),
        }

    # ---- mapping -----------------------------------------------------------

    @staticmethod
    def _first(item: Dict[str, Any], *keys: str) -> Optional[str]:
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if v not in (None, "", [], {}):
                return v
        return None

    def _map_lead(
        self, item: Dict[str, Any], url_to_query: Dict[str, str]
    ) -> Optional[dict]:
        """Map a raw enrichment item to a Lead row dict.

        Returns None if we cannot resolve a LinkedIn URL for the item (we can't
        upsert without the unique key).
        """
        raw_url = self._first(
            item, "linkedinUrl", "profileUrl", "url", "publicProfileUrl", "inputUrl"
        )
        if not raw_url or not isinstance(raw_url, str):
            return None
        url = normalize_linkedin_url(raw_url)

        search_query = url_to_query.get(url)

        full_name = self._first(item, "fullName", "name")
        if not full_name:
            first = self._first(item, "firstName")
            last = self._first(item, "lastName")
            full_name = " ".join(p for p in (first, last) if p) or None

        # Company / job title can be nested under experiences.
        company = self._first(item, "companyName", "company", "currentCompany")
        job_title = self._first(item, "jobTitle", "headline", "occupation", "title")
        headline = self._first(item, "headline", "occupation")

        location = self._first(
            item, "location", "addressWithCountry", "geoLocationName", "city"
        )

        email = self._extract_email(item)
        phone = self._extract_phone(item)

        return {
            "linkedin_url": url,
            "full_name": full_name if isinstance(full_name, str) else None,
            "headline": headline if isinstance(headline, str) else None,
            "job_title": job_title if isinstance(job_title, str) else None,
            "company_name": company if isinstance(company, str) else None,
            "location": location if isinstance(location, str) else None,
            "email": email,
            "mobile_number": phone,
            "raw_data": item,
            "search_query": search_query,
        }

    @staticmethod
    def _extract_email(item: Dict[str, Any]) -> Optional[str]:
        for k in ("email", "emailAddress", "workEmail"):
            v = item.get(k)
            if isinstance(v, str) and "@" in v:
                return v.strip()
        emails = item.get("emails")
        if isinstance(emails, list) and emails:
            first = emails[0]
            if isinstance(first, str) and "@" in first:
                return first.strip()
            if isinstance(first, dict):
                val = first.get("email") or first.get("value")
                if isinstance(val, str) and "@" in val:
                    return val.strip()
        return None

    @staticmethod
    def _extract_phone(item: Dict[str, Any]) -> Optional[str]:
        for k in ("mobileNumber", "phone", "phoneNumber", "mobile"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        phones = item.get("phoneNumbers") or item.get("phones")
        if isinstance(phones, list) and phones:
            first = phones[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                val = first.get("number") or first.get("value") or first.get("phone")
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    # ---- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        summary = RunSummary()
        if self._cfg.profiles:
            if not self._cfg.enrich_profiles:
                msg = (
                    "Profile mode requires enrichment (ENRICH_PROFILES=true) — "
                    "there is no search result to build a lead from."
                )
                logger.error(msg)
                summary.errors.append(msg)
                return summary
            logger.info(
                "Starting pipeline (profiles): %d profile URL(s), budget=$%.2f",
                len(self._cfg.profiles),
                self._cfg.max_budget_usd,
            )
        elif self._cfg.companies:
            logger.info(
                "Starting pipeline (companies): %d company(ies), max_leads=%d, "
                "budget=$%.2f",
                len(self._cfg.companies),
                self._cfg.max_leads,
                self._cfg.max_budget_usd,
            )
        else:
            logger.info(
                "Starting pipeline: %d queries, max_leads=%d, budget=$%.2f",
                len(self._cfg.search_queries),
                self._cfg.max_leads,
                self._cfg.max_budget_usd,
            )

        url_to_query = self._search(summary)
        if not url_to_query:
            logger.warning("No profiles collected from search — nothing to enrich.")
            summary.estimated_cost = self._cost.cost
            return summary

        if self._cfg.enrich_profiles:
            self._enrich_and_save(url_to_query, summary)
        else:
            logger.info(
                "Enrichment disabled — building %d lead(s) from search data "
                "(no email/phone).", len(url_to_query),
            )
            self._save_from_search(self._search_items, url_to_query, summary)

        summary.estimated_cost = self._cost.cost
        return summary
