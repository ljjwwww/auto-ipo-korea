# auto-ipo-korea

개인용 국내 공모주 정보 수집·분석 CLI. 공개 RSS에서 새 글을 저장하고, AI가 글쓴이의 청약 의견을 분석하며, 불확실한 글은 공개 본문으로 추가 확인하는 프로젝트입니다.

**개발 중인 프로토타입입니다. 투자 추천이나 수익 보장 도구가 아니며 청약·매도 주문을 실행하지 않습니다.**

## 현재 상태

| 기능 | 상태 |
| --- | --- |
| 출처 설정 → RSS 수집 → SQLite 저장·중복 방지 | 구현 및 실제 블로그 확인 |
| 공모주/부동산/기타 분류, 종목별 의견·근거·참여 시점 | 구현, 고정 응답·mock 테스트 검증 |
| 불확실한 글의 공개 본문 수집·재분석 흐름 | 구현, 실제 본문 3건 접근 확인 |
| 실제 AI 분석 정확도 | **미검증 — 실제 유료 API 호출 0회** |
| Ollama Cloud 연동 | 다음 작업, 아직 미구현 |
| 공식 공시 대조·Telegram/Slack·예약 실행 | 미구현 |
| 청약·매도 자동화 | 미구현, 현재 수동 처리 방침 |

