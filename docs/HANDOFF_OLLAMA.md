# 인수인계 — IPO Monitor / Ollama Cloud 전환 재개

## 현재 인수 기준 — 2026-11-18

현재 운영·복구 명령은 [OPERATIONS.md](OPERATIONS.md), 언급 이벤트 정책은
[MENTION_NOTIFICATIONS.md](MENTION_NOTIFICATIONS.md), 모듈/트랜잭션은
[ARCHITECTURE.md](ARCHITECTURE.md)의 현재 운영 경로를 먼저 읽는다.
아래 날짜별 단락은 결정·검증 이력이다. 과거의 미구현/미검증/승인 대기 설명을 현재로 적용하지 않는다.
Ollama·Telegram·예약 실행은 구현되어 있고 실제 독립 예약 실행 이력도 있다.
추가 개선은 입력별 분석 체크포인트·요약 검증·수집 경계·자막 재확인·상태/개별 복구·백업/복원이다.

**작성: 2026-11-14 / 공개 저장소 정리·재검증: 2026-11-15 (Asia/Seoul)**. 다음 작업자는 이 문서를 먼저 읽고 현재 코드와 대조한다. 문서의 '제안'은 사용자 확정 사항이 아니다. 코드 구현·테스트·실서비스 검증·사용자 승인을 서로 구분한다.

## 최신 일일/Telegram 작업 — 2026-11-16

사용자가 일일 AI 상한 **600회**로 변경하고 Telegram 개인 전송 구현을 요청했다. 매일19:00 KST Windows 작업은 등록 확인됨. 일일 실행/영속 한도/재시도/캐시/잠금/20분 실행 예산과 Telegram 로컬 연결·요약 전달 원장이 추가됐다. 초기 Telegram은 비활성, 사용자가 BotFather 봇 생성→로컬 `telegram setup`→개인 일회성 연결→`telegram enable`을 해야 한다. 연결 전 실제 메신저 전송 검증은 하지 않았다. [연결 안내](TELEGRAM_SETUP.md)가 최신 명령이다.

운영 단점: 일일 블로그는 RSS만, 본문 fallback은 별도 pipeline/실험에만 있다. 유튜브는 최신3개. 과거 날짜 실험은 운영 흐름이나 당시 아카이브가 아니다. 이 제한을 숨기거나 실험 결과를 운영 결과로 설명하지 않는다. 아래 미구현/fixture전용/테스트 개수는 과거 기록일 수 있으므로 최신 코드와 확인한다.

## 최신 Ollama 실제 검증 — 2026-11-16

키 숨김 입력·권한600 저장·Ollama 어댑터를 구현했고 사용자가 실제 검증을 승인했다. 입력이 긴 이번 시험에 저가 Nemotron-3-Super를 선택, 실제 요청7회 중 응답6회·네트워크 오류1회를 확인했다. 응답의 인용/사이트 평가 해석을 보강했고, 영상의 ‘엠비디아’ 부분 일치 오탐을 추가 호출 없이 미확정 연결로 보류했다. 최종 실험 판단은 추가 확인이다. 테스트208개. [상세 보고](OLLAMA_LIVE_VALIDATION.md)와 [키/연결 안내](OLLAMA_SETUP.md)가 최신 기준이다. 아래 실제 호출0회/연동 미구현 설명은 과거 기록이다. 현재 키 값은 로컬에만 있고 인계서·로그·Git에 넣지 않는다.

## 0.2.0 업데이트 — 2026-11-15

사용자는 세 출처 공통 분석 입력·조정 가능한 초기 가중 판단·고정 응답으로 알림 미리보기까지의 1~3번 작업을 승인했고, 완료 후 Git 버전 게시를 요청했다. 최신 기준은 [ASSESSMENT_USAGE.md](ASSESSMENT_USAGE.md)와 CHANGELOG/실제 Git 로그다.

추가 모듈 evidence.py/assessment.py/decision.py/preview.py, 정책 config/decision.toml, 가상 예제 samples/assessment/. 테스트183개. 고정 응답 분석9건 → 반복 호출0/재사용9, 판단/미리보기3건 → 반복 신규0건. 입력/정책 변화와 복귀는 불변 판단 버전·상태 전이로 추적한다. 원문/수집 상태/정책이 바뀐 예전 미리보기는 기본 조회에서 제외한다.

