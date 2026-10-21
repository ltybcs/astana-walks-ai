"""Explicit, immutable alternatives for UNKNOWN optional conveniences only.

These pure helpers do not replace origin/radius/clock/routing checks. The caller
must run the same availability, safety and verified budget gates for both pools.
No source feature is rewritten and no provider search is performed here.
"""
from __future__ import annotations

from dataclasses import replace

from .category_catalog import CATEGORY_CATALOG
from .semantic_intent import IntentPlan
from .retrieval import feature, matched_goal_indices

AMENITY_KEYS = ("socket", "wifi", "quiet")


def relax_amenities(plan):
    """Copy only required convenience bits; all literal/goal/scope metadata stays."""
    if not isinstance(plan, IntentPlan):
        raise TypeError("Expected one validated IntentPlan")
    if not any(item.required and item.key in AMENITY_KEYS for item in plan.requirements):
        return None
    requirements = tuple(replace(item, required=False) if item.required and item.key in AMENITY_KEYS
                         else item for item in plan.requirements)
    return replace(plan, requirements=requirements)


def _nonamenity_access(place, plan):
    if not isinstance(place.get("category"), str) or not isinstance(place.get("features", {}), dict):
        return False
    tags = place.get("source_tags", {})
    if not isinstance(tags, dict):
        return False
    for access in (tags.get("access"), place.get("access_evidence")):
        if access is not None and (not isinstance(access, str) or access.strip().casefold() in {"private", "no"}):
            return False
    actual = {place.get("category")}
    for field in ("source_categories", "source_types"):
        values = place.get(field, ())
        if isinstance(values, (list, tuple)):
            actual.update(value for value in values if isinstance(value, str))
    if any(item.flag == "child" for item in plan.context) and actual.intersection({"bar", "nightclub"}):
        return False
    for item in plan.excluded:
        if item.category_hint is None:
            return False  # Do not relax an exclusion whose type cannot be proved.
        if actual.intersection(CATEGORY_CATALOG[item.category_hint]["source_categories"]):
            return False
    return True


def unconfirmed_amenities(place, original_plan, *, goal_indices=None):
    """Return UNKNOWN required keys for one own matched branch, else None.

    A known negative in that branch is ineligible, even if another property is
    unknown. A strictly matching branch needs no alternative label. Identity,
    type, primary/OR policy and non-convenience requirements remain unchanged.
    """
    relaxed = relax_amenities(original_plan)
    if relaxed is None or not isinstance(place, dict) or not _nonamenity_access(place, original_plan):
        return None
    active = set(range(len(original_plan.goals))) if goal_indices is None else set(goal_indices)
    if not active or any(type(index) is not int or not 0 <= index < len(original_plan.goals) for index in active):
        return None
    if set(matched_goal_indices(place, original_plan)) & active:
        return None
    for index in matched_goal_indices(place, relaxed):
        if index not in active:
            continue
        required = set(original_plan.effective_required_keys(index))
        requested = [key for key in AMENITY_KEYS if key in required]
        states = {key: feature(place, key) for key in requested}
        if not requested or any(value not in {"yes", "unknown"} for value in states.values()):
            continue
        unknown = tuple(key for key in requested if states[key] == "unknown")
        if unknown:
            return unknown
    return None
