"""Local-only notification previews. This module has no sending capability."""

from datetime import datetime
import math
from zoneinfo import ZoneInfo

from . import CollectionError, terminal_text


def ensure_schema(conn):
    """Prepare the outbox without committing the caller's transaction."""
    conn.execute('''CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY,
        decision_id INTEGER NOT NULL,
        stock TEXT NOT NULL,
        channel TEXT NOT NULL CHECK(channel IN ('telegram','slack')),
        destination TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'preview' CHECK(status = 'preview'),
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(decision_id,channel,destination))''')


def _text(value):
    return terminal_text(str(value)) if value is not None else '미상'


def render_message(stock, decision, *, is_test=True):
    """Render plain text, not platform markup or an actionable trade request."""
    if is_test is not True:
        raise CollectionError('preview_test_only')
    if not isinstance(stock, str) or not stock.strip() or not isinstance(decision, dict):
        raise CollectionError('preview_input_invalid')
    score, coverage = decision.get('score'), decision.get('coverage')
    if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
        raise CollectionError('preview_score_invalid')
    if type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1:
        raise CollectionError('preview_coverage_invalid')
    contributions = decision.get('contributions')
    if not isinstance(contributions, list) or any(not isinstance(c, dict) for c in contributions):
        raise CollectionError('preview_contributions_invalid')
    lines = [
        f'[테스트/미발송] {_text(stock)}',
        '고정 응답 기반 실험 결과입니다. 실제 AI 분석·메시지 전송이 아닙니다.',
        f"검토 상태: {_text(decision.get('status', '판단 대기'))}",
        f"실험 점수: {'미정' if score is None else f'{score:.3f}'} | 작성자별 신호 커버리지: {coverage:.0%}",
        f"출처 간 상충: {'있음' if decision.get('conflict') else '없음'}",
        f"정책: {_text(decision.get('policy_version', '미상'))}",
    ]
    if decision.get('input_hash'):
        lines.append(f"입력 해시: {_text(decision['input_hash'])}")
    for index, signal in enumerate(contributions, 1):
        lines.append(f"\n{index}. {_text(signal.get('source_id'))} / {_text(signal.get('source_type'))} / {_text(signal.get('creator'))}")
        lines.append(f"분석 상태: {_text(signal.get('analysis_status', '미상'))} | 의견: {_text(signal.get('opinion', '판단 불가'))} | 평가: {_text(signal.get('assessment', 'unknown'))}")
        lines.append(f"출처 수집: {_text(signal.get('source_status', 'unknown'))} | 집계 선택: {_text(signal.get('selected', True))} | 집계 근거: {_text(signal.get('reason'))}")
        lines.append(f"출처 점수: {_text(signal.get('score'))} | 출처 가중치: {_text(signal.get('source_weight'))} | 작성자 가중치: {_text(signal.get('creator_weight'))} | 시점 계수: {_text(signal.get('stage_factor'))}")
        lines.append(f"시점 구분: {_text(signal.get('stage', 'unknown'))} | 관찰 시각: {_text(signal.get('observed_at'))}")
        rating = signal.get('rating')
        lines.append(f"사이트 별점: {'미표시' if rating is None else _text(rating)} | 원문 표시: {_text(signal.get('marker') or '없음')}")
        lines.append(f"근거: {_text(signal.get('evidence') or '근거 미확보')}")
        lines.append(f"출처: {_text(signal.get('url'))}")
        if signal.get('analysis_reason'):
            lines.append(f"분석 설명: {_text(signal['analysis_reason'])}")
        for segment in signal.get('segments', []):
            lines.append(f"자막 근거 구간: {_text(segment.get('start'))} ~ {_text(segment.get('end'))}")
        if signal.get('error'):
            lines.append(f"확인 필요: {_text(signal['error'])}")
        if signal.get('source_error'):
            lines.append(f"수집 확인 필요: {_text(signal['source_error'])}")
    if not contributions:
        lines.append('출처별 근거가 아직 없습니다. 판단 대기입니다.')
    lines.append('\n초기 가중치의 실험적 검토 우선순위이며 수익 예측·참여 확정·주문 지시가 아닙니다.')
    return '\n'.join(lines)


def enqueue(conn, decision_id, stock, decision, channel='telegram', destination='preview', is_test=True):
    """Store one preview per decision/channel/destination; never perform I/O to a service."""
    if type(decision_id) is not int or decision_id <= 0:
        raise CollectionError('preview_decision_id_invalid')
    if channel not in ('telegram', 'slack'):
        raise CollectionError('preview_channel_invalid')
    if not isinstance(destination, str) or not destination.strip():
        raise CollectionError('preview_destination_invalid')
    message = render_message(stock, decision, is_test=is_test)
    ensure_schema(conn)
    cursor = conn.execute('''INSERT INTO notification_outbox
        (decision_id,stock,channel,destination,status,message,created_at)
        VALUES (?,?,?,?,'preview',?,?) ON CONFLICT(decision_id,channel,destination) DO NOTHING''',
        (decision_id, stock, channel, destination, message,
         datetime.now(ZoneInfo('Asia/Seoul')).isoformat()))
    row = conn.execute('''SELECT id FROM notification_outbox
        WHERE decision_id=? AND channel=? AND destination=?''',
        (decision_id, channel, destination)).fetchone()
    return row[0], bool(cursor.rowcount)
