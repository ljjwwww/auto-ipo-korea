# 현재 운영·장애 확인·복구

기준: 2026-11-18. 운영은 `config/daily.toml`의 `workflow="mention_events"`이다.
기릿 사이트는 월별 청약 대상과 일정, 블로그·유튜브는 새 언급 알림의 출처다.
새 언급이 없으면 AI 요청과 Telegram 발송은 0건이다. 전체 종목 digest는 재활성화하지 않는다.
Windows 예약은 정각 1시간 간격(08:00~23:00 KST, 야간 제외)이며 기존 `scheduler_entry → daily`를 계속 사용한다.

## 상태 확인 (수집·키 읽기·네트워크·DB 쓰기 없음)

```bash
uv run ipo-monitor status
uv run ipo-monitor events
```

`status`는 마지막 실행의 날짜·상태·실패 단계와 출처별 상태·누락 경고, 이벤트 원장을 JSON으로 보여준다.
`health=never_run/stale/success/partial/error/running`을 구분한다. 조회 성공의 종료코드0은 운영 정상 판정이 아니며
`healthy`, `last_run.status`, `last_run.summary.phase/error_code`를 함께 본다.
실패 단계는 collection/catalog/analysis/delivery다. 성공한 실행의 phase는 마지막 처리 단계인 delivery다.
`active_pending/cooldown/exhausted/expired/closed`를 구분하며 만료·종료 이벤트는 활성 분석 대기에서 제외한다.
이벤트의 `delivery_unknown`은 수신 여부를 확정하지 못했다는 뜻이다.

## AI 판단 이유와 분석 이력

현재 운영 모델은 Ollama Cloud `nemotron-3-ultra`다. 이전 `nemotron-3-super`와 같은 API를 사용하며
일일1200회·20분 예산을 유지한다. 모델 변경은 실제 한국어 분석 품질 개선을 보증하지 않는다.

`status/events`의 `decision_reason`은 마지막 AI 응답의 번호·짧은 설명·모델·검증 여부를,
`reason_history`는 재시도 전후의 이력을 보여준다. 이유 번호는 `ReasonCode(IntEnum)`의 고정 식별자이며
높고 낮은 신뢰도나 투자 추천 등급이 아니다.

| 번호 | 의미 | 가능한 의견 |
| --- | --- | --- |
| 0 | 보류 없음 | 참여 또는 미참여가 명시됨 |
| 1 | 자료 부족 | 판단 불가 |
| 2 | 조건 확인 필요 | 조건부 |
| 3 | 근거 상충 | 판단 불가 |
| 4 | 명시적 의견 없음 | 판단 불가 |

예를 들어 `code=2`, `validated=true`는 AI가 조건 확인이 필요하다고 판단했고 원문 검증을 통과한 경우다.
`validated=false`인 응답은 채택된 판단이 아니다. `error_code`는 수치 근거 불일치·통신 오류 같은
프로그램의 실패 원인이며 AI가 선택하는 이유 번호와 구분한다.

원장은 비공개 `mention_analysis_attempts`에 입력·모델·프롬프트 식별자, 번호, 180자 이내 설명,
응답 원문·검증 오류·시각을 함께 보존한다. 검증 거절/JSON 오류 응답도 남지만 성공 캐시나 발송 대상으로
승격하지 않는다. API/통신 실패로 응답이 없으면 이유 번호·설명은 null이며 임의 추정하지 않는다.
조회에는 짧은 설명만 내보내고 응답 원문은 내보내지 않는다. DB/백업은 계속 비공개로 보관한다.
구형 분석은 과거 설명을 그대로 보여주되 저장되지 않았던 번호·모델을 지어내지 않는다.
새 테이블은 다음 일일 실행 때 추가되며 읽기 전용 조회 자체는 마이그레이션하지 않는다.

재시도 제한은 입력·모델별로 적용하므로 새 모델에는 이전 모델의2시간 대기가 적용되지 않는다.
이전 사용량·실패·전송 이력은 삭제하지 않는다. 이미 분석 성공한 이벤트를 모델 변경만으로 다시 분석하거나
전송하지 않는다.

## 개별 이벤트 복구 (원장 수정만, 전송·AI 호출 없음)

먼저 `events`에서 확인한 이벤트 ID를 사용한다. 아래 ID1은 예시다.
`--reason`은 로컬 감사 이력에 저장하므로 키·계좌·개인정보를 넣지 않는다.

```bash
uv run ipo-monitor events --event 1 --action close --reason "더 이상 알림 불필요"
uv run ipo-monitor events --event 1 --action reopen --reason "관찰 재개"
uv run ipo-monitor events --event 1 --action retry-analysis --reason "원문과 응답을 확인해 재분석 결정"
uv run ipo-monitor events --event 1 --action retry-delivery --reason "확인된 전송 거절 재시도"
```

