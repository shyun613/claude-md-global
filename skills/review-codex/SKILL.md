---
name: review-codex
description: One-shot consult with the local Codex in a tmux session — send a single question or task, retrieve the answer, and critically analyze it as Claude (no loop, single round-trip). Default read-only for Codex. Use for a quick second opinion or document/code review (e.g. /review-codex <question or task>).
argument-hint: "[--session <tmux-session>] <question or task description>"
---

# review-codex

1회 왕복 자문: 질문/작업을 tmux 세션의 Codex에 보내고, 답을 회수해 **네가(Claude) 비판적으로 분석**해서 보고한다. review-loop와 달리 루프·VERDICT·수정-재리뷰 사이클이 없다.

모든 Codex 통신은 review-loop의 브리지를 경로 참조로 쓴다 (이 스킬은 자체 브리지를 두지 않는다):
`BRIDGE="python3 ~/.claude/skills/review-loop/codex_bridge.py"`

## 0. 모드 결정 — 읽기 전용이 기본

- 기본은 **읽기 전용**: Codex는 분석·리뷰·답변만 하고 파일 수정 금지.
- 사용자가 Codex의 파일 수정을 **명시적으로** 요청한 경우에만 수정 허용 모드. 애매하면 읽기 전용.

## 1. 세션 확보

- 인자에 `--session <이름>` 이 있으면 그 세션, 없으면 `codex` 고정.
- `$BRIDGE resolve --session <이름> --cwd <프로젝트 절대경로>` 실행.
  - 세션이 없으면 자동 생성되어 `codex --sandbox read-only` 로 뜬다.
  - exit 5 (`trust_prompt`): pane을 capture해서 확인하고, 대상 프로젝트의 신뢰 확인이 맞으면 `tmux send-keys -t <pane> Enter` 로 수락 후 resolve 재실행.
- 출력 JSON의 `created_readonly` 를 기억해라.
  - 읽기 전용 모드에서 `false`(기존 codex 재사용)면 그 codex는 쓰기 권한이 있을 수 있으므로 프롬프트의 수정 금지 문구가 유일한 방어선이다 — 절대 빼먹지 마라.
  - 수정 허용 모드에서 `true`(새로 생성된 read-only)면 sandbox가 쓰기를 막는다 — 진행하지 말고 사용자에게 쓰기 가능한 codex 세션을 직접 띄워달라고 안내하라.
- `rollout: null` 은 정상이다 (첫 메시지 전에는 파일이 없음).
- **resolve pane 오라우팅·read-only 신규 세션 bwrap 불능 함정**은 review-loop 스킬 §0의
  「2026-08-31 실측 함정 2건」을 따른다 — send 전 pane 소유 교차 확인, 신규 read-only 세션에 의존 금지.

## 2. 프롬프트 작성·전송

프롬프트를 임시 파일로 작성한다 (`$CLAUDE_JOB_DIR/tmp` 우선, 없으면 세션 tmp). 템플릿:

```
[1회성 자문 요청]

## 절대 규칙                     ← 읽기 전용 모드일 때만. 수정 허용 모드면 허용 범위를 명시.
- 어떤 파일도 수정하지 마라. 쓰기 명령(파일 편집, git 조작 등)을 실행하지 마라. 읽기 전용으로 분석만 수행하라.

## 맥락
{질문/작업의 배경. 리포 절대경로와 읽을 파일 목록 명시 — 기존 codex 세션은 cwd가 다를 수 있다.}

## 요청
{사용자의 질문/작업을 명확히. 판단 규칙이 있으면 함께 명시.}

## 출력 형식
- 주장마다 근거를 파일:라인 수준으로 제시하라.
- 마지막에 "핵심 결론:" 으로 시작하는 한 단락 요약을 붙여라.
```

전송: `$BRIDGE send --session <이름> --text-file <프롬프트파일>` → 출력되는 marker JSON을 보관. exit 3이면 codex가 아직 작업 중이니 기다렸다가 재시도 (`--force`로 끼어들지 마라).

## 3. 완료 대기

`$BRIDGE wait --session <이름> --marker '<marker JSON>'` 을 Bash `run_in_background`로 실행.

- exit 0: 완료 → 4로.
- exit 3: 9분 경과, 아직 진행 중 → 그대로 재실행. 누적 1시간 초과 시 스톨로 판단하고 사용자에게 보고.
- exit 4: codex 프로세스 소멸 등 오류 → 사용자에게 보고.

대기 중에 사용자에게 진행 상황을 한 줄로 알려라.

## 4. 회수·분석 (이 스킬의 핵심)

`$BRIDGE last --session <이름> --rollout <wait가 출력한 rollout>` 으로 답변 전문을 회수한다.

**그대로 전달 금지.** 반드시 다음 3단으로 분석해 보고한다:

1. **요지 요약** — Codex 답변의 핵심 주장.
2. **타당성 평가** — 주장·지적을 코드/문서로 직접 검증. 동의·이견을 근거와 함께. Codex도 틀린다 — 우리 컨텍스트(프로젝트 규칙, 정본 문서 등)에 어긋나는 부분을 짚어라.
3. **채택 제안** — 반영할 것 / 버릴 것 / 추가 확인이 필요한 것 + 후속 액션.

필요하면 Codex 원문에서 핵심 대목을 인용으로 첨부한다.

## 5. 주의

- **1회 왕복 원칙**: 후속 질문이 필요하면 사용자에게 보고하고 지시를 받아 새로 1회 실행한다. 스스로 루프를 돌지 마라 — 반복 교차 리뷰가 필요한 작업이면 review-loop를 써라.
- 답변 회수를 tmux 화면 캡처로 하지 마라 — 화면은 잘리고 접힌다. 반드시 `last` (rollout의 agent_message 전문)를 써라.
- 읽기 전용 모드에서 기존 세션을 재사용했다면, 수정이 의심될 때 `git status`로 워킹 트리를 확인해라.
- 수정 허용 모드에서는 완료 후 `git status`/`git diff`로 실제 변경 내역을 확인하고 분석 보고에 포함하라.
