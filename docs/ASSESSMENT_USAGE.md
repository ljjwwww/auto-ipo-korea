# 세 출처 공통 입력 → 실험 판단 → 미발송 알림

현재 기준: **2026-11-18**. `assess`는 fixture와 명시 승인된 Ollama를 지원하는 별도 실험 경로입니다.
아래 fixture 예제는 키·네트워크 없이 동작하며 실제 AI/메신저를 호출하지 않습니다.
현재 예약 알림은 [언급 이벤트](MENTION_NOTIFICATIONS.md)이고, 초기 가중치는 검증된 투자 성과 모델이 아닙니다.

분석은 입력마다 결과·사용량을 커밋하고 판단·전이·미리보기는 별도 트랜잭션으로 저장합니다.
판단 실패 후 재실행은 성공 분석을 재사용합니다. 분석 도중 수집 상태가 바뀌면
`assessment_inputs_changed`로 중단하며 저장한 분석은 유지합니다. 명시적으로 재실행해 최신 판단을 만듭니다.
프로세스 종료/디스크 장애가 원격 응답 직후 로컬 커밋 전에 발생하는 구간의 exactly-once 과금은 보장하지 않습니다.

수집 명령과 실제 출처 범위는 [MULTI_SOURCE_USAGE.md](MULTI_SOURCE_USAGE.md)를 참고하세요.

## 1. 비용 없는 전체 예제

프로젝트 루트에서 기존 설치 환경의 Python을 사용합니다. 아래 예제는 가상 글·자막·별점만 사용합니다. 처음 사용할 DB 경로로 시작하면 예상 개수가 일치합니다.

```bash
.venv/bin/python -m ipo_monitor collect --offline --config samples/assessment/sources.toml --db data/assessment-demo.sqlite3
.venv/bin/python -m ipo_monitor assess --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --fixture samples/assessment/responses.json
.venv/bin/python -m ipo_monitor assess --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --fixture samples/assessment/responses.json
.venv/bin/python -m ipo_monitor notification-preview --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --stock 가상기업
.venv/bin/python -m ipo_monitor decisions --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --history
```

| 순서 | 새 DB의 예상 결과 |
| --- | --- |
| 오프라인 수집 | 블로그·영상·사이트 각3건, 총9개 관찰 |
| 첫 assess | 고정 응답9건, 판단 버전3건·상태 전이3건·미발송 미리보기3건 생성 |
| 같은 assess 재실행 | 호출0건·재사용9건, 새 판단/전이/미리보기0건 |

샘플 결과는 다음과 같습니다.

- **가상기업:** 검토 후보, 실험 점수0.6875.
- **가상대기기업:** 판단 대기, 점수 미정. 정보 부족을 미참여나0점으로 바꾸지 않습니다.
- **가상상충기업:** 추가 확인, 점수0.0. 출처별 양·음 점수가 상충하므로 보류합니다.

이 값은 사람이 미리 작성한 응답과 초기 산식의 결과입니다. 실제 작성자의 발언, 실제 AI 정확도, 수익 가능성을 입증하지 않습니다.

## 2. 공통 분석 입력 확인·내보내기

```bash
.venv/bin/python -m ipo_monitor evidence-inputs --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --stock DEMO --output data/assessment-inputs.json
```

`DEMO`는 샘플 설정에 등록된 `가상기업`의 별칭입니다. 이 예제는 세 출처 입력3건을 저장합니다. `--stock`을 반복해 여러 종목을 선택할 수 있고, 생략하면 사이트 종목명과 등록된 종목 목록을 기준으로 입력을 만듭니다.

공통 입력은 다음 두 층으로 나뉩니다.

| 층 | 포함 내용 |
| --- | --- |
| `input` | 버전·종목·명시적 별칭·출처 ID/유형·작성자·링크·제목·게시 시각·정리된 text/자막 segments/달력 events/ratings/table_rows |
| 관찰 연결 정보 | `snapshot_id`, `observed_at`, 출처 이름·마지막 수집 상태/오류, `input_hash` |

- 블로그는 RSS 제목·설명을 사용합니다. 기존 본문 수집 테이블의 전문을 이 입력에 자동 합치는 기능은 아직 없습니다.
- 영상은 제목·설명·확보한 자막과 원래 구간 start/end를 보존합니다. 자막이 없으면 그 상태를 남깁니다.
- 사이트는 원문 별점·❌·표시 없음·달력·표 값을 보존합니다.
- 이름/별칭 일치는 연결 후보이지 동일 공모 건 확정이 아닙니다. 서로 다른 종목에 겹치는 명시적 별칭을 등록하면 오류로 처리합니다.
- 원문이 포함되므로 파일 내보내기는 프로젝트 루트에서 `data/` 아래 경로만 허용합니다. `--output`이 없으면 터미널에 출력합니다. 키·계좌정보를 원문이나 설정에 넣지 마세요.

