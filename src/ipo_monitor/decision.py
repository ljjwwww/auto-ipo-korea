"""Experimental, configurable review-priority scoring; never executes trades.

Only the newest observation per source participates in a stock's calculation.
All supplied signals, including superseded/failed ones, remain in contributions.
The weights are unvalidated defaults, not measured probabilities or returns.
"""

from datetime import datetime, timezone
import math
from pathlib import Path
import tomllib

from . import CollectionError

VERSION = 'weighted-review-v1'
OPINIONS = {'참여', '조건부', '미참여', '판단 불가'}
ASSESSMENTS = {'positive', 'negative', 'neutral', 'unknown'}
STAGES = {'planned', 'applied', 'retrospective', 'unknown'}
SOURCE_TYPES = {'rss', 'youtube', 'ipo_site'}
STATUSES = {'success', 'error', 'unprocessed'}
_POLICY_FIELDS = {'version', 'expected_creators', 'source_weights', 'creator_weights',
                  'opinion_scores', 'assessment_scores', 'rating_scores', 'rating',
                  'stage_factors', 'thresholds'}


class DecisionError(CollectionError):
    """A static diagnostic safe to report without exposing source content."""


def _number(value, minimum=None, maximum=None):
    if type(value) not in (int, float):
        raise DecisionError('decision_policy_invalid')
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise DecisionError('decision_policy_invalid') from None
    if not math.isfinite(number):
        raise DecisionError('decision_policy_invalid')
    if minimum is not None and number < minimum or maximum is not None and number > maximum:
        raise DecisionError('decision_policy_invalid')
    return number


def _mapping(value, keys, minimum, maximum):
    if not isinstance(value, dict) or set(value) != keys:
        raise DecisionError('decision_policy_invalid')
    for score in value.values():
        _number(score, minimum, maximum)


def _validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise DecisionError('decision_policy_invalid')
    if not isinstance(policy['version'], str) or not policy['version'].strip():
        raise DecisionError('decision_policy_invalid')
    creators = policy['expected_creators']
    if (not isinstance(creators, list) or any(not isinstance(c, str) or not c.strip() for c in creators)
            or len(set(creators)) != len(creators)):
        raise DecisionError('decision_policy_invalid')
    _mapping(policy['source_weights'], SOURCE_TYPES, 0, 1000)
    weights = policy['creator_weights']
    if (not isinstance(weights, dict) or any(not isinstance(c, str) or not c.strip() for c in weights)
            or not set(creators).issubset(weights)):
        raise DecisionError('decision_policy_invalid')
    for weight in weights.values():
        _number(weight, 0, 1000)
    _mapping(policy['opinion_scores'], OPINIONS - {'판단 불가'}, -1, 1)
    _mapping(policy['assessment_scores'], ASSESSMENTS - {'unknown'}, -1, 1)
    _mapping(policy['rating_scores'], {'0', '1', '2', '3'}, -1, 1)
    _mapping(policy['rating'], {'cross_score'}, -1, 1)
    _mapping(policy['stage_factors'], STAGES, 0, 1)
    thresholds = policy['thresholds']
    if (not isinstance(thresholds, dict) or set(thresholds) !=
            {'candidate', 'low_priority', 'minimum_coverage', 'hold_on_source_issue'}
            or type(thresholds['hold_on_source_issue']) is not bool):
        raise DecisionError('decision_policy_invalid')
    _number(thresholds['candidate'], -1, 1)
    _number(thresholds['low_priority'], -1, 1)
    _number(thresholds['minimum_coverage'], 0, 1)
    if thresholds['low_priority'] >= thresholds['candidate']:
        raise DecisionError('decision_policy_invalid')
    return policy


def load_policy(path):
    """Load an explicit TOML policy; malformed/missing files never use defaults."""
    try:
        with Path(path).open('rb') as stream:
            policy = tomllib.load(stream)
    except (OSError, ValueError):
        raise DecisionError('decision_policy_invalid') from None
    return _validate_policy(policy)


