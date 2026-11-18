# Ollama Cloud 키 입력과 제한된 연결

현재 연결 코드·숨김 키 입력·인증과 소량 실호출을 확인했습니다. 자세한 실제 결과와 오탐/비용 한계는 [실제 검증 보고](OLLAMA_LIVE_VALIDATION.md)를 보세요. 아래는 키 설정과 제한된 실행 안내입니다.

## 1. 키 입력 (사용자 WSL 터미널)

Ollama 계정에서 API 키를 발급받은 뒤, 프로젝트 루트에서 실행하세요. 발급 안내는 [공식 Cloud API 문서](https://docs.ollama.com/cloud/api)를 참고합니다.

```bash
cd ~/projects/ipo-monitor
uv run ipo-monitor credentials set ollama
uv run ipo-monitor credentials status ollama
```

첫 명령은 화면에 나타나지 않는 키를 두 번 입력받습니다. 입력값은 셸 명령 인수/히스토리·채팅에 넣지 않습니다. 이 명령 자체는 API 요청을 하지 않으며, 비대화형/piped 입력의 화면 표시 대체 기능은 거부합니다. 입력 불일치나 취소는 기존 키를 덮어쓰지 않습니다.

저장 위치는 `data/secrets/ollama.key`, 디렉터리 권한700·파일 권한600입니다. `data/`는 Git 제외입니다. 임시 파일을 권한600으로 만든 뒤 교체하고, 심볼릭 링크 경로·다른 사용자가 읽을 수 있는 키 파일은 거부합니다.

**파일은 암호화된 금고가 아니라 로컬 평문입니다.** 같은 WSL 사용자·관리자·백업 프로그램은 접근할 수 있으므로 프로젝트/WSL 계정과 백업을 보호하세요. 키 값은 로그·스크린샷·채팅·Git에 넣지 마세요. `data/`를 별도 원격 서비스로 동기화하지 마세요. 키가 노출되면 Ollama에서 폐기·재발급해야 합니다.

`OLLAMA_API_KEY` 환경변수가 설정되어 있으면 파일보다 우선합니다. 기존 환경변수가 잘못된 경우 파일을 다시 입력해도 우선순위가 바뀌지 않으므로 환경 설정을 확인하세요. 상태 명령은 키의 준비 여부만 확인하고 값이나 접두사를 출력하지 않으며, 실제 유효성/구독/잔여량/모델 접근 권한을 확인한 것은 아닙니다.

## 2. 연결 코드의 경계

- 공통 입력용 `assess`에 `--backend ollama`를 추가했습니다. 기존 fixture와 구형 OpenAI 명령은 유지합니다.
- Ollama 전용 키만 읽고 `https://ollama.com/api/chat`의 Bearer 인증으로 전달합니다. OpenAI 키는 재사용하지 않습니다.
- 고정 HTTPS endpoint, 리다이렉트/환경변수 프록시 비활성화, 45초 timeout, 자동 재시도0입니다.
- Cloud의 서버 강제 structured outputs를 가정하지 않습니다. JSON 응답을 프롬프트로 요청한 뒤 로컬 형식·근거 검증을 통과해야 성공으로 저장합니다.
- `done`, 종료 사유, 오류/거절/도구 호출 여부, JSON·필드·원문 근거를 확인합니다. 잘린 답·코드블록·중복 JSON 키·원문에 없는 근거 등은 실패로 남깁니다. 실패 후 재실행은 추가 사용량이 들 수 있습니다.
- 입력은10만 문자 이하만 허용하며 조용히 자르지 않습니다. `think:false`, `temperature=0`, `num_predict=1200`을 요청하지만 모델별 처리와 reasoning 과금 등을 포함한 금액 상한을 보장하지 않습니다.
- 제공자·모델·프롬프트/옵션별로 분석 캐시를 분리합니다. API 키 자체는 해시/DB 캐시 식별자에 포함하지 않습니다.

공식 계약 확인: [Chat](https://docs.ollama.com/api/chat), [Cloud](https://docs.ollama.com/cloud), [Structured Outputs 제한](https://docs.ollama.com/capabilities/structured-outputs) (2026-11-16). 실제 계정의 모델 목록·요금은 별도 확인 대상입니다. API model ID와 로컬 CLI의 `:cloud` 이름이 같다고 가정하지 마세요.

## 3. 키 설정 후 제한된 실행 형태

아래는 연결 형태 설명이며 **모델과 대상·요청 범위가 준비된 뒤** 사용합니다. `MODEL_ID`는 실제 계정에서 이용 가능한 API 모델 ID로 교체해야 합니다.

```bash
uv run ipo-monitor assess --backend ollama --model MODEL_ID --allow-api --max-calls 3 --stock 에스에프씨 --db data/three-source-check.sqlite3
```

- `--allow-api`, 모델, `--stock` 명시가 모두 필요합니다. 기본 fixture 백엔드는 여전히 키·네트워크를 사용하지 않습니다.
- 실제 모델 요청 기본 상한은3회(옵션 범위1~100). 캐시 제외 필요 요청 수가 상한을 넘으면 **요청을 시작하기 전에** 중단합니다. 일부만 호출하고 나머지를 성공처럼 처리하지 않습니다.
- DB/별칭/정책은 기존 옵션을 사용합니다. 이미 저장된 관찰만 분석하며 수집이나 실제 메신저 전송을 자동 시작하지 않습니다.
- HTTP/timeout 실패도 요청 횟수를 소비합니다. API 사용량/비용은 계정 측도 확인하세요. 과거 OpenAI용 실험 승인이나 키의 존재를 무제한 호출 승인으로 간주하지 않습니다.
- 미리보기는 여전히 `[테스트/미발송]`인 실험 정책 결과입니다. Ollama 결과는 ‘실제 Ollama 분석’, fixture 결과는 ‘고정 응답’으로 구별하고, 어떤 경우에도 전송하지 않습니다.

현재 사용자의 키로 인증과 제한된 실험을 진행했습니다. 다른 계정/환경에서는 값을 출력하지 않고 준비 상태를 먼저 확인합니다.

## 이번 변경 검증

전체208개 오프라인/mock 테스트와 독립 보안 검토 통과. 키 노출·권한·symlink·비대화형 입력/취소·제공자 캐시·요청 상한 선검사·응답 검증을 확인했습니다. 실제 키 입력·API 인증·소량 모델 호출 결과와 품질 한계는 별도 보고서에 기록했습니다.
