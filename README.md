# auto-ipo-korea

현재 버전 **0.2.0** · [변경 이력](CHANGELOG.md)

개인용 국내 공모주 정보 수집·분석 CLI. 공개 RSS에서 새 글을 저장하고, AI가 글쓴이의 청약 의견을 분석하며, 불확실한 글은 공개 본문으로 추가 확인하는 프로젝트입니다.

**개발 중인 프로토타입입니다. 투자 추천이나 수익 보장 도구가 아니며 청약·매도 주문을 실행하지 않습니다.**

## 최신 알림 방식 — 2026-11-17

**월별 청약 목록 → 블로그/유튜브 새 언급 → 한 줄 요약·원문 링크 알림**으로 변경했습니다. 전체 종목 목록을 매일/변경 때마다 전송하지 않습니다. 새 언급이 없으면 메시지0건입니다. 첫 실행은 기존 자료를 기준선으로만 저장합니다. [최신 동작·검증·한계](docs/MENTION_NOTIFICATIONS.md)가 아래 이전 기능 설명보다 우선합니다.

## 현재 상태

| 기능 | 상태 |
| --- | --- |
| 3개 출처 수집 → SQLite 관찰 저장·중복/변경 이력 | 구현 및 실제 블로그·유튜브 자막·사이트 확인 |
| 공모주/부동산/기타 분류, 종목별 의견·근거·참여 시점 | 구현, 고정 응답·mock 테스트 검증 |
| 불확실한 글의 공개 본문 수집·재분석 흐름 | 구현, 실제 본문 3건 접근 확인 |
| 공통 분석 입력·실험 가중 판단·상태 전이·알림 미리보기 | fixture와 Ollama 지원. 소량 실검증·mock 검증 |
| 실제 AI 분석 정확도 | Ollama 소량 실검증 완료. 인용·종목 연결 오탐 발견/보류 처리, 운영 품질 미검증 |
| Ollama Cloud 연동 | 숨김 키 입력·인증·실제 모델 호출·제한/캐시 검증. 운영 품질 검증은 후속 |
| 일일 실행·Windows 예약 | 구현. 매일19:00 KST 작업 등록 확인 |
| Telegram 개인 요약 알림 | 구현. 개인 연결·API 전송2건 성공·중복 억제 확인 |
| 공식 공시 대조·Slack | 미구현 |
| 청약·매도 자동화 | 미구현, 현재 수동 처리 방침 |