응답 불명/중단 상태를 재전송하려면 중복 가능성을 확인하고 다음 플래그를 명시한다.
실제로 수신했음을 확인한 경우에는 재전송 대신 `mark-sent`를 사용한다.

```bash
uv run ipo-monitor events --event 1 --action retry-delivery --reason "미수신 확인 후 재시도 결정" --confirm-duplicate-risk
uv run ipo-monitor events --event 1 --action mark-sent --reason "Telegram 앱에서 수신 확인"
```

재분석은 다음 명시적/예약 `daily`에서, 재전송은 다음 명시적/예약 전송에서 처리한다.
`retry-analysis`는 해당 입력의 분석 캐시를 무효화하고 시도 횟수의 새 기준 시각을 남긴다.
이전 일일 호출 원장은 삭제하지 않으므로 하루1200회 상한은 그대로 적용된다.
이미 전송 성공한 이벤트는 재전송 초기화를 거부한다. `close`는 분석·전송을 제외하며 이력을 삭제하지 않는다.
제어·감사 테이블은 `mention_event_controls`, `mention_event_actions`로 추가되고 기존 테이블은 보존된다.
새 이벤트에는 당시 `catalog_end`도 기록해 목록에서 빠진 뒤 만료 여부를 설명한다.

## SQLite 백업·복원

```bash
uv run ipo-monitor backup --output data/backups/2026-11-18
uv run ipo-monitor restore --source data/backups/2026-11-18 --output data/restored/2026-11-18
```

둘 다 **존재하지 않는 새 data/ 하위 디렉터리**를 요구한다. 기존 디렉터리·DB를 덮어쓰지 않는다.
설정의 sources/ledger/events/telegram DB 중 존재하는 파일만 SQLite backup API로 복사하고
무결성을 확인한다. 커밋된 WAL 내용도 포함한다. DB 파일600·백업 디렉터리700으로 생성한다.
`data/secrets`, 토큰, 설정 원문, 수집 파일·로그는 백업 묶음에 넣지 않는다.
DB 자체에는 수집 원문이 포함될 수 있으므로 백업도 비공개로 관리한다.

백업 동안 일일 실행·이벤트 전송 잠금을 잡는다. 각 DB는 일관된 SQLite 스냅샷이지만
별도 수동 collect/assess까지 묶는 분산 트랜잭션은 아니다. 이 명령들과 겹치지 않게 실행한다.
복원은 별도 폴더에서 검증하기 위한 것이며 운영 DB로 자동 전환하지 않는다.
복원된 이벤트 원장을 버리고 수집 DB만 바꾸면 기준선·중복 전송 상태가 달라질 수 있다.
운영 전환은 실행을 멈춘 상태에서 관련 원장 전체의 일관성을 확인한 뒤 별도로 수행한다.

## 검증 경계와 남은 한계

2026-11-18 개선은 실제 CLI 처리기/서브프로세스와 임시 SQLite로 검증했다.
외부 HTTP와 자막 다운로더 경계만 모사했으며 실제 유료 AI·Telegram·예약 설정은 실행/변경하지 않았다.
검증 증거는 로컬 `data/qa-hardening/{assessment,sources,mentions,backup,final}.txt`에 보관한다.
전체 회귀는 `uv run python -m unittest discover -s tests -v`로 실행한다.
앞선 운영 보강 시점의 검증은 전체374개 테스트, `uv sync --locked`, compileall, diff 검사를 통과했다.
LSP는 basedpyright 미설치로 실행하지 못했고 의존성을 추가하지 않았다.

요약 검증은 정확한 인용 선택지·대상 이름·대상 종목 구절의 날짜/수치·명시적 참여 표현을 검사한다.
조건부를 참여로 승격하거나 별점/청약정보 제목만으로 참여를 단정한 응답은 보류한다.
날짜 표기·쉼표는 정규화하지만 `4만원 ↔ 40,000원` 같은 단위 환산은 하지 않는다.
원문에 없는 수치·인용을 거절해도 자유 요약의 모든 의미가 맞음을 증명하지는 않는다.
명시적 표현·인용 구간이 부족하면 정상적인 요약도 보수적으로 보류될 수 있다.
전송 전 같은 검사를 다시 수행한다. 이미 전송 완료된 이력은 새 정책 때문에 오류로 재분류하지 않는다.

블로그 일일 입력은 RSS이며 본문 재확인은 별도 pipeline에 남아 있다.
유튜브는 최신3개이며 경고는 누락 가능성의 표시일 뿐 누락 영상 복구가 아니다.
1200회는 하루 요청 상한이며 정각(08:00~23:00 16회) 예약 실행이 같은 날짜 한도를 함께 쓴다. 수동 assess까지 포함하는 전역 비용 한도가 아니다.
