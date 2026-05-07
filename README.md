# Claude Usage Dashboard

Claude Code 로컬 사용량 대시보드. **각자 자신의 PC에서 실행**해서 자기 데이터를 확인한다.  
API 호출 없음 — `~/.claude*` 파일만 읽는다.

![dashboard preview](https://raw.githubusercontent.com/cjkim91/claude-usage-dashboard/main/static/preview.png)

## 무엇을 보여주나

| 섹션 | 내용 |
|---|---|
| 플랜 사용량 | 5시간 rolling 사용률 % + 재설정까지 남은 시간 (Anthropic 어드민과 동일 수치) |
| Today / Live Now / This Week / All Time | 토큰·비용 요약 카드. Live Now는 현재 실행 중인 세션 수 |
| 모델별 / 서브에이전트별 분해 | 어떤 모델·에이전트가 토큰을 얼마나 썼는지 |
| 세션 목록 | 진행 중(LIVE) + 최근 50개. 클릭하면 turn-by-turn 상세 |

---

## 설치 & 실행

### 1단계 — 저장소 클론

```bash
git clone https://github.com/cjkim91/claude-usage-dashboard.git
cd claude-usage-dashboard
```

### 2단계 — 실행

```bash
./run.sh
# → http://localhost:8765 열기
```

> Python 3.9+ 필요. `run.sh`가 자동으로 가상환경(`.venv`) 생성 후 의존성 설치.

### 종료

```bash
pkill -f app.py
```

---

## 경로 설정 (하네스 환경이 다를 경우)

앱은 실행 시 **`~/.claude*` 디렉토리를 자동 감지**한다.  
`projects/` 서브디렉토리가 있는 디렉토리를 모두 데이터 소스로 사용한다.

**표준 Claude Code** (하네스 없음, `~/.claude/` 하나만 있는 경우) → 별도 설정 불필요.

경로가 다르거나 레이블을 바꾸고 싶다면 `.env` 파일 생성:

```bash
cp .env.example .env
# .env 파일에서 CLAUDE_HOMES 항목 수정
```

```env
# 예시 — 표준 Claude Code 단일 경로
CLAUDE_HOMES=claude:~/.claude

# 예시 — 여러 경로
CLAUDE_HOMES=work:~/.claude-work,personal:~/.claude-personal
```

---

## 플랜 사용량 % (어드민 일치 여부)

**macOS**에서는 macOS Keychain에 저장된 Claude Code OAuth 토큰을 자동으로 읽어  
Anthropic 공식 API(`api.anthropic.com/api/oauth/usage`)에서 사용률을 가져온다.  
→ 어드민 페이지와 **동일한 수치**.

**macOS 외 / 토큰 없음** → JSONL 파싱 기반 추정치로 fallback (약간 차이 있음).

---

## 데이터 소스

```
~/.claude*/
  sessions/*.json          ← 실행 중인 프로세스 (PID, status)
  projects/<cwd>/<id>.jsonl ← 메시지·토큰·tool_use 기록
  projects/<cwd>/<id>/subagents/
    agent-*.jsonl           ← 서브에이전트 별도 파일
    agent-*.meta.json
```

로컬 파일만 읽으므로 **Claude API 토큰을 소모하지 않는다**.

---

## 비용 추정

`core.py`의 `PRICING` dict 기준 (1M 토큰당 USD).  
모델이 목록에 없으면 비용 0으로 처리. cache write는 1h 가격 기준.

---

## 알려진 한계

- 취소된 요청·웹 클라이언트 요청은 JSONL에 기록 안 됨 → JSONL 추정 시 수분 오차
- Tool token attribution은 균등 분배 (근사)
- 플랜 한도값(Pro/Max)은 비공식 추정치