실제 AI·메신저 발송·주문은 여전히0회다. 아래 '가중 판단 미구현', '아직 커밋하지 않음'은 이전 단계 기록이다. 현재 가중 판단은 고정 응답 기반의 미검증 초기 정책이며 Ollama runtime과 실제 전송은 미구현이다.

## 최신 구현 업데이트 — 2026-11-15

사용자가 첫 권장 수집 묶음 개발을 승인했고, `explicit_alias_candidates` 선택 후 **추가 인터뷰 없이 권장안으로 진행**하도록 변경했다. 세 출처 수집·변경 이력·종목명/등록 별칭 기반 후보 조회가 구현됐다. 실제 수집50/3/29건과 반복 신규/변경0건, 테스트133개를 확인했다. [MULTI_SOURCE_USAGE.md](MULTI_SOURCE_USAGE.md)가 최신 구현·명령·제한의 기준이다.

아래 '블로그만 정식 지원', '이번에는 문서만'과 인터뷰 필수 문구는 이전 시점 기록이다. 현재 collect는 세 출처, AI pipeline은 기존 블로그 posts만 처리한다. 가중 판단·알림·예약·주문·Ollama 연동은 미구현이고 유료 AI 호출은 여전히0회다. 새 모듈은 multisource.py/youtube.py/ipo_site.py, 추가 설정은 config/stocks.toml이다. 기존 사용자 수정 문서는 보존했다. 이번 구현은 아직 커밋/원격 업로드하지 않았다.

## 최신 방향 변경 — 2026-11-15 사용자 확정

[docs/MULTI_SOURCE_PLAN.md](MULTI_SOURCE_PLAN.md)가 최신 계획이다. **팽 블로그 + 기릿 유튜브 + 기릿 사이트** 세 출처를 축적하고 가중 종합 판단 후 Telegram/Slack으로 전달한다. 출처 확장을 런타임 AI 연동보다 먼저 고려한다. 특정 신호의 활용을 AI가 임의 배제하지 말고, 가중치/반영 방식은 사용자와 결정한다. 추가 데이터 대기 종목은 후속 변화를 함께 확인한다.

이번 요청은 **문서 반영만**이며 코드/설정/DB 개발·실호출 승인이 아니다. 아래 '블로그 하나'는 현재 코드의 구현 상태를 뜻할 뿐 향후 출처 제한이 아니다. 아래 Ollama 전환 순서와 재개 프롬프트는 이전 시점의 기록이며, 다음 작업은 최신 계획/사용자 요청과 대조해야 한다. 자막2건과 사이트 공개 HTML 접근 시험은 최신 계획의 검증 현황에 기록했다.

## 0. 1분 요약 / 지금 멈춘 지점

- 개인용 공모주 정보 수집 프로그램. 초기 단계는 **수집·분석·알림만**, 청약/매도는 사용자가 직접 한다.
- 실제 출처는 네이버 블로그 **https://blog.naver.com/hycj1025** 하나. RSS는 **https://rss.blog.naver.com/hycj1025.xml**.
- RSS 수집·중복방지·SQLite 저장, 가상 응답을 이용한 의미 분석, 불확실한 글의 본문 추가 수집/재분석, 단계별 저장/조회까지 구현했다.
- **실제 RSS 접근 및 실제 본문 3건 접근 성공. 실제 AI API 호출은 단 한 번도 실행하지 않았다.** AI 정확도는 미검증이다.
- 사용자는 ChatGPT Plus로 API가 포함되는지 질문했고 별도 과금임을 설명받았다. **Ollama Cloud로 다음 날 전환해 재개할 의향**을 밝혔다. 지금 요청은 인수인계 문서 작성이며 Ollama 연동 구현/모델 선택/실호출 승인이 아니다.
- Ollama Cloud 어댑터는 **아직 없다**. 현재 AI 백엔드는 OpenAI Responses에 고정되어 있다. API 키/URL만 교체해서 실행하면 안 된다.
- 마지막 fresh 검증: **83 tests OK**, compileall(src/tests/scripts), git diff --check 통과. 별도 lint/typecheck는 구성되지 않았다.
- 2026-11-15 사용자가 `https://github.com/ljjwwww/auto-ipo-korea`에 코드·AI 문서를 정리해 올리도록 요청했다. 게시 상태는 실제 Git 로그/remote로 확인한다. `.gitignore` 대상 DB·키·수집 원문은 공개 저장소에 포함하지 않는다. 이전 untracked 상태 설명은 초기 스냅샷이었다.