현재 코드에 정식 연결된 출처는 팽 [블로그](https://blog.naver.com/hycj1025), 기릿 유튜브, 기릿 공모주 사이트 세 곳입니다. 키 없는 수집·종목별 연결 후보 조회는 [3개 출처 사용법](docs/MULTI_SOURCE_USAGE.md)을 보세요. 출처 URL만 공개하며, 수집 원문·개인 DB·키는 저장소에 포함하지 않습니다.

## 일일 실행 / Telegram 연결

일일 AI 상한은 사용자 요청으로 **600회**입니다. 호출마다 비용/사용량이 발생할 수 있으며 고정 금액 한도는 아닙니다. 성공 캐시 재사용·입력별 재시도 한도·20분 실행 예산·중복 실행 잠금이 있습니다.

```bash
uv run ipo-monitor daily --dry-run
uv run ipo-monitor telegram setup       # 본인 터미널에서 토큰 숨김 입력·개인 대화 연결
uv run ipo-monitor telegram preview     # 네트워크/전송 없음
uv run ipo-monitor telegram enable      # 준비 후 지속 전송 명시 활성화
uv run ipo-monitor telegram send --allow-send
```

[Telegram 연결 안내](docs/TELEGRAM_SETUP.md)와 [매일19시 Windows/WSL 예약](docs/WINDOWS_SCHEDULER.md)을 확인하세요. 키/대화 연결 전에는 기본 비활성입니다. 현재 일일 블로그 입력은 RSS이며 자동 본문 재분석은 아직 미연결, 유튜브는 최신3개만 수집합니다.

## Ollama Cloud 키 준비

프로젝트 루트의 **사용자 WSL 터미널**에서 숨김 입력합니다. 채팅에 키를 보내지 마세요.

```bash
uv run ipo-monitor credentials set ollama
uv run ipo-monitor credentials status ollama
```

로컬 `data/secrets/ollama.key`에 권한600으로 보관하며 Git에서 제외합니다. 암호화 저장소는 아니므로 사용자 계정/백업도 보호하세요. 입력 명령은 실제 API를 호출하지 않습니다. [상세 연결·보안·호출 제한](docs/OLLAMA_SETUP.md)을 확인하세요.

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

블로그 수집은 제목·링크·원문 게시 시각·RSS 설명을 저장하며, 통합 `collect`는 유튜브 자막·사이트 달력/표도 함께 저장합니다. `collect`는 AI·알림을 호출하지 않습니다. RSS의 설명은 제공자가 내려주는 텍스트이지 우리 프로그램이 요약한 값이 아닙니다. RSS에서 사라진 과거 글을 모두 복구하지는 못합니다.

## 3개 출처 관찰 자료 조회 (AI 없음)

```bash
uv run ipo-monitor observations --stock 에스에프씨
uv run ipo-monitor observations --stock 유캐스트 --history
uv run ipo-monitor source-status
```

`config/stocks.toml`에 등록한 명시적 별칭으로 자료를 함께 찾습니다. 문자열 연결 후보일 뿐 공모 건 확정이나 참여 판단·가중 점수가 아닙니다. 전체 오프라인 재현과 수집 범위/실패 처리/기존 AI 명령과의 차이는 [사용법](docs/MULTI_SOURCE_USAGE.md)을 참고하세요.

## 공통 분석 → 실험 판단 → 미발송 알림 (키·네트워크 없음)

```bash
uv run ipo-monitor collect --offline --config samples/assessment/sources.toml --db data/assessment-demo.sqlite3
uv run ipo-monitor assess --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --fixture samples/assessment/responses.json
uv run ipo-monitor assess --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --fixture samples/assessment/responses.json
uv run ipo-monitor notification-preview --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml
```

새 DB 기준 가상 3종목×3출처 입력9건 → 판단3건·미리보기3건. 같은 명령을 반복하면 분석 호출0건·재사용9건, 새 판단/미리보기0건입니다. 가상기업은 검토 후보, 가상대기기업은 판단 대기, 가상상충기업은 추가 확인으로 구분합니다. 고정 응답은 실제 작성자의 평가가 아닙니다.

`config/decision.toml`에서 초기 가중치·임계값을 조정할 수 있고 정책/근거/변경 이력은 보존합니다. 원문의 의견·별점과 시스템의 실험 점수는 다른 값이며 **실제 AI 분석·수익 검증·메신저 전송은 아닙니다.** 상세 계약은 [분석·판단·미리보기 사용법](docs/ASSESSMENT_USAGE.md)을 참고하세요.

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
- 실제 API에는 **키와 명시적 실호출 승인**이 필요합니다. 기존 블로그 pipeline의 OpenAI 어댑터와 공통 assess의 Ollama 어댑터를 분리합니다. 키/주소를 섞어 바꾸지 마세요.
- `pipeline`은 기본적으로 지정 DB 전체를 분석합니다. `--limit`는 정렬상 앞 N개 제한이지 운영 큐나 금액 상한이 아닙니다. 제한 없는 실호출을 피하세요.
- `extract`/`candidates`는 호환용 구형 추출 명령입니다. 새 파이프라인 결과는 `pipeline-results`로 조회합니다.

## 개발·검증

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests scripts
git diff --check
```

2026-11-16 재검증: 오프라인 테스트 **208개 통과**. 통합 실제 수집은 블로그50건·유튜브3건(자막 포함)·사이트29건이며 재실행 신규/변경0건을 확인했습니다. 실제 RSS 최초 50건/재실행 0건과 공개 본문 3건 접근은 2026-11-14에 확인했습니다. Ollama 실제 요청7회와 품질 문제 보류 처리를 확인했습니다. 운영 수준 모델 품질과 Windows 예약 실행은 미검증입니다. [실제 검증 보고](docs/OLLAMA_LIVE_VALIDATION.md)를 참고하세요. 별도 lint/typecheck 도구는 아직 구성하지 않았습니다.

## 문서와 AI 재개 순서

| 문서 | 용도 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | AI 작업 지침, 승인 경계, 테스트 명령 |
| [docs/HANDOFF_OLLAMA.md](docs/HANDOFF_OLLAMA.md) | 결정 이력·현재 구현·미검증·Ollama 전환 재개 프롬프트 |
| [docs/ASSESSMENT_USAGE.md](docs/ASSESSMENT_USAGE.md) | 공통 분석·실험 정책·판단 이력·미발송 알림 |
| [docs/MULTI_SOURCE_USAGE.md](docs/MULTI_SOURCE_USAGE.md) | 3개 출처 수집·변경 이력·종목별 후보 조회 명령 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 모듈·DB·스키마·캐시·보안 경계 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 개발·검증·fixture와 확장 절차 |
| [docs/PRODUCT_VISION.md](docs/PRODUCT_VISION.md) | 장기 사용자 여정·단계별 목표 |
| [초기 프롬프트](docs/CODEX_IPO_MONITOR_PROMPT.md), [후속 프롬프트](docs/CODEX_IPO_NEXT_PROMPT.md) | 원본 요구 기록. 이후 인터뷰 결정이 우선 |

최신 확정 계획은 **팽 블로그 + 기릿 유튜브 + 기릿 사이트의 데이터 축적 → 가중 종합 판단 → Telegram/Slack 개인 알림**입니다. 키 없는 수집·공통 분석 입력·실험 가중 판단·미발송 알림 묶음까지 구현했습니다. Ollama 어댑터·키 입력도 준비했으며 실제 인증·소량 호출은 확인했고, 모델 운영 품질·메신저 전송·운영 정책 검증은 남아 있습니다. [3개 출처 계획](docs/MULTI_SOURCE_PLAN.md)을 참조하세요. 사용자는 이후 첫 수집 묶음 개발을 승인했고, 추가 인터뷰 없이 권장안으로 진행하도록 변경했습니다. 자동주문·게시·유료화는 별도 후속 범위입니다.

## 비밀정보·데이터·라이선스

`.env*`, 키 파일을 둔 `data/`, SQLite DB, 수집 원문, `.venv`, 로컬 AI 런타임 상태는 커밋하지 않습니다. 로그에 키·계좌·토큰·API 원응답을 남기지 마세요. 저장소는 가상 샘플만 포함합니다. 실제 출처의 저작권·약관은 별도로 준수해야 합니다.

소스의 공개 배포 라이선스는 아직 지정하지 않았습니다. 공개 저장소라는 이유만으로 상업적 재사용 권한을 부여한 것으로 해석하지 마세요. 라이선스 선택은 소유자가 결정할 후속 사항입니다.
