"""Ephemeral preparation: one intent, one verified origin, one-use RAM ticket.

Nothing here writes a query, coordinate, provider record or ticket to disk.
The public token is a random handle, not an encoded copy of personal context.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import re
import secrets
from threading import Lock
from time import monotonic
import unicodedata

from .google_budget import GoogleError
from .google_client import json_bytes
from .lab_context import ContextError
from .semantic_intent import deterministic_plan, explicit_start_quote, IntentError
from .route_request import validate_walking_request, walking_waypoints, attach_walking_baseline, walking_bindings, _walking_binding, _endpoint


def prepare_walking_baseline(resolved, descriptor, google, *, deadline, clock=monotonic):
    """One Google WALK, before extraction; reusable by the legacy engine path.

    Client transport accounts for the request in the existing shared pilot.
    No baseline, provider body, query or endpoint is persisted by this helper.
    """
    origin, destination = walking_waypoints(descriptor)
    remaining = deadline - clock()
    if remaining <= 0:
        raise GoogleError("direct_route_timeout", "Истекло время проверки прямого пешего маршрута; подбор мест не выполнен.", 504)
    compute = getattr(google, "compute_route", None) if google is not None else None
    if not callable(compute):
        raise GoogleError("direct_route_unavailable", "Проверка прямого пешего маршрута временно недоступна; подбор мест не выполнен.", 503)
    workflow = getattr(google, "workflow", None)
    try:
        manager = workflow(min(60, remaining)) if callable(workflow) else nullcontext()
        with manager:
            response = compute(origin, destination)
        if clock() >= deadline:
            raise GoogleError("direct_route_timeout", "Истекло время проверки прямого пешего маршрута; подбор мест не выполнен.", 504)
    except GoogleError as error:
        if error.code == "direct_route_timeout":
            raise
        status = error.status if error.status in {429, 503, 504} else 502
        raise GoogleError("direct_route_unavailable", "Не удалось проверить прямой пеший маршрут между указанными точками; подбор мест не выполнен.", status) from None
    except (TimeoutError, OSError):
        raise GoogleError("direct_route_timeout", "Истекло время проверки прямого пешего маршрута; подбор мест не выполнен.", 504) from None
    return attach_walking_baseline(resolved, descriptor, response)


@dataclass(frozen=True)
class PreparedRequest:
    form: dict
    resolved: dict
    intent_plan: object
    deadline: float
    intent_status: str
    intent_attempts: int
    place_attempts: int
    route_state: dict | None = None


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(form):
    return _digest({key: value for key, value in form.items() if key not in {"geolocation", "location_status"}})


def _place_terms(value):
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    stop = {"я", "сейчас", "нахожусь", "стою", "около", "возле", "рядом", "с", "у", "в", "на", "от",
            "здание", "здания", "здании", "улица", "улице", "улицы", "ул", "проспект", "проспекте",
            "астана", "астане", "казахстан", "astana", "kazakhstan", "street", "avenue", "near", "at", "the",
            "отель", "отеля", "отеле", "гостиница", "гостиницы", "гостинице", "тц", "трц",
            "торговый", "торгового", "торговом", "центр", "центра", "центре",
            "mall", "malls", "shopping", "center", "centre", "centers", "centres"}
    result = []
    for word in re.findall(r"[\w]+", text):
        if word in stop or (len(word) < 2 and not word.isdecimal()):
            continue
        if len(word) > 5:
            word = re.sub(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ой|ей|ов|ев|ах|ях|ом|ем|ам|ям|ы|и|а|я|у|ю|е)$", "", word)
        result.append(word)
    return result


def _latin_name(word):
    # Systematic spelling normalization, not a table of venue aliases or fuzzy rank.
    alphabet = dict(zip("абвгдеёзийклмнопрстуфыэ", "abvgdeezijklmnoprstufye"))
    alphabet.update({"ж":"zh", "х":"kh", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"shch",
                     "ю":"yu", "я":"ya", "ь":"", "ъ":"", "й":"y", "ә":"a", "і":"i",
                     "қ":"q", "ғ":"gh", "ұ":"u", "ү":"u", "ө":"o", "ң":"ng"})
    return "".join(alphabet.get(character, character) for character in word)


def unique_place(candidates, quote):
    """Select by actual name/address agreement, never by the first ranked hit."""
    needed = _place_terms(quote)
    # A numbered street plus a complete house number is a real address
    # identity, unlike a bare '7'. Require its actual ordinal street phrase
    # in the returned source too, not just coincident numbers in a name.
    ordinal = re.fullmatch(r"(?P<street>\d{1,4}-(?:й|я|го|ой|ая)\s+"
        r"(?:улиц(?:а|ы|е|у)|проспект(?:а|е)?))\s+\d{1,4}[a-zа-я]?(?:[/\-]\d+[a-zа-я]?)?",quote.casefold())
    if (not needed or not isinstance(candidates,list)
            or not ordinal and not any(not term.isdecimal() and len(term)>=3 for term in needed)):
        return None
    numbers = set(re.findall(r"(?<!\w)\d+[a-zа-яё]?\d*(?:[/\-][\da-zа-яё]+)*", quote.casefold()))
    matched = {}
    for place in candidates:
        if not isinstance(place, dict):
            continue
        name_address = place.get("name", "") + " " + place.get("address", "")
        if ordinal and re.search(r"(?<!\w)"+re.escape(ordinal["street"])+r"(?!\w)",name_address.casefold()) is None:
            continue
        actual_numbers = set(re.findall(r"(?<!\w)\d+[a-zа-яё]?\d*(?:[/\-][\da-zа-яё]+)*", name_address.casefold()))
        if not numbers.issubset(actual_numbers):
            continue
        actual = _place_terms(name_address)
        def agrees(term):
            if term.isdecimal():
                return term in actual
            term = _latin_name(term)
            return any(term == _latin_name(word) for word in actual)
        if all(agrees(term) for term in needed):
            matched[place.get("id")] = place
    return next(iter(matched.values())) if len(matched) == 1 else None


class RequestPreparation:
    def __init__(self, resolver, stager, google, gateway, *, clock=monotonic, lifetime=200, capacity=128):
        self.resolver, self.stager, self.google, self.gateway = resolver, stager, google, gateway
        self.clock, self.lifetime, self.capacity = clock, lifetime, capacity
        self._lock, self._entries = Lock(), {}

    @staticmethod
    def _form(payload):
        if not isinstance(payload, dict) or payload.get("consent") is not True:
            raise GoogleError("consent_required", "Нужно согласие на поиск мест рядом.", 400)
        return {key: value for key, value in payload.items() if key not in {"consent", "preparation_token"}}

    def _purge(self):
        now = self.clock()
        self._entries = {token: entry for token, entry in self._entries.items() if entry["prepared"].deadline > now}

    @staticmethod
    def _invalid():
        return GoogleError("invalid_preparation", "Подготовленный запрос изменился или устарел. Запустите подбор снова.", 400)

    def _origin(self, form, plan, deadline, previous=None):
        quote = plan.start_quote if plan.extraction_mode == "semantic" else explicit_start_quote(form["query"])
        options = {"start_quote": quote} if plan.extraction_mode == "semantic" or quote is not None else {}
        if previous and previous.resolved.get("origin") is not None:
            return deepcopy(previous.resolved), previous.place_attempts
        draft = self.stager(form, **options)
        if not draft.get("needs_place_resolution"):
            return draft, previous.place_attempts if previous else 0
        quote = draft.get("unresolved_start_quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ContextError("Не удалось определить указанное место старта.", code="location_unresolved", status=422)
        if not _place_terms(quote):
            # Mentioning only Astana specifies the supported city, not a start point.
            return self.stager(form, start_quote=None), 0
        if self.google is None or not hasattr(self.google, "resolve_place"):
            raise GoogleError("place_resolution_unavailable", "Поиск названного места временно недоступен.", 503)
        candidates = self.google.resolve_place(quote, deadline=deadline)
        place = unique_place(candidates, quote)
        if place is None:
            raise ContextError("По указанному названию нет однозначного места в Астане. Подбор мест не выполнен.",
                               code="location_unresolved", status=422, fields=("origin",))
        verified = {"origin": {"lat": place["lat"], "lon": place["lon"], "label": place["name"]},
                    "origin_source": "google_places_text_search", "origin_reference": place["source_url"], "origin_id": None}
        resolved = self.stager(form, verified_start=verified, **options)
        resolved["resolved_context"]["origin_attributions"] = deepcopy(place.get("attributions", []))
        return resolved, 1

    def _extract(self, form, deadline):
        query=form['query'].strip()
        plan,status,attempts=self.gateway.extract(query,deadline) if self.gateway else (None,'unconfigured',0)
        if plan is None:
            try:plan=deterministic_plan(query)
            except IntentError:
                if status=='deadline':raise GoogleError('intent_timeout','Разбор пожелания занял слишком много времени. Попробуйте снова.',504) from None
                if status in {'invalid_intent','invalid_response','provider_json','content_json','truncated_response'}:
                    raise GoogleError('intent_not_understood','Не удалось надёжно разобрать пожелание. Поиск мест не запускался.',422) from None
                raise GoogleError('intent_temporarily_unavailable','Не удалось обработать пожелание: локальный ИИ временно недоступен. Попробуйте позже.',503) from None
        if self.clock()>=deadline:raise GoogleError('context_timeout','Истекло время подготовки запроса.',504)
        return plan,status,attempts

    def _route_draft(self, form, route):
        spec=route['origin_spec']
        quote=spec['quote'] if spec['kind']!='current' else explicit_start_quote(form['query'])
        try:return self.stager(form,start_quote=quote)
        except ContextError as error:
            if (quote is not None and error.code=='invalid_request' and error.fields==['origin']
                    and str(error)=='Выбранный фрагмент не подтверждает текущий старт.'):
                raise GoogleError('route_endpoints_unresolved','Фрагмент места маршрута неоднозначен; подбор мест не выполнялся.',422) from None
            raise

    def _route_places(self, route, draft, deadline):
        """Resolve each endpoint at most once; never choose a ranked first hit."""
        from .lab_context import PUBLIC_ORIGINS
        state=deepcopy(route);count=0
        for prefix in ('origin','destination'):
            if state.get(prefix) is not None:continue
            spec=state[prefix+'_spec']
            quote=spec['quote']
            if prefix=='origin' and spec['kind']=='current':
                context=draft['resolved_context']
                if draft.get('needs_geolocation'):
                    continue
                if draft.get('needs_place_resolution'):
                    quote=draft.get('unresolved_start_quote')
                else:
                    identifier=context.get('origin_id');source=context.get('origin_source')
                    if identifier in PUBLIC_ORIGINS or source in {'browser_geolocation','explicit_coordinates'}:
                        for suffix in ('','_id','_source','_reference','_place_id','_attributions'):
                            key=prefix+suffix
                            if key in context:state[key]=deepcopy(context[key])
                        continue
                    quote=explicit_start_quote(draft['query'])
            if not isinstance(quote,str) or not quote.strip():
                raise GoogleError('route_endpoints_unresolved','Место маршрута не определено однозначно; подбор мест не выполнялся.',422)
            if prefix=='origin' and spec['kind']=='current':
                # A recovered location ROLE still is not an actual place name.
                # Apply the same endpoint admission as an explicit A-to-B pair.
                _endpoint(quote,origin=True)
            if self.clock()>=deadline:raise GoogleError('direct_route_timeout','Истекло время подготовки пешего маршрута.',504)
            resolver=getattr(self.google,'resolve_place',None) if self.google is not None else None
            if not callable(resolver):raise GoogleError('route_endpoint_unavailable','Подтверждение адресов маршрута временно недоступно.',503)
            try:candidates=resolver(quote,deadline=deadline)
            except GoogleError as error:
                raise GoogleError('route_endpoint_unavailable','Не удалось подтвердить место маршрута; подбор мест не выполнялся.',error.status if error.status in {429,503,504} else 502) from None
            except (OSError,TimeoutError):
                raise GoogleError('route_endpoint_unavailable','Подтверждение места маршрута временно недоступно; подбор мест не выполнялся.',503) from None
            count+=1
            if self.clock()>=deadline:raise GoogleError('direct_route_timeout','Истекло время подтверждения мест маршрута.',504)
            candidate=unique_place(candidates,quote)
            if candidate is None:raise GoogleError('route_endpoints_unresolved','По названию или адресу нет одного подтверждённого места в Астане; подбор мест не выполнялся.',422)
            state.update({prefix:{'lat':candidate.get('lat'),'lon':candidate.get('lon'),'label':candidate.get('name')},
                prefix+'_id':None,prefix+'_source':'google_places_text_search',prefix+'_place_id':candidate.get('id'),
                prefix+'_reference':candidate.get('source_url'),prefix+'_attributions':deepcopy(candidate.get('attributions',[]))})
            try:_walking_binding(state,prefix)
            except (ValueError,TypeError):raise GoogleError('route_endpoints_unresolved','Место маршрута не подтверждено в области Астаны; подбор мест не выполнялся.',422) from None
        if state.get('origin') is not None:
            try:walking_bindings(state)
            except (ValueError,TypeError):raise GoogleError('route_endpoints_unresolved','Места маршрута не подтверждены в области Астаны; подбор мест не выполнялся.',422) from None
        else:
            try:_walking_binding(state,'destination')
            except (ValueError,TypeError):raise GoogleError('route_endpoints_unresolved','Конечная точка маршрута не подтверждена в области Астаны.',422) from None
        return state,count

    def _complete_route(self, form, draft, route, deadline, place_attempts=0):
        route,count=self._route_places(route,draft,deadline)
        if route.get('origin') is None:
            pending=deepcopy(draft)
            pending['resolved_context'].update(route_kind='point_to_point',preparation_phase='awaiting_origin',
                destination=deepcopy(route['destination']),destination_id=route.get('destination_id'),
                destination_source=route['destination_source'],destination_reference=route.get('destination_reference'),
                destination_place_id=route.get('destination_place_id'),sensitive_origin=True,
                budget_mode=route.get('budget_mode','total'))
            if route.get('budget_mode')=='extra':pending['resolved_context']['extra_budget_options']=deepcopy(route['extra_budget_options'])
            return PreparedRequest(deepcopy(form),pending,None,deadline,'awaiting_origin',0,place_attempts+count,route)
        first,last=walking_bindings(route)
        if all(round(first[key],7)==round(last[key],7) for key in ('lat','lon')):
            raise GoogleError('route_same_endpoint','Начальная и конечная точки совпадают; отдельный пеший маршрут не рассчитывался.',422)
        resolved=prepare_walking_baseline(draft,route,self.google,deadline=deadline,clock=self.clock)
        plan,status,attempts=self._extract(form,deadline)
        return PreparedRequest(deepcopy(form),resolved,plan,deadline,status,attempts,place_attempts+count)

    def _new(self, form):
        started=self.clock();route,route_error=None,None
        raw=form.get('query')
        if isinstance(raw,str) and raw.strip() and len(raw)<=2000:
            try:route=validate_walking_request(raw.strip(),[480])
            except GoogleError as error:route_error=error
        try:draft=self._route_draft(form,route) if route else self.stager(form)
        except ContextError as error:
            if route_error is not None and error.code=='known_start_required':raise route_error from None
            raise
        if route_error is not None:raise route_error
        route=validate_walking_request(form['query'].strip(),draft['resolved_context']['budget_options'])
        deadline=started+self.lifetime
        if route:return self._complete_route(form,draft,route,deadline)
        plan,status,attempts=self._extract(form,deadline)
        resolved,count=self._origin(form,plan,deadline)
        return PreparedRequest(deepcopy(form),resolved,plan,deadline,status,attempts,count)

    def prepare(self, payload):
        form = self._form(payload)
        token = payload.get("preparation_token")
        if "preparation_token" in payload:
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token) is None:
                raise self._invalid()
            with self._lock:
                self._purge()
                entry = self._entries.pop(token, None)
            if entry is None or entry["identity"] != _identity(form) or not entry["prepared"].resolved.get("needs_geolocation"):
                raise self._invalid()
            previous = entry["prepared"]
            if previous.route_state is not None:
                draft=self._route_draft(form,previous.route_state)
                prepared=self._complete_route(form,draft,previous.route_state,previous.deadline,previous.place_attempts)
            else:
                resolved, count = self._origin(form, previous.intent_plan, previous.deadline, previous)
                prepared = replace(previous, form=deepcopy(form), resolved=resolved, place_attempts=count)
        else:
            prepared = self._new(form)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            if len(self._entries) >= self.capacity:
                raise GoogleError("context_busy", "Подготовка запросов занята. Попробуйте немного позже.", 429)
            self._entries[token] = {"identity": _identity(form), "prepared": prepared}
        return {"preparation_token": token, "needs_geolocation": prepared.resolved["needs_geolocation"],
                "resolved_context": deepcopy(prepared.resolved["resolved_context"])}

    def consume(self, payload):
        form = self._form(payload)
        if "preparation_token" not in payload:
            prepared = self._new(form)
        else:
            token = payload["preparation_token"]
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token) is None:
                raise self._invalid()
            with self._lock:
                self._purge()
                entry = self._entries.pop(token, None)
            if entry is None or _digest(form) != _digest(entry["prepared"].form):
                raise self._invalid()
            prepared = entry["prepared"]
        if prepared.resolved.get("needs_geolocation") or not prepared.resolved.get("origin"):
            raise ContextError("Для подбора нужен старт в сообщении или разрешённая геолокация.", code="known_start_required", status=422)
        if self.clock() >= prepared.deadline:
            raise GoogleError("context_timeout", "Истекло время подготовки запроса.", 504)
        return prepared
