# Claude Usage Dashboard

Claude Code 로컬 사용량 대시보드. 두 환경(`cdw`, `cdp`)의 세션을 한 화면에서 본다.

- **읽기 전용**: `~/.claude/`(work)와 `~/.claude-personal/`(personal)의 파일만 파싱한다.
- **API 토큰 0**: Claude를 호출하지 않으므로 모니터링 자체로 토큰을 쓰지 않는다.
- **실시간**: SSE로 3초마다 갱신.

## 실행

```bash
chmod +x run.sh
./run.sh
# → http://localhost:8765
```

또는 수동:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## 보이는 것

**최상단 — 플랜 한도 위젯**
- 5시간 rolling 윈도우의 실제 토큰 사용량 (이는 정확한 데이터)
- 플랜 dropdown (Pro / Max 5x / Max 20x / Custom) → % 바
- "X시간 Y분 후 재설정" 카운트다운 (윈도우 첫 메시지 + 5h)
- ⚠️ 플랜 한도값은 **추정치** — Anthropic의 정확한 토큰 한도는 공개되지 않아 어림 잡음. Custom으로 직접 입력 가능
- 데이터 한계: 정확한 플랜 한도 (`rate_limits.five_hour`)는 API 응답 헤더에만 있고 JSONL에 저장되지 않음

**상단 카드** — 오늘 / 이번주 / 전체 토큰 합계 (input / output / cache write / cache read 분리, USD 추정 비용)

**전체 분해 섹션**
- 모델별 토큰 (claude-opus-4-7, sonnet 등) — 막대 + %
- 서브에이전트별 토큰 (general-purpose 등 agent_type별) — 토큰 분리됨
- Slash command / 스킬 호출 횟수 (예: `/meta-ads-analyzer` 3회) — 토큰은 메인 흐름에 합쳐져 있어 호출 횟수만 추적

**세션 카드** (필터: All / Live / Work / Personal)
- 살아있는 세션은 좌측 초록 라인 + LIVE 뱃지
- cwd, git branch, 모델, 진행중 subagent 수
- 최근 assistant 메시지 또는 직전 user prompt 스니펫
- 누적 토큰 / 비용 / 사용 도구 / Agent 호출 수 / PID / 마지막 활동 시각

**세션 클릭 → 상세**
- **Workflow 타임라인**: 시작 시각, user prompt들, agent 호출(타입+description), slash command 사용을 시간순 점선 트리로 표시
- **메인 vs 서브에이전트** 토큰 분리 + % 비율
- **Subagent 분해표**: agent_type별 호출 수 / 토큰 / 비용 / 사용 도구. 개별 호출은 description + 토큰 + 도구 카운트
- **Tool token attribution**: 같은 메시지 안에서 여러 tool_use가 있으면 토큰 균등 분배 (근사). 도구별 막대 그래프
- **Skill / slash command 사용 카운트**
- **Per-model 표**: main + subagent 합산
- 분 단위 토큰 사용량 타임라인 (subagent 활동도 포함)
- 첫 user prompt

## 데이터 소스

| 경로 | 용도 |
| --- | --- |
| `~/.claude/sessions/*.json`, `~/.claude-personal/sessions/*.json` | 살아있는 프로세스(PID, cwd, status: busy/idle) |
| `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` | 메인 스레드 메시지·토큰·도구 호출·Agent 호출 |
| `~/.claude-personal/projects/<encoded-cwd>/<sessionId>.jsonl` | 동일 |
| `<sessionId>/subagents/agent-<id>.jsonl` + `.meta.json` | 서브에이전트 실행 기록 (별도 파일). meta는 `agentType`, `description` |

JSONL의 assistant 메시지에서 `message.usage`(input/output/cache_*) + `message.model` + `content[].tool_use`를 모은다. PID는 `kill -0`로 살아있는지 확인.

## 비용 계산

`core.py`의 `PRICING` dict에서 1M 토큰당 USD를 정의. cache write는 1h 가격으로 가정(보수적). 모델이 dict에 없으면 비용 0으로 처리한다.

## 캐시

같은 세션 JSONL은 (mtime, size)가 바뀌지 않으면 재파싱하지 않는다. 33MB 이상 파일도 첫 파싱만 좀 걸리고 이후는 즉시.

## 한계 / TODO

- **Tool token attribution은 근사치** — 한 메시지에 여러 tool_use가 있으면 균등 분배. 실제로는 tool 호출 결과의 입력 비용은 *다음* assistant 메시지의 cache_read에 반영되므로 도구별 정확한 비용은 추적 불가.
- 분 단위 타임라인은 모델/소스 정보가 합쳐짐 (정확한 분리는 detail의 표 참고)
- 비용은 추정치 (cache write를 1h 가격으로 가정)
- 미구현: 5h rolling block + burn rate ETA, plan quota 추적 (`rate_limits.*`), Live tail (현재 마지막 작업이 뭔지)
