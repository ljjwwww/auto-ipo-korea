# 개발과 검증

현재 구현과 제한은 [ARCHITECTURE.md](ARCHITECTURE.md), 다음 작업의 결정 사항은 [HANDOFF_OLLAMA.md](HANDOFF_OLLAMA.md)를 먼저 읽습니다. 이 문서의 예제는 프로젝트 루트에서 실행합니다.

## 환경

Python 3.12와 uv를 사용합니다. `.python-version`, `pyproject.toml`, `uv.lock`을 유지하고 새 의존성은 사용자 요청 없이 추가하지 않습니다. 기존 환경은 다음 명령으로 재현합니다. 최초 의존성 설치에는 네트워크가 필요할 수 있습니다.

```bash
uv sync --locked
```

WSL에서 검증한 프로젝트 Python은 3.12.16입니다. 시스템 Python 대신 uv의 프로젝트 환경을 사용합니다. uv 캐시 쓰기가 제한된 환경에서는 정상 권한 승인 경로를 사용하거나 이미 준비된 `.venv/bin/python`으로 오프라인 테스트를 실행합니다.

## 기본 검증

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

2026-11-15 문서 작성 시 **83개 테스트**가 통과했습니다. 실제 외부 API는 mock이며 모델 정확도를 검증한 결과가 아닙니다. 별도 lint/typecheck 도구는 현재 구성되어 있지 않습니다. `git diff --check`는 추적/스테이징된 diff의 공백 검사이므로 새 파일 자체 검토를 대신하지 않습니다.

| 테스트 파일 | 검증 대상 |
| --- | --- |
| test_cli.py | 최초 수집·반복·신규 추가, 잘못된 피드, ID, 시각, HTTP 오류 |
| test_sources.py | 출처 설정·활성 여부·반복 수집 |
| test_extraction.py | 구형 추출·캐시·근거·API 승인·응답 오류 |
| test_semantic.py | 새 분류 계약·본문 조건·fixture·API transport |
| test_body.py | 본문 HTML 추출·호스트 제한·리다이렉트/크기/오류 |
| test_pipeline.py | 본문 재분석·재사용·실패 재처리·현재 결과 조회 |

## API 키 없는 샘플 검증

아래 새 DB 경로를 처음 사용할 때 RSS 기준 3건, 첫 분석 호출 4건, 반복 분석 호출 0건/재사용 4건을 기대합니다. fixture 모드는 미리 정한 응답을 반환하며 AI 추론이 아닙니다.

```bash
.venv/bin/python -m ipo_monitor --feed samples/pipeline.xml --db data/development-demo.sqlite3
.venv/bin/python -m ipo_monitor pipeline --skip-collect --db data/development-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
.venv/bin/python -m ipo_monitor pipeline --skip-collect --db data/development-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
.venv/bin/python -m ipo_monitor pipeline-results --db data/development-demo.sqlite3
```

기대 결과는 가상기업 RSS 판단 불가 → 본문 미참여, 가상조건기업 조건부(본문 생략), 가상아파트 부동산(본문 생략)입니다. fixture 모드에는 본문 fixture도 필수라 실수로 웹 본문을 읽지 않습니다. 기존 데이터가 있는 경로를 사용하면 첫 실행 카운트가 달라집니다.

## fixture를 수정할 때

두 형식을 혼동하지 않습니다.

- **구형 `extract`:** 원문 링크 → `{input_hash, response}`. `samples/extractions.json` 참조.
- **새 `pipeline`:** 입력 digest → 응답. `samples/pipeline-responses.json` 참조.
- **새 본문 fixture:** RSS 링크 문자열 그대로 → 본문 텍스트. `samples/pipeline-bodies.json` 참조.

정규화 입력은 `extraction.source_input(post)`이며 `digest`는 정렬된 compact JSON의 SHA-256입니다. 새 본문 단계 입력은 RSS 입력에 `body` 필드를 추가한 객체입니다. 샘플 글·시각·본문을 바꾸면 대응 해시도 갱신합니다. 유효하지 않은 응답이나 잘못 연결된 fixture를 실제 성공으로 취급하지 않습니다. 작성 예는 `tests/test_pipeline.py`의 준비 코드가 기준입니다.

## 제공자 확장 시 계약

기존 `semantic.FixtureBackend`와 `AIBackend`는 다음 인터페이스를 사용합니다.

