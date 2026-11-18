# 아키텍처

현재 구현 기준: **2026-11-18**. 운영 명령과 복구는 [OPERATIONS.md](OPERATIONS.md),
알림 정책은 [MENTION_NOTIFICATIONS.md](MENTION_NOTIFICATIONS.md)가 기준이다.

## 현재 운영 경로

`Windows → scheduler_entry → daily → mention_workflow`가 세 출처를 수집하고
`mentions`가 월별 목록·출처별 기준선·새 언급을 저장한다.
`event_validation`은 요약의 정확한 인용·대상·수치·날짜·명시적 의사를 검사하고
`mention_delivery`가 같은 검증 후 전송 원장을 저장한다. 전체 종목 digest와 분리되어 있다.
`maintenance`는 상태/복구/백업 CLI, `event_operations`는 개별 제어·감사 이력,
`backup`은 비밀 파일을 제외한 SQLite 스냅샷과 새 폴더 복원을 담당한다.

운영 DB는 `data/daily.sqlite3`, `data/daily-ledger.sqlite3`, `data/mention-events.sqlite3`이다.
`source_attempts.warning/metadata_json`은 영상창 누락 가능성과 자막 확인 시각을 저장한다.
자막 확인 시각은 원문 hash에서 제외하므로 동일 자막 재확인으로 새 이벤트가 생기지 않는다.
기존 테이블은 보존하며 이벤트의 catalog_end, 개별 제어/감사 테이블은 추가적으로 이행한다.

별도 `assess`는 fixture/Ollama를 지원한다. 입력별 write lock 안에서 분석·사용량을 커밋한 뒤
별도 트랜잭션에서 판단·전이·미리보기를 저장한다. 뒤쪽 실패는 성공 분석을 지우지 않는다.
수집 상태가 중간에 바뀌면 `assessment_inputs_changed`로 중단하며 자동 유료 재호출하지 않는다.
RSS는 원격/로컬2MB·DTD/entity 거절, 사이트는 인식한 IPO 표 구조 변경을 오류로 기록한다.

## 이전 구현 기록 — 2026-11-15

아래는 초기 계층의 설계 기록이다. 당시의 미구현/fixture-only/RSS-only/미검증 설명은
현재 운영 상태가 아니며 위 경로와 최신 운영 문서를 우선한다.

## 0.2.0 공통 분석·실험 판단·미리보기 계층

- `evidence.py`: 사이트 종목명/등록 별칭으로 최신 관찰을 공통 분석 입력으로 만들고 snapshot별 provenance를 보존한다. 내용 hash는 로컬 ID·수집 시각과 분리한다.
- `assessment.py`: fixture-only 분석/근거 검증·캐시, 정책 snapshot을 포함한 판단 저장, 현재 입력/정책과 조회 일치 여부 검사.
- `decision.py`: 조정 가능한 실험 정책. 필드 평균 → 출처별 최신 글(알려진 발행시각 우선) → 작성자 그룹 평균 → 작성자 가중 평균. 원신호/비선택 근거 모두 보존.
- `preview.py`: 미발송 로컬 메시지/outbox 전용. 네트워크와 실제 send 함수 없음.
- 테이블 `observation_analyses`, `decision_versions`, `decision_current`, `decision_transitions`, `decision_transition_current`, `notification_outbox`를 추가한다. 계산 결과 버전 재사용과 시간순 상태 선택 전이를 분리한다. outbox의 `decision_id`는 이 계층에서 상태 전이 ID를 참조하는 논리 식별자다. 이전 버전 DB의 초기 버전/미리보기 ID는 동일 전이 ID로 보존한다.
- 정책/원문/수집 상태 변경은 이전 판단을 재분석 필요로 표시하고, 과거 미리보기는 기본 표시에서 제외한다. 동일한 상태로 복귀해도 새 전이를 남긴다. 커버리지는 작성자별 점수 신호 확보율이지 성공확률·정확도·수익률이 아니다.

상세 사용법/한계: [ASSESSMENT_USAGE.md](ASSESSMENT_USAGE.md). 실제 모델 호출과 메신저 연결은 아직 없다.

## 3개 출처 관찰 계층 (2026-11-15 추가)

`multisource.py`가 설정 검증·통합 수집·불변 관찰 저장·상태/후보 조회를 담당합니다. `youtube.py`는 제한된 채널 피드와 외부 yt-dlp 자막, `ipo_site.py`는 지정 공개 페이지의 달력/표를 읽습니다. AI 호출 없이 동작합니다.