실제 수집 출처는 사용자가 지정한 [네이버 블로그 hycj1025](https://blog.naver.com/hycj1025) 하나입니다. 출처 URL만 공개하며, 수집 원문·개인 DB·키는 저장소에 포함하지 않습니다.

## 시작하기

Python 3.12와 [uv](https://docs.astral.sh/uv/)를 사용합니다. WSL2 Ubuntu에서 개발했으며 Windows 네이티브 실행은 아직 검증하지 않았습니다.

```bash
git clone https://github.com/ljjwwww/auto-ipo-korea.git
cd auto-ipo-korea
uv sync --locked
```

설치된 환경에서는 아래 샘플 검증에 API 키나 외부 네트워크가 필요 없습니다. 최초 의존성 설치에는 네트워크가 필요할 수 있습니다.

### 1. RSS 저장과 중복 방지

```bash
uv run ipo-monitor --feed samples/feed.xml --db data/sample.sqlite3
# 새 DB 최초: 기준 목록 2건
uv run ipo-monitor --feed samples/feed.xml --db data/sample.sqlite3
# 새 글 없음: 0건
```

### 2. 비용 없는 전체 파이프라인 샘플

```bash
uv run ipo-monitor --feed samples/pipeline.xml --db data/pipeline-demo.sqlite3
uv run ipo-monitor pipeline --skip-collect --db data/pipeline-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
uv run ipo-monitor pipeline --skip-collect --db data/pipeline-demo.sqlite3 --backend fixture --fixture samples/pipeline-responses.json --body-fixture samples/pipeline-bodies.json
uv run ipo-monitor pipeline-results --db data/pipeline-demo.sqlite3
```

새 DB 기준 RSS 3건 → 고정 응답 분석 4건 → 재실행 분석 호출 0건·재사용 4건. 가상기업은 RSS 판단 불가 → 본문 미참여, 조건부 의견은 조건부로 보존하고 아파트 글은 부동산으로 분류합니다.

**fixture는 미리 작성한 테스트 응답입니다. 실제 AI가 추론한 결과나 정확도 검증이 아닙니다.**

### 3. 설정 출처 수동 수집 (외부 네트워크 사용)

```bash
uv run ipo-monitor collect
uv run ipo-monitor collect
```

`config/sources.toml`의 활성 출처를 읽어 기본 `data/ipo-monitor.sqlite3`에 저장합니다. 새 글이 없으면 두 번째 실행은 0건입니다. 수집 시점에 따라 최초 개수는 다릅니다. 비활성화는 `enabled = false`, 다른 파일/DB는 `--config`, `--db`로 지정합니다. 상대 경로는 프로젝트 루트에서 실행하는 것을 전제로 합니다.

수집은 제목·링크·원문 게시 시각·RSS 설명을 저장하며, `collect` 자체는 글 전문·AI·알림을 호출하지 않습니다. RSS의 설명은 제공자가 내려주는 텍스트이지 우리 프로그램이 요약한 값이 아닙니다. RSS에서 사라진 과거 글을 모두 복구하지는 못합니다.

## 분석 동작과 안전 경계

```text
RSS 수집 → 신규/변경 저장 → AI 유형·의견 분석
                           ↓ 불확실한 경우
                     공개 본문 확인 → AI 재분석
                           ↓
                   단계별 저장·터미널 조회
```

- 의견: **참여 / 조건부 / 미참여 / 판단 불가**. 명시된 작성자 의사만 분석하며 호평·경쟁률을 참여 선언으로 보지 않습니다.
- 시점: 예정 / 신청했다고 서술 / 사후 후기 / 불명. 실제 증권사 접수 사실을 검증하지 않습니다.
- 판단 불가 후보가 있거나 유형/정보가 불확실하면 본문 확인. 명확한 부동산/기타와 명확한 조건부만 있는 글은 생략합니다.
- 본문 실패·모호함을 미참여로 바꾸지 않습니다. RSS/본문 판단을 별도 보존하며 최종 종목 병합 정책은 미완료입니다.
- 성공 분석은 입력·모델·프롬프트 버전별 재사용, 실패는 다음 실행에 재처리합니다. 본문만 바뀌면 `--refresh-body`가 필요합니다.
- 본문 수집은 현재 위 블로그의 공개 HTTPS 글 주소만 허용합니다. 로그인/차단/외부 리다이렉트를 우회하지 않으며 이미지/OCR은 처리하지 않습니다.
- 실제 API에는 **키와 명시적 실호출 승인**이 필요합니다. 현재 OpenAI 어댑터만 존재하며 Ollama로 키/주소만 바꾸는 것은 지원하지 않습니다.
- `pipeline`은 기본적으로 지정 DB 전체를 분석합니다. `--limit`는 정렬상 앞 N개 제한이지 운영 큐나 금액 상한이 아닙니다. 제한 없는 실호출을 피하세요.
- `extract`/`candidates`는 호환용 구형 추출 명령입니다. 새 파이프라인 결과는 `pipeline-results`로 조회합니다.

## 개발·검증

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests scripts
git diff --check
```

2026-11-15 재검증: 오프라인 테스트 **83개 통과**. 실제 RSS 최초 50건/재실행 0건과 공개 본문 3건 접근은 2026-11-14에 확인했습니다. AI API는 mock이며 실제 모델 품질·Windows 예약 실행은 검증하지 않았습니다. 별도 lint/typecheck 도구는 아직 구성하지 않았습니다.

## 문서와 AI 재개 순서

| 문서 | 용도 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | AI 작업 지침, 승인 경계, 테스트 명령 |
| [docs/HANDOFF_OLLAMA.md](docs/HANDOFF_OLLAMA.md) | 결정 이력·현재 구현·미검증·Ollama 전환 재개 프롬프트 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 모듈·DB·스키마·캐시·보안 경계 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 개발·검증·fixture와 확장 절차 |
| [docs/PRODUCT_VISION.md](docs/PRODUCT_VISION.md) | 장기 사용자 여정·단계별 목표 |
| [초기 프롬프트](docs/CODEX_IPO_MONITOR_PROMPT.md), [후속 프롬프트](docs/CODEX_IPO_NEXT_PROMPT.md) | 원본 요구 기록. 이후 인터뷰 결정이 우선 |

다음 목표는 **Ollama Cloud 제공자·모델·한도를 사용자와 확정 → 어댑터와 오프라인 테스트 → 별도 승인한 소량 실호출**입니다. 이후 공식 일정 대조와 개인 알림을 구현합니다. 자동주문·게시·유료화는 별도 후속 범위입니다.

## 비밀정보·데이터·라이선스

`.env*`, 키 파일을 둔 `data/`, SQLite DB, 수집 원문, `.venv`, 로컬 AI 런타임 상태는 커밋하지 않습니다. 로그에 키·계좌·토큰·API 원응답을 남기지 마세요. 저장소는 가상 샘플만 포함합니다. 실제 출처의 저작권·약관은 별도로 준수해야 합니다.

소스의 공개 배포 라이선스는 아직 지정하지 않았습니다. 공개 저장소라는 이유만으로 상업적 재사용 권한을 부여한 것으로 해석하지 마세요. 라이선스 선택은 소유자가 결정할 후속 사항입니다.
