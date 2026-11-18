# Windows에서 하루 한 번 실행 (WSL2)

Windows 작업 스케줄러가 지정한 WSL 배포판·사용자로 프로젝트의 `.venv/bin/python`을 직접 실행합니다. **Windows 네이티브 Python으로 이전하는 방식이 아닙니다.** Windows와 WSL 설치, 해당 사용자 계정의 프로젝트·의존성·키 준비가 선행되어야 합니다.

일일 실행은 세 출처 수집 → 제한된 AI 분석 → 실험 판단 → **로컬 미발송 미리보기 저장**까지입니다. Telegram은 별도 로컬 연결 후 `telegram enable`을 실행한 경우에만 전송합니다. Slack·주문은 실행하지 않습니다. 등록만으로 작업이 즉시 실행되지는 않습니다.

## 1. 사전 확인

WSL 프로젝트 루트에서:

```bash
uv sync --locked
.venv/bin/python -m ipo_monitor daily --config config/daily.toml --dry-run
.venv/bin/python -m ipo_monitor credentials status ollama
```

`--dry-run`은 기존 DB/대기 상태만 확인하며 수집·네트워크·키 읽기를 하지 않습니다. 실제 실행에는 설정의 활성화와 명시적 `--allow-api`가 필요합니다. 등록 스크립트의 `-AllowApi`가 이 플래그를 추가합니다. 키는 작업 인수·Windows 환경변수에 복사하지 않고 기존 WSL 사용자 로컬 키 파일을 사용하세요. 대화형 셸의 환경변수가 예약 실행에도 있다고 가정하지 마세요.

일일 설정의 모델·요청 상한·활성 출처를 검토하세요. 호출 수 제한은 금액 상한/정확도 보장이 아닙니다. 실패와 남은 대기 자료는 다음 실행에서 다시 처리될 수 있습니다.

Windows PowerShell에서 배포판·시간대를 확인합니다:

```powershell
wsl.exe --list --verbose
Get-TimeZone
```

아래 예시는 배포판 `Ubuntu`, WSL 사용자 `ljjw`, 경로 `/home/ljjw/projects/ipo-monitor`입니다. 실제 환경에 맞춰 바꾸세요. `19:00`은 **Windows의 현지 시간**이며 한국 오후 7시을 원하면 Windows 시간대가 `Korea Standard Time`인지 확인하세요. 스크립트는 시간대를 변경하지 않습니다.

## 2. 미리보기 후 등록

WSL 프로젝트 루트에서 Windows PowerShell을 호출할 수 있습니다. `wslpath -w`는 PowerShell 스크립트 경로만 Windows 형식으로 바꾸며 `ProjectPath`는 Linux 절대 경로를 유지합니다.

```bash
script=$(wslpath -w "$PWD/scripts/windows/register-daily.ps1")
powershell.exe -NoProfile -File "$script" \
  -Distribution Ubuntu -LinuxUser ljjw -ProjectPath "$PWD" \
  -At 19:00 -AllowApi -WhatIf

# 위 결과의 대상/시각/API 사용 설정을 검토한 뒤 같은 명령에서 -WhatIf만 제거
powershell.exe -NoProfile -File "$script" \
  -Distribution Ubuntu -LinuxUser ljjw -ProjectPath "$PWD" \
  -At 19:00 -AllowApi
```

Windows 실행 정책으로 차단되면 로컬 정책/조직 정책을 확인하세요. 스크립트는 실행 정책·관리자 권한·비밀번호를 변경하거나 우회하지 않습니다. 실행 환경의 권한 정책에 따라 현재 사용자 등록도 거부될 수 있으며 이때 성공으로 간주하지 않습니다.