## 1. 사용자의 장기 계획과 현재 범위

장기 흐름은 `PRODUCT_VISION.md` 참조:
1. 사용자가 블로그/유튜브 출처를 설정 문서에 등록 → 새 글 감지/정기 조사 → 청약 N일 전 Telegram/Slack으로 후보 전달 → 수동 청약/매도.
2. 같은 수집 기반에 사용자 입력으로 최종 종목·조건 결정 → 공식 API 범위 내 승인 기반 청약/매도 자동화.
3. 개인 실적을 축적하고 수익성·안정성을 검토. 한 달은 관찰 시작점이며 수익성 입증으로 취급하지 않는다.
4. 카드뉴스/스레드 등 콘텐츠 작성 → 추후 유료 제품화 검토. 저작권·광고·개인정보·투자정보 규제를 별도 확인한다.

현재는 **블로그 하나의 수동 실행 수집·AI 분석 파이프라인**만. 유튜브, 공식 일정 대조, N일 전 알림, 예약 실행, 주문은 미구현이다. 기업 설명·높은 경쟁률·수익률만으로 참여를 추정하지 않는다. 작성자 의견과 사용자 투자 결정을 혼동하지 않는다.

## 2. 반드시 지킬 사용자 결정 / 승인 이력

사용자는 **중요한 결정은 혼자 하지 말고 인터뷰**하라고 명시했다. 안전한 코드 구현 세부사항/테스트는 진행할 수 있지만 제공자·모델·과금·보관 정책·분석 범위 변경은 질문한다.

완료된 인터뷰(`omx question`의 `answers[]` 확인):

| 질문 | 사용자 선택 | 의미 |
| --- | --- | --- |
| 이번 구현 범위 | `implementation_first` | 구현과 오프라인 샘플 검증 우선. 유료 호출·알림·예약·주문 제외 |
| 조건부 의견 | `separate_conditional` | 참여/조건부/미참여/판단 불가 네 가지 유지. 조건 충족 추측 금지 |
| 본문 추가 확인 | `all_uncertain` | 판단 불가 종목이 하나라도 있거나 유형/정보가 불확실하면 본문 확인. 명확한 부동산/기타와 명확한 조건부만 있는 글은 제외 |
| 본문 보관 | `retain_text` | 읽을 수 있는 텍스트·URL·시각·해시 로컬 보관. HTML/이미지 저장 안 함 |

압력 검증 시나리오: A는 참여, B는 판단 불가인 한 글 → 본문 확인, RSS와 본문 판단은 각각 보존. 본문에서도 불확실/수집 실패면 보류. **판단 불가를 미참여로 변환하지 않는다.**

이후 실제 검증에 대한 별도 승인:
- 제이앤티지/엠비디/영광와이케이엠씨 **3개 글, 최초 실행 최대 6회 AI 요청**, 자동 재시도 없음, 전용 DB, 알림/거래 없음 (`approve_3_max6`).
- 모델 **`gpt-6-luna`** 선택. 그러나 키가 없어 호출은 실행하지 않았다.
- **이 승인은 Ollama 제공자/다른 모델/재실행 과금에 대한 포괄 승인이 아니다.** 전환 후 모델·한도·실험 대상 유지 여부를 다시 확인한다. “내일 전환할 것 같다”만으로 호출하지 않는다.
- 마지막 키 확인: `OPENAI_API_KEY` 없음, `data/openai-api-key.txt` 없음. 이후 바뀔 수 있으므로 존재 여부만 다시 확인. 값은 절대 출력하지 않는다. Ollama 키는 아직 요청/설정하지 않았다.

## 3. 환경과 작업 규칙

