---
name: review-loop
description: Cross-review loop where Claude workers write code and Codex (in a tmux session) reviews it in detail. Repeats retrieve-review→assess→fix→re-review up to 5 rounds, terminating on VERDICT. Use when a coding task should be iterated with Codex cross-review (e.g. /review-loop <task description>).
argument-hint: "[--session <tmux-session>] <coding task description>"
---

# review-loop

역할 분리: **구현 = `worker`, 라운드 수정 = `patcher`**, **리뷰 = tmux Codex(읽기 전용)**, **판정·지시·보고 = 너(메인 세션)**.

**사용자의 명시 지시("보내/진행/리뷰 돌려") 없이 이 스킬이 호출됐다면 즉시 중단하고 그 사실만 보고한다.**

`~/.claude/skills/review-loop/common.md` 의 **§A~§C를 읽고 시작한다**(세션 확보·프롬프트 규칙·대기 회수). §D는 exit 4/5, wait exit 3 누적, 중단 시에만.

## 1. 코딩 (worker)

작업을 분해해 `worker` 들에게 구현을 지시하고 빌드/테스트로 검증한다. 검증 전에 리뷰를 요청하지 마라 — 컴파일도 안 되는 코드를 보내는 건 라운드 낭비다 (총 5라운드).

## 2. 상태 파일·스냅샷

- 상태 파일: `$CLAUDE_JOB_DIR/tmp/review_loop_state.json` (변수 없으면 `/tmp/review-loop-<리포 디렉터리명>/state.json`). 라운드별 `tree`·`verdict`·`issues` 저장.
- 라운드 r 리뷰 요청 **직전에** `$BRIDGE snapshot --cwd <리포 절대경로>` → `tree` 를 `tree_r` 로 기록. (임시 인덱스만 — 워킹 트리·인덱스 무변경)

## 3. 리뷰 요청 (라운드 r/5)

프롬프트 임시 파일 템플릿:

```
[코드 리뷰 요청 — round {r}/5]

## 절대 규칙
{common.md §B 절대 규칙 1~5}

## 작업 맥락
{목표 요약. 리포 절대경로와 브랜치/커밋 — codex 세션은 cwd가 다를 수 있다}

## 변경 사항
{r=1: 전체 변경(워킹 트리 vs <base>) — git diff --stat 또는 변경 파일 목록}
{r>=2: 델타 = `git diff <tree_{r-1}> <tree_r>` (이 리포에서 직접 실행 가능). 전체 맥락은 워킹 트리에서 읽어라.}

## 리뷰 요청
관점: 정확성(버그), 엣지 케이스, 설계 문제, 테스트 커버리지, 성능.
{r>=2: 델타를 먼저 리뷰하라. 델타와 이전 지적 재검증에서 critical/major가 하나도 안 남으면, APPROVED를 내기 전에 전체 변경(워킹 트리 vs <base>)을 한 번 더 훑고 그 결과로 VERDICT를 정하라. 지적이 남으면 전체 훑기 없이 NEEDS_CHANGES로 끝내라.}

{r>=2: ## 이전 라운드 지적과 조치 내역 — 상태 파일 `issues` 에서 항목별 반영/반박(근거)/보류로 생성. 각 항목이 해결됐는지 재확인하고, 반박 항목은 근거가 타당한지 판단하라.}

## 출력 형식
- 지적마다: 심각도(critical/major/minor), 파일:라인, 문제, 권장 수정.
{common.md §B 출력 형식 6~9}
- 리뷰의 마지막 줄은 반드시 정확히 다음 중 하나여야 한다:
  - VERDICT: APPROVED
  - VERDICT: NEEDS_CHANGES (critical: yes)
  - VERDICT: NEEDS_CHANGES (critical: no)
- APPROVED 는 critical/major 지적이 하나도 없을 때만.
- 그 줄 바로 앞에 JSON 블록 하나:
  {"verdict":"APPROVED|NEEDS_CHANGES","critical":true|false,
   "issues":[{"sev":"critical|major|minor","file":"path","line":123,"msg":"한 문장"}]}
```

전송·대기·회수는 §C.

## 4. 판정·수정 (patcher)

- 텍스트 `VERDICT:` 와 JSON이 불일치하면 보수적으로 **NEEDS_CHANGES**. JSON이 없으면 텍스트로 판정하고 지적은 서술에서 추린다. VERDICT 줄도 없으면 NEEDS_CHANGES.
- `issues` 를 상태 파일에 저장하고, 각 지적을 직접 코드로 검증해라 — Codex도 틀린다.
- **APPROVED** → 성공 종료·최종 보고.
- **NEEDS_CHANGES** → 타당한 항목만 `patcher` 브리프로 넘긴다(항목별 반영/반박/보류를 보고받는다). 반박 항목은 근거와 함께 다음 라운드 프롬프트에 넣는다. 수정 결과를 빌드/테스트로 검증 → 라운드 +1 하고 2로.

## 5. 종료 조건

1. **APPROVED** → 성공 종료.
2. **5라운드 도달**: 6번째 리뷰 요청은 절대 보내지 않는다.
   - 5번째가 APPROVED → 성공 종료.
   - 5번째가 NEEDS_CHANGES (critical: no) 이고 이전 지적이 모두 해소 → 남은 minor(`issues` 의 `sev == "minor"` 로 센다)만 `patcher` 로 수정·검증하고 "리뷰 한도 도달, minor 반영 후 종료"로 마무리.
   - critical 지적이 있거나 이전 지적이 미해결 → **중단 절차 (common.md §D)**.
