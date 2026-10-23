#!/usr/bin/env python3
"""de_endpoint.py — Discovery Engine API base URL and resource name helpers.

Supports multi-region ('us', 'eu') and 'global' Discovery Engine endpoints and resource paths.
Default resource paths and base URLs target locations/global and
https://discoveryengine.googleapis.com/v1/.
"""

from typing import Optional

DEFAULT_LOCATION = "global"
DEFAULT_COLLECTION = "default_collection"


def api_base(location: str = "global") -> str:
    """Return the Discovery Engine v1 API base URL for the given location.

    For 'global', returns 'https://discoveryengine.googleapis.com/v1/'.
    For regional or multi-regional locations (e.g. 'us', 'eu'), returns
    'https://{location}-discoveryengine.googleapis.com/v1/'.
    """
    loc = (location or DEFAULT_LOCATION).strip()
    if loc == "global":
        return "https://discoveryengine.googleapis.com/v1/"
    return f"https://{loc}-discoveryengine.googleapis.com/v1/"


def collection_name(
    project: str,
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full resource name of a Discovery Engine collection."""
    loc = (location or DEFAULT_LOCATION).strip()
    return f"projects/{project}/locations/{loc}/collections/{collection}"


def data_store_name(
    project: str,
    data_store_id: str,
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full resource name of a Discovery Engine data store."""
    loc = (location or DEFAULT_LOCATION).strip()
    return f"projects/{project}/locations/{loc}/collections/{collection}/dataStores/{data_store_id}"


def branch_name(
    project: str,
    data_store_id: str,
    branch_id: str = "default_branch",
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full resource name of a Discovery Engine data store branch."""
    ds = data_store_name(project, data_store_id, location=location, collection=collection)
    return f"{ds}/branches/{branch_id}"


def engine_name(
    project: str,
    engine_id: str,
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full resource name of a Discovery Engine search engine."""
    loc = (location or DEFAULT_LOCATION).strip()
    return f"projects/{project}/locations/{loc}/collections/{collection}/engines/{engine_id}"


def serving_config_name(
    project: str,
    engine_id: str,
    serving_config_id: str = "default_search",
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full resource name of a Discovery Engine serving config."""
    eng = engine_name(project, engine_id, location=location, collection=collection)
    return f"{eng}/servingConfigs/{serving_config_id}"


def search_url(
    project: str,
    engine_id: str,
    serving_config_id: str = "default_search",
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the full :search URL for a Discovery Engine serving config."""
    base = api_base(location)
    sc = serving_config_name(
        project,
        engine_id,
        serving_config_id=serving_config_id,
        location=location,
        collection=collection,
    )
    return f"{base}{sc}:search"


def documents_import_url(
    project: str,
    data_store_id: str,
    branch_id: str = "default_branch",
    location: str = "global",
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Return the documents:import URL for a Discovery Engine data store branch."""
    base = api_base(location)
    b = branch_name(
        project,
        data_store_id,
        branch_id=branch_id,
        location=location,
        collection=collection,
    )
    return f"{base}{b}/documents:import"
