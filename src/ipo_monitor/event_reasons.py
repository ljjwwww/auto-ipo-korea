"""Stable AI hold reasons and private, append-only analysis diagnostics."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
import json
import sqlite3
from typing import assert_never

from . import CollectionError, terminal_text


class ReasonCode(IntEnum):
    """Persistent identifiers, not confidence scores; never renumber these."""

    NO_HOLD = 0
    INSUFFICIENT_INFORMATION = 1
    CONDITION_UNCONFIRMED = 2
    CONFLICTING_EVIDENCE = 3
    NO_EXPLICIT_OPINION = 4

    @property
    def label(self) -> str:
        match self:
            case ReasonCode.NO_HOLD:
                return '보류 없음'
            case ReasonCode.INSUFFICIENT_INFORMATION:
                return '자료 부족'
            case ReasonCode.CONDITION_UNCONFIRMED:
                return '조건 확인 필요'
            case ReasonCode.CONFLICTING_EVIDENCE:
                return '근거 상충'
            case ReasonCode.NO_EXPLICIT_OPINION:
                return '명시적 의견 없음'
            case unreachable:
                assert_never(unreachable)


REASON_INSTRUCTIONS = (
    'reason_code 키를 추가한 JSON 객체를 반환한다. reason_code는 다음 정수 중 하나다: '
    + ', '.join(f'{int(code)}={code.label}' for code in ReasonCode)
    + '. 0은 참여/미참여가 명시된 경우, 2는 조건부, 1/3/4는 판단 불가에만 쓴다. '
    'reason은 이 이유를 설명하는 근거 기반 한국어 한 문장 180자 이내다. '
    '보류는 미참여와 다르며, 번호는 신뢰도 점수나 투자 추천 등급이 아니다.'
)


def validate_reason_code(value, opinion: str) -> ReasonCode:
    """Parse the model-selected category and its opinion consistency."""
    if type(value) is not int:
        raise CollectionError('event_reason_code_invalid')
    try:
        code = ReasonCode(value)
    except ValueError:
        raise CollectionError('event_reason_code_invalid') from None
    permitted = {
        ReasonCode.NO_HOLD: {'참여', '미참여'},
        ReasonCode.INSUFFICIENT_INFORMATION: {'판단 불가'},
        ReasonCode.CONDITION_UNCONFIRMED: {'조건부'},
        ReasonCode.CONFLICTING_EVIDENCE: {'판단 불가'},
        ReasonCode.NO_EXPLICIT_OPINION: {'판단 불가'},
    }
    if opinion not in permitted[code]:
        raise CollectionError('event_reason_code_invalid')
    return code


@dataclass(frozen=True, slots=True)
class Attempt:
    """One dispatched analysis outcome; rejected text remains untrusted."""

    event_id: int
    input_hash: str
    backend_key: str
    model: str
    response_text: str | None
    error_code: str | None


def save_attempt(conn: sqlite3.Connection, attempt: Attempt) -> None:
    """Save diagnostics without promoting failed output into the success cache."""
    code = text = None
    if attempt.response_text is not None:
        try:
            parsed = json.loads(attempt.response_text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get('reason_code')
            if type(value) is int and value in ReasonCode:
                code = int(ReasonCode(value))
            reason = parsed.get('reason')
            if isinstance(reason, str):
                text = ' '.join(terminal_text(reason).split())[:180]
    conn.execute("""INSERT INTO mention_analysis_attempts
        (event_id,input_hash,backend_key,model,reason_code,reason_text,
         response_text,error_code,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (attempt.event_id, attempt.input_hash, attempt.backend_key, attempt.model,
         code, text, attempt.response_text, attempt.error_code,
         datetime.now(timezone.utc).isoformat()))
