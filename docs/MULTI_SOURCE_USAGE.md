# 3개 출처 수집·관찰 이력·종목별 후보 조회

현재 수집 계약: 2026-11-18. **AI 키 없이** 팽 블로그·기릿 유튜브·기릿 사이트를 수집합니다.
`collect`는 참여 판단·알림·주문을 실행하지 않습니다. 별도 운영 알림은 [OPERATIONS.md](OPERATIONS.md)를 참고합니다.

## 1. 실제 출처 수동 수집

WSL/Ubuntu 프로젝트 루트에서:

```bash
uv sync --locked
uv run ipo-monitor collect
uv run ipo-monitor collect
uv run ipo-monitor source-status
```

기본 설정 `config/sources.toml`, 기본 DB `data/ipo-monitor.sqlite3`. 최초는 현재 조회 범위를 기준 목록으로 저장하고 이후 신규/변경/동일 건수를 표시합니다. 같은 DB를 계속 사용하세요. 제목·내용이 바뀌었다가 예전 값으로 돌아와도 별도 변경 이력으로 남습니다. HTTP 실패, 파싱 실패, 자막 없음/오류와 빈 피드는 구분합니다.

다른 DB와 특정 출처만 사용할 수도 있습니다:

```bash
uv run ipo-monitor collect --db data/my-observations.sqlite3 --source girit-youtube
uv run ipo-monitor collect --db data/my-observations.sqlite3 --source pang-blog --source girit-site
uv run ipo-monitor source-status --db data/my-observations.sqlite3
```

정식 출처 ID는 `pang-blog`, `girit-youtube`, `girit-site`입니다. `enabled=false`는 수집을 중지하지만 기존 관찰 자료를 삭제하지 않습니다. 출처 ID에 연결된 URL/type을 바꾸려면 새 ID를 사용하세요. 예전 name/url/enabled만 있는 RSS 설정도 유지됩니다.

## 2. 유튜브 수집 범위와 도구

- 채널 Atom 피드에서 영상 ID·제목·설명·게시/수정 시각을 읽습니다.
- `max_videos=3`은 **현재 피드에 나온 최신 영상 3개**를 대상으로 합니다. 최초 과도한 다운로드를 피하는 조정 가능한 범위이며 처리 큐나 전체 채널 아카이브가 아닙니다. 실행 사이 3개 초과 업로드, 피드에서 사라진 영상은 놓칠 수 있습니다. 필요하면 1~50 범위로 늘리되 피드 자체 범위는 별도 한계입니다.
- 설치된 `yt-dlp`가 있으면 사용하고, 없으면 `uvx yt-dlp`를 호출합니다. uvx 도구 캐시와 최초 설치 네트워크 권한이 필요할 수 있습니다. 프로젝트 pyproject 의존성은 추가하지 않았습니다. 도구 버전은 아직 고정하지 않았으므로 운영 재현성 검증 시 버전 고정을 검토하세요.
- `--ignore-config --skip-download --write-subs --write-auto-subs`로 **자막만** 요청합니다. 영상/음원/썸네일/로그인 쿠키는 수집하지 않습니다.
- `ko-orig`, `ko` 자막을 요청하고 VTT 형식을 확인합니다. 둘 다 있으면 ko-orig를 선택하며, ko만 있으면 해당 트랙을 사용합니다. 실제 언어 코드를 함께 보존합니다. 언어 선택은 원문 의미의 정확성을 보장하지 않습니다.
- 인접 자동자막의 반복 표시를 정리하되 구간의 start/end를 남깁니다. 단어별 시점은 복원하지 않습니다. 종목명 오인식/발언 뜻을 자동 보정하지 않습니다.
- 자막 성공은 메타데이터가 같고 마지막 확인 후 `subtitle_refresh_hours`(기본24시간, 0~8760)가 지나지 않았으면 재사용합니다. `refresh_subtitles=true`는 강제 재확인입니다. 자막 없음/실패는 다음 수집에서 다시 시도합니다. 확인 시각만 바뀌면 새 원문 스냅샷을 만들지 않습니다.
- 최초 기준선 이후 최신 영상 창 전체가 이전에 본 영상과 겹치지 않으면 누락 가능성을 `source_attempts.warning`과 `source-status`/`status`에 표시합니다. 의도한 최신3개 제한을 매번 실패로 만들거나 과거 영상을 복구하는 기능은 아닙니다.
- 자막 상태는 available/missing/error. 일부 자막이 없거나 실패하면 메타데이터는 저장하고 출처는 `partial`, 명령은 종료코드1입니다. 예전 성공 자막 등은 `--history`에서 볼 수 있습니다.
- 네트워크 요청·프로세스는 제한 시간과 재시도0을 사용합니다. 도구/사이트 변경, 차단, 자막 미제공을 우회하지 않습니다.

