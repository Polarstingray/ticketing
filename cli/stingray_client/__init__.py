"""Shared client library for Stingray Tickets.

Used by the ``stingray`` CLI and by the resolver. Depends only on ``requests``.
"""
from stingray_client.api import StingrayClient
from stingray_client.tickets import (
    PARENT_PREFIX,
    PRIORITIES,
    REPO_PREFIX,
    RESERVED_EXACT,
    RESERVED_PREFIXES,
    REVIEW_BY_PREFIX,
    TAG_DELEGATE,
    TYPES,
    build_payload,
    derive_repo_tag,
    has_repo_tag,
    inherited_parent_tags,
    is_reserved_tag,
    parse_code_block,
)

__all__ = [
    "StingrayClient",
    "build_payload",
    "parse_code_block",
    "derive_repo_tag",
    "has_repo_tag",
    "inherited_parent_tags",
    "is_reserved_tag",
    "TYPES",
    "PRIORITIES",
    "TAG_DELEGATE",
    "PARENT_PREFIX",
    "REVIEW_BY_PREFIX",
    "REPO_PREFIX",
    "RESERVED_PREFIXES",
    "RESERVED_EXACT",
]