`input_hash`는 정규화한 `input`의 해시입니다. 로컬 DB의 snapshot ID·관찰 시각은 제외하므로 같은 자료를 다른 DB에 수집해도 분석 응답을 재사용할 수 있습니다. 반면 판단의 컨텍스트 해시는 실제 snapshot과 출처 상태도 포함해 자료 변경·실패를 추적합니다.

## 3. 고정 응답 계약

기본 `--backend fixture`는 `--fixture`가 필수입니다. Ollama는 [설정 안내](OLLAMA_SETUP.md)의
`--backend ollama --model --stock --allow-api --max-calls` 계약을 사용합니다.
fixture 파일은 **`input_hash → 응답 객체`** JSON입니다. 응답에는 다음 문자열 필드가 정확히 있어야 합니다.

| 필드 | 허용 값·검증 |
| --- | --- |
| opinion | 참여 / 조건부 / 미참여 / 판단 불가 |
| assessment | positive / negative / neutral / unknown |
| evidence | `input.content.text`에서 그대로 가져온1~300자 |
| stage | planned / applied / retrospective / unknown |
| reason | 분석 설명, 최대500자 |

종목명은 공통 입력의 `stock`에 있으므로 응답에 임의 필드를 추가하지 않습니다. 입력 해시가 없거나 응답 스키마·근거가 잘못되면 실패를 저장하고 다음 실행에 재처리합니다. 동일 입력의 성공 응답은 재사용합니다. 한 응답만 바꾸면 해당 응답의 캐시만 달라집니다.

새 테스트 응답은 `evidence-inputs`로 내보낸 입력의 해시와 원문을 대조해 작성할 수 있습니다. 공개 저장소에는 **가상 자료용 fixture만** 넣고 실제 수집 원문·개인 DB는 `data/`에 보관하세요. 올바른 JSON과 근거 구절의 존재만으로 의미 해석 정확성을 증명할 수는 없습니다.

## 4. 초기 판단 정책

정책 파일은 기본 `config/decision.toml`이며 `assess`, `decisions`, `notification-preview`에 `--policy 다른경로`로 지정할 수 있습니다. 형식이 잘못되거나 파일이 없으면 조용히 기본값으로 대체하지 않고 실패합니다.

### 현재 산식

1. 종목별로 출처의 관찰 자료를 보존합니다. **출처 ID별 최신 자료 하나**를 집계에 선택합니다. 시간대가 명확한 게시 시각을 우선하고, 없으면 관찰 시각을 사용합니다. 동률에는 관찰 시각·snapshot ID를 사용합니다. 오래된 글이 나중에 수집됐다는 이유만으로 최신 게시글의 의견을 덮지 않게 합니다.
2. 선택한 자료의 의견·평가·별점/표시 중 수치가 있는 값들을 평균한 뒤 시점 계수를 곱합니다.
3. 같은 작성자의 출처들을 출처 가중 평균으로 합칩니다. 기릿의 영상·사이트를 독립 작성자 두 명처럼 더하지 않습니다.
4. 작성자 점수를 작성자 가중 평균으로 합칩니다. 원자료·필드별 점수·선택 여부·가중치·시점 계수는 결과에 함께 남깁니다.

| 초기 매핑 | 설정값 |
| --- | --- |
| 참여 / 조건부 / 미참여 | 1 / 0.25 / -1 |
| positive / negative / neutral | 0.5 / -0.5 / 0 |
| 별0 / 별1 / 별2 / 별3 | -1 / 0 / 0.5 / 1 |
| ❌ | -1 |
| 사후 후기 시점 계수 | 0.25 |
| 나머지 시점 계수 | 1 |

**별점 없음과 별0개는 다릅니다.** 판단 불가·unknown·평가 부재를 부정 점수로 바꾸지 않습니다. ❌를 -1로 반영하는 것은 조정 가능한 초기 점수 가정이지 작성자의 실제 청약 미참여 사실을 확정하는 규칙이 아닙니다. 서로 다른 사이트 평가 표기가 함께 있으면 원문을 보존하고 추가 확인으로 표시합니다.

### 상태와 커버리지

기본 `expected_creators`는 팽·기릿 두 명입니다. 숫자 신호가 있는 작성자 수를 예상/관찰 작성자 수로 나눈 값이 커버리지이며, **정확도·신뢰확률·정보 전체의 완전성을 뜻하지 않습니다.** 추가 작성자는 분모에 포함되고 명시적으로 가중치0인 작성자는 제외됩니다.