## 3. 사이트 수집

`https://letsgetitstock.com/ipo` 공개 HTML의 달력과 표만 읽습니다. API·로그인·숨겨진 데이터를 읽지 않습니다. 별/❌/표시 없음, 날짜·이벤트 종류, 수요예측 등 표의 값과 미정 표시를 원문 관찰로 저장합니다. 수치를 참여 의사나 점수로 바꾸지 않습니다. 날짜와 표 행이 구조적으로 잘못되면 실패로 기록합니다.

인식한 IPO 구역의 표 헤더가 바뀌거나 사라지면 조용히 건너뛰지 않고 실패로 기록합니다.
무관한 공지 표는 무시합니다. 별과 ❌가 섞인 표시는 원문을 보존하고 충돌로 표시하며
별점 하나로 확정하지 않습니다. RSS는 HTTP·로컬 모두2MB를 제한하고 DTD/entity 선언을 거절합니다.

달력 월이 바뀌거나 영상 피드에서 항목이 사라져도 이력을 삭제하지 않습니다. **조회 결과는 항목별 마지막 저장 관찰이지 지금도 화면/피드에 노출된다는 보장이 아닙니다.** 스냅샷 시각과 출처의 마지막 수집 상태를 함께 보세요. 발행 시각이 없는 사이트 데이터는 관찰 시각만 기록하고 발행 시각을 추정하지 않습니다.

## 4. 종목명·등록 별칭으로 조회

```bash
uv run ipo-monitor observations --stock 에스에프씨
uv run ipo-monitor observations --stock SFC
uv run ipo-monitor observations --stock 유캐스트 --history
uv run ipo-monitor observations --source girit-site --limit 5
```

- `config/stocks.toml`의 명시적 별칭을 함께 검색합니다. 기본 에스에프씨/SFC 그룹이 있습니다. 임의 유사도·AI 추정으로 별칭을 만들지 않습니다.
- 사이트에 나온 정확한 종목명으로 검색하거나, 사용자가 `[[stocks]] name=... aliases=[...]`를 추가할 수 있습니다. 조회할 종목이 등록돼 있지 않아도 입력 문자열 그대로 검색합니다.
- 블로그 제목·설명, 사이트 관찰, 자막의 문자열 일치는 **연결 후보**입니다. 기업/공모 건 동일성, 참여 여부, 가중치나 결론을 확정하지 않습니다. 한글 부분 문자열 검색은 짧은 이름에서 오탐이 생길 수 있습니다.
- 출처 이름·작성자·링크·발행/관찰 시각·전체 payload를 함께 출력합니다. 자막 payload에는 시점별 발언이 포함됩니다.
- 기본은 항목별 최신 관찰, `--history`는 변경 이력을 포함합니다. 현재 데이터에서 사라진 문구는 기본 검색에 과거 결과로 섞지 않습니다.
- `--limit`는 표시 개수만 제한합니다. 원문은 신뢰하지 않는 데이터이며 터미널 제어 문자를 제거합니다.
- 마지막 출처 수집이 실패/부분완료면 마지막 저장 관찰일 수 있음을 경고합니다. 실패가 있었다는 사실을 과거 데이터로 숨기지 않습니다.

