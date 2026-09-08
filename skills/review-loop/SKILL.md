---
name: review-loop
description: Cross-review loop where Claude workers write code and Codex (in a tmux session) reviews it in detail. Repeats retrieve-review→assess→fix→re-review up to 5 rounds, terminating on VERDICT. Use when a coding task should be iterated with Codex cross-review (e.g. /review-loop <task description>).
argument-hint: "[--session <tmux-session>] <coding task description>"
---

# review-loop

역할 분리를 지켜라: **구현·수정 = Claude workers (Agent tool)**, **리뷰 = tmux 세션의 Codex (읽기 전용)**, **판정·지시·보고 = 너(메인 세션)**.

모든 Codex 통신은 헬퍼로 한다: `BRIDGE="python3 ~/.claude/skills/review-loop/codex_bridge.py"`

## 0. 세션 확보

- 인자에 `--session <이름>` 이 있으면 그 세션, 없으면 `codex` 고정.
- `$BRIDGE resolve --session <이름> --cwd <프로젝트 절대경로>` 실행.
  - 세션이 없으면 자동 생성하고 `codex --sandbox read-only` 로 띄운다 (리뷰 전용이므로 read-only).
  - 세션은 있는데 codex 프로세스가 없으면 그 세션에 `review` window를 새로 열어 read-only codex를 띄운다.
  - exit 5 (`trust_prompt`): pane을 capture해서 확인하고, 리뷰 대상 프로젝트의 신뢰 확인이 맞으면 `tmux send-keys -t <pane> Enter` 로 수락 후 resolve 재실행.
- 출력 JSON의 `created_readonly` 를 기억해라. `false`(기존 codex 재사용)면 그 codex는 쓰기 권한이 있을 수 있으므로(YOLO 등) 리뷰 프롬프트의 수정 금지 문구가 유일한 방어선이다 — 절대 빼먹지 마라.
- `rollout: null` 은 정상이다 (첫 메시지 전에는 파일이 없음). send 후 wait가 잡는다.
- **(2026-08-31 실측 함정 2건)** ① resolve가 요청한 이름과 **다른 세션의 pane을 돌려줄 수 있다**
  (`--session codex` 요청에 `codex-extra`의 %2를 반환한 실례). send 전에
  `tmux list-panes -a -F '#{session_name} #{pane_id}'`로 pane 소유를 교차 확인하라.
  wait도 옛 rollout을 짚고 조기 발화할 수 있다 — `last` 회수 결과가 이번 질문과 무관해 보이면
  pane capture의 "Working" 표시로 진행 중 여부를 먼저 확인하고 pane 유휴 대기로 전환하라.
  ② 이 호스트에서 **새로 생성된 read-only codex는 bwrap loopback 오류로 파일 읽기 전부 불가**
  (`bwrap: loopback: Failed RTM_NEWADDR`) — 신규 세션 생성에 의존하지 말고 기존 세션을 재사용하고,
  필요하면 리뷰 대상 전문을 프롬프트에 인라인하라.

## 1. 코딩

작업을 분해해 worker 서브에이전트들에게 구현을 지시하고, 완료되면 빌드/테스트로 검증한다. 검증 전에는 리뷰를 요청하지 마라 — 컴파일도 안 되는 코드를 Codex에 보내는 것은 라운드 낭비다 (총 5라운드 제한).

## 2. 리뷰 요청 (라운드 r/5)

리뷰 프롬프트를 임시 파일로 작성한다 (bracketed paste로 멀티라인 안전 전송됨). 템플릿:

