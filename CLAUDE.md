# Claude Usage Dashboard

로컬 파일만 읽어 Claude Code 사용량을 모니터링하는 FastAPI 대시보드. API 토큰 소비 없음.

## 실행

```bash
./run.sh          # venv 자동 생성 후 http://localhost:8765
pkill -f app.py   # 종료
```

## 파일 구조

```
app.py        FastAPI 서버 (엔드포인트 3개: /, /api/stats, /api/sessions, /api/sessions/{id})
core.py       데이터 파싱·집계 (캐시: mtime+size+subdir mtime)
static/index.html  단일 파일 UI (vanilla JS, 다크 테마)
```

## 데이터 소스 — 핵심 경로

| 경로 | 내용 |
|---|---|
| `~/.claude-work/sessions/*.json` | work(cdw) live 세션 (pid, status, sessionId) |
| `~/.claude-personal/sessions/*.json` | personal(cdp) live 세션 |
| `~/.claude-work/projects/<encoded-cwd>/<sid>.jsonl` | work 메시지·토큰·tool_use |
| `~/.claude-personal/projects/<encoded-cwd>/<sid>.jsonl` | personal 메시지·토큰·tool_use |
| `<sid>/subagents/agent-<id>.{jsonl,meta.json}` | 서브에이전트 실행 기록 (별도 파일) |

**중요**: `~/.claude/` 는 base config (세션 없음). work는 반드시 `~/.claude-work/`.

## 주요 설계 결정

- **HOMES** (`core.py`): `~/.claude-work/` (work) + `~/.claude-personal/` (personal). `~/.claude/`는 포함하지 않음
- **live 판별**: `os.kill(pid, 0)` — 프로세스 생존 여부만 확인
- **subagent 토큰 분리**: 메인 JSONL의 sidechain 메시지 없음. 별도 `subagents/` 디렉토리에 저장됨
- **slash command 감지**: user 메시지의 `<command-name>/foo</command-name>` 태그 파싱 (`/exit`, `/compact` 제외)
- **tool token attribution**: 한 assistant 메시지에 여러 tool_use → 균등 분배 (근사)
- **새로고침**: SSE 제거, 버튼/reload 시에만 fetch (토큰 절약)
- **플랜 한도**: 정확한 token limit은 API 헤더에만 있어 로컬 미저장. 5h rolling 실사용량 + 추정 한도 dropdown으로 대체

## 알려진 한계

- 플랜 한도값은 추정치 (Pro ~7M / Max 5x ~35M / Max 20x ~140M tokens per 5h — 비공식)
- tool token attribution은 근사값
- burn rate / ETA 미구현