def _signal_time(signal):
    try:
        value = datetime.fromisoformat(signal['observed_at'].replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise DecisionError('decision_signal_invalid') from None


def _publication_time(signal, observed):
    raw=signal.get('published_at')
    if raw is None:
        return observed
    try:
        value=datetime.fromisoformat(raw.replace('Z','+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except (TypeError,ValueError,AttributeError,OverflowError):
        raise DecisionError('decision_signal_invalid') from None


def _validate_signal(signal):
    if not isinstance(signal, dict):
        raise DecisionError('decision_signal_invalid')
    for field in ('stock', 'source_id', 'source_type', 'creator', 'url', 'evidence', 'marker'):
        if not isinstance(signal.get(field), str):
            raise DecisionError('decision_signal_invalid')
    if any(not signal[field].strip() for field in ('stock', 'source_id', 'creator')):
        raise DecisionError('decision_signal_invalid')
    for field, values in (('source_type', SOURCE_TYPES), ('opinion', OPINIONS),
                          ('assessment', ASSESSMENTS), ('stage', STAGES),
                          ('analysis_status', STATUSES)):
        if not isinstance(signal.get(field), str) or signal[field] not in values:
            raise DecisionError('decision_signal_invalid')
    source_status = signal.get('source_status', 'unknown')
    if (not isinstance(source_status, str) or source_status not in {'success', 'partial', 'error', 'unknown'}
            or type(signal.get('snapshot_id')) is not int or signal['snapshot_id'] < 0):
        raise DecisionError('decision_signal_invalid')
    rating = signal.get('rating')
    if rating is not None and (type(rating) is not int or rating not in range(4)):
        raise DecisionError('decision_signal_invalid')
    if signal.get('error') is not None and not isinstance(signal['error'], str):
        raise DecisionError('decision_signal_invalid')
    return _signal_time(signal)


def _mean(values):
    """Weighted mean for nonempty positive-weight terms."""
    total = math.fsum(weight for _, weight in values)
    return math.fsum(value * (weight / total) for value, weight in values)


def evaluate(signals, policy):
    """Return an explainable stock review priority from already grounded signals.

    Different stocks may not be mixed in one call. Input ordering is irrelevant.
    Unknown creators use weight 1 and extend the coverage denominator; configured
    expected creators with no evidence still reduce coverage. Explicit zero
    weights are preserved/explained but do not act as positive or negative votes.
    """
    _validate_policy(policy)
    if not isinstance(signals, list):
        raise DecisionError('decision_signal_invalid')
    timestamps = [_validate_signal(signal) for signal in signals]
    if len({s['stock'] for s in signals}) > 1:
        raise DecisionError('decision_stock_mismatch')
    # source identity must not change creator/type within one evaluation.
    identities = {}
    newest = {}
    for index, signal in enumerate(signals):
        identity = (signal['creator'], signal['source_type'])
        if signal['source_id'] in identities and identities[signal['source_id']] != identity:
            raise DecisionError('decision_source_identity_mismatch')
        identities[signal['source_id']] = identity
        rank = (_publication_time(signal,timestamps[index]),timestamps[index],signal['snapshot_id'])
        source = signal['source_id']
        if source in newest and rank == newest[source][0]:
            # Ambiguous duplicate IDs must not depend on the caller's ordering.
            prior = signals[newest[source][1]]
            if signal != prior:
                raise DecisionError('decision_duplicate_signal')
        elif source not in newest or rank > newest[source][0]:
            newest[source] = (rank, index)

    contributions = []
    active = {}
    for index, signal in enumerate(signals):
        selected = newest[signal['source_id']][1] == index
        source_weight = float(policy['source_weights'][signal['source_type']])
        creator_weight = float(policy['creator_weights'].get(signal['creator'], 1))
        factor = float(policy['stage_factors'][signal['stage']])
        fields = {}
        reason = 'superseded'
        score = None
        if selected and signal['analysis_status'] != 'success':
            reason = signal['analysis_status']
        elif selected:
            if signal['opinion'] != '판단 불가':
                fields['opinion'] = float(policy['opinion_scores'][signal['opinion']])
            if signal['assessment'] != 'unknown':
                fields['assessment'] = float(policy['assessment_scores'][signal['assessment']])
            if signal.get('rating') is not None:
                fields['rating'] = float(policy['rating_scores'][str(signal['rating'])])
            elif '❌' in signal['marker']:
                fields['marker'] = float(policy['rating']['cross_score'])
            if fields:
                score = math.fsum(fields.values()) / len(fields) * factor
                reason = 'scored' if source_weight > 0 and creator_weight > 0 else 'zero_weight'
            else:
                reason = 'unknown'
        item = {**signal, 'selected': selected, 'field_scores': fields,
                'selection_time_basis': 'published_at' if signal.get('published_at') else 'observed_at',
                'stage_factor': factor, 'source_weight': source_weight,
                'creator_weight': creator_weight, 'score': score, 'reason': reason}
        contributions.append(item)
        if reason == 'scored':
            active.setdefault(signal['creator'], []).append((score, source_weight))

    creator_scores = {creator: _mean(values) for creator, values in sorted(active.items())}
    configured = set(policy['expected_creators']) | {s['creator'] for s in signals}
    expected = {c for c in configured if policy['creator_weights'].get(c, 1) > 0}
    coverage = len(creator_scores) / len(expected) if expected else 0.0
    scored = [c['score'] for c in contributions if c['reason'] == 'scored']
    conflict = any(s > 0 for s in scored) and any(s < 0 for s in scored)
    score = _mean([(value, float(policy['creator_weights'].get(creator, 1)))
                   for creator, value in creator_scores.items()]) if creator_scores else None
    source_issues = [{'source_id': c['source_id'], 'creator': c['creator'],
                      'status': c['source_status'], 'snapshot_id': c['snapshot_id']}
                     for c in contributions if c['selected'] and
                     c.get('source_status') in {'partial', 'error'}]
    source_issues.sort(key=lambda issue: (issue['source_id'], issue['snapshot_id']))
    if source_issues and policy['thresholds']['hold_on_source_issue']:
        status = '추가 확인'
    elif score is None:
        status = '판단 대기'
    elif conflict or coverage < policy['thresholds']['minimum_coverage']:
        status = '추가 확인'
    elif score >= policy['thresholds']['candidate']:
        status = '검토 후보'
    elif score <= policy['thresholds']['low_priority']:
        status = '낮은 우선순위'
    else:
        status = '관찰'
    for contribution in contributions:
        contribution['creator_score'] = creator_scores.get(contribution['creator'])
    contributions.sort(key=lambda c: (c['source_id'], c['observed_at'], c['snapshot_id'],
                                      c['evidence'], c['opinion'], c['assessment']))
    return {'score': None if score is None else round(score, 10),
            'coverage': round(coverage, 10), 'status': status, 'conflict': conflict,
            'contributions': contributions, 'policy_version': policy['version'],
            'source_issues': source_issues}