```
[코드 리뷰 요청 — round {r}/5]

## 절대 규칙
- 어떤 파일도 수정하지 마라. 쓰기 명령(파일 편집, git 조작 등)을 실행하지 마라. 읽기 전용으로 리뷰만 수행하라.

## 작업 맥락
{작업 목표 요약. 리포 절대경로와 브랜치/커밋 명시 — 기존 codex 세션은 cwd가 다를 수 있다}

## 변경 사항
{git diff --stat 결과 또는 변경 파일 목록}

## 리뷰 요청
다음 관점에서 상세히 리뷰하라: 정확성(버그), 엣지 케이스, 설계 문제, 테스트 커버리지, 성능·보안 우려.
{r>=2: ## 이전 라운드 지적과 조치 내역 — 각 항목이 제대로 해결됐는지 재확인하라. 우리가 반박한 항목은 근거가 타당한지 판단하라.}

## 출력 형식
- 지적마다: 심각도(critical/major/minor), 파일:라인, 문제 설명, 권장 수정.
- 리뷰의 마지막 줄은 반드시 정확히 다음 중 하나여야 한다:
  - VERDICT: APPROVED
  - VERDICT: NEEDS_CHANGES (critical: yes)
  - VERDICT: NEEDS_CHANGES (critical: no)
- APPROVED 는 critical/major 지적이 하나도 없을 때만.
```

전송: `$BRIDGE send --session <이름> --text-file <프롬프트파일>` → 출력되는 marker JSON을 보관. exit 3이면 codex가 아직 작업 중이니 wait로 기다렸다가 재시도 (`--force`로 끼어들지 마라).

## 3. 완료 대기

`$BRIDGE wait --session <이름> --marker '<marker JSON>'` 을 Bash `run_in_background`로 실행.

- exit 0: 리뷰 완료 → 4로.
- exit 3: 9분 경과, 아직 진행 중 → 그대로 재실행. 누적 2시간 초과 시 스톨로 판단하고 6의 중단 절차로.
- exit 4: codex 프로세스 소멸 등 오류 → 6의 중단 절차로.

대기 중에 사용자에게 진행 상황을 한 줄로 알려라.

## 4. 리뷰 회수·판정

`$BRIDGE last --session <이름> --rollout <wait가 출력한 rollout>` 으로 리뷰 전문을 회수하고, 사용자에게 요약을 보고한다 (verdict + 주요 지적).

- **VERDICT: APPROVED** → 성공 종료. 최종 보고.
- **VERDICT: NEEDS_CHANGES** → 각 지적을 네가 직접 코드로 검증해라. Codex도 틀린다 — 타당한 지적만 수정 대상으로 삼고, 동의하지 않는 지적은 근거를 기록해 다음 라운드 프롬프트의 반박 섹션에 넣는다.
  - 타당한 지적: workers에게 수정 지시 → 수정 결과를 빌드/테스트로 검증 → 라운드 +1 하고 2로.
- VERDICT 줄이 없으면 내용으로 판단하되 보수적으로 NEEDS_CHANGES 취급.

## 5. 종료 조건

1. **APPROVED** → 성공 종료.
2. **5라운드 도달**: 6번째 리뷰 요청은 절대 보내지 않는다.
   - 5번째 verdict가 APPROVED → 성공 종료.
   - 5번째가 NEEDS_CHANGES (critical: no) 이고 이전 라운드 지적이 모두 해소된 상태 → 남은 minor만 workers로 수정·검증하고 "리뷰 한도 도달, minor 반영 후 종료"로 마무리.
   - 5번째에 critical 지적이 있거나 이전 지적이 여전히 미해결 → **중단 절차(6)**.

## 6. 중단 절차 (한도 초과 실패 / 스톨 / 오류)

1. PushNotification 도구로 사용자에게 알림 (ToolSearch로 스키마 로드 후 사용). 불가하면 보고 메시지로 대체.
2. 최종 보고: 라운드별 verdict 이력, 미해결 이슈 목록(심각도·파일 포함), 시도한 조치, 권장 다음 단계.
3. 코드는 마지막 검증된 상태로 남긴다 — 미검증 수정을 얹지 마라.

## 주의

- codex가 busy일 때 절대 프롬프트를 주입하지 마라 (send가 idle을 확인해주지만, --force는 쓰지 않는다).
- 리뷰 판정을 tmux 화면 캡처로 하지 마라 — 화면은 잘리고 접힌다. 반드시 `last` (rollout의 agent_message 전문)를 써라.
- Codex 리뷰가 파일을 실제로 수정했는지 의심되면 (기존 세션 재사용 시) `git status`로 워킹 트리를 확인해라.