- 프로젝트: `/home/ljjw/projects/ipo-monitor` (기본 `~/projects/ipo-monitor`). Ubuntu 26.04.1 LTS / WSL2. Windows 호스트 버전 자체는 확인하지 않음.
- `.python-version`: 3.12, `.venv`: Python 3.12.16, uv 0.13.8. 시스템 Python은 3.14.4였으므로 프로젝트 Python을 사용할 것.
- 기존 `pyproject.toml`/`uv.lock` 재사용. 의존성은 httpx, feedparser. SQLite/tomllib/HTMLParser 등은 표준 라이브러리. 새 의존성은 사용자 요청 없이 추가하지 않는다.
- `uv sync --locked`, `uv run ...` 또는 캐시 쓰기 제한 시 기존 `.venv/bin/python ...`로 검증. uv 캐시/네트워크/tmux 접근이 샌드박스에서 거부된 적이 있으며 정상 승인 경로로 재실행했다. 제한 우회 금지.
- `config/sources.toml`: name/url/enabled. 현재 위 블로그 RSS 하나. 설정 경로·DB 상대 경로는 **실행 CWD 기준**이다.
- `data/`, `.env*`, `*.sqlite*`, `*.db*` 등이 Git 제외. Git 제외는 암호화/접근통제와 같지 않다. 키 파일을 쓰면 최소 권한으로 생성하고 로그·채팅에 값을 남기지 않는다.
- OMX `.omx/`와 락 메타데이터는 로컬 상태이며 소스 인계의 필수 입력이 아니다. `.omx` 세션 식별자는 다른 세션의 쓰기 권한이 아니다.
- Windows 작업 스케줄러 → `wsl.exe` 실행은 향후 방향일 뿐 아직 설정·검증하지 않았다.

## 4. 파일 지도

| 경로 | 현재 책임 |
| --- | --- |
| `src/ipo_monitor/__init__.py` | CLI 분기, RSS 읽기/파싱/시각 처리/저장, 설정 수집 |
| `src/ipo_monitor/__main__.py` | `python -m ipo_monitor` 진입점 |
| `src/ipo_monitor/extraction.py` | **기존 v1 추출/조회** 및 공용 함수. OpenAI strict-schema API, 구형 fixture |
| `src/ipo_monitor/semantic.py` | **새 v1 의미 분류** 스키마/프롬프트/검증/FixtureBackend/AIBackend |
| `src/ipo_monitor/body.py` | 지정 블로그 공개 모바일 본문만 추출하는 제한된 수집기 |
| `src/ipo_monitor/pipeline.py` | RSS 분석 → 조건부 본문 수집/재분석 → 저장/캐시/조회 |
| `scripts/live_check.py` | **OpenAI용** 3개 승인 글 제한 실험 보조 스크립트. Ollama 지원 안 됨 |
| `config/sources.toml` | 현재 실제 RSS 한 곳 |
| `samples/feed.xml` | 기존 RSS 수집 예제 |
| `samples/candidates.xml`, `samples/extractions.json` | 구형 추출 fixture 예제 |
| `samples/pipeline.xml`, `pipeline-responses.json`, `pipeline-bodies.json` | 새 파이프라인 fixture 예제 |
| `tests/test_cli.py`, `test_sources.py` | RSS 수집/설정 회귀 테스트 |
| `tests/test_extraction.py` | 기존 추출/캐시/API mock |
| `tests/test_semantic.py`, `test_body.py`, `test_pipeline.py` | 새 분류/본문/파이프라인 회귀 테스트 |
| `docs/CODEX_IPO_MONITOR_PROMPT.md`, `CODEX_IPO_NEXT_PROMPT.md` | 초기 원본 요구. 이후 사용자 인터뷰가 충돌 시 우선 |
| `docs/PRODUCT_VISION.md` | 장기 방향·원칙 |
| `README.md` | 실제 명령·구현 제한. 역사적 문장 일부는 최신 상태와 상충할 수 있음 |

`*:Zone.Identifier` 파일은 업로드 시 생성된 메타데이터이지 추가 프롬프트가 아니다.

## 5. CLI 및 두 추출기의 차이

### 공통 수집

```bash
cd ~/projects/ipo-monitor
uv sync --locked
uv run ipo-monitor collect
# config/sources.toml -> data/ipo-monitor.sqlite3
uv run ipo-monitor --feed samples/feed.xml --db data/sample.sqlite3
```

RSS ID는 입력 URL(원격)/절대 파일 URI(로컬). 원본 entry ID 우선, 없으면 링크. `(feed_id,entry_id)` 중복 방지. 최초 목록 보관, 이후 신규만 출력. 같은 ID의 내용 변경은 UPDATE하지만 신규 글로 출력하지 않는다. 시간대가 불명확하면 원본만 보존하고 서울 시각을 추정하지 않는다. RSS에 없는 과거 글을 전부 가져오는 기능은 아니다.