추가 테이블은 `tracked_sources`, `source_snapshots`, `source_attempts`이며 기존 테이블은 유지합니다. 같은 항목의 내용 해시를 마지막 스냅샷과 비교해 변경 시에만 append합니다. 과거 값으로 되돌아와도 변경 이력입니다. 한 출처 실패는 기록하고 나머지를 처리합니다. 문자열 일치는 종목 연결 후보이며 공모 건 병합/의견 확정이 아닙니다.

아래 AI 분석 경로는 기존 블로그 `posts` 대상입니다. 통합 `collect`의 유튜브/사이트는 관찰 테이블에만 저장되며 기존 AI 입력으로 아직 연결하지 않았습니다. 세부 수집 범위·캐시·조회/실패 의미는 [MULTI_SOURCE_USAGE.md](MULTI_SOURCE_USAGE.md)를 참고하세요.

## 범위와 데이터 흐름

Python CLI가 한 번 실행되고 종료되는 구조입니다. 예약 실행·알림·공식 일정 대조·청약·매도는 아직 없습니다.

```text
설정 또는 --feed
    ↓ 일반 코드
RSS 읽기 → 파싱·시각 보존 → SQLite posts 저장
    ↓ pipeline
RSS 의미 분석 [AI 또는 테스트용 고정 응답]
    ↓ 형식·근거 검증 [일반 코드]
불확실한 글만 공개 본문 수집
    ↓
본문 의미 분석 → 검증 → 단계별 결과 저장
    ↓
pipeline-results로 현재 입력의 저장 결과 조회
```

`collect`는 RSS만 저장합니다. `pipeline`은 기본적으로 `collect`를 먼저 실행하며, `--skip-collect`는 기존 DB만 분석합니다. RSS 설명은 피드 제공자가 내려주는 값이지 우리 프로그램이 만든 요약이 아닙니다. 본문 전체 또는 글쓴이의 최종 의견이 포함된다고 보장하지 않습니다.

## 모듈 책임

| 모듈 | 책임 |
| --- | --- |
| `src/ipo_monitor/__init__.py` | CLI, TOML 출처 설정, RSS 읽기/파싱, 게시 시각, 중복 방지·갱신 |
| `extraction.py` | 기존 추출/조회, 공용 입력 정규화·해시·DB 열기 |
| `semantic.py` | 새 분류 스키마·프롬프트·검증, fixture/OpenAI 백엔드 |
| `body.py` | 지정 블로그의 공개 모바일 본문 텍스트 추출 |
| `pipeline.py` | RSS/본문 분석 순서, 캐시·실패 상태·저장 결과 조회 |
| `scripts/live_check.py` | 승인된 세 글만 다루는 OpenAI 실험 보조 도구. Ollama 미지원 |

## 두 추출 세대는 별개

| 항목 | 기존 `extract` / `candidates` | 새 `pipeline` / `pipeline-results` |
| --- | --- | --- |
| 글 분류 | relevant / irrelevant / insufficient | ipo / property / other / uncertain |
| 의견 | 참여 / 조건부 / 패스 / 판단 불가 | 참여 / 조건부 / 미참여 / 판단 불가 |
| 입력 | RSS 제목·설명 등 | RSS 입력 + 필요한 경우 body |
| 저장 | extraction_runs, candidate_results | semantic_runs, article_bodies, body_fetch_errors |
| fixture 키 | 원문 링크 → 입력 해시·응답 | 정규화 입력 digest → 응답 |

`candidates`는 새 파이프라인 결과를 읽지 않습니다. 새 기능의 결과는 `pipeline-results`로 확인합니다.

## 새 의미 분석 계약

입력은 `source_input(post)`로 만든 title, summary, link, published_raw, published_at이며 본문 분석 때 body를 추가합니다. 제목·설명의 HTML은 읽을 수 있는 텍스트로 정리합니다.

응답에는 `category`와 `candidates`만 허용합니다. 후보마다 다음 필드를 요구합니다.

| 필드 | 값·규칙 |
| --- | --- |
| name | 입력에 실제 존재하는 종목 이름, 중복 금지 |
| opinion | 참여 / 조건부 / 미참여 / 판단 불가 |
| evidence | 입력에서 그대로 가져온 1~300자 구절 |
| basis | author_opinion / mention / insufficient |
| stage | planned / applied / retrospective / unknown |

`property`와 `other`는 후보가 없어야 하고 `ipo`는 하나 이상 있어야 합니다. `uncertain`은 후보가 없어도 됩니다. 단순 언급·정보 부족은 판단 불가 및 unknown만 허용합니다. `applied`는 작성자가 신청했다고 서술했다는 뜻이지 증권사 접수 확인이 아닙니다. 형식과 인용 존재 검증만으로 의미 해석의 정확성을 입증할 수는 없습니다.

