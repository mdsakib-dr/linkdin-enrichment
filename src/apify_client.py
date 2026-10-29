"""Thin Apify REST API wrapper for the two actors we use.

Actors:
  - Search:  harvestapi/linkedin-profile-search
  - Company employees: harvestapi/linkedin-company-employees
  - Enrich:  dev_fusion/linkedin-profile-scraper  (actor id 2SyF0bVxmgGr8IVCZ)

All network calls retry with exponential backoff via tenacity. The search
helper uses the run-sync-get-dataset-items endpoint and transparently falls
back to async run + poll when the synchronous call times out.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"

SEARCH_ACTOR = "harvestapi~linkedin-profile-search"
COMPANY_EMPLOYEES_ACTOR = "harvestapi~linkedin-company-employees"
ATOMUS_SEARCH_ACTOR = "atomus~linkedin-profile-search"
ENRICH_ACTOR = "2SyF0bVxmgGr8IVCZ"  # dev_fusion/linkedin-profile-scraper

# HTTP status codes worth retrying (transient server / rate-limit errors).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# tenacity retry decorator shared by all API calls.
_retry = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.RequestException,)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


class ApifyError(Exception):
    """Raised for non-retryable Apify API failures."""


class ApifyClient:
    def __init__(self, token: str, *, sync_timeout_secs: int = 120):
        if not token:
            raise ApifyError("Apify token is required.")
        self._token = token
        self._sync_timeout_secs = sync_timeout_secs
        self._session = requests.Session()

    # ---- low-level helpers -------------------------------------------------

    def _params(self, **extra: Any) -> Dict[str, Any]:
        params = {"token": self._token}
        params.update(extra)
        return params

    @staticmethod
    def _raise_for_retryable(resp: requests.Response) -> None:
        """Turn retryable HTTP statuses into RequestException so tenacity retries.

        Non-retryable 4xx errors raise ApifyError immediately (no retry).
        """
        if resp.status_code in _RETRYABLE_STATUS:
            raise requests.HTTPError(
                f"Retryable Apify status {resp.status_code}: {resp.text[:300]}",
                response=resp,
            )
        if resp.status_code >= 400:
            raise ApifyError(
                f"Apify API error {resp.status_code}: {resp.text[:500]}"
            )

    @_retry
    def _post_run_sync_dataset(
        self, actor: str, run_input: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """POST run-sync-get-dataset-items; returns dataset items list."""
        url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
        resp = self._session.post(
            url,
            params=self._params(),
            json=run_input,
            timeout=self._sync_timeout_secs + 10,
        )
        self._raise_for_retryable(resp)
        return resp.json()

    @_retry
    def _post_run_async(self, actor: str, run_input: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{APIFY_BASE}/acts/{actor}/runs"
        resp = self._session.post(
            url, params=self._params(), json=run_input, timeout=60
        )
        self._raise_for_retryable(resp)
        return resp.json()["data"]

    @_retry
    def _get_run(self, run_id: str) -> Dict[str, Any]:
        url = f"{APIFY_BASE}/actor-runs/{run_id}"
        resp = self._session.get(url, params=self._params(), timeout=60)
        self._raise_for_retryable(resp)
        return resp.json()["data"]

    @_retry
    def _get_dataset_items(self, dataset_id: str) -> List[Dict[str, Any]]:
        url = f"{APIFY_BASE}/datasets/{dataset_id}/items"
        resp = self._session.get(
            url, params=self._params(clean="true", format="json"), timeout=120
        )
        self._raise_for_retryable(resp)
        return resp.json()

    def _run_and_poll(
        self,
        actor: str,
        run_input: Dict[str, Any],
        *,
        poll_interval: float = 5.0,
        max_wait_secs: int = 900,
    ) -> List[Dict[str, Any]]:
        """Start an async run and poll until it finishes, then fetch items."""
        run = self._post_run_async(actor, run_input)
        run_id = run["id"]
        logger.info("Started async Apify run %s for actor %s", run_id, actor)

        waited = 0.0
        backoff = poll_interval
        while waited < max_wait_secs:
            time.sleep(backoff)
            waited += backoff
            run = self._get_run(run_id)
            status = run["status"]
            if status == "SUCCEEDED":
                return self._get_dataset_items(run["defaultDatasetId"])
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise ApifyError(
                    f"Apify run {run_id} ended with status {status}."
                )
            # Gentle backoff growth, capped at 15s between polls.
            backoff = min(backoff * 1.5, 15.0)

        raise ApifyError(
            f"Apify run {run_id} did not finish within {max_wait_secs}s."
        )

    def _run_sync_or_poll(
        self, actor: str, run_input: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Try sync endpoint; fall back to async run+poll on timeout."""
        try:
            return self._post_run_sync_dataset(actor, run_input)
        except requests.Timeout:
            logger.warning(
                "Sync call to %s timed out; falling back to async run + poll.",
                actor,
            )
            return self._run_and_poll(actor, run_input)

    # ---- public: search ----------------------------------------------------

    def search_profiles(
        self, query: str, *, max_results: int
    ) -> List[Dict[str, Any]]:
        """Run the search actor for one query. Returns raw profile items.

        The harvestapi search actor accepts a free-text search plus a result
        cap. We pass both; unknown fields are ignored by the actor.
        """
        run_input = {
            "searchQuery": query,
            "search": query,
            "maxItems": max_results,
            "maxResults": max_results,
        }
        logger.info("Searching: %r (max %d)", query, max_results)
        items = self._run_sync_or_poll(SEARCH_ACTOR, run_input)
        logger.info("Search %r returned %d raw items", query, len(items))
        return items

    # ---- public: company employees -----------------------------------------

    def search_company_employees(
        self,
        companies: List[str],
        *,
        max_results: int,
        job_titles: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Run the company-employees actor for one or more company names.

        Returns raw profile items (same shape as the profile search actor), so
        the pipeline can reuse its mapping/filtering logic.
        """
        if not companies:
            return []
        run_input: Dict[str, Any] = {
            "companies": companies,
            "maxItems": max_results,
            "companyBatchMode": "all_at_once",
        }
        if job_titles:
            run_input["jobTitles"] = job_titles
        logger.info(
            "Searching employees at %d company(ies) (max %d): %s",
            len(companies), max_results, ", ".join(companies),
        )
        items = self._run_sync_or_poll(COMPANY_EMPLOYEES_ACTOR, run_input)
        logger.info("Company employees search returned %d raw items", len(items))
        return items

    # ---- public: email lookup (atomus) -------------------------------------

    def find_profiles_by_name(
        self,
        names: List[str],
        *,
        max_results: int,
        find_email: bool = True,
    ) -> List[Dict[str, Any]]:
        """Look profiles up by full name with the atomus profile-search actor.

        The actor is a search API (it takes no profile URLs), but accepts the
        API-only ``fullName`` filter. Rows carry ``linkedin_url`` and — on paid
        Apify plans — a verified ``email``.
        """
        if not names:
            return []
        run_input: Dict[str, Any] = {
            "fullName": list(names),
            "maxResults": max_results,
            "findEmail": find_email,
        }
        logger.info("Looking up %d name(s) for verified emails", len(names))
        items = self._run_sync_or_poll(ATOMUS_SEARCH_ACTOR, run_input)
        logger.info("Email lookup returned %d item(s)", len(items))
        return items

    # ---- public: enrich ----------------------------------------------------

    def enrich_profiles(self, profile_urls: List[str]) -> List[Dict[str, Any]]:
        """Run the enrichment actor for a batch of profile URLs.

        Returns raw enriched items. Callers should map results back to their
        input URLs and treat missing profiles as per-profile failures.
        """
        if not profile_urls:
            return []
        run_input = {
            "profileUrls": profile_urls,
            "urls": profile_urls,
        }
        logger.info("Enriching batch of %d profiles", len(profile_urls))
        items = self._run_sync_or_poll(ENRICH_ACTOR, run_input)
        logger.info("Enrichment returned %d items", len(items))
        return items


def extract_profile_url(item: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of a LinkedIn profile URL from a search item.

    Actors vary in field naming, so we probe several common keys and normalize.
    """
    candidates = (
        item.get("linkedinUrl"),
        item.get("profileUrl"),
        item.get("url"),
        item.get("publicProfileUrl"),
        item.get("link"),
    )
    for c in candidates:
        if c and isinstance(c, str) and "linkedin.com/in/" in c:
            return normalize_linkedin_url(c)
    return None


def normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn profile URL for stable dedup / unique keys.

    Strips query strings, fragments, and trailing slashes; lowercases host.
    """
    url = url.strip()
    # Drop query and fragment.
    for sep in ("?", "#"):
        idx = url.find(sep)
        if idx != -1:
            url = url[:idx]
    url = url.rstrip("/")
    # Lowercase scheme+host only, keep the vanity slug case-insensitive too:
    # LinkedIn slugs are case-insensitive, so lowercasing the whole thing is safe
    # and gives us a stable dedup key.
    return url.lower()
