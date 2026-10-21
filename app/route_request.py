"""Pure explicit public walking endpoints and validated direct-route attachment."""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from .activity_intent import _clause_prefix
from .google_budget import GoogleError
from .lab_context import ORIGIN_PATTERNS, PUBLIC_ORIGINS, ORIGIN_REFERENCES
from .retrieval import distance_m


# The context aliases contain broad inflection wildcards. For an endpoint,
# accept closed case endings rather than a differently named compound venue.
_CASE_ENDING = r"(?:иями|ями|ами|ого|ему|ому|ыми|ими|ий|ый|ая|яя|ое|ее|ые|ие|ую|юю|ой|ей|ов|ев|ах|ях|ом|ем|ам|ям|ым|им|а|я|у|ю|е|и|ы)?"
_ANCHORS = {key: re.compile(pattern.replace(r"\w*", _CASE_ENDING))
            for key, pattern in ORIGIN_PATTERNS.items() if key in PUBLIC_ORIGINS}
_FROM = re.compile(r"(?<!\w)(?:от|из|с|со|from)(?!\w)\s+(?:the\s+)?")
_INTO = re.compile(r"\s+в(?!\w)\s+")
_TO = re.compile(r"\s+(?:до|to)(?!\w)(?:\s+(?:the\s+)?|(?=$))")


def _at(query, position):
    return [(key, match.end()) for key, pattern in _ANCHORS.items()
            if (match := pattern.match(query, position)) is not None]


def _route_negated(query, start, end):
    # Negate the route itself, not uncertainty or walking without a car.
    prefix = _clause_prefix(query, start)
    return bool(re.search(
        r"(?:\bне|\bnot|\b(?:do\s+not|don't))\s+"
        r"(?:(?:хочу|хотел\w*|нужен|нужна|нужно|надо|пойду|иду|планирую|собираюсь|"
        r"идти|ехать|пройти|маршрут|путь|пеш\w*|want|need|go|walk|walking|route|to|a|the)\s+){0,5}$", prefix
    ) or re.match(
        r"\s+(?:я\s+)?не\s+(?:пойду|планирую|собираюсь|хочу|нужен|нужна|нужно)(?!\w)", query[end:]
    ))


def _unsupported():
    return GoogleError(
        "unsupported_point_to_point",
        "Нужен один однозначный пеший маршрут между двумя точками; альтернативные и составные направления не рассчитывались.",
        422,
    )


def _unresolved():
    return GoogleError("route_endpoints_unresolved",
                       "Для маршрута между двумя точками нужны два однозначно подтверждённых места; маршрут и подбор мест не выполнялись.", 422)


def _nonwalking(text, begin, end):
    from .activity_intent import _negated
    left = max((match.end() for match in re.finditer(r"[,;.!?\n]", text[:begin])), default=0)
    following = re.search(r"[,;.!?\n]", text[end:])
    right = end + following.start() if following else len(text)
    pattern = (r"(?<!\w)(?:на\s+(?:машин\w*|автомобил\w*|автобус\w*|поезд\w*|велосипед\w*)|"
               r"такси|by\s+(?:car|bus|taxi|train|bike)|drive|driving|transit)(?!\w)")
    if any(not _negated(text,left+match.start(),left+match.end())
           for match in re.finditer(pattern,text[left:right])):
        return True
    # A mode-only suffix is tied to this route. An unrelated arrival story
    # after punctuation is not; negated/without-car text stays walking.
    suffix = text[right:]
    mode = (r"(?:на\s+(?:такси|машин\w*|автомобил\w*|автобус\w*|поезд\w*|велосипед\w*)|"
            r"by\s+(?:car|bus|taxi|train|bike)|driving|transit)")
    return bool(re.fullmatch(r"[,;.!?]\s*(?:пожалуйста\s+)?"+mode+
        r"(?:\s+(?:за|for)\s+(?:\d+\s*(?:минут\w*|час\w*|minutes?|hours?)|час))?\s*[.!?]*",suffix))



