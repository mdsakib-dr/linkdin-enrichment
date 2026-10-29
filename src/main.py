"""CLI entry point for the LinkedIn lead generation pipeline."""
from __future__ import annotations

import logging
import sys

import click

from .apify_client import ApifyClient
from .config import Config, ConfigError
from .db import init_db, make_engine, make_session_factory
from .pipeline import Pipeline


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party libraries.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--queries",
    "queries",
    multiple=True,
    help="Search query (repeatable). Overrides SEARCH_QUERIES in .env. "
    'You may also pass a single comma-separated string.',
)
@click.option(
    "--company",
    "companies",
    multiple=True,
    help="Find people working at this company (repeatable, or comma-separated). "
    "Uses the company-employees actor instead of the text search. "
    "Overrides COMPANIES in .env.",
)
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    help="Enrich this LinkedIn profile URL directly (repeatable, or "
    "comma-separated). Skips search; requires enrichment. Overrides PROFILES.",
)
@click.option("--max-leads", type=int, default=None, help="Cap on leads to collect.")
@click.option(
    "--max-budget",
    type=float,
    default=None,
    help="Hard budget cap in USD (overrides MAX_BUDGET_USD).",
)
@click.option(
    "--no-enrich",
    "no_enrich",
    is_flag=True,
    help="Skip the enrichment actor and build leads from search results only "
    "(no email/phone). Works on the free Apify plan.",
)
@click.option(
    "--find-email",
    "find_email",
    is_flag=True,
    help="Look up a verified business email per lead via the atomus "
    "profile-search actor ($0.006/profile + $0.006/email; paid Apify plans "
    "only).",
)
@click.option(
    "--country",
    "country",
    default=None,
    help="Keep only profiles whose current location is in this ISO-3166 "
    'country code (e.g. "BD"). Overrides FILTER_COUNTRY.',
)
@click.option(
    "--city",
    "city",
    default=None,
    help='Keep only profiles whose location contains this text, e.g. "Dhaka" '
    "(case-insensitive substring). Overrides FILTER_CITY.",
)
@click.option(
    "--init-db",
    "do_init_db",
    is_flag=True,
    help="Create tables directly instead of using Alembic (quick local setup).",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def main(
    queries, companies, profiles, max_leads, max_budget, no_enrich, find_email,
    country, city, do_init_db, verbose,
):
    """Automate LinkedIn lead generation: search -> enrich -> save to Postgres."""
    _configure_logging(verbose)
    log = logging.getLogger("main")

    # Allow --queries "a,b" / --company "a,b" as well as repeated flags.
    query_list = []
    for q in queries:
        query_list.extend(part.strip() for part in q.split(",") if part.strip())

    company_list = []
    for c in companies:
        company_list.extend(part.strip() for part in c.split(",") if part.strip())

    profile_list = []
    for u in profiles:
        profile_list.extend(part.strip() for part in u.split(",") if part.strip())

    try:
        config = Config.load(
            queries_override=query_list or None,
            companies_override=company_list or None,
            profiles_override=profile_list or None,
            max_leads_override=max_leads,
            max_budget_override=max_budget,
            enrich_override=False if no_enrich else None,
            find_email_override=True if find_email else None,
            filter_country_override=country,
            filter_city_override=city,
        )
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(2)

    engine = make_engine(config.database_url)

    if do_init_db:
        log.info("Creating tables directly (init-db)...")
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to create tables: %s", exc)
            sys.exit(1)
        log.info("Tables created.")

    session_factory = make_session_factory(engine)
    client = ApifyClient(config.apify_token)
    pipeline = Pipeline(config, client, session_factory)

    try:
        summary = pipeline.run()
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        log.error("Pipeline failed: %s", exc, exc_info=verbose)
        sys.exit(1)

    click.echo(summary.render())


if __name__ == "__main__":
    main()
