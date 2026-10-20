"""One live three-source search, directed walk plan and whole AI answer.

Provider content is request-scoped RAM only. No corpus/cache/archive writes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from threading import BoundedSemaphore

from .google_budget import GoogleError
from .google_grounded import prepare, route_minutes, validate_grounding, WALK_WARNING
from .retrieval import architecture_evidence, detect_intent, distance_m, feature
from .unified_narration import narrate
from .category_catalog import CATEGORY_CATALOG
from .activity_intent import parse_search_intent


def _point(place):
    if place.get("provider") == "google":
        return {"id": place["id"]}
    return {"latLng": {"latitude": place["lat"], "longitude": place["lon"]}}


def _source_classes(categories):
    result = set()
    for category in categories:
        if category == "walk":
            continue  # A broad walk is not a hard venue-type requirement.
        spec = CATEGORY_CATALOG.get(category)
        result.update(spec["source_categories"] if spec else (category,))
    return result


def _eligible(place, intent):
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
    if (intent.get("activity_mode") == "single_venue"
            and category not in (explicit_sources or preferred)):
        return False
    if preferred and category not in preferred and not (intent["architecture"] and architecture_evidence(place)):
        return False
    if place.get("source_tags", {}).get("access") in {"private", "no"}:
        return False
    for wanted, key in (("socket", "socket"), ("accessible", "wheelchair"), ("strict_quiet", "quiet")):
        if intent[wanted] and feature(place, key) != "yes":
            return False
    if intent["night"] and place.get("opening_hours") != "24/7":
        return False
    if intent["child"] and actual_types & {"bar", "nightclub"}:
        return False
    if intent["architecture"] and category not in explicit_sources and not architecture_evidence(place):
        return False
    if not preferred and intent["outdoor"] and not intent["tourist"] and category not in {"park", "playground", "promenade", "monument", "attraction", "viewpoint", "theatre"}:
        return False
    return True


def _stop(place, provider, source_id):
    return {"stop_id": provider + ":" + place["id"], "id": place["id"], "source_id": source_id,
            "provider": provider, **{key: deepcopy(place.get(key)) for key in
            ("name", "category", "description", "features", "opening_hours", "source_url", "lat", "lon")}}


def _evidence(stop):
    return {key: deepcopy(stop[key]) for key in ("source_id", "id", "name", "category", "source_url", "provider")}


class UnifiedWalks:
    def __init__(self, resolver, osm_retriever, google_client, dgis, *, llm_config=None, narrator=None, route_map=None):
        self.resolver, self.osm, self.google, self.dgis = resolver, osm_retriever, google_client, dgis
        self.llm_config, self.narrator, self.route_map = llm_config, narrator or narrate, route_map
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
        if self.dgis is None:
            raise GoogleError("unified_dgis_unconfigured", "Общий поиск требует настроенного 2ГИС и общего счётчика запросов.", 503)
        quota = self.google.budget.status()
        if quota.get("state") != "ready":
            raise GoogleError("quota_exhausted" if quota.get("state") == "exhausted" else "quota_unavailable", "Общий лимит Google недоступен или исчерпан; поиск не выполнен.", 429 if quota.get("state") == "exhausted" else 503)
        status = self.dgis.status()
        if not status.get("available"):
            raise GoogleError("unified_dgis_unavailable", "2ГИС недоступен или его общий лимит исчерпан; общий поиск не выполнен.", 503)

    def recommend(self, payload):
        # Strict gate precedes status, retrieval, executors, all HTTP and LLM.
        form = {key: value for key, value in payload.items() if key != "consent"} if isinstance(payload, dict) else payload
        resolved = self.resolver(form)
        if not isinstance(payload, dict) or payload.get("consent") is not True:
            raise GoogleError("consent_required", "Нужно согласие на общий поиск в Google и 2ГИС.", 400)
        self._preflight()
        if not self._slots.acquire(blocking=False):
            raise GoogleError("unified_busy", "Предыдущий общий поиск ещё выполняется.", 429)
        try:
            response = self._recommend(form, resolved)
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

    def _recommend(self, form, resolved):
        # prepare reuses this exact already validated context, not a second clock.
        context, safe_intent, _, _, parameters = prepare(form, lambda _: resolved)
        context = deepcopy(context)
        origin, query = resolved["origin"], resolved["query"]
        budgets = context["budget_options"]
        radius = parameters["locationBias"]["circle"]["radiusMeters"]
        intent = detect_intent(query, resolved["time_of_day"])
        context["activity_mode"] = intent["activity_mode"]
        structured_intent = intent.get("search_intent") or parse_search_intent(query)
        context["search_intent"] = deepcopy(structured_intent)
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unified-source")
        local_future = executor.submit(self.osm.retrieve, query, origin, max(budgets), resolved["time_of_day"])
        dgis_future = executor.submit(self.dgis.search, query, origin, radius)
        try:
            with self.google.workflow(60) if hasattr(self.google, "workflow") else nullcontext():
                grounding = validate_grounding(self.google.search_places(parameters))
                try:
                    local = local_future.result(timeout=30)
                    live = dgis_future.result(timeout=30)
                except Exception:
                    raise GoogleError("unified_provider_unavailable", "Один из источников не ответил. Общий ответ ИИ не создан; повторите позже.", 502) from None
                candidates = {"osm": [], "google": [], "dgis": []}
                for index, item in enumerate(local["selected"]):
                    poi = item["poi"]
                    if distance_m(origin, poi) <= radius and _eligible(poi, intent):
                        candidates["osm"].append(_stop(poi, "osm", "o" + str(index)))
                for index, poi in enumerate(live["places"]):
                    if distance_m(origin, poi) <= radius and _eligible(poi, intent):
                        candidates["dgis"].append(_stop(poi, "dgis", "d" + str(index)))
                # Google summary remains whole. Only structured source metadata
                # may identify candidate places; never mine its prose for facts.
                hard_unknown = intent["socket"] or intent["accessible"] or intent["strict_quiet"] or intent["night"]
                # Query text is not venue-type evidence. Only a broad walk or
                # architectural overview can use untyped Google stops; explicit
                # venue types, exclusions and family constraints stay unverified.
                broad_google = (not structured_intent["categories"] or
                                set(structured_intent["categories"]) <= {"walk", "architecture"})
                if (not hard_unknown and broad_google and not intent["child"]
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
                        candidates["google"].append(_stop(poi, "google", source["source_id"]))
                selected = self._select(candidates, origin, intent)
                alternatives = self._plans(selected, budgets, context, origin)
        finally:
            # Both adapter and local retrieval are bounded; never keep content
            # workers alive after the request or start a background retry.
            executor.shutdown(wait=True, cancel_futures=True)
        selected_union = []
        seen = set()
        for option in alternatives:
            if option["budget_status"] != "fits":
                continue
            for stop in option["stops"]:
                if stop["stop_id"] not in seen:
                    selected_union.append(stop)
                    seen.add(stop["stop_id"])
        answer, llm_status, llm_attempts = None, "no_verified_plan", 0
        if selected_union:
            answer, llm_status = self.narrator(grounding, selected_union, alternatives, context, safe_intent, self.llm_config)
            llm_attempts = int(llm_status not in {"local_model_unconfigured", "route_not_budget_verified",
                "invalid_grounding", "selected_source_missing", "context_exceeds_local_bound",
                "sensitive_origin_in_model_context", "invalid_narration_context"})
        receipt = {}
        for provider in ("osm", "google", "dgis"):
            receipt[provider] = {"status": "searched" if candidates[provider] else "no_data",
                                 "candidate_count": len(candidates[provider]),
                                 "selected_count": sum(stop["provider"] == provider for stop in selected_union)}
        warnings = list(context["assumptions"]) + list(live.get("warnings", []))
        if any(word in query.casefold() for word in ("вылет", "аэропорт", "рейс", "flight", "airport")):
            warnings.append("Дорога в аэропорт, регистрация, контроль и посадка не входят в бюджет прогулки. Два часа до вылета не означают два свободных часа; возможность выхода и запас до посадки проверьте самостоятельно.")
        if "интерактив" in query.casefold():
            warnings.append("Категория музея не подтверждает интерактивы, текущую выставку или возрастной допуск; эти условия требуют отдельной проверки.")
        warnings += [WALK_WARNING, "Время остановок предложено приложением; часы работы, доступ и свойства мест могут быть неизвестны.",
                     "Карта рассчитывает путь отдельно: её оценка может отличаться от суммы проверенных переходов."]
        if intent["activity_mode"] == "single_venue":
            warnings.append("Подбирается одно заведение запрошенного типа. Бюджет включает путь туда и обратно; оставшееся время посещения предложено приложением, возможность сеанса не подтверждена.")
            if not selected_union:
                warnings.append("Подтверждённых подходящих заведений в полученных данных не найдено; другие категории вместо них не подбирались.")
        secondary = structured_intent["categories"][1:]
        if intent["activity_mode"] == "single_venue" and secondary:
            labels = ", ".join(CATEGORY_CATALOG[value]["ru_label"] for value in secondary)
            warnings.append("План подбирается под основную цель. Дополнительные типы мест отдельно не проверены: " + labels + ".")
        if answer is None:
            warnings.append("Единый текст ИИ не подтверждён. Показаны план и полный исходный ответ Google с источниками.")
        return {"mode": "llm-grounded-unified" if answer else "grounding-only", "resolved_context": context,
                "answer": answer, "grounding": grounding,
                "sources": {provider: [_evidence(stop) for stop in candidates[provider]] for provider in ("osm", "dgis")},
                "source_receipt": receipt, "alternatives": alternatives,
                "default_option_id": next((option["id"] for option in alternatives if option["budget_status"] == "fits"), alternatives[0]["id"]),
                "warnings": warnings, "diagnostics": {"llm_attempts": llm_attempts, "llm_status": llm_status,
                "response_storage": "request-memory-only", "searches": {"osm": 1, "google": 1, "dgis": 1}}}

    @staticmethod
    def _select(candidates, origin, intent=None):
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

    def _plans(self, selected, budgets, context, origin):
        memo = {}
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
            error_code = (outgoing["error_code"] or returning["error_code"] if not verified else
                          None if fits_largest else "minimum_time_exceeds_largest_budget")
            route_checks.append({"candidate_index": index, "outgoing_verified": outgoing_verified,
                                 "return_verified": return_verified, "verified_legs": verified,
                                 "fits_largest_budget": fits_largest, "error_code": error_code})
            # A failed or infeasible candidate never changes the accepted path.
            # The next candidate starts at the last accepted stop, not this one.
            if not fits_largest:
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
                fitting.append((stops, legs, walking, visit_each))
            status = "fits" if fitting else "no_candidates" if not selected else "unverified" if any(item["minutes"] is None for item in memo.values()) else "does_not_fit"
            chosen = min(fitting, key=lambda item: (item[2], item[0][0]["stop_id"])) if fitting and single_venue else fitting[-1] if fitting else None
            stops, legs, walking, visit_each = chosen if chosen else ([], [], None, 0)
            visits = visit_each * len(stops)
            plan = {"budget_minutes": budget, "verified": bool(fitting), "returns_to_origin": bool(fitting),
                    "walk_minutes": walking, "return_walk_minutes": legs[-1]["minutes"] if legs else None,
                    "visit_minutes": visits if fitting else None, "buffer_minutes": 5,
                    "total_minutes": walking + visits + 5 if fitting else None,
                    "visit_is_proposal": True, "time_allocation": "proposed_remaining_after_roundtrip" if single_venue else "proposed_stops",
                     "route_attributions": [item["attribution"] for item in legs if item["attribution"]],
                    "legs": legs, "route_checks": deepcopy(route_checks),
                    "verification_scope": "Google WALK durations, not opening, access or safety verification"}
            shown = [{**deepcopy(stop), "visit_minutes": visit_each} for stop in stops]
            ticket = self.route_map.issue(origin, shown, context=context) if shown and self.route_map else None
            options.append({"id": "budget-" + str(budget), "budget_minutes": budget,
                            "resolved_context": {**deepcopy(context), "minutes": budget}, "route_built": bool(fitting),
                            "budget_status": status, "plan": plan, "stops": shown, "map_ticket": ticket})
        return options