_TEMPORAL = re.compile(r"^(?:\d{1,2}:\d{2}|"
    r"(?:\d+(?:[.,]\d+)?|одного|один|двух|два|две|трех|три|четырех|четыре|пяти|пять|пару|полутора)"
    r"\s*(?:минут\w*|час\w*|minutes?|hours?)|"
    r"утра|рассвета|полудня|вечера|заката|ночи|\d{1,2}$)(?!\w)")
_ACCOMPANIMENT = re.compile(r"^(?:розет\w*|wi[\s-]?fi|вай[\s-]?фай\w*|интернет\w*|"
    r"ребен\w*|дет\w*|друз\w*|семь\w*|собой|собою|коляск\w*|"
    r"(?:(?:моим|моей|своим|своей|нашим|нашей|этим|этой)\s+)?(?:друг(?:ом|а|у|е)?|подруг(?:ой|ою|а|у|е|и|ами)?)(?!\w)|"
    r"friend\w*|child\w*|kids|family)(?!\w)")
_END_TAIL = re.compile(r"\s+(?=за\s+(?:\d|час|полчас|полтора|пару)|"
    r"на\s+(?:\d|час|полчас|полтора|пару)|в\s+течение\b|"
    r"хочу\b|хочется\b|интересует\b|покажи\b|посоветуй\w*\b|что\b|"
    r"where\b|want\b|for\s+(?:\d|an?\s+hour)|within\b|пешком\b|walking\b|"
    r"walk\b|on\s+foot\b|без\s+машин\w*\b|есть\s+(?:лишн|дополнительн)|"
    r"(?:лишн|дополнительн)\w*\b)")
_EXTRA_MARKER = r"(?:дополнительн\w*|лишн\w*|extra|additional)"
_DURATION = (r"(?:\d+(?:[.,]\d+)?\s*(?:[-–—]|до)\s*\d+(?:[.,]\d+)?\s*"
    r"(?:минут(?:а|ы)?|мин\.?|час(?:а|ов)?|hours?|minutes?)|"
    r"(?:\d+(?:[.,]\d+)?|один|два|две|три|четыре|пять|пару)\s*"
    r"(?:минут(?:а|ы)?|мин\.?|час(?:а|ов)?|hours?|minutes?)|пол\s*часа|полтора\s*часа|an?\s+hour)")


def _budget_mode(query):
    """Only explicitly additional time changes the meaning of the budget."""
    from .activity_intent import _negated
    from .lab_context import _time_options
    markers = [m for m in re.finditer(r"(?<!\w)"+_EXTRA_MARKER+r"(?!\w)",query)
               if not _negated(query,m.start(),m.end())]
    if not markers:
        return "total", None
    if len(markers) != 1:
        raise GoogleError("route_budget_ambiguous","Указано несколько дополнительных бюджетов; маршрут не рассчитывался.",422)
    marker=markers[0]
    after=re.match(r"\s*(?:времени\s+)?(?P<duration>"+_DURATION+r")(?!\w)",query[marker.end():])
    before=re.search(r"(?P<number>\d+(?:[.,]\d+)?)\s*$",query[:marker.start()])
    unit=re.match(r"\s*(минут(?:а|ы)?|мин\.?|час(?:а|ов)?|hours?|minutes?)(?!\w)",query[marker.end():])
    if after:
        duration=after['duration'];begin,end=marker.start(),marker.end()+after.end()
    elif before and unit:
        duration=before['number']+' '+unit[1];begin,end=before.start(),marker.end()+unit.end()
    else:
        raise GoogleError("route_budget_ambiguous","Дополнительное время не определено однозначно; маршрут не рассчитывался.",422)
    if re.fullmatch(r"an?\s+hour",duration):duration='60 minutes'
    try:
        options,source,_,_= _time_options(duration,None)
        remainder=query[:begin]+' '+query[end:]
        _,other,_,_=_time_options(remainder,None)
    except ValueError:
        raise GoogleError("route_budget_out_of_range","Дополнительный бюджет должен быть от 10 до 480 минут; маршрут не рассчитывался.",422) from None
    if source not in {"query","query_range"} or other not in {"default_options"}:
        raise GoogleError("route_budget_ambiguous","Общий и дополнительный бюджеты противоречат друг другу; маршрут не рассчитывался.",422)
    return "extra", options