- `cache_key(source)`: 제공자·모델·프롬프트/스키마 버전을 구별하는 안정적인 키.
- `extract(source)`: 스키마/근거 검증을 통과한 결과 또는 `ExtractionError`.

Ollama Cloud는 다음 작업 후보일 뿐 현재 지원하지 않습니다. 현행 Cloud 구조화 출력 지원 여부를 공식 문서에서 다시 확인하고, 로컬 Ollama 전용 예제를 그대로 적용하지 않습니다. JSON을 프롬프트로 요청하는 것과 서버가 스키마를 강제하는 것은 다릅니다. 기존 `semantic.validate` 및 본문 확인 정책을 유지하고, JSON 형식 오류·잘림·거절·429·timeout을 mock으로 검증합니다.

OpenAI와 Ollama의 키·엔드포인트·캐시 이름을 분리합니다. 기존 OpenAI 승인이나 키를 다른 제공자에 재사용하지 않습니다. 제공자/모델/비용/대상 범위는 사용자와 인터뷰로 확정하고, 실호출 승인이 있을 때만 실행합니다. 실패 교정을 위한 추가 호출도 예산과 승인을 소비합니다.

## 변경 전후 체크리스트

1. 변경할 동작과 테스트 가능한 성공 기준을 정합니다. 범위·비용·보관·제공자 같은 중요한 선택은 사용자에게 확인합니다.
2. 기존 CLI 및 DB 데이터 호환성을 유지합니다. 캐시 의미가 바뀌면 버전/식별자를 갱신하고 마이그레이션 테스트를 추가합니다.
3. 입력 부족·혼합 종목·의견 변경·단순 비교·사후 후기를 포함해 회귀 테스트를 작성합니다. 판단 불가를 미참여로 바꾸지 않습니다.
4. 변경 테스트 → 전체 테스트 → compileall → diff 검사를 실행합니다. 샘플 성공을 실제 AI 품질로 보고하지 않습니다.
5. 성공한 본문 새로고침·실패·다음 실행의 재처리와 과거 판단 숨김을 함께 확인합니다.
6. 키·계좌정보·로컬 DB·실제 수집 전문·로그·`.omx/` 등을 커밋하지 않습니다. 원문 샘플이 필요하면 가상화하거나 허용 범위를 먼저 확인합니다.
7. README·아키텍처·인수인계의 구현/검증 상태를 함께 갱신합니다. 실거래·알림·유료 호출은 테스트 부수 효과로 실행하지 않습니다.

## 검증 경계

이 문서 작성 시 `.venv/bin/python` 테스트·샘플 명령을 직접 실행했습니다. uv 설치/새 환경 구축과 실제 RSS/API 호출은 이 문서 작업에서 수행하지 않았습니다. 과거 공개 RSS·본문 접근 확인 기록은 인수인계 문서에 있으며, 이후 마크업·모델·계정 가용성을 보장하지 않습니다.

## 3개 출처 수집 확장

현재 회귀 기준은 테스트133개입니다. 신규 tests/test_multisource.py, test_youtube.py, test_ipo_site.py가 통합 저장/이력/조회, VTT/피드/다운로더, 사이트 달력/표를 검증합니다. 오프라인 실행 명령과 fixture 경로는 [MULTI_SOURCE_USAGE.md](MULTI_SOURCE_USAGE.md) 참조.

이전 문서의 테스트83개와 RSS-only 설명은 초기 기준입니다. 기존 CLI 회귀를 유지하고 새 공급원의 원문 관찰을 AI 판단으로 위장하지 마세요. collect 설정은 세 출처로 늘었으나 AI pipeline은 블로그 posts만 분석합니다.

## 0.2.0 분석·판단·미리보기

회귀 기준은183개로 늘었다. tests/test_assessment.py가 원문 기반 검증·캐시·스냅샷/정책 무효화·상태 복귀 이력·미발송 중복 억제를, test_decision.py와 test_preview.py가 초기 정책·작성자 그룹·메시지 계약을 검증한다. [오프라인 재현](ASSESSMENT_USAGE.md)을 먼저 실행하며 실제 API나 실제 발송을 테스트 대체로 사용하지 않는다. pyproject/lock 버전은0.2.0, 신규 프로젝트 의존성은 없다.
