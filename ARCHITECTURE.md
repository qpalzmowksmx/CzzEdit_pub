# 구현 구조

버전 0.3.2 · 로컬 Python 서버 + 브라우저 화면 + SQLite. 일반 실행에 Node나 외부 웹 서버는 필요하지 않습니다.

```text
설치 쉘 ─ Python 확인/로컬 설치 ─ .venv 의존성
실행 쉘 ─ czzedit.__main__ ─ server.py (127.0.0.1)
                                  |
                    ui.html + v2.js + live.js
                         /                 \
              VOD 작업                     LIVE 작업
         service/collector              live_service
          transport                       live_transport
             |                            읽기 전용 WSS
           store                              |
     페이지+진행위치 저장               유한 큐 → live_store
             |                               |
       detector 공통 규칙 ← live_analyzer 증분 평가
             |                               |
      exporter/review                 수신 TXT/고정 checkpoint
             |                               |
       player.js/HLS                 최근 채팅/편집 후보/문맥
```

## 코드별 역할

| 영역 | 파일 |
|---|---|
| 실행·검증·공통 출력 | `__main__.py`, `common.py`, `records.py` |
| VOD 조회·저장·분석 연결 | `transport.py`, `collector.py`, `store.py`, `service.py` |
| 공통 탐지 D1~D5 | `detector.py` |
| VOD 내보내기·재생·문맥 | `exporter.py`, `review.py`, `playback.py`, `player.js` |
| 영상 댓글·통합 타임라인 | `comments.py`, `timeline.py`, `v2.js` |
| LIVE 연결·증분 처리·저장 | `live_transport.py`, `live_service.py`, `live_analyzer.py`, `live_store.py`, `live.js` |
| 시청자 보정·선택형 AI | `viewers.py`, `ai.py` |
| 로컬 API·화면 | `server.py`, `ui.html` |

## 수집과 분석

VOD는 서버가 반환한 채팅 페이지 위치를 따라 조회합니다. 원문 응답, 정규화 채팅, 다음 위치를 함께 저장해 이어받기를 지원합니다. 같은 시각·작성자·본문의 실제 반복은 관측한 개수만큼 보존합니다. 응답 경계가 불확실하거나 형식이 바뀌면 오류·누락으로 남깁니다.

LIVE는 익명 READ 연결을 소유한 수신 작업과 DB를 쓰는 작업을 나눕니다. 최대 10,000개 대기 항목을 두고, 약 1초 또는 200건 단위로 DB/TXT를 갱신합니다. 과부하를 조용한 정상 종료로 바꾸지 않습니다. 재접속은 최대 60초 간격의 지연을 두며 약 10분 동안 복구하지 못하면 사용자 재개를 기다립니다. 권한·응답 형식·시계 변경은 명시적 중단 대상입니다.

10초 창을 5초 간격으로 평가하고, 웃음·채팅량·동일 문구·감정 표현·참여자 증가를 감지합니다. 작성자별 과도한 반복, 인사·명령을 줄이고 과거 창으로 기준을 학습합니다. 후보는 근접 반응을 합치거나 긴 반응을 나누고 앞뒤 문맥을 붙입니다. LIVE는 5초 지연된 평가 시각을 사용하며 오래된 분석 자료를 메모리에서 제거합니다. 늦게 도착한 채팅은 저장하되 이미 확정한 과거 판정을 소급 수정하지 않습니다.

연결 전·연결 공백을 무반응 자료로 학습하지 않습니다. 시청자 표본이 부족하면 채팅 기준으로 동작합니다. 시청자 감소 보정은 직전 기준 대비 10%를 사용하며 누락을 0으로 대체하지 않습니다.

## 저장·복구 경계

- SQLite 스키마 3: 기존 VOD와 LIVE 테이블을 분리. 이전 DB의 업그레이드는 백업하고 트랜잭션으로 처리.
- VOD와 LIVE 작업 잠금 분리. 같은 작업폴더의 앱 서버 중복 실행은 차단.
- DB가 원본 기준. TXT 실패가 DB를 되돌리지 않으며 LIVE 재개 시 TXT를 DB에서 복원.
- 정상 중단은 남은 자료를 저장하고 출력. 비정상 종료 뒤 자동 접속하지 않고 미완료 상태 표시.
- 고정 checkpoint는 하나의 DB 읽기 시점으로 채팅·분석 경계·근거를 묶고 이전 출력을 덮어쓰지 않음.
- 다운로드는 열린 파일의 길이를 고정하고 작은 블록으로 전송. 생성 중 TXT의 엄밀한 시점별 자료는 checkpoint를 사용.
- DB에 반영되기 전 메모리 대기 입력은 강제 종료로 잃을 수 있음. 플랫폼 전체와의 무손실 일치는 보장하지 않음.

## 후속 범위

공식 OAuth 채팅, LIVE 일반 계정 로그인, LIVE→VOD 시간 대응·재분석 연결, LIVE 영상 재생, 자동 AI 분석, 여러 채널 동시 수집은 현재 제공하지 않습니다. 기존 VOD 선택형 AI와 공식 시청자 표본은 별도 기능입니다.

## 목록 표시와 macOS 실행

`history.py`는 records의 hidden_session 표시만 갱신합니다. 원문 DB·출력 파일은 변경하거나 삭제하지 않습니다. UI 목록 조회에서만 숨긴 ID를 제외하고, 복원은 숨김 표시를 없앱니다. VOD/LIVE 잠금을 사용해 수집 중 목록 변경을 막습니다.

`설치.command`와 `실행.command`는 macOS Finder용 진입점으로 기존 설치·실행 쉘을 호출합니다. 별도 Python이나 시스템 파일 연결을 변경하지 않습니다.