def _endpoint(quote, *, origin=False):
    quote=quote.strip(' \t\r\n«»"“”')
    if not 1<=len(quote)<=200 or re.search(r"[<>\x00-\x1f]|https?://",quote):raise _unresolved()
    if origin and re.fullmatch(r"(?:меня|моего\s+местоположения|сюда|отсюда|my\s+location|here)",quote):
        return {'kind':'current','quote':None,'public_id':None}
    matches=[key for key,pattern in _ANCHORS.items() if pattern.fullmatch(quote)]
    if len(matches)==1:return {'kind':'public','quote':quote,'public_id':matches[0]}
    # Role labels alone are not addresses; a literal address after an explicit
    # address cue is independently matched against the returned source record.
    quote=re.sub(r"^(?:работ\w*|дом\w*|home|work)\s+(?:по\s+адресу|at\s+address)\s+",'',quote)
    if (not re.search(r"[a-zа-я]",quote) or _TEMPORAL.match(quote)
            or re.fullmatch(r"(?:(?:мой|моего|моей|моя|мою|my|the)\s+)?(?:дом\w*|работ\w*|офис\w*|home|work|office|"
                r"отель\w*|гостиниц\w*|кафе|парк\w*|музей\w*|неизвестн\w*(?:\s+\w+){0,3})",quote)
            or re.search(r"(?<!\w)(?:или|либо|or)(?!\w)",quote)):
        raise _unresolved()
    return {'kind':'named','quote':quote,'public_id':None}


def _descriptor(first,last,mode,extra):
    result={'route_kind':'point_to_point','origin_spec':first,'destination_spec':last,
            'budget_mode':mode,'extra_budget_options':extra}
    for prefix,spec in (('origin',first),('destination',last)):
        identifier=spec['public_id']
        result.update({prefix:deepcopy(PUBLIC_ORIGINS[identifier]) if identifier else None,
            prefix+'_id':identifier,prefix+'_quote':spec['quote'],
            prefix+'_source':'query_landmark' if identifier else None,
            prefix+'_reference':ORIGIN_REFERENCES.get(identifier),prefix+'_place_id':None})
    return result


def _clause_limit(text,start):
    for match in re.finditer(r'[,;.!?\n]',text[start:]):
        position=start+match.start()
        if match[0]=='.':
            token=re.search(r'([a-zа-я]+)$',text[:position])
            if token and token[1] in {'ул','пр','просп','д','st','ave'}:continue
            if position and text[position-1].isdigit() and position+1<len(text) and text[position+1].isdigit():continue
        if match[0]==',' and re.match(r'\s*\d{1,4}[a-zа-я]?(?!\w)',text[position+1:]):continue
        return position
    return len(text)