### 기존 `extract` / `candidates`

- schema: `status = relevant/irrelevant/insufficient`, 후보 name/opinion/evidence/basis. opinion은 참여/조건부/**패스**/판단 불가.
- 테이블 `extraction_runs`, `candidate_results`.
- fixture는 **원문 링크 → {input_hash, response}** 형태.
- 명령을 깨뜨리지 않기 위해 그대로 유지했다. 새 파이프라인의 결과는 `candidates`에 나타나지 않는다.

### 새 `pipeline` / `pipeline-results`

```bash
uv run ipo-monitor --feed samples/pipeline.xml --db data/pipeline-demo.sqlite3
uv run ipo-monitor pipeline --skip-collect --db data/pipeline-demo.sqlite3 \
  --backend fixture --fixture samples/pipeline-responses.json \
  --body-fixture samples/pipeline-bodies.json
# 재실행: 호출 0, 재사용 4
uv run ipo-monitor pipeline-results --db data/pipeline-demo.sqlite3
```

- `pipeline`은 기본적으로 collect 먼저 수행. `--skip-collect`로 기존 DB만 분석.
- 백엔드 현재 fixture/openai뿐. openai는 `--allow-api`와 키 모두 필요. 기본 모델은 extraction.DEFAULT_MODEL; `--model` → `IPO_MODEL` → 기본값 순.
- fixture는 **정규화 입력 digest → 응답** 형태. 구형 fixture와 호환되지 않는다. body-fixture는 **RSS 링크 문자열 그대로 → 본문 텍스트**. fixture 모드에 body-fixture 필수(우발적 웹 접근 차단).
- `pipeline-results`는 키/AI 호출/웹 요청 없이 현재 입력과 일치하는 저장 결과 조회. 테이블 준비 시 DB 쓰기가 발생할 수 있어 엄밀한 읽기 전용 SQLite 연결은 아니다.
- 기본 DB **전체**를 분석한다. `--limit N`은 정렬 순서상 앞 N개 글만. 페이지 이동/다음 N개 큐 처리가 아니므로 예전 캐시 글만 계속 선택될 수 있다. 시험용 옵션이며 운영 큐가 아니다.
- 글마다 RSS 1회 + 필요 시 본문 1회. 자동 네트워크 재시도 없음. 실패를 다음 명령으로 재처리하면 과금될 수 있다. 요청 타임아웃 뒤 서버 처리 여부까지 확정해 exactly-once 과금을 보장하지 않는다.

## 6. 새 의미 분석 계약

입력: `source_input(post)`의 title/summary(읽을 수 있는 텍스트로 정규화), link, published_raw, published_at. 본문 재분석 시 body 추가.

응답 정확한 키:

```json
{
  "category": "ipo",
  "candidates": [{
    "name": "가상기업",
    "opinion": "참여",
    "evidence": "가상기업 청약을 신청했다.",
    "basis": "author_opinion",
    "stage": "applied"
  }]
}
```

- category: `ipo / property / other / uncertain`. property는 부동산 청약 외 부동산 정보도 포함. property/other는 후보 0개, ipo는 1개 이상, uncertain은 0개 가능.
- opinion: `참여 / 조건부 / 미참여 / 판단 불가`.
- basis: `author_opinion / mention / insufficient`.
- stage: `planned / applied / retrospective / unknown`. 실제 증권사 접수 검증이 아니라 작성자 진술이다. 후기에서 사전 참여 의사를 역추론하지 않는다.
- 검증: 정확한 필드/enum/문자열/길이, 종목명 중복 방지, 이름·근거가 입력 title/summary/body에 실제 존재. mention/insufficient는 판단 불가+unknown만 허용.
- 근거가 인용되어 있다는 것은 의미 해석이 맞다는 증명이 아니다. **AI 정확도는 별도 사람 검토 필요.**
- `needs_body`: uncertain 또는 어느 후보라도 판단 불가. property/other는 false. 명확한 조건부만 있으면 false.
- 혼합 종목 결과는 단계별로 보존할 뿐 최종 병합 규칙은 아직 없다. 본문 결과가 RSS에 있던 모든 종목을 재출력한다는 보장도 없다. 단계별 결과를 함께 검토해야 한다. 임의로 한 종목만 골라 최종 승인하지 않는다.