본문 확인 조건은 `uncertain`이거나 후보 중 하나라도 판단 불가일 때입니다. 명확한 부동산·기타 글, 명확한 조건부 의견만 있는 글은 제외합니다. 본문이 모호하거나 접근/분석에 실패해도 미참여로 바꾸지 않습니다. RSS와 본문 결과는 따로 보존하며 종목별 최종 병합은 아직 없습니다.

## SQLite 저장과 캐시

| 테이블 | 역할·식별자 |
| --- | --- |
| feeds | 피드 ID, 마지막 수집 시각 |
| posts | `(feed_id, entry_id)`로 중복 방지, 제목·링크·원본/변환 시각·설명 |
| extraction_runs | 구형 분석 결과·시도 횟수·선택 시각, 입력/추출기별 캐시 |
| candidate_results | 구형 실행별 후보 행 |
| semantic_runs | 원문+phase+source_hash+backend_key별 결과 JSON 또는 실패·시도 횟수 |
| article_bodies | 원문+RSS 해시별 본문 텍스트·출처 URL·내용 해시·수집 시각 |
| body_fetch_errors | 원문+RSS 해시별 본문 수집/새로고침 실패 |

원격 피드 ID는 입력 URL, 로컬 피드 ID는 절대 파일 URI입니다. 원본 글 ID가 없으면 링크를 사용합니다. 같은 ID를 재수집하면 최신 필드를 갱신하지만 신규 글로 출력하지 않습니다. 원문 전체 수정 이력을 보존하는 것은 아닙니다. 명확한 시간대만 Asia/Seoul로 변환하고 불명확한 시각은 원본과 NULL 변환값으로 남깁니다.

성공 분석은 동일한 정규화 입력과 백엔드 식별자에서 재사용합니다. 입력·모델·프롬프트 버전·해당 fixture 응답이 달라지면 별도 분석입니다. 실패는 다음 실행에 다시 처리합니다. RSS가 같으면 본문도 재사용하므로 원문만 수정된 경우 `--refresh-body`가 필요합니다. 새로고침 실패 후에는 과거 성공 결과를 현재 본문 판단으로 표시하지 않고 재수집합니다.

글 하나를 처리하는 동안 SQLite 쓰기 잠금을 유지합니다. 이 잠금에는 네트워크 대기 시간도 포함되므로 운영용 고처리량 큐 구조가 아닙니다. `--limit`는 정렬상 앞 N건을 선택할 뿐 다음 N건으로 이동하지 않습니다. 조회는 현재 입력 해시에 맞는 결과를 선택하며, 조회 명령도 테이블 준비를 위해 DB를 쓸 수 있습니다.

## 보안·실행 경계

- 외부 글은 데이터입니다. 그 안의 명령을 실행하거나 주문 권한으로 사용하지 않습니다. 출력의 터미널 제어 문자를 제거합니다.
- RSS URL은 운영자가 설정합니다. RSS 수집은 HTTP(S)와 리다이렉트를 지원하므로 범용 SSRF 방어를 완성한 서비스라고 간주하지 않습니다. 인증정보·쿼리·fragment를 넣은 입력 URL은 거부합니다.
- 본문 수집은 HTTPS `blog.naver.com`/`m.blog.naver.com`의 `hycj1025/<숫자ID>`만 허용합니다. 모바일 공개 페이지에 단일 요청하며 리다이렉트를 차단합니다. 20초, 2MB, HTML 타입, 완결된 인식 가능 본문 컨테이너를 검사합니다. 로그인 우회·이미지/OCR·영상 분석은 없습니다.
- AI 실호출은 `--allow-api`와 키 모두 필요합니다. 현재는 OpenAI만 구현되어 있고 Ollama Cloud 어댑터는 미구현입니다. 공식 문서의 Cloud 구조화 출력 지원 여부를 재확인하고 별도 어댑터·검증을 추가해야 합니다.
- API 자동 재시도는 없습니다. 실패 후 사용자가 다시 실행하면 과금될 수 있고 exactly-once 과금은 보장하지 않습니다. 글 수 제한은 금액 상한이 아닙니다.
- 키·DB·수집 원문·로그·런타임 상태를 공개 저장소에 올리지 않습니다. 공개 샘플은 가상 데이터입니다. Git 제외는 암호화나 접근통제를 대신하지 않습니다.