def validate_walking_request(query,budget_options):
    """One explicit pair. Named endpoints are specs, never fabricated points."""
    if (not isinstance(query,str) or not isinstance(budget_options,(list,tuple)) or not budget_options
            or len(budget_options)>3 or any(type(v)is not int or not 10<=v<=480 for v in budget_options)):
        raise ValueError('Walking preflight requires validated query and minute budgets')
    text=unicodedata.normalize('NFKC',query).casefold().replace('ё','е').replace('’',"'")
    pairs=[]
    for beginning in _FROM.finditer(text):
        limit=_clause_limit(text,beginning.end())
        connector=_TO.search(text,beginning.end(),min(limit,beginning.end()+210))
        if connector is None and beginning[0].strip()=='из':
            connector=_INTO.search(text,beginning.end(),min(limit,beginning.end()+210))
        if connector is None:continue
        if beginning[0].strip() in {'с','со'} and _FROM.search(text,beginning.end(),connector.start()):continue
        start=text[beginning.end():connector.start()].strip()
        finish=text[connector.end():limit].strip()
        # Accompaniment/property and time endpoints are ordinary request syntax,
        # not an invitation to geocode friends, sockets or a clock reading.
        if beginning[0].strip() in {'с','со'} and _ACCOMPANIMENT.match(start):continue
        if _TEMPORAL.match(finish):continue
        tail=_END_TAIL.search(finish)
        endpoint_finish=finish[:tail.start()] if tail else finish
        end=connector.end()+len(endpoint_finish)
        negative=re.search(r'\s+(?:я\s+)?не\s+(?:пойду|планирую|собираюсь|хочу|нужен|нужно)(?!\w)',endpoint_finish)
        if negative:end=connector.end()+negative.start();endpoint_finish=endpoint_finish[:negative.start()]
        if _route_negated(text,beginning.start(),end):continue
        if _nonwalking(text,beginning.start(),end):
            raise GoogleError('unsupported_route_mode','Пока проверяются только пешие маршруты; другой транспорт не рассчитывался.',422)
        if re.search(r'(?<!\w)(?:или|либо|or)(?!\w)',start+' '+endpoint_finish):raise _unsupported()
        first,last=_endpoint(start,origin=True),_endpoint(endpoint_finish)
        pairs.append((first,last))
    if not pairs:
        # A strong walking destination with no departure explicitly requests
        # the verified current/GPS start, not the destination as a new origin.
        travel=re.search(r'(?<!\w)(?:пешком|пеший\s+маршрут|дойти|пройти|walk|walking)(?!\w)',text)
        if travel:
            connector=_TO.search(text,travel.end())
            if connector and not _route_negated(text,travel.start(),connector.end()):
                finish=text[connector.end():_clause_limit(text,connector.end())]
                tail=_END_TAIL.search(finish);finish=finish[:tail.start()] if tail else finish
                if not _TEMPORAL.match(finish):
                    if _nonwalking(text,travel.start(),connector.end()):raise GoogleError('unsupported_route_mode','Пока проверяются только пешие маршруты; другой транспорт не рассчитывался.',422)
                    pairs=[({'kind':'current','quote':None,'public_id':None},_endpoint(finish))]
    if not pairs:return None
    if len(pairs)!=1:raise _unsupported()
    first,last=pairs[0]
    if first['kind']==last['kind']=='public' and first['public_id']==last['public_id']:return None
    mode,extra=_budget_mode(text)
    if mode=='total' and first['kind']==last['kind']=='public':
        if distance_m(PUBLIC_ORIGINS[first['public_id']],PUBLIC_ORIGINS[last['public_id']])>max(budget_options)*150:
            raise GoogleError('route_does_not_fit',f'Пеший маршрут между указанными точками не помещается в {max(budget_options)} мин; дополнительные остановки не подбирались.',422)
    return _descriptor(first,last,mode,extra)


def _walking_binding(context,prefix):
    """One source-owned endpoint proof; also validates pending destinations."""
    from .google_client import valid_place_id,maps_url
    import math
    if not isinstance(context,dict) or context.get('route_kind')!='point_to_point':raise ValueError('Expected walking context')
    point=context.get(prefix)
    if (not isinstance(point,dict) or set(point)!={'lat','lon','label'}
            or not isinstance(point['label'],str) or not 1<=len(point['label'])<=200
            or any(type(point[k])not in {int,float} or not math.isfinite(point[k]) or not low<=point[k]<=high
                for k,low,high in (('lat',51.00,51.32),('lon',71.20,71.70)))):
        raise ValueError('Endpoint outside the validated city/point schema')
    source=context.get(prefix+'_source')
    identifier=context.get(prefix+'_id')
    if identifier in PUBLIC_ORIGINS:
        source=source or 'query_landmark'  # Public-only legacy descriptors.
        if source not in {'query_landmark','explicit_landmark'} or point!=PUBLIC_ORIGINS[identifier]:raise ValueError('Public endpoint identity changed')
        identity=identifier
    elif source=='google_places_text_search':
        identity=context.get(prefix+'_place_id')
        if identifier is not None or not valid_place_id(identity) or not maps_url(context.get(prefix+'_reference')):raise ValueError('Missing verified Places endpoint identity')
    elif prefix=='origin' and source in {'browser_geolocation','explicit_coordinates'}:
        if identifier is not None:raise ValueError('Coordinate origin cannot claim a public identity')
        identity='browser-geolocation' if source=='browser_geolocation' else 'explicit-coordinates'
    else:raise ValueError('Unconfirmed endpoint source')
    return {'source':source,'id':identity,'lat':point['lat'],'lon':point['lon']}