## 7. 본문 접근 / 데이터 / 캐시

- `body.canonical_url`: HTTPS `blog.naver.com`/`m.blog.naver.com`의 `/hycj1025/<숫자ID>`만 허용. 추적 query 제거. 임의 호스트/경로/인증정보 금지.
- 실제 요청은 `https://m.blog.naver.com/hycj1025/<ID>` 1회. 리다이렉트 차단, 20초, 디코딩 후 최대 2MB, HTML content-type 확인.
- `.se-main-container` 또는 `#postViewArea`의 읽을 수 있는 텍스트만 수집. script/style 등 제외. 완결된 본문 컨테이너가 없으면 실패. 로그인/접근차단 우회나 범용 브라우저 자동화 없음.
- 이미지/OCR/동영상 내용은 처리하지 않으므로 추출한 텍스트가 원문 정보 전체를 포함한다고 주장하면 안 된다.

실제 접근 확인(2026-11-14):

| 글 | ID | 본문 텍스트 길이 |
| --- | --- | --- |
| 제이앤티지 | 224473034613 | 1,273자 |
| 엠비디 | 224471910652 | 1,749자 |
| 영광와이케이엠씨 | 224463746936 | 1,276자 |

로컬 `data/live-body-check.json`에 읽은 텍스트, URL, 수집 시각, 해시가 있다(미추적). 존재는 재개 시 확인. 공개 페이지 현재 마크업의 영구 호환성은 보장하지 않는다.

SQLite:
- 기존 `feeds/posts` 유지, 추가 테이블만 생성.
- `semantic_runs`: 원문 식별자/phase/입력 hash/backend key/status/result JSON/error/attempts/updated_at. 성공 캐시 재사용, 실패 재시도. 제공자/모델/프롬프트 버전은 캐시 키에 포함해야 함.
- `article_bodies`: 원문 식별자/RSS hash/source_url/text/content_hash/fetched_at. 같은 RSS 입력이면 본문 재사용.
- `body_fetch_errors`: 본문 수집/새로고침 실패 기록. 실패 이후 과거 성공 본문을 현재 결과로 표시하지 않고 다음 실행 재수집. 과거 텍스트는 DB에 남아 있음.
- RSS가 그대로이고 원문만 수정됐으면 자동 감지하지 않음. `--refresh-body` 필요. 본문 내용이 같으면 AI 캐시 재사용.
- 글 하나 처리 중 SQLite 쓰기 잠금을 네트워크 요청 동안 유지해 겹치는 프로세스 중복 호출을 억제한다. API 요청 45초, DB 잠금 대기 60초. 장기 운영에서는 대기/중단/워크큐 개선 검토.
- `pipeline-results`는 현재 source hash의 최근 선택 결과를 보여준다. 과거 입력 전체 이력 전용 UI/마이그레이션 버전 관리/정리 정책/전용 백업 명령은 없음.

## 8. 실제 RSS 분석 관찰 — AI API 테스트와 혼동 금지

이전 대화에서 Codex가 RSS 제목/설명을 직접 읽고 2026-09-14~11-14 기간에 현재 RSS에 남은 **29건**을 분류했다. 독립 앱의 유료 API가 수행한 결과가 아니며, 자동화 정확도 평가 완료가 아니다.
- 의견 확인 예: 옵토닉스 참여 의사, 영광와이케이엠씨·엠에스바이오 신청했다고 서술.
- 미참여 의사 예: 엠비디, 바로팜, 넥스아이, 래블업, 덕산넵코어스, 글로벌테크놀로지.
- 멜콘은 제목상 참여 의사로 해석 가능하지만 완료는 불명확. 그 외 여러 글 판단 불가.
- 영광과 영광와이케이엠씨는 별도 이름. 동원파츠 긍정 평가만으로 참여 확정 금지. 매도 후기 인텔리빅스/티앤이코리아를 사전 신호로 소급 사용 금지.
- 래미안원펜타스·철산자이 더 헤리티지는 아파트 무순위 청약. 임장/세금 글은 청약 대상과 구분.
- 본문에서는 RSS에 안 담긴 후반 의견/일정도 추출됐다. 하지만 제이앤티지처럼 본문까지 읽어도 참여 결론이 없을 수 있다.