기본 작업 이름은 `AutoIpoKorea-Daily`, 작업 폴더는 `\`입니다. `-TaskName`으로 다른 단일 이름을 지정할 수 있습니다. 같은 이름이 이미 있으면 관리 설명·단일 실행 동작·인수·현재 사용자·권한이 **모두 일치할 때만** 시각/설정을 갱신합니다. 다른 작업은 덮어쓰지 않습니다. 프로젝트·배포판·API 스위치를 바꾸려면 기존의 정확한 인수로 제거하고 새로 등록하거나 다른 이름을 사용하세요.

실제 작업 동작은 다음 토큰을 Windows 인수로 각각 인용하여 전달합니다. `bash -c`, 셸 문자열, API 키는 포함하지 않습니다:

```text
C:\Windows\System32\wsl.exe
-d Ubuntu -u ljjw --cd /home/ljjw/projects/ipo-monitor
--exec /home/ljjw/projects/ipo-monitor/.venv/bin/python
-m ipo_monitor daily --config /home/ljjw/projects/ipo-monitor/config/daily.toml --allow-api
```

`-AllowApi`를 생략하면 마지막 `--allow-api`도 생략됩니다. 인수에는 큰따옴표·백틱·역슬래시·제어문자를 허용하지 않습니다. 경로의 공백은 개별 토큰 인용으로 보존합니다.

## 3. 운영 확인과 한계

Windows PowerShell에서 확인합니다:

```powershell
Get-ScheduledTask -TaskName AutoIpoKorea-Daily -TaskPath '\' |
    Select-Object TaskName, State, Actions, Principal, Settings
Get-ScheduledTaskInfo -TaskName AutoIpoKorea-Daily -TaskPath '\' |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

- 현재 Windows 사용자로 **로그온한 동안** 실행하며 관리자 권한이나 비밀번호 저장을 요구하지 않습니다. 화면 잠금과 로그아웃은 다릅니다.
- 놓친 예약을 가능할 때 실행하는 `StartWhenAvailable`을 사용하지만 절전·전원 종료·로그아웃·네트워크 단절 중 실행을 보장하지 않습니다. 자동 깨우기는 끕니다. 24시간 무인 실행이 필요하면 별도의 항상 켜진 실행 환경을 검토해야 합니다.
- 배터리에서도 시작/지속 허용, 동일 작업 실행 중 새 예약은 무시, 최대 실행 시간30분, 일반 우선순위입니다. 30분 강제 종료 등은 부분 실행을 남길 수 있으므로 마지막 결과와 프로그램 실행 기록을 함께 확인하세요.
- Windows 작업 성공 코드만으로 수집/AI 품질·Telegram 발송 성공을 주장하지 않습니다. 실제 발송 기능은 아직 연결하지 않습니다.
- 예약 시각은 하루1회이며 수동 실행/재시도는 별개입니다. 프로그램의 일일 API 상한도 함께 사용하세요.

이 문서는 등록 방법과 동작 계약입니다. 실제 Windows 등록·실행 검증 여부는 작업 완료 보고와 로컬 작업 조회 결과로 구분하세요.

## 4. 정확한 작업 제거

등록할 때 쓴 배포판·사용자·경로·`-AllowApi`·작업 이름을 그대로 사용합니다:

```bash
script=$(wslpath -w "$PWD/scripts/windows/remove-daily.ps1")
powershell.exe -NoProfile -File "$script" \
  -Distribution Ubuntu -LinuxUser ljjw -ProjectPath "$PWD" -AllowApi -WhatIf
powershell.exe -NoProfile -File "$script" \
  -Distribution Ubuntu -LinuxUser ljjw -ProjectPath "$PWD" -AllowApi
```

다른 관리 표식/동작/사용자의 작업은 제거하지 않습니다. 실행 중인 작업은 종료하지 않고 제거를 거부하므로 완료 후 다시 실행하세요. 예약 제거는 DB·키·수집 자료를 삭제하지 않습니다.

## 공식 동작 참고

설정·로그온 방식은 Microsoft의 [ScheduledTask settings](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset), [principal](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal), [등록](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask), [수정](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/set-scheduledtask), [제거](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/unregister-scheduledtask) 계약을 사용합니다.

## 현재 로컬 등록 확인 (2026-11-16)

- 작업: `AutoIpoKorea-Daily`, 상태 `Ready`
- 매일 **19:00**, Windows 시간대 `Korea Standard Time`
- 등록 후 조회된 다음 실행: `2026-11-17T19:00:00`
- WSL `Ubuntu`, Linux 사용자 `ljjw`, 프로젝트의 `daily --allow-api` 실행
- 등록 확인 당시 실제 메신저 전송 없음. 최신 Telegram 기능은 별도 enable 시 연결됨. 현재 사용자 로그온과 PC 실행/네트워크 필요.
- 수동 일일 실검증에서 세 출처 수집 성공. 최초 6회 AI 요청 후 같은 날 재실행은 추가 요청 0회, 성공 캐시 6건 재사용. 분석 대기 43건과 종목명 연결 확인 3건은 미해결 상태로 보존됨.
- 로컬 실제 출력 예시: `data/daily-preview-example.txt` (Git 제외). 대기 자료가 있어 현재 결과는 참여 확정 알림이 아닌 추가 확인/판단 대기 미리보기임.