- 점수가 전혀 없으면 **판단 대기**.
- 출처별 점수에 양·음이 함께 있거나 커버리지0.5 미만이면 **추가 확인**.
- 그 외 점수0.4 이상은 **검토 후보**, -0.4 이하는 **낮은 우선순위**, 중간은 **관찰**.
- 선택한 출처의 수집이 partial/error면 초기 정책은 점수와 자료를 버리지 않고 추가 확인으로 표시합니다. 선택한 분석의 실패나 모호한 사이트 표기도 추가 확인으로 남깁니다.

`opinion_scores`, `assessment_scores`, `rating_scores`, 출처/작성자 가중치, 시점 계수와 임계값을 조정할 수 있습니다. 이 초기 정책은 수익률로 검증하지 않았습니다. 공식 일정 대조·신선도 감쇠·공모 건 확정·실주문 규칙을 대신하지 않습니다.

## 5. 판단 이력과 알림 미리보기

```bash
.venv/bin/python -m ipo_monitor assess --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --fixture samples/assessment/responses.json --stock DEMO --channel slack --destination preview
.venv/bin/python -m ipo_monitor notification-preview --db data/assessment-demo.sqlite3 --aliases samples/assessment/stocks.toml --stock DEMO --history
```

`--channel`은 telegram/slack 중 미리보기 분류이며 `--destination`도 로컬 식별자입니다. 봇 토큰이나 실제 채널 연결 정보를 넣지 않습니다. 다른 채널/목적지 조합은 별도 미리보기로 보관합니다. 위 Slack 예제는 기존 Telegram 미리보기를 전송하거나 삭제하지 않습니다.

| 저장 단위 | 의미 |
| --- | --- |
| `observation_analyses` | 입력/fixture 응답별 성공 캐시 또는 재처리할 오류·시도 횟수 |
| `decision_versions` | 근거 묶음·정책 해시별 계산 결과, 정확한 정책 snapshot·엔진 버전·출처/분석/snapshot 연결 정보 |
| `decision_transitions` | 종목의 판단 선택이 바뀐 순서와 시각 |
| `notification_outbox` | 상태 전이·채널·목적지별 중복 없는 미발송 메시지 |

같은 상태를 연속 실행하면 새 판단/전이/미리보기를 만들지 않습니다. 정책 A→B→A처럼 되돌아오면 기존 계산 결과는 재사용하더라도 **새 상태 전이와 미리보기**를 남깁니다. 판단 이력은 점수만이 아니라 당시의 근거·정책·분석 상태를 설명합니다.

현재 입력이나 정책이 바뀌면 기존 판단에 재분석 필요를 표시하고, 기본 알림 조회에서는 과거/변경된 미리보기를 숨깁니다. `--history`로 과거 전이와 미리보기를 확인할 수 있습니다. 자료에서 종목 언급이 사라져도 저장된 이력 자체는 삭제하지 않습니다.

메시지에는 `[테스트/미발송]`, 고정 응답 안내, 상태·점수·커버리지, 상충 여부, 정책, 출처별 근거·가중치·시점·링크·확보된 자막 구간을 표시합니다. 실제 전송 기능이 없고 outbox 상태는 `preview`만 허용합니다.

## 6. 기존 명령과 남은 작업

- `collect`는 세 출처 관찰 자료를 수집할 뿐 위 가중 판단을 자동 실행하지 않습니다.
- 구형 `extract/candidates`와 기존 블로그용 `pipeline/pipeline-results`는 별개 경로입니다. 새 공통 입력·판단은 `evidence-inputs/assess/decisions/notification-preview`로 다룹니다.
- 공통 assess는 Ollama를 지원하며 제한된 실제 연결 검증 이력이 있습니다. 운영 의미 정확도 검증은 완료하지 않았습니다.
- assess는 실제 메시지를 보내지 않습니다. 현재 Telegram 운영 전송은 별도 mention_events 경로입니다.
  Slack·청약 N일 전 알림·공식 정보 대조·자동주문은 미구현입니다.

### 문서 검증

작성자는 위 명령 경로8개를 설치된 `.venv`와 새 임시 DB로 오프라인 실행했습니다. 최초9개 분석·판단/전이/미리보기 각3개, 반복 호출0개·재사용9개·신규0개, 종목별 입력 내보내기3개와 Slack별 미리보기를 확인했습니다. 이 문서 작성에서 `uv sync`나 실제 네트워크/API 호출은 실행하지 않았습니다.

리더 최종 회귀 기준은 테스트 **183개 통과**입니다. 샘플/검증 통과는 실제 모델의 정확도나 투자 성과 검증이 아닙니다.