## 9. Ollama Cloud 전환: 확인한 사실과 미확정 결정

2026-11-14 공식 문서 조회 결과(다음날 최신 여부 재확인):
- API 키로 `https://ollama.com/api/chat` 직접 호출 가능. 로컬 Ollama 설치/GPU 불필요.
- `Authorization: Bearer $OLLAMA_API_KEY`, messages, `stream:false`; 응답은 `message.content`.
- **Cloud는 현재 Structured Outputs 미지원**이라고 문서에 명시. 로컬 Ollama의 `format` JSON Schema 예제를 Cloud에 그대로 적용하면 안 됨.
- JSON을 프롬프트로 요청하고 로컬 검증하는 것은 가능하나 서버 강제 스키마 보장이 아니다. 형식 오류 시 성공으로 저장하지 말고 재처리 가능 실패로 남길 것.
- 신규 Pro는 월 크레딧 포함 요금제. 기존 legacy 가입자는 조건이 다를 수 있다. 사용자의 구독 여부/플랜/잔여량/모델 접근 권한은 **미확인**. 무제한으로 설명하지 말 것.

공식 출처:
- https://docs.ollama.com/cloud
- https://docs.ollama.com/cloud/api
- https://docs.ollama.com/capabilities/structured-outputs
- https://ollama.com/pricing
- https://ollama.com/blog/transparent-pricing

다음 사용자에게 확인할 사항(하나씩, 이미 답한 질문 반복 금지):
1. 실제로 Ollama Cloud를 제공자로 채택할지, Pro 계정/키 준비 여부.
2. 계정에서 이용 가능한 모델 중 어떤 모델로 시험할지. 한국어 문맥/JSON 안정성 실험 전 모델을 임의 결정하지 말 것.
3. 앞서 정한 3개 대상/최대6회/자동 재시도 없음 범위를 Ollama에서도 유지해 실호출 승인하는지. 형식 오류 추가 교정 호출도 추가 과금이므로 승인 없이 자동 추가하지 않는다.
4. 입력 원문이 Cloud에 전송되는 경계와 비용 초과 방지 설정을 확인. 금액 상한과 요청 수 상한은 다름.

## 10. 제안하는 다음 구현 순서 — 아직 미승인·미구현

1. 공식 Ollama API 계약을 확인하고 최소 `OllamaBackend` 추가. 기존 httpx 사용, 의존성 추가하지 않는 방향. 기존 OpenAI/fixture 백엔드 유지.
2. CLI의 provider/backend 선택을 명시화. OpenAI 키와 Ollama 키를 분리하고 backend key에 `ollama:<version>:<model>` 등 제공자별 identity 포함. `OPENAI_API_KEY`를 Ollama에 전송하는 실수 금지.
3. semantic 스키마와 근거 검증 공용 사용. JSON 응답 파싱/빈 응답/HTTP429/거절/잘림/timeout 테스트. JSON을 못 만들면 안전 실패. 조용히 regex로 감정을 확정하거나 fixture로 성공 위장 금지.
4. 명시적 유료 호출 opt-in 유지. 사용자가 결정한 최대 요청 수를 앱 수준에서 보장. 기본값으로 예기치 않게 DB 전체 실호출하지 않게 보호 검토.
5. `scripts/live_check.py`는 현재 OpenAI 전용이므로 CLI/backend/key-file 전달과 범위 검증을 새 제공자에 맞춰 수정하거나 별도 안전한 실행 경로 마련. 단순 `--model`만 바꾸어 호출하지 않는다.
6. 전체 오프라인 회귀 테스트, 캐시 제공자 분리/재사용/실패 재처리/본문 보류 검증. user key를 받기 전까지 실제 모델 호출 금지.
7. 승인 후 전용 DB로 세 글의 RSS 분석, 불확실한 글만 본문 재분석. 모델 원응답이 검증 실패해도 오류를 숨기지 않고 기록. 호출 수·가능하면 usage 토큰·한계 보고.
8. 사람 검토로 원문/근거/종목 연결/의견/시점 분류 비교. 본문에서도 판단 불가 유지가 정상인지 확인. 더 많은 글의 실험은 재승인.

## 11. 다음 작업자의 첫 검증 명령