## 2026-11-18 예약 실패 원인과 수정 검증

11월17일19:00:01 예약은 종료코드127로 실패했고, 애플리케이션 원장에는 실행이 없었다. 등록된 실제 raw명령 문자열을 사용하되 앱 작업을 `--help`로 바꾼 비업무 진단에서 동일127과 `/bin/bash: line 1: -d: command not found`를 재현했다. 모든 토큰을 따옴표로 감싼 인수 생성이 WSL 옵션 해석을 깨뜨린 것이 원인이다. 같은 문자열에서 공백 없는 토큰의 따옴표만 제거한 도움말 비교 실행은 종료0·표준오류 없음이었다.

실제 수정은 공백 있는 값만 인용하고 옵션 플래그를 인용하지 않는 방식이다. 예약 실행은 Windows 로컬 런처를 거쳐 WSL 시작 전/후 stdout·stderr·종료코드를 사용자 로컬 로그에 남긴다. 기존 v1 작업은 실행 동작·계정·권한이 정확히 일치할 때만 v2로 이행한다. 기존19시 예약을 유지하며 별도1회 테스트를 조치 후5분 뒤에 등록한다. 테스트 예약을 `Start-ScheduledTask` 등으로 수동 시작하지 않고, 예정 시각에는 에이전트가 명령을 실행하지 않는다. 예약 시간이 지난 뒤 작업 상태와 로그를 읽어 독립 실행 여부를 검증한다.

### 독립 자동 실행 최종 검증 — 2026-11-18

추가로 v1→v2 이행 시 Windows가 저장한 사용자 ID가 짧은 이름으로 반환되는 경우를 SID 변환으로 비교하도록 수정했다. CIM 권한 enum도 Interactive=3/Limited=0으로 검증한다.

첫 일회성 테스트는09:25:59 예약→09:26:00 자동 시작. Python 진입은 성공했지만 예약 환경 PATH에 `~/.local/bin`이 없어 유튜브 자막3건이 `youtube_downloader_missing`으로 실패했다. 이를 숨기지 않고 종료1로 기록했다.

예약 전용 `ipo_monitor.scheduler_entry`에서 가상환경 bin과 해당 Linux 사용자의 `~/.local/bin`을 PATH에 추가하도록 조치했다. 사용자 프로필이나 시스템 PATH는 변경하지 않는다. 전체316개 테스트·compileall·diff check와 도움말 런처 검증 통과.

두 번째 테스트는09:29:01 등록→**09:34:01 예약**, **09:34:02 자동 시작→09:34:15 완료**, 런처 종료0·표준오류 없음.3개 출처 모두 성공, 실제 신규 언급 이벤트1건, AI1회, Telegram API 전송1건 성공, 대기/실패/불명0. 애플리케이션 원장 daily_runs.id=7이 같은 시각의 성공을 기록한다. 테스트 시각까지 도구 명령 없이 대기했고, `Start-ScheduledTask`/수동 daily 실행/원문 조작은 하지 않았다. 종료 후 로그와 DB만 읽어 결과를 확인했다.

매일19:00 예약은 동일하게 검증된 v2 Windows 로컬 런처를 사용한다. 로그는 사용자 LOCALAPPDATA/AutoIpoKorea/logs의 `.run.json`, `.stdout.log`, `.stderr.log`이다. 완료된 일회성 작업은 증거로 남겨두며 새 일회성 실행을 추가하지 않는다. 첫/두 번째 테스트의 실행 기록을 혼동하지 않는다.

### 오늘 오전10시 추가 테스트 예약

사용자 요청으로2026-11-18 10:00 KST 일회성 작업 `AutoIpoKorea-Test-20261118100000-04facc55`를 추가했다. 실제 트리거/NextRun10:00·상태Ready를 조회했고, 종료 경계는 당일23:59:59라 다음 날 실행 대상으로 남지 않는다. 등록 전후 매일 작업 XML 비교가 동일(`DailyUnchanged=true`)이며 오후19:00 NextRun을 확인했다. 고정 시각 일회성 등록은 `-TestOnly -TestAt '2026-11-18T10:00:00'`를 사용하며 daily 작업/런처를 수정하거나 즉시 실행하지 않는다.
