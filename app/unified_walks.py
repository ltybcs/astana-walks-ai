"""One live three-source search, directed walk plan and whole AI answer.

Provider content is request-scoped RAM only. No corpus/cache/archive writes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import math
import re
from threading import BoundedSemaphore
from time import monotonic

from .google_budget import GoogleError
from .google_grounded import prepare, route_minutes, validate_grounding, WALK_WARNING
from .retrieval import architecture_evidence, detect_intent, distance_m, feature, source_goal_evidence, matched_goal_indices, night_hours_eligible
from .unified_narration import narrate
from .place_evidence import normalize_place_evidence, PlaceEvidenceError
from .opening_hours import evaluate_schedule
from .category_catalog import CATEGORY_CATALOG
from .activity_intent import parse_search_intent
from .amenity_fallback import AMENITY_KEYS, relax_amenities, unconfirmed_amenities
from .semantic_intent import deterministic_plan, IntentError
from .route_request import validate_walking_request, walking_waypoints, walking_bindings
from .request_preparation import prepare_walking_baseline
from .ordered_goals import ordered_goals
from .place_exclusions import excluded_place_names, is_place_excluded, proven_place_exclusions


def _point(place):
    if place.get("provider") == "google":
        return {"id": place["id"]}
    return {"latLng": {"latitude": place["lat"], "longitude": place["lon"]}}


def _baseline(resolved):
    """Bind prepared duration to the exact server-catalogue endpoint pair."""
    context = resolved["resolved_context"]
    if context.get("route_kind") != "point_to_point":
        return {}
    try:
        walking_waypoints(context)
        origin_binding, destination_binding = walking_bindings(context)
        leg = resolved["route_baseline"]
        minutes = leg["minutes"]
        if (resolved["origin"] != context["origin"] or type(minutes) is not int or minutes <= 0
                or minutes != context.get("direct_walk_minutes")
                or minutes + 5 > max(context["budget_options"])
                or leg.get("origin_id") != context.get("origin_id")
                or leg.get("destination_id") != context.get("destination_id")
                or leg.get("origin_binding") != origin_binding
                or leg.get("destination_binding") != destination_binding
                or leg.get("from_ref") != "origin" or leg.get("to_ref") != "destination"
                or leg.get("error_code") is not None
                or leg.get("attribution") != context.get("direct_route_attribution")):
            raise ValueError("Unbound direct route")
    except (KeyError, TypeError, ValueError):
        raise GoogleError("direct_route_unverified", "Прямой пеший маршрут не подтверждён; подбор мест не выполнен.", 502) from None
    return {("origin", "destination"): deepcopy(leg)}


def _without_named_exclusions(plan, names):
    """Name filtering owns only positively parsed literal excluded identities.

    Unknown service/category exclusions stay fail-closed. The original request
    and immutable plan are retained; this copy avoids treating an already bound
    named exclusion as an unprovable category during convenience fallback.
    """
    if plan is None or not names:
        return plan
    remaining = tuple(item for item in plan.excluded if item.category_hint is not None
                      or not is_place_excluded({"name": item.quote}, names))
    return plan if remaining == plan.excluded else replace(plan, excluded=remaining)


def _source_classes(categories):
    result = set()
    for category in categories:
        if category == "walk":
            continue  # A broad walk is not a hard venue-type requirement.
        spec = CATEGORY_CATALOG.get(category)
        result.update(spec["source_categories"] if spec else (category,))
    return result


def _eligible(place, intent, intent_plan=None, *, schedule_when=None):
    if intent_plan is not None and not source_goal_evidence(place, intent_plan):
        return False
    category = place.get("category")
    preferred = _source_classes(intent["preferred_categories"])
    explicit_sources = _source_classes([value for value in intent["preferred_categories"] if value not in {"walk", "architecture"}])
    primary = intent.get("primary_category") or next(iter(intent["preferred_categories"]), None)
    excluded = _source_classes(intent["excluded_categories"])
    raw_types = place.get("source_categories", [])
    actual_types = {value for value in raw_types if isinstance(value, str)} if isinstance(raw_types, (list, tuple)) else set()
    actual_types.add(category)
    if actual_types & excluded:
        return False
    # A primary venue type is a hard condition, even with secondary interests.
    if (intent_plan is None and intent.get("activity_mode") == "single_venue"
            and (explicit_sources or preferred) and category not in (explicit_sources or preferred)):
        return False
    if intent_plan is None and preferred and category not in preferred and not (intent["architecture"] and architecture_evidence(place)):
        return False
    if place.get("source_tags", {}).get("access") in {"private", "no"}:
        return False
    for wanted, key in (("socket", "socket"), ("wifi", "wifi"), ("accessible", "wheelchair"), ("strict_quiet", "quiet")):
        if intent.get(wanted) and feature(place, key) != "yes":
            return False
    if not night_hours_eligible(place, night=intent["night"], allow_unknown_outdoor_hours=True,
                                require_confirmed_hours=intent.get("require_confirmed_hours", False),
                                schedule_when=schedule_when):
        return False
    if intent["child"] and actual_types & {"bar", "nightclub"}:
        return False
    if intent_plan is None and intent["architecture"] and category not in explicit_sources and not architecture_evidence(place):
        return False
    if intent_plan is None and not preferred and intent["outdoor"] and not intent["tourist"] and category not in {"park", "playground", "promenade", "monument", "attraction", "viewpoint", "theatre"}:
        return False
    return True



# An explicit period/date is not an injected exact visit instant. This bounded
# guard only WITHHOLDS current-time evaluation; it never parses a future date.
_NONCURRENT_TIME = re.compile(
    r"(?<!\w)(?:сегодня|завтра|послезавтра|позже|потом|today|tomorrow|tonight|later|"
    r"понедельник\w*|вторник\w*|сред[ауеы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|"
    r"январ\w*|феврал\w*|март[ае]?|апрел\w*|ма[йея]|июн\w*|июл\w*|август\w*|"
    r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)(?!\w)|"
    r"\b(?:следующ\w*|через)\s+|\b(?:в|at)\s+[0-9]{1,2}(?::[0-9]{2}|\s+час\w*)|"
    r"(?<![0-9])[0-9]{1,2}[./][0-9]{1,2}(?:[./][0-9]{2,4})?(?![0-9])|"
    r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])", re.I)


def _current_schedule_when(context, query, clock):
    if (context.get("time_of_day_source") not in {"astana_current_time", "client_current_time"}
            or _NONCURRENT_TIME.search(query)):
        return None
    try:
        when = clock()
        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            return None
        return when.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _opening_status(stop, when, duration_minutes):
    result = (evaluate_schedule(stop.get("opening_hours"), when, duration_minutes=duration_minutes)
              if when is not None else {"status": "unknown", "reason": "imprecise_visit_time"})
    notes = {
        "open": "Указанное расписание покрывает предложенное посещение; фактический доступ не проверен.",
        "closed": "Указанное расписание не покрывает предложенное посещение.",
        "unknown": "Расписание на время посещения не подтверждено; проверьте перед выходом.",
    }
    return {**result, "source": "source-reported", "note": notes[result["status"]]}


def _arrival_statuses(stops, legs, visit_each, schedule_when):
    elapsed = 0
    statuses = []
    for index, stop in enumerate(stops):
        elapsed += legs[index]["minutes"]
        try:
            when = schedule_when + timedelta(minutes=elapsed) if schedule_when is not None else None
        except OverflowError:
            when = None
        statuses.append(_opening_status(stop, when, visit_each))
        elapsed += visit_each
    return statuses


def _stop_reason(stop, option, intent):
    """Explain an existing choice from its own evidence and verified plan."""
    category = stop.get("category")
    spec = CATEGORY_CATALOG.get(category)
    labels = {"attraction":"достопримечательность", "monument":"памятник", "viewpoint":"видовая точка"}
    provider = {"osm":"OpenStreetMap", "dgis":"2ГИС", "google":"Google Maps"}[stop["provider"]]
    if spec or category in labels:
        label = spec["ru_label"].lower() if spec else labels[category]
        parts = ["Подходит по типу: " + label + ", по данным " + provider + "."]
    else:
        parts = ["Google Maps предложил это место рядом с вашим стартом."]
    for wanted, key, fact in (("socket","socket","розетки"), ("wifi","wifi","Wi-Fi"), ("accessible","wheelchair","доступный вход"),
                               ("strict_quiet","quiet","тишина")):
        if intent.get(wanted) and feature(stop,key) == "yes":
            parts.append("В источнике подтверждены: " + fact + ".")
    if intent.get("night") and stop.get("opening_hours") == "24/7":
        parts.append("В источнике указан круглосуточный режим.")
    plan = option["plan"]
    if option.get("route_built") and plan.get("verified") and plan.get("returns_to_origin"):
        parts.append("Пеший путь с возвращением проверен; план укладывается в " + str(option["budget_minutes"]) + " мин.")
    elif option.get("route_built") and plan.get("verified") and plan.get("reaches_destination"):
        parts.append("Пеший путь через это место до конечной точки проверен; план укладывается в " + str(option["budget_minutes"]) + " мин.")
    return " ".join(parts)


_AMENITY_LABELS = {"socket": "розетки", "wifi": "Wi-Fi", "quiet": "тишина"}


def _amenity_note(keys):
    labels = ", ".join(_AMENITY_LABELS[key] for key in keys)
    return "Не подтверждены: " + labels + ". Они могут быть на месте; уточните перед посещением."


def _amenity_notice(keys):
    labels = ", ".join(_AMENITY_LABELS[key] for key in keys)
    return ("Не удалось подобрать маршрут с подтверждением всех запрошенных удобств. "
            + "В этом варианте не подтверждены: " + labels
            + ". Вот места, подходящие по остальным условиям. Наличие удобств уточните перед посещением.")


def _requested_features(stop, original_plan, intent):
    """Own goal-scoped request projection; it grants no property evidence."""
    mapping = {"socket": "socket", "wifi": "wifi", "quiet": "quiet", "accessible": "wheelchair"}
    if original_plan is not None:
        active = matched_goal_indices(stop, relax_amenities(original_plan) or original_plan)
        if "fulfilled_goal_indices" in stop:
            active = tuple(index for index in active if index in stop["fulfilled_goal_indices"])
        keys = {item.key for item in original_plan.requirements
                if item.goal_index is None or item.goal_index in active}
    else:
        keys = {key for key, flags in {"socket": ("socket",), "wifi": ("wifi",),
                "quiet": ("quiet", "strict_quiet"), "accessible": ("accessible",)}.items()
                if any(intent.get(flag) for flag in flags)}
    return [value for key, value in mapping.items() if key in keys]


def _source_description(stop):
    """A proposed activity from this exact source type, never a model answer."""
    spec = CATEGORY_CATALOG.get(stop.get("category"))
    if spec:
        activity = spec["proposed_activity"]
        if activity.startswith("Предложить "):
            activity = activity[len("Предложить "):]
            if not activity.startswith(("проверить ", "уточнить ", "посмотреть ", "зайти ")):
                activity = "запланировать " + activity
    else:
        # An unmapped source rubric establishes no catalogue-specific activity.
        activity = "запланировать посещение после проверки условий."
    name = stop.get("name")
    name = name if isinstance(name, str) and name.strip() else "Выбранное место"
    # The full original name remains on the card and in its source reference.
    label = name if len(name) <= 160 else name[:159] + "…"
    text = label + ". Идея: " + activity
    if stop.get("opening_hours") == "24/7":
        provider = {"osm": "OpenStreetMap", "dgis": "2ГИС", "google": "Google Maps"}[stop["provider"]]
        text += " В источнике " + provider + " указан круглосуточный режим."
    return text


def _stop(place, provider, source_id):
    # Observation time comes from this record's actual collection, never now.
    stamp = place.get("collected_at")
    evidence = []
    if stamp is not None:
        try:
            projection = normalize_place_evidence(place, provider=provider, place_id=place["id"],
                source_url=place.get("source_url"), observed_at=stamp)
            evidence = projection["evidence"]
        except PlaceEvidenceError:
            stamp = None  # No fabricated provenance for legacy/invalid metadata.
    tags = place.get("source_tags", {})
    # Only bounded own type/access proof survives the public stop projection.
    # Do not forward addresses, full source tags or arbitrary provider bodies.
    type_tags = {key: value for key, value in tags.items()
                 if key in {"historic", "building:architecture", "architectural_style", "amenity", "leisure", "tourism", "shop", "sport", "access"}
                 and isinstance(value, str) and 1 <= len(value) <= 200} if isinstance(tags, dict) else {}
    return {"source_tags": type_tags, "feature_evidence": evidence, "source_observed_at": stamp,"stop_id": provider + ":" + place["id"], "id": place["id"], "source_id": source_id,
            "provider": provider, **{key: deepcopy(place.get(key)) for key in
            ("name", "category", "description", "features", "opening_hours", "source_url", "lat", "lon",
             "source_categories", "source_rubrics", "source_types", "source_type")}}


def _evidence(stop):
    return {key: deepcopy(stop[key]) for key in ("source_id", "id", "name", "category", "source_url", "provider")}


class UnifiedWalks:
    def __init__(self, resolver, osm_retriever, google_client, dgis, *, llm_config=None, narrator=None, route_map=None, preparation=None, gateway=None, clock=None):
        self.resolver, self.osm, self.google, self.dgis = resolver, osm_retriever, google_client, dgis
        self.llm_config, self.narrator, self.route_map = llm_config, narrator or narrate, route_map
        self.preparation, self.gateway = preparation, gateway
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._custom_narrator = narrator is not None
        self._slots = BoundedSemaphore(1)
        self._last_workflow = None

    def status(self):
        return {"google": self.google.status() if self.google else {"enabled": False},
                "dgis": self.dgis.status() if self.dgis else {"configured": False, "available": False},
                "map": self.route_map.status() if self.route_map else {"configured": False},
                "response_storage": "request-memory-only", "last_completed_workflow": deepcopy(self._last_workflow)}

    def _preflight(self):
        if self.google is None:
            raise GoogleError("unified_google_unconfigured", "Общий поиск требует настроенного Google API.", 503)
        quota = self.google.budget.status()
        if quota.get("state") != "ready":
            raise GoogleError("quota_exhausted" if quota.get("state") == "exhausted" else "quota_unavailable", "Общий лимит Google недоступен или исчерпан; поиск не выполнен.", 429 if quota.get("state") == "exhausted" else 503)
        if self.dgis is None:
            return "not_configured"
        try:
            status = self.dgis.status()
            if isinstance(status, dict) and status.get("available") is True:
                return None
            code = status.get("code") if isinstance(status, dict) else None
            return code if code in {"not_configured", "quota_exhausted"} else "provider_unavailable"
        except Exception:
            return "provider_unavailable"

    def recommend(self, payload):
        # Strict gate precedes status, retrieval, executors, all HTTP and LLM.
        prepared = self.preparation.consume(payload) if self.preparation else None
        form = prepared.form if prepared else ({key: value for key, value in payload.items() if key != "consent"} if isinstance(payload, dict) else payload)
        resolved = prepared.resolved if prepared else self.resolver(form)
        if not isinstance(payload, dict) or payload.get("consent") is not True:
            raise GoogleError("consent_required", "Нужно согласие на общий поиск в Google и 2ГИС.", 400)
        dgis_unavailable = self._preflight()
        if not self._slots.acquire(blocking=False):
            raise GoogleError("unified_busy", "Предыдущий общий поиск ещё выполняется.", 429)
        try:
            if prepared is None:
                descriptor = validate_walking_request(resolved["query"], resolved["resolved_context"]["budget_options"])
                if descriptor is not None:
                    if descriptor.get("origin") is None or descriptor.get("destination") is None:
                        raise GoogleError("route_preparation_required", "Конечные точки маршрута требуют подтверждения; запустите обычный подбор через подготовку запроса.", 422)
                    resolved = prepare_walking_baseline(resolved, descriptor, self.google, deadline=monotonic() + 120)
            _baseline(resolved)  # Fail before source workers if internal endpoint binding changed.
            response = self._recommend(form, resolved, prepared=prepared, dgis_unavailable=dgis_unavailable)
            self._last_workflow = {
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": response["mode"], "llm_status": response["diagnostics"]["llm_status"],
                "llm_attempts": response["diagnostics"]["llm_attempts"],
                "source_receipt": deepcopy(response["source_receipt"]),
                "route_statuses": [{"budget_status": item["budget_status"], "route_built": item["route_built"],
                                    "route_checks": deepcopy(item["plan"].get("route_checks", []))}
                                   for item in response["alternatives"]]}
            return response
        finally:
            self._slots.release()

    def _recommend(self, form, resolved, *, prepared=None, dgis_unavailable=None):
        # prepare reuses this exact already validated context, not a second clock.
        excluded_names = excluded_place_names(resolved["query"])
        proven_exclusions = proven_place_exclusions(resolved["query"])
        intent_plan = _without_named_exclusions(prepared.intent_plan if prepared else None, proven_exclusions)
        try:
            staged_plan = intent_plan or deterministic_plan(resolved["query"])
            ordered_plan, goal_groups = ordered_goals(resolved["query"], staged_plan)
            if goal_groups:
                intent_plan = ordered_plan
        except IntentError:
            goal_groups = ()
        context, safe_intent, _, _, parameters = prepare(form, lambda _: resolved, **({"intent_plan": intent_plan} if intent_plan else {}))
        context = deepcopy(context)
        origin, query = resolved["origin"], resolved["query"]
        budgets = context["budget_options"]
        excluded_counts = {provider: 0 for provider in ("osm", "google", "dgis")}
        radius = parameters["locationBias"]["circle"]["radiusMeters"]
        intent = detect_intent(query, resolved["time_of_day"], **({"intent_plan": intent_plan} if intent_plan else {}))
        context["activity_mode"] = intent["activity_mode"]
        if goal_groups:
            context["ordered_goal_groups"] = [list(group) for group in goal_groups]
        structured_intent = intent.get("search_intent") or parse_search_intent(query)
        context["search_intent"] = deepcopy(structured_intent)
        # A witnessed "then" inside this walk is not a request for a later date.
        schedule_query = re.sub(r"(?<!\w)(?:потом|затем|then)(?!\w)", " ", query, flags=re.I) if goal_groups else query
        schedule_when = _current_schedule_when(context, schedule_query, self.clock)
        # Legacy callers receive the same bounded, deterministic interpretation;
        # this is never another model extraction or a changed original plan.
        try:
            original_plan = _without_named_exclusions(intent_plan or deterministic_plan(query), proven_exclusions)
            alternate_plan = relax_amenities(original_plan)
        except IntentError:
            original_plan, alternate_plan = None, None
        alternate_intent = (detect_intent(query, resolved["time_of_day"], intent_plan=alternate_plan)
                            if alternate_plan is not None else None)
        phase_started = monotonic()
        google_seconds = min(60, prepared.deadline - phase_started - 3) if prepared else 60
        source_deadline = min(phase_started + 30, prepared.deadline - 3) if prepared else phase_started + 30
        if google_seconds <= 0 or source_deadline <= phase_started:
            raise GoogleError("context_timeout", "Истекло время подготовки маршрута.", 504)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unified-source")
        adapter_options = {"intent_plan": intent_plan} if intent_plan else {}
        attempts = {"osm": 0, "google": 1, "dgis": 0}
        def source_call(provider, operation, *args, **kwargs):
            if monotonic() >= source_deadline:
                raise TimeoutError("Source phase deadline")
            attempts[provider] = 1
            return operation(*args, **kwargs)
        def local_search():
            options = dict(allow_unknown_outdoor_hours=True, schedule_when=schedule_when, deadline=source_deadline)
            value = self.osm.retrieve(query, origin, max(budgets), resolved["time_of_day"], **options, **adapter_options)
            if alternate_plan is not None:
                # Same loaded finite corpus, with the same absolute deadline.
                # No HTTP, extra extraction, source search or response storage.
                alternate = self.osm.retrieve(query, origin, max(budgets), resolved["time_of_day"],
                                              **options, intent_plan=alternate_plan)
                value = {**value, "fallback_selected": alternate["selected"]}
            return value
        local_future = executor.submit(source_call, "osm", local_search)
        dgis_options = {"include_amenity_fallback": True} if alternate_plan is not None else {}
        dgis_future = (executor.submit(source_call, "dgis", self.dgis.search, query, origin, radius,
                                       deadline=source_deadline, **adapter_options, **dgis_options) if dgis_unavailable is None else None)
        failures = {"dgis": dgis_unavailable} if dgis_unavailable else {}
        try:
            with self.google.workflow(google_seconds) if hasattr(self.google, "workflow") else nullcontext():
                grounding = validate_grounding(self.google.search_places(parameters))
                def optional_result(future, provider, key):
                    if future is None:
                        return {key: []}
                    try:
                        value = future.result(timeout=max(0, source_deadline - monotonic()))
                        if (not isinstance(value, dict) or not isinstance(value.get(key), list)
                                or len(value[key]) > 5000 or any(not isinstance(item, dict) for item in value[key])):
                            raise ValueError("Invalid optional source shape")
                        fallback_key = "fallback_selected" if provider == "osm" else "fallback_places"
                        fallback = value.get(fallback_key, [])
                        if (not isinstance(fallback, list) or len(fallback) > 5000
                                or any(not isinstance(item, dict) for item in fallback)):
                            raise ValueError("Invalid alternate source shape")
                        if provider == "osm" and any(not isinstance(item.get("poi"), dict) for item in value[key] + fallback):
                            raise ValueError("Invalid local source shape")
                        return value
                    except TimeoutError:
                        future.cancel()
                        failures[provider] = "source_timeout"
                        return {key: []}
                    except Exception:
                        failures[provider] = "provider_unavailable"
                        return {key: []}
                local = optional_result(local_future, "osm", "selected")
                live = optional_result(dgis_future, "dgis", "places")
                candidates = {"osm": [], "google": [], "dgis": []}
                alternate_candidates = {"osm": [], "google": [], "dgis": []}
                # Source IDs bind once to the strict+alternate source snapshot.
                # Conflicting duplicate identities reject the entire provider.
                for provider, records in (("osm", [item["poi"] for item in local["selected"] + local.get("fallback_selected", [])]),
                                           ("dgis", live["places"] + live.get("fallback_places", []))):
                    try:
                        own_candidates, own_alternates, identities = [], [], {}
                        for poi in records:
                            if (not isinstance(poi.get("id"), str) or not poi["id"]
                                    or not isinstance(poi.get("name"), str) or not poi["name"]
                                    or any(type(poi.get(key)) not in {int, float} or not math.isfinite(poi[key])
                                           for key in ("lat", "lon"))):
                                raise ValueError("Invalid optional place")
                            if poi["id"] in identities:
                                if identities[poi["id"]] != poi:
                                    raise ValueError("Conflicting source identity")
                                continue
                            index = len(identities)
                            identities[poi["id"]] = poi
                            if distance_m(origin, poi) > radius:
                                continue
                            if is_place_excluded(poi, excluded_names):
                                excluded_counts[provider] += 1
                                continue
                            stop = _stop(poi, provider, ("o" if provider == "osm" else "d") + str(index))
                            if _eligible(poi, intent, intent_plan, schedule_when=schedule_when):
                                own_candidates.append(stop)
                            elif (alternate_plan is not None and unconfirmed_amenities(poi, original_plan)
                                  and _eligible(poi, alternate_intent, alternate_plan, schedule_when=schedule_when)):
                                own_alternates.append(stop)
                        candidates[provider], alternate_candidates[provider] = own_candidates, own_alternates
                    except Exception:
                        failures[provider] = "provider_invalid_response"
                # Google summary remains whole. Only structured source metadata
                # may identify candidate places; never mine its prose for facts.
                hard_unknown = (intent["socket"] or intent.get("wifi") or intent["accessible"] or intent["strict_quiet"]
                                or intent["night"] or intent.get("require_confirmed_hours", False))
                # Query text is not venue-type evidence. Only a broad walk or
                # architectural overview can use untyped Google stops; explicit
                # venue types, exclusions and family constraints stay unverified.
                broad_google = (not structured_intent["categories"] or
                                set(structured_intent["categories"]) <= {"walk", "architecture"})
                semantic_broad = intent_plan is None or (not intent_plan.open_goal and
                    all(goal.fulfilment == "generic_walk" for goal in intent_plan.goals))
                if (not hard_unknown and broad_google and semantic_broad and not intent["child"]
                        and not intent["excluded_categories"]):
                    for source in grounding["sources"]:
                        location = source.get("location")
                        if not location:
                            continue
                        point = {"lat": location["latitude"], "lon": location["longitude"]}
                        distance = distance_m(origin, point)
                        if not 100 <= distance <= radius:
                            continue
                        poi = {"id": source["id"], "name": source.get("title") or "Место из Google Maps",
                               "category": "unknown", "description": "", "features": {}, "opening_hours": None,
                               "source_url": source["url"], **point}
                        if is_place_excluded(poi, excluded_names):
                            excluded_counts["google"] += 1
                            continue
                        candidates["google"].append(_stop(poi, "google", source["source_id"]))
                selected = self._select(candidates, origin, intent, goal_groups=goal_groups, intent_plan=intent_plan)
                route_memo = _baseline(resolved)
                alternatives = self._plans(selected, budgets, context, origin, schedule_when=schedule_when, route_memo=route_memo)
                missing_budgets = [option["budget_minutes"] for option in alternatives if not option["route_built"] or option["plan"].get("direct_only")]
                if alternate_plan is not None and missing_budgets and any(alternate_candidates.values()):
                    fallback_pool = ({provider: candidates[provider] + alternate_candidates[provider] for provider in candidates}
                                     if goal_groups else alternate_candidates)
                    alternate_selected = self._select(fallback_pool, origin, alternate_intent, goal_groups=goal_groups, intent_plan=alternate_plan, fallback_original_plan=original_plan)
                    fallback_options = self._plans(alternate_selected, missing_budgets, context, origin,
                                                   schedule_when=schedule_when, route_memo=route_memo)
                    replacements = {}
                    for option in fallback_options:
                        if not option["route_built"] or not option["stops"]:
                            continue
                        keys = set()
                        for index, stop in enumerate(option["stops"]):
                            own_group = goal_groups[index] if goal_groups else None
                            unknown = unconfirmed_amenities(stop, original_plan, goal_indices=own_group)
                            if not unknown:
                                if own_group and set(matched_goal_indices(stop, original_plan)) & set(own_group):
                                    continue  # Only a strict match of THIS stage is a strict sibling.
                                break  # Fail closed if source evidence changed.
                            stop["unconfirmed_amenities"] = list(unknown)
                            stop["amenity_note"] = _amenity_note(unknown)
                            keys.update(unknown)
                        else:
                            if not keys:
                                continue  # No empty/direct-only option can claim amenity fallback.
                            unknown = [key for key in AMENITY_KEYS if key in keys]
                            option["amenity_fallback"] = {"applied": True, "unconfirmed": unknown,
                                "notice": _amenity_notice(unknown)}
                            replacements[option["id"]] = option
                    alternatives = [replacements.get(option["id"], option) for option in alternatives]
        finally:
            # Drain the finite local scan and deadline-bound network adapter.
            # A Python thread timeout is not cancellation; never detach retries
            # or claim that shutdown can forcibly terminate an arbitrary worker.
            executor.shutdown(wait=True, cancel_futures=True)
        for option in alternatives:
            for stop in option["stops"]:
                if (intent["night"] and stop.get("category") in {"park", "promenade"}
                        and stop.get("opening_hours") in (None, "")):
                    stop["availability_note"] = "Ночной доступ и освещение не подтверждены; проверьте перед выходом."
                stop["requested_features"] = _requested_features(stop, original_plan, intent)
                stop["why_text"] = _stop_reason(stop, option, intent)
        selected_union = []
        seen = set()
        for option in alternatives:
            if option["budget_status"] != "fits":
                continue
            for stop in option["stops"]:
                if stop["stop_id"] not in seen:
                    selected_union.append(stop)
                    seen.add(stop["stop_id"])
        if excluded_names:
            context["place_exclusions"] = {"requested_count": len(excluded_names), "matched_source_records": sum(excluded_counts.values())}
        context["source_availability"] = {provider: "unavailable" if provider in failures else "completed"
                                          for provider in ("osm", "google", "dgis")}
        narration_plan = (alternate_plan if any(option.get("amenity_fallback") for option in alternatives) else intent_plan)
        answer, llm_status, llm_attempts = None, "no_verified_plan", 0
        if selected_union:
            if self.gateway and not self._custom_narrator:
                try:
                    deadline = prepared.deadline if prepared else monotonic() + 120
                    with self.gateway.session(deadline - 2, seconds=120):
                        answer, llm_status = self.narrator(grounding, selected_union, alternatives, context, safe_intent,
                            self.llm_config, intent_plan=narration_plan, http_open=self.gateway.open)
                except Exception:
                    answer, llm_status = None, "local_model_unavailable_or_invalid"
            else:
                answer, llm_status = self.narrator(grounding, selected_union, alternatives, context, safe_intent, self.llm_config)
            llm_attempts = int(llm_status not in {"local_model_unconfigured", "route_not_budget_verified",
                "invalid_grounding", "selected_source_missing", "context_exceeds_local_bound",
                "sensitive_origin_in_model_context", "invalid_narration_context", "no_verified_match"})
        # A rejected whole model answer never leaves a ready card blank. The
        # fallback is our source-type proposal, with its own provenance label.
        # Accepted descriptions replace only their exact stop ID, unchanged.
        descriptions = {item["stop_id"]: item["text"] for item in (answer or {}).get("venue_descriptions", [])
                        if isinstance(item, dict) and isinstance(item.get("stop_id"), str)
                        and isinstance(item.get("text"), str) and 1 <= len(item["text"]) <= 600}
        for option in alternatives:
            for stop in option["stops"]:
                stop["user_description"] = _source_description(stop)
                stop["description_origin"] = "source-template"
                if stop["stop_id"] in descriptions:
                    stop["user_description"] = descriptions[stop["stop_id"]]
                    stop["description_origin"] = "local-model"
                if stop.get("availability_note"):
                    # Older open tabs render user_description but do not know
                    # the new optional note. Keep the application notice there
                    # too; the new UI presents that exact suffix separately.
                    stop["user_description"] += " Примечание приложения: " + stop["availability_note"]
                    if stop["description_origin"] == "local-model":
                        stop["description_origin"] = "local-model-with-note"
        receipt = {}
        for provider in ("osm", "google", "dgis"):
            if provider in failures:
                receipt[provider] = {"status": "unavailable", "candidate_count": None, "selected_count": 0,
                                     "attempted": bool(attempts[provider]), "error_code": failures[provider]}
            else:
                total = len(candidates[provider]) + len(alternate_candidates[provider])
                receipt[provider] = {"status": "searched" if total else "no_data",
                                     "candidate_count": total,
                                     "selected_count": sum(stop["provider"] == provider for stop in selected_union)}
                if alternate_plan is not None:
                    receipt[provider].update(strict_candidate_count=len(candidates[provider]),
                                              alternative_candidate_count=len(alternate_candidates[provider]))
        if excluded_names:
            for provider in receipt:
                receipt[provider]["excluded_named_count"] = excluded_counts[provider]
        warnings = list(context["assumptions"]) + list(live.get("warnings", []))
        if excluded_names:
            warnings.append("Ранее названные места исключаются по полному названию из источника; история посещений не сохраняется.")
        if failures:
            warnings.append("Поиск выполнен не полностью: " + ", ".join(
                {"osm": "OpenStreetMap", "dgis": "2ГИС"}[provider] for provider in failures)
                + " недоступен. Использованы только ответившие источники.")
        if any(option["plan"].get("schedule_reason") == "reported_schedule_does_not_cover_interval" for option in alternatives):
            warnings.append("Часть остановок не включена: указанное расписание не покрывает предложенное посещение. Это не проверка фактического доступа.")
        if "интерактив" in query.casefold():
            warnings.append("Категория музея не подтверждает интерактивы, текущую выставку или возрастной допуск; эти условия требуют отдельной проверки.")
        warnings += [WALK_WARNING, "Время остановок предложено приложением; часы работы, доступ и свойства мест могут быть неизвестны.",
                     "Карта рассчитывает путь отдельно: её оценка может отличаться от суммы проверенных переходов."]
        if intent["activity_mode"] == "single_venue":
            direction = "путь до конечной точки через заведение" if context.get("route_kind") == "point_to_point" else "путь туда и обратно"
            warnings.append("Подбирается одно заведение запрошенного типа. Бюджет включает " + direction + "; оставшееся время посещения предложено приложением, возможность сеанса не подтверждена.")
            if not selected_union:
                warnings.append("Подтверждённых подходящих заведений в полученных данных не найдено; другие категории вместо них не подбирались.")
        if context.get("route_kind") == "point_to_point":
            warnings.append("Места проверены в ограниченной области возле старта; это не полный поиск вдоль всего пути.")
        secondary = structured_intent["categories"][1:]
        if intent["activity_mode"] == "single_venue" and secondary:
            labels = ", ".join(CATEGORY_CATALOG[value]["ru_label"] for value in secondary)
            warnings.append("План подбирается под основную цель. Дополнительные типы мест отдельно не проверены: " + labels + ".")
        if goal_groups and not selected_union:
            warnings.append("Полный план из запрошенных занятий по порядку не подтверждён; частичный маршрут не выдан за выполнение всего пожелания.")
        if answer is None:
            warnings.append("Единый текст ИИ не подтверждён. Показаны план и полный исходный ответ Google с источниками.")
        return {"mode": "llm-grounded-unified" if answer else "grounding-only", "resolved_context": context,
                "answer": answer, "grounding": grounding,
                "sources": {provider: [_evidence(stop) for stop in candidates[provider] + alternate_candidates[provider]] for provider in ("osm", "dgis")},
                "source_receipt": receipt, "search_complete": not failures, "alternatives": alternatives,
                "default_option_id": next((option["id"] for option in alternatives if option["budget_status"] == "fits"), alternatives[0]["id"]),
                "warnings": warnings, "diagnostics": {"llm_attempts": llm_attempts, "llm_status": llm_status,
                "response_storage": "request-memory-only", "searches": attempts,
                "intent_llm_attempts": prepared.intent_attempts if prepared else 0,
                "intent_status": prepared.intent_status if prepared else "deterministic",
                "place_resolution_attempts": prepared.place_attempts if prepared else 0}}

    @staticmethod
    def _select(candidates, origin, intent=None, *, goal_groups=(), intent_plan=None, fallback_original_plan=None):
        if goal_groups and intent_plan is not None:
            # Two cafés × three requested outdoor alternatives at most. Source
            # identities stay separate; these are own-goal membership witnesses.
            ordered = sorted((stop for items in candidates.values() for stop in items),
                             key=lambda stop: (distance_m(origin, stop), stop["stop_id"]))
            pool = []
            for stop in ordered:
                if not any(stop["name"].casefold() == other["name"].casefold()
                           and distance_m(stop, other) < 80 for other in pool):
                    matched = matched_goal_indices(stop, intent_plan)
                    if fallback_original_plan is not None:
                        strict = set(matched_goal_indices(stop, fallback_original_plan))
                        matched = tuple(index for index in matched if index in strict or
                            unconfirmed_amenities(stop, fallback_original_plan, goal_indices=(index,)))
                    pool.append({**stop, "matched_goal_indices": list(matched)})
            stages = [[stop for stop in pool if set(stop["matched_goal_indices"]) & set(group)]
                      for group in goal_groups]
            if any(not stage for stage in stages):
                return []
            chosen = stages[0][:2] + stages[1][:3]
            return list({stop["stop_id"]: stop for stop in chosen}.values())
        if intent and intent.get("activity_mode") == "single_venue":
            # Try at most three relevant alternatives, never one compulsory stop
            # per source. The planner evaluates each venue independently.
            ordered = sorted((stop for items in candidates.values() for stop in items),
                             key=lambda stop: (distance_m(origin, stop), stop["stop_id"]))
            unique = []
            for stop in ordered:
                if not any(stop["name"].casefold() == other["name"].casefold()
                           and distance_m(stop, other) < 80 for other in unique):
                    unique.append(stop)
                if len(unique) == 3:
                    break
            return unique
        # Relevant sources have equal access to three stops; proximity orders
        # the resulting walk. No rating/sponsor boosts or speculative merging.
        selected = []
        queues = {key: sorted(value, key=lambda stop: (distance_m(origin, stop), stop["stop_id"])) for key, value in candidates.items()}
        while len(selected) < 3 and any(queues.values()):
            for provider in ("osm", "google", "dgis"):
                if len(selected) == 3:
                    break
                while queues[provider]:
                    stop = queues[provider].pop(0)
                    if not any(stop["name"].casefold() == other["name"].casefold() and distance_m(stop, other) < 80 for other in selected):
                        selected.append(stop)
                        break
        ordered, last = [], origin
        while selected:
            chosen = min(selected, key=lambda stop: (distance_m(last, stop), stop["stop_id"]))
            selected.remove(chosen)
            ordered.append(chosen)
            last = chosen
        return ordered

    def _plans(self, selected, budgets, context, origin, *, schedule_when=None, route_memo=None):
        if context.get("ordered_goal_groups"):
            return self._ordered_plans(selected, budgets, context, origin,
                schedule_when=schedule_when, route_memo=route_memo)
        if context.get("route_kind") == "point_to_point":
            return self._point_to_point_plans(selected, budgets, context, origin,
                schedule_when=schedule_when, route_memo=route_memo)
        memo = route_memo if route_memo is not None else {}
        # Planner and Embed share this validated coordinate start. Public
        # landmark names can resolve to a different place and must not reroute it.
        anchor = {"latLng": {"latitude": origin["lat"], "longitude": origin["lon"]}}
        def leg(a, b, a_ref, b_ref):
            key = (a_ref, b_ref)
            if key not in memo:
                error_code = None
                try:
                    minutes, attribution = route_minutes(self.google.compute_route(
                        anchor if a_ref == "origin" else _point(a),
                        anchor if b_ref == "origin" else _point(b)))
                    if minutes is None:
                        error_code = "route_duration_unverified"
                except GoogleError:
                    minutes, attribution, error_code = None, None, "route_unavailable"
                memo[key] = {"from_ref": a_ref, "to_ref": b_ref, "minutes": minutes,
                             "attribution": attribution, "error_code": error_code}
            return memo[key]
        prefixes, route_checks, accepted = [], [], []
        outward, previous, ref = [], origin, "origin"
        largest_budget = max(budgets)
        single_venue = context.get("activity_mode") == "single_venue"
        for index, stop in enumerate(selected[:3]):
            outgoing = leg(previous, stop, ref, stop["stop_id"])
            returning = leg(stop, origin, stop["stop_id"], "origin")
            outgoing_verified = outgoing["minutes"] is not None
            return_verified = returning["minutes"] is not None
            verified = outgoing_verified and return_verified
            trial_legs = outward + [outgoing, returning]
            fits_largest = (verified and sum(item["minutes"] for item in trial_legs)
                            + 5 * (len(accepted) + 1) + 5 <= largest_budget)
            # A known closed minimum arrival never poisons the accepted path.
            # Each actual budget still rechecks its full proposed visit below.
            minimum_stops = [stop] if single_venue else accepted + [stop]
            minimum_statuses = _arrival_statuses(minimum_stops, trial_legs, 5, schedule_when) if verified else []
            schedule_closed = any(item["status"] == "closed" for item in minimum_statuses)
            error_code = (outgoing["error_code"] or returning["error_code"] if not verified else
                          "outside_reported_schedule" if schedule_closed else
                          None if fits_largest else "minimum_time_exceeds_largest_budget")
            route_checks.append({"candidate_index": index, "outgoing_verified": outgoing_verified,
                                 "return_verified": return_verified, "verified_legs": verified,
                                 "fits_largest_budget": fits_largest, "error_code": error_code})
            # A failed or infeasible candidate never changes the accepted path.
            # The next candidate starts at the last accepted stop, not this one.
            if not fits_largest or schedule_closed:
                continue
            if single_venue:
                prefixes.append(([deepcopy(stop)], deepcopy([outgoing, returning])))
            else:
                outward.append(outgoing)
                accepted.append(stop)
                prefixes.append((deepcopy(accepted), deepcopy(outward + [returning])))
                previous, ref = stop, stop["stop_id"]
        options = []
        for budget in budgets:
            schedule_rejected = any(check["error_code"] == "outside_reported_schedule" for check in route_checks)
            proposal = max(10, min(60, budget // 4)) if len(budgets) > 1 else 20
            fitting = []
            for stops, legs in prefixes:
                if any(item["minutes"] is None for item in legs):
                    continue
                walking = sum(item["minutes"] for item in legs)
                remaining = budget - walking - 5
                if remaining < 5 * len(stops):
                    continue
                visit_each = remaining if single_venue else min(proposal, remaining // len(stops))
                statuses = _arrival_statuses(stops, legs, visit_each, schedule_when)
                if any(item["status"] == "closed" for item in statuses):
                    schedule_rejected = True
                    continue
                fitting.append((stops, legs, walking, visit_each, statuses))
            status = "fits" if fitting else "no_candidates" if not selected else "unverified" if any(item["minutes"] is None for item in memo.values()) else "does_not_fit"
            chosen = min(fitting, key=lambda item: (item[2], item[0][0]["stop_id"])) if fitting and single_venue else fitting[-1] if fitting else None
            stops, legs, walking, visit_each, statuses = chosen if chosen else ([], [], None, 0, [])
            visits = visit_each * len(stops)
            plan = {"budget_minutes": budget, "verified": bool(fitting), "returns_to_origin": bool(fitting),
                    "schedule_reason": "reported_schedule_does_not_cover_interval" if schedule_rejected else None,
                    "walk_minutes": walking, "return_walk_minutes": legs[-1]["minutes"] if legs else None,
                    "visit_minutes": visits if fitting else None, "buffer_minutes": 5,
                    "total_minutes": walking + visits + 5 if fitting else None,
                    "visit_is_proposal": True, "time_allocation": "proposed_remaining_after_roundtrip" if single_venue else "proposed_stops",
                     "route_attributions": [item["attribution"] for item in legs if item["attribution"]],
                    "legs": legs, "route_checks": deepcopy(route_checks),
                    "verification_scope": "Google WALK durations, not opening, access or safety verification"}
            shown = [{**deepcopy(stop), "visit_minutes": visit_each, "opening_status": deepcopy(status)}
                     for stop, status in zip(stops, statuses)]
            ticket = self.route_map.issue(origin, shown, context=context) if shown and self.route_map else None
            options.append({"id": "budget-" + str(budget), "budget_minutes": budget,
                            "resolved_context": {**deepcopy(context), "minutes": budget}, "route_built": bool(fitting),
                            "budget_status": status, "plan": plan, "stops": shown, "map_ticket": ticket})
        return options


    def _point_to_point_plans(self, selected, budgets, context, origin, *, schedule_when=None, route_memo=None):
        """One bounded relevant stop between verified public endpoints, or travel only."""
        memo = route_memo if route_memo is not None else {}
        baseline = memo.get(("origin", "destination"))
        _baseline({"origin": origin, "resolved_context": context, "route_baseline": baseline})
        destination = context["destination"]
        def leg(a, b, a_ref, b_ref):
            key = (a_ref, b_ref)
            if key not in memo:
                try:
                    minutes, attribution = route_minutes(self.google.compute_route(_point(a), _point(b)))
                    error = None if minutes is not None else "route_duration_unverified"
                except GoogleError:
                    minutes, attribution, error = None, None, "route_unavailable"
                memo[key] = {"from_ref": a_ref, "to_ref": b_ref, "minutes": minutes,
                             "attribution": attribution, "error_code": error}
            return memo[key]
        choices, checks = [], []
        for index, stop in enumerate(selected[:3]):
            outgoing = leg(origin, stop, "origin", stop["stop_id"])
            final = leg(stop, destination, stop["stop_id"], "destination")
            verified = outgoing["minutes"] is not None and final["minutes"] is not None
            walking = outgoing["minutes"] + final["minutes"] if verified else None
            fits = verified and walking + 10 <= max(budgets)
            statuses = _arrival_statuses([stop], [outgoing, final], 5, schedule_when) if verified else []
            closed = any(item["status"] == "closed" for item in statuses)
            checks.append({"candidate_index": index, "outgoing_verified": outgoing["minutes"] is not None,
                "final_verified": final["minutes"] is not None, "verified_legs": verified,
                "fits_largest_budget": fits, "error_code": outgoing["error_code"] or final["error_code"]
                or ("outside_reported_schedule" if closed else None if fits else "minimum_time_exceeds_largest_budget")})
            if fits and not closed:
                choices.append((stop, [outgoing, final], walking))
        options = []
        for budget in budgets:
            fitting = []
            schedule_rejected = any(check["error_code"] == "outside_reported_schedule" for check in checks)
            proposal = max(10, min(60, budget // 4)) if len(budgets) > 1 else 20
            for stop, legs, walking in choices:
                remaining = budget - walking - 5
                if remaining < 5 or baseline["minutes"] + 5 > budget:
                    continue
                visit = remaining if context.get("activity_mode") == "single_venue" else min(proposal, remaining)
                statuses = _arrival_statuses([stop], legs, visit, schedule_when)
                if any(status["status"] == "closed" for status in statuses):
                    schedule_rejected = True
                    continue
                fitting.append((stop, legs, walking, visit, statuses[0]))
            built = bool(fitting) or baseline["minutes"] + 5 <= budget
            chosen = min(fitting, key=lambda item: (item[2], item[0]["stop_id"])) if fitting else None
            if chosen:
                stop, legs, walking, visit, opening = chosen
                shown = [{**deepcopy(stop), "visit_minutes": visit, "opening_status": deepcopy(opening)}]
            else:
                legs, walking, visit, shown = ([baseline], baseline["minutes"], 0, []) if built else ([], None, 0, [])
            direct_only = built and not shown
            plan = {"route_kind": "point_to_point", "destination": deepcopy(destination),
                "budget_minutes": budget, "verified": built, "reaches_destination": built,
                "returns_to_origin": False, "direct_only": direct_only,
                "walk_minutes": walking, "final_walk_minutes": legs[-1]["minutes"] if legs else None,
                "return_walk_minutes": None, "visit_minutes": visit if built else None, "buffer_minutes": 5,
                "total_minutes": walking + visit + 5 if built else None,
                "visit_is_proposal": True, "time_allocation": "verified_direct_walk" if direct_only else "proposed_stop_before_destination",
                "route_attributions": [deepcopy(item["attribution"]) for item in legs if item["attribution"]],
                "legs": deepcopy(legs), "route_checks": deepcopy(checks),
                "schedule_reason": "reported_schedule_does_not_cover_interval" if schedule_rejected else None,
                "verification_scope": "Google WALK durations, not opening, access or safety verification"}
            ticket = self.route_map.issue(origin, shown, context=context, plan=plan) if built and self.route_map else None
            option = {"id": "budget-" + str(budget), "budget_minutes": budget,
                "resolved_context": {**deepcopy(context), "minutes": budget}, "route_built": built,
                "budget_status": "fits" if built else "does_not_fit", "plan": plan, "stops": shown, "map_ticket": ticket}
            if direct_only:
                option["route_note"] = "Подходящую остановку по пути подтвердить не удалось. Показан только проверенный пеший путь до конечной точки; пожелание о месте или услуге не подтверждено."
            options.append(option)
        return options


    def _ordered_plans(self, selected, budgets, context, origin, *, schedule_when=None, route_memo=None):
        """Verify bounded complete two-stage choices; vary routes only when valid."""
        memo = route_memo if route_memo is not None else {}
        groups = context["ordered_goal_groups"]
        directed = context.get("route_kind") == "point_to_point"
        destination, final_ref = (context["destination"], "destination") if directed else (origin, "origin")
        if directed:
            _baseline({"origin": origin, "resolved_context": context, "route_baseline": memo.get(("origin", "destination"))})
        stages = [[stop for stop in selected if set(stop.get("matched_goal_indices", ())) & set(group)]
                  for group in groups]
        def leg(a, b, a_ref, b_ref):
            key = (a_ref, b_ref)
            if key not in memo:
                try:
                    minutes, attribution = route_minutes(self.google.compute_route(_point(a), _point(b)))
                    error = None if minutes is not None else "route_duration_unverified"
                except GoogleError:
                    minutes, attribution, error = None, None, "route_unavailable"
                memo[key] = {"from_ref": a_ref, "to_ref": b_ref, "minutes": minutes,
                             "attribution": attribution, "error_code": error}
            return memo[key]
        choices, checks = [], []
        for first in stages[0][:2]:
            for second in stages[1][:3]:
                if (first["stop_id"] == second["stop_id"] or first["name"].casefold() == second["name"].casefold()
                        and distance_m(first, second) < 80):
                    continue
                legs = [leg(origin, first, "origin", first["stop_id"]),
                        leg(first, second, first["stop_id"], second["stop_id"]),
                        leg(second, destination, second["stop_id"], final_ref)]
                verified = all(item["minutes"] is not None for item in legs)
                walking = sum(item["minutes"] for item in legs) if verified else None
                fits = verified and walking + 15 <= max(budgets)
                checks.append({"candidate_index": len(checks), "verified_legs": verified,
                    "fits_largest_budget": fits, "error_code": next((item["error_code"] for item in legs if item["error_code"]),
                    None if fits else "minimum_time_exceeds_largest_budget")})
                if fits:
                    choices.append(([first, second], legs, walking))
        options, used = [], set()
        for budget in budgets:
            fitting, schedule_rejected = [], False
            for stops, legs, walking in choices:
                available = budget - walking - 5
                if available < 10 or directed and context["direct_walk_minutes"] + 5 > budget:
                    continue
                visit = min(max(10, min(60, budget // 4)), available // 2)
                statuses = _arrival_statuses(stops, legs, visit, schedule_when)
                if any(item["status"] == "closed" for item in statuses):
                    schedule_rejected = True
                    continue
                fitting.append((stops, legs, walking, visit, statuses))
            fitting.sort(key=lambda item: (item[2], tuple(stop["stop_id"] for stop in item[0])))
            novel = [item for item in fitting if tuple(stop["stop_id"] for stop in item[0]) not in used]
            chosen = (novel or fitting)[0] if fitting else None
            if not chosen and directed:
                option = self._point_to_point_plans([], [budget], context, origin, schedule_when=schedule_when, route_memo=memo)[0]
                option["plan"]["route_checks"] = deepcopy(checks)
                option["plan"]["schedule_reason"] = "reported_schedule_does_not_cover_interval" if schedule_rejected else None
                options.append(option)
                continue
            stops, legs, walking, visit, statuses = chosen if chosen else ([], [], None, 0, [])
            identity = tuple(stop["stop_id"] for stop in stops)
            repeated = bool(chosen) and identity in used
            if chosen:
                used.add(identity)
            plan = {"budget_minutes": budget, "verified": bool(chosen), "returns_to_origin": bool(chosen),
                "ordered_goals_covered": bool(chosen), "walk_minutes": walking,
                "return_walk_minutes": legs[-1]["minutes"] if legs else None,
                "visit_minutes": 2 * visit if chosen else None, "buffer_minutes": 5,
                "total_minutes": walking + 2 * visit + 5 if chosen else None,
                "visit_is_proposal": True, "time_allocation": "proposed_ordered_stops",
                "route_attributions": [deepcopy(item["attribution"]) for item in legs if item["attribution"]],
                "legs": deepcopy(legs), "route_checks": deepcopy(checks),
                "schedule_reason": "reported_schedule_does_not_cover_interval" if schedule_rejected else None,
                "verification_scope": "Google WALK durations, not opening, access or safety verification"}
            if directed:
                plan.update(route_kind="point_to_point", reaches_destination=bool(chosen), returns_to_origin=False,
                            direct_only=False, destination=deepcopy(destination), final_walk_minutes=legs[-1]["minutes"] if legs else None,
                            return_walk_minutes=None)
            shown = [{**deepcopy(stop), "visit_minutes": visit, "opening_status": deepcopy(status),
                      "fulfilled_goal_indices": [index for index in stop.get("matched_goal_indices", ()) if index in group]}
                     for stop, status, group in zip(stops, statuses, groups)]
            ticket = (self.route_map.issue(origin, shown, context=context, **({"plan": plan} if directed else {}))
                      if shown and self.route_map else None)
            option = {"id": "budget-" + str(budget), "budget_minutes": budget,
                "resolved_context": {**deepcopy(context), "minutes": budget}, "route_built": bool(chosen),
                "budget_status": "fits" if chosen else "no_candidates" if not selected else "unverified" if any(
                    item["minutes"] is None for item in memo.values()) else "does_not_fit",
                "plan": plan, "stops": shown, "map_ticket": ticket}
            if repeated:
                option["route_note"] = "Другого полного маршрута в полученных данных не подтверждено. Здесь тот же путь; распределение времени показано выше."
            options.append(option)
        return options