## 5. 완전 오프라인 재현

```bash
uv run ipo-monitor collect --offline --config samples/multisource/sources.toml --db data/multisource-demo.sqlite3
uv run ipo-monitor collect --offline --config samples/multisource/sources.toml --db data/multisource-demo.sqlite3
uv run ipo-monitor observations --db data/multisource-demo.sqlite3 --stock 가상기업
uv run ipo-monitor source-status --db data/multisource-demo.sqlite3
```

새 DB 기준 각 출처 신규1건 → 각 동일1건 → 가상기업 연결 후보3건. 데이터는 모두 가상이며 AI 분석이 아닙니다. `--offline`은 활성 출처의 fixture가 모두 있어야 시작하며 네트워크/yt-dlp를 호출하지 않습니다. fixture 상대경로는 TOML 파일의 디렉터리 기준입니다.

샘플을 복사한 뒤 사이트 JSON의 별 개수·원문 표시를 함께 바꿔 재실행하면 해당 항목만 변경으로 기록됩니다. 되돌리면 되돌아온 값도 별도 변경입니다. 영상 자막 파일 파싱·다운로더 자체는 mock/VTT 단위 테스트로 검증합니다.

## 6. 기존 기능과 데이터 호환

- `--feed` 직접 실행, 구형 `extract/candidates`, 새 의미분석 `pipeline/pipeline-results`를 유지합니다.
- 통합 collect의 블로그 RSS는 기존 feeds/posts에도 저장하므로 기존 블로그 분석은 계속 가능합니다.
- 추가 테이블: `tracked_sources`(출처 정의), `source_snapshots`(불변 변경 관찰), `source_attempts`(실행별 성공/오류·개수). 기존 테이블을 삭제/변경하지 않습니다.
- **유튜브/사이트 관찰은 기존 AI pipeline 입력으로 아직 연결하지 않았습니다.** pipeline은 기본 collect로 세 출처를 읽을 수 있지만 AI 처리 대상은 기존 posts의 블로그 글뿐입니다. 현재 키 없는 기능은 `collect/observations/source-status`입니다.
- 통합 관찰 테이블과 기존 본문 수집 테이블의 전문은 아직 통합하지 않았습니다. 블로그 관찰은 RSS 설명 기반입니다.
- 성공0, 실패/부분완료1, CLI 인수 오류2. 한 출처 수집 오류여도 나머지 출처를 계속 처리합니다.

## 7. 과거 실제 검증 기록 — 2026-11-15

2026-11-15 별도 `data/three-source-check.sqlite3`에서 실검증:
- 블로그50개 글, 유튜브 최신3개 영상/한국어 자막, 사이트29개 종목 관찰 저장.
- 유튜브 실제 피드의 UC 접두어 생략과 VTT 공백 줄을 회귀 테스트로 추가해 수정.
- 즉시 재실행: 신규/변경0건, 동일50/3/29건, 세 출처 모두 success. 자막 성공 캐시 재사용.
- 스냅샷에서 종목별 조회와 원문/별칭 후보를 확인. 로컬 관찰 자료/자막/DB는 Git 제외.
- 테스트133개 통과. 실제 유료 AI/메신저 전송/예약 실행/주문은 0회.
- 별도 독립 리뷰 에이전트는 사용량 제한으로 실행하지 못했습니다. 구현자 검토와 회귀 테스트로 검증했으며 독립 리뷰 완료를 주장하지 않습니다.

다음은 이 관찰 자료를 분석 입력으로 연결하고, 사용자 확정 방향에 맞춰 가중 종합 판단 및 개인 알림을 추가하는 단계입니다. 이번에는 점수/알림 기능을 만들거나 실제 AI를 호출하지 않았습니다.