```bash
cd ~/projects/ipo-monitor
git status --short
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests scripts
git diff --check
# 아직은 네트워크/유료 호출 없는 샘플로 확인
uv run ipo-monitor --feed samples/pipeline.xml --db data/handoff-demo.sqlite3
uv run ipo-monitor pipeline --skip-collect --db data/handoff-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
uv run ipo-monitor pipeline --skip-collect --db data/handoff-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
uv run ipo-monitor pipeline-results --db data/handoff-demo.sqlite3
```

새 DB라면 기준3건 → 분석호출4건 → 반복호출0건/재사용4건. 기존 DB라면 최초 카운트는 다를 수 있음. 위 명령은 유료 AI를 사용하지 않는다. 네트워크 권한 없이도 샘플 검증 가능(최초 의존성 설치는 별도).

테스트 총83개는 RSS/설정/구형 추출/새 의미분류/본문/파이프라인을 포함한다. OpenAI HTTP는 mock이고 Ollama 테스트는 아직 없다. `scripts/live_check.py`는 compileall만 확인했으며, 실제 키로 실행하거나 별도의 스크립트 전용 회귀 테스트를 작성하지 않았다.

## 12. 미완료·잔여 위험 체크리스트

- [ ] Ollama Cloud 제공자/모델/한도 인터뷰 후 연동
- [ ] 실제 모델 호출 및 한국어 의미 분석 검증
- [ ] API 호출량/비용 추적과 실행별 예산 제한 (현재 글수 limit만 존재)
- [ ] 입력 크기/컨텍스트 초과의 명시적 처리, 무손실 분할 정책 (본문은 최대2MB로서 모델 한도보다 클 수 있음)
- [ ] 모델 결과 누락 종목/혼합 글 최종 병합 규칙 확정
- [ ] 공모주 공식 일정·가격·주관사 대조 / 마감 N일 전 알림
- [ ] Telegram/Slack 채널·키·개인 알림 정책 확정 및 구현
- [ ] Windows/WSL 예약 실행과 절전/장애 복구 검증
- [ ] DB 백업/복구·보유기간/삭제·규모 증가 대응
- [ ] 공시/유튜브 소스 추가, 주문/실계좌, 콘텐츠 게시/상용화는 별도 후속 범위

### 문서 간 충돌 주의

README는 2026-11-15 공개 저장소 준비 시 최신 상태로 다시 정리했다. 초기 원본 프롬프트에는 이전 단계 설명이 남아 있으므로 실제 코드/새 검증 결과와 이 인계서를 대조한다. 초기 원본 프롬프트의 모델명은 고정 요구가 아니며 이후 사용자 결정을 우선한다. 이전 대화의 모델 가용성/가격 안내는 계정 접근 검증이 아니므로 새 제공자 선정에 그대로 사용하지 않는다.

## 13. 다음 세션에 붙여넣을 재개 프롬프트

> docs/HANDOFF_OLLAMA.md를 먼저 읽고 코드·문서·Git 상태를 확인해. Ollama Cloud로 AI 제공자를 전환하려고 해. 기존 RSS 수집과 본문 재분석 흐름, 오프라인 fixture, 기존 CLI/DB는 유지해. 중요한 결정은 혼자 하지 말고 인터뷰로 정하고, Ollama 모델·키 준비·실호출 범위부터 확인해. 기존의 OpenAI 3개글/최대6회 승인을 Ollama 호출 승인으로 간주하지 마. 유료 API는 별도 승인 전 호출하지 말고 먼저 어댑터와 테스트를 구현해. 인계서의 완료/미완료와 최신 코드 차이를 보고하고 진행해.

## 2026-11-17 최종 사용자 흐름 변경

전체 종목 요약 발송은 운영에서 중단하고 월별 청약 목록+새 블로그/유튜브 언급 이벤트 전용으로 변경했다. 현재 상태·정확한 명령·baseline/중복/월 변경/실패 처리·309테스트·실행 증거는 [MENTION_NOTIFICATIONS.md](MENTION_NOTIFICATIONS.md)가 우선한다. 로컬 Telegram은 새 방식으로 활성화되어 있고 동일 자료 실제 재실행은 AI0/발송0이었다. 소량 실제 AI 검증/이전 digest전송과 신규 이벤트 AI실검증을 구분한다.