def walking_bindings(context):
    """Return the two exact endpoint identities bound to a verified baseline."""
    return tuple(_walking_binding(context,prefix) for prefix in ('origin','destination'))


def walking_waypoints(descriptor):
    bindings=walking_bindings(descriptor)
    if all(round(bindings[0][key],7)==round(bindings[1][key],7) for key in ('lat','lon')):
        raise ValueError('Expected distinct walking endpoint coordinates')
    return tuple({'latLng':{'latitude':item['lat'],'longitude':item['lon']}} for item in bindings)


def attach_walking_baseline(resolved,descriptor,response):
    """Attach one measured leg bound to both complete endpoint identities."""
    from .google_grounded import route_minutes
    walking_waypoints(descriptor)
    if not isinstance(resolved,dict) or not isinstance(resolved.get('resolved_context'),dict):raise ValueError('Expected request context')
    budgets=resolved['resolved_context'].get('budget_options')
    if not isinstance(budgets,(list,tuple)) or not budgets or any(type(v)is not int or not 10<=v<=480 for v in budgets):raise ValueError('Expected validated budgets')
    minutes,attribution=route_minutes(response)
    if minutes is None or minutes<=0:raise GoogleError('direct_route_unverified','Google не подтвердил прямой пеший маршрут между указанными точками; подбор мест не выполнен.',502)
    if descriptor.get('budget_mode')=='extra':
        extra=descriptor.get('extra_budget_options')
        if not isinstance(extra,list) or not 1<=len(extra)<=3 or any(type(v)is not int or not 10<=v<=480 for v in extra):raise ValueError('Expected explicit extra budgets')
        budgets=[minutes+value for value in extra]
        if any(value>480 for value in budgets):raise GoogleError('route_budget_out_of_range','Прямой путь и дополнительное время превышают предел 480 минут; бюджет не сокращался автоматически.',422)
    if minutes+5>max(budgets):raise GoogleError('route_does_not_fit',f'Прямой пеший маршрут занимает {minutes} мин по данным Google; вместе с запасом 5 мин он не помещается в {max(budgets)} мин. Дополнительные остановки не подбирались.',422)
    result=deepcopy(resolved);context=result['resolved_context']
    for prefix in ('origin','destination'):
        for suffix in ('','_id','_source','_reference','_place_id','_attributions'):
            key=prefix+suffix
            if key in descriptor:context[key]=deepcopy(descriptor[key])
    context.update(route_kind='point_to_point',sensitive_origin=any(context.get(prefix+'_source')not in {'query_landmark','explicit_landmark'} for prefix in ('origin','destination')),
                   is_city_sample=False,direct_walk_minutes=minutes,direct_route_attribution=deepcopy(attribution),
                   budget_mode=descriptor.get('budget_mode','total'))
    if descriptor.get('budget_mode')=='extra':
        context.update(extra_budget_options=deepcopy(descriptor['extra_budget_options']),budget_options=budgets,
                       minutes=budgets[0],minutes_source='query_extra',budget_source='query_extra',requested_range=None)
        result['minutes']=budgets[0]
    first,last=walking_bindings(context)
    result.update(origin=deepcopy(context['origin']),needs_geolocation=False)
    if 'needs_place_resolution'in result:result.update(needs_place_resolution=False,unresolved_start_quote=None)
    result['route_baseline']={'from_ref':'origin','to_ref':'destination','minutes':minutes,'attribution':deepcopy(attribution),
        'error_code':None,'origin_id':context.get('origin_id'),'destination_id':context.get('destination_id'),
        'origin_binding':first,'destination_binding':last}
    return result
