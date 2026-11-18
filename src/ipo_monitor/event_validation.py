"""Stricter grounding for per-event one-line summaries.

Generic assessment responses keep ``assessment.validate_response`` as their only
contract. New-mention events additionally require the evidence to be one of the
exact quote choices offered to the model, to name the target stock/alias, and to
support any participation/applied-stage claim. Numbers and dates written in the
one-line reason must already exist in the source text in an equivalent form.

These checks never turn a weak response into a positive opinion: they only
reject responses whose claims the supplied source does not support.
"""

from datetime import date
import re

from . import CollectionError, terminal_text
from .assessment import validate_response
from .evidence import entity_match
from .event_reasons import validate_reason_code
from .ollama import evidence_choices

REASON_LIMIT = 180

# Full dates are parsed into (year, month, day); month/day-only forms use
# (None, month, day). ``2026-11-20``, ``2026.11.20`` and ``2026년 11월 20일``
# therefore validate the same reason text without letting a stray ``20`` pass.
_DATE_PATTERNS = (
    (re.compile(r'(?<!\d)(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)'), 3),
    (re.compile(r'(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일'), 3),
    (re.compile(r'(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일'), 2),
    (re.compile(r'(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)'), 2),
)
# Money/percent/ratio claims must match as a whole token, never by number alone.
_MONEY = re.compile(r'(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*((?:만|억|조)?\s*원|%|퍼센트|배)')
_BARE_NUMBER = re.compile(r'(?<![\d.])(\d[\d,]*(?:\.\d+)?)')

_POSITIVE = re.compile(
    r'(?:참여|신청|청약|매수)(?:를|을)?\s*(?:하겠|할|합니다|했|하였|한다|하려|완료|예정|계획)'
    r'|청약을\s*신청')
_NEGATIVE = re.compile(
    r'참여하지\s*않|청약하지\s*않|신청하지\s*않|참여\s*안|청약\s*안|신청\s*안'
    r'|미참여|하지\s*않겠|건너뛰|패스|불참')
_CONDITIONAL = re.compile(r'라면|이면|으면|면\s|경우|조건|여부|따라|확인한?\s*뒤')
_FUTURE = re.compile(r'하겠|겠|예정|계획|하려|생각|참여합니다|신청합니다|청약합니다')
_APPLIED = re.compile(
    r'신청했|청약했|참여했|신청\s*완료|청약\s*완료|참여\s*완료|접수했'
    r'|(?:신청|청약|참여|접수)(?:을|를)?\s*(?:완료했|마쳤)'
    r'|신청하였|청약하였|참여하였')


def _overlaps(span, spans):
    return any(start < span[1] and span[0] < end for start, end in spans)


def _claims(text):
    """Return (dates, money tokens, bare numbers) with date/money spans removed."""
    spans = []
    dates = set()
    for pattern, size in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if _overlaps(match.span(), spans):
                continue
            spans.append(match.span())
            values = tuple(int(value) for value in match.groups())
            year, month, day = ((None, *values) if size == 2 else values)
            dates.add((year, month, day))
    money = set()
    for match in _MONEY.finditer(text):
        if _overlaps(match.span(), spans):
            continue
        spans.append(match.span())
        number = match.group(1).replace(',', '')
        unit = re.sub(r'\s+', '', match.group(2)).replace('퍼센트', '%')
        money.add(number + unit)
    numbers = set()
    for match in _BARE_NUMBER.finditer(text):
        if _overlaps(match.span(), spans):
            continue
        numbers.add(match.group(1).replace(',', ''))
    return dates, money, numbers


def _multiple_sentences(reason):
    body = reason.strip()
    for match in re.finditer(r'[.!?。！？]', body):
        position = match.start()
        # Decimal separators such as 4.5 or 2026.11 are not sentence ends.
        if (body[position] == '.' and 0 < position < len(body) - 1
                and body[position - 1].isdigit() and body[position + 1].isdigit()):
            continue
        if body[match.end():].strip():
            return True
    return False


def _display_text(value):
    """Remove terminal escape sequences so their digits are not read as claims."""
    return terminal_text(re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', ' ', value))


def _check_reason(reason, text):
    reason = _display_text(reason)
    if not reason.strip():
        raise CollectionError('event_reason_empty')
    if len(reason) > REASON_LIMIT:
        raise CollectionError('event_reason_too_long')
    if _multiple_sentences(reason):
        raise CollectionError('event_reason_multiple_sentences')
    reason_dates, reason_money, reason_numbers = _claims(reason)
    dates, money, numbers = _claims(text)
    for year, month, day in reason_dates:
        try:
            date(year or 2000, month, day)
        except ValueError:
            raise CollectionError('event_reason_date_ungrounded') from None
        if year is None:
            matched = any((known_month, known_day) == (month, day)
                          for _, known_month, known_day in dates)
        else:
            matched = (year, month, day) in dates
        if not matched:
            raise CollectionError('event_reason_date_ungrounded')
    if any(token not in money for token in reason_money):
        raise CollectionError('event_reason_number_ungrounded')
    if any(token not in numbers for token in reason_numbers):
        raise CollectionError('event_reason_number_ungrounded')


def _check_opinion(response, evidence):
    opinion = response['opinion']
    if opinion == '참여':
        if not _POSITIVE.search(evidence) or _NEGATIVE.search(evidence) or _CONDITIONAL.search(evidence):
            raise CollectionError('event_opinion_ungrounded')
    elif opinion == '미참여':
        if not _NEGATIVE.search(evidence):
            raise CollectionError('event_opinion_ungrounded')
    elif opinion == '조건부':
        if not (_CONDITIONAL.search(evidence) and _POSITIVE.search(evidence)):
            raise CollectionError('event_opinion_ungrounded')
    # 판단 불가/unknown stays unproven; never converted to participation here.
    if response['stage'] == 'applied' and not _APPLIED.search(evidence):
        raise CollectionError('event_stage_ungrounded')
    if response['stage'] == 'planned' and not (
            _FUTURE.search(evidence) or _APPLIED.search(evidence)):
        raise CollectionError('event_stage_ungrounded')


def validate_event_response(response, source):
    """Generic validation plus event-specific quote/number/date grounding."""
    if isinstance(response, dict) and 'reason_code' in response:
        validate_response({key: value for key, value in response.items()
                           if key != 'reason_code'}, source)
        validate_reason_code(response['reason_code'], response['opinion'])
    else:
        # Historical events and offline fixtures retain the original five keys.
        validate_response(response, source)
    text = source['content']['text']
    if response['evidence'] not in evidence_choices(source):
        raise CollectionError('event_evidence_not_choice')
    aliases = source.get('aliases') or [source['stock']]
    if not entity_match(response['evidence'], aliases):
        raise CollectionError('event_evidence_not_target_stock')
    target_context = '\n'.join(quote for quote in evidence_choices(source) if entity_match(quote, aliases))
    _check_reason(response['reason'], target_context)
    _check_opinion(response, response['evidence'])
    reason = response['reason']
    evidence = response['evidence']
    if _NEGATIVE.search(reason):
        if not _NEGATIVE.search(evidence):
            raise CollectionError('event_opinion_ungrounded')
    elif _POSITIVE.search(reason):
        if (not _POSITIVE.search(evidence) or _NEGATIVE.search(evidence)
                or (_CONDITIONAL.search(evidence) and not _CONDITIONAL.search(reason))):
            raise CollectionError('event_opinion_ungrounded')
    if _APPLIED.search(reason) and not _APPLIED.search(evidence):
        raise CollectionError('event_stage_ungrounded')
    return response
