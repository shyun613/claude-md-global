---
name: review-loop
description: Cross-review loop where Claude workers write code and Codex (in an orca tab or tmux session) reviews it in detail. Repeats retrieve-review→assess→fix→re-review up to 5 rounds, terminating on VERDICT. Use when a coding task should be iterated with Codex cross-review (e.g. /review-loop <task description>).
argument-hint: "[--tab <name>|--terminal <handle>|--session <tmux>] <coding task description>"
---

# review-loop

역할 분리: **구현 = `worker`, 라운드 수정 = `patcher`**, **리뷰 = Codex(orca 탭/tmux 세션, 읽기 전용)**, **판정·지시·보고 = 너(메인 세션)**.

**사용자의 명시 지시("보내/진행/리뷰 돌려") 없이 이 스킬이 호출됐다면 즉시 중단하고 그 사실만 보고한다.**

`~/.claude/skills/review-loop/common.md` 의 **§A~§C를 읽고 시작한다**(대상 확보·프롬프트 규칙·대기 회수). §D는 exit 4/5, wait exit 3 누적, 중단 시에만.

## 0. 완료 기준 (worker·patcher 공통)

- 브리프의 완료 기준·종결 조건은 **관찰 가능한 결과**로 쓴다 — 실제 진입 경로(CLI·호출부)에 어떤 입력을 주면 어떤 결과가 나와야 하는지. 테스트 개수·PASS 수는 완료 기준이 아니다.
- Advisor 는 브리프에 **수정 범위 파일을 열거**한다. 열거 전에 영향 경로(호출부·옵션 분기·문서 예시·기존 테스트)를 Advisor 가 조사한다. 수정 범위는 좁게, **조사 범위는 제한하지 않는다.**
- 종결 조건은 **원래 요구(브리프 계약)와 리뷰의 실패 조건에 대조**해 쓴다. 원래 요구를 약화하는 변경(실패를 다른 상태로 재분류 등)은 해결이 아니다. 구현 대안은 Advisor 가 고르되, 수용 기준 완화나 사용자 확정 설계 변경이 필요하면 사용자 결정으로 올린다.
- **인수**: worker/patcher 보고를 받으면 Advisor 가 항목별로 종결 조건 성립을 **항목에 적합한 정적 검토 또는 실행 검증**으로 직접 확인하고(실행 동작에 관한 지적은 실제 진입 경로 기준) `evidence` 에 **방법과 결과**를 쓴다(예: "`label_raster` CLI 를 `--cells` 없이 진입 → 범위 오류로 중단, 로더 미호출·캐시 불변 확인"). "pytest 통과"·"diff 확인"만 적힌 것은 증거가 아니다. `evidence` 가 없는 항목은 리뷰 프롬프트에 "해결"로 쓰지 않는다. 검증 불가·이견 항목은 막지 말고 검증 한계 또는 반박 근거를 적어 전달한다. 첫 리뷰 전 worker 인수의 완료 기준·확인 근거는 worker 브리프의 항목별 인수 기록에 남기고, 상태 파일에는 그 경로를 `worker_acceptance` 로 기록한다.
- auditor 는 다음 경우에 Advisor 판단으로 선택 투입한다: 여러 모듈의 계약을 함께 바꿈 · 재개/동시성/좌표계처럼 오류가 조용히 누적됨 · Advisor 검증 후에도 중요한 불확실성이 남음. auditor 는 Advisor 인수 검증을 대신하지 않는다.

## 1. 코딩 (worker)

작업을 분해해 `worker` 들에게 구현을 지시하고 빌드/테스트로 검증한다. 검증 전에 리뷰를 요청하지 마라 — 컴파일도 안 되는 코드를 보내는 건 라운드 낭비다 (총 5라운드). 라운드 1 전송 전 Advisor 가 브리프 완료 기준을 항목별로 대조한다(§0 인수).

## 2. 상태 파일·스냅샷

- 상태 파일: `$CLAUDE_JOB_DIR/tmp/review_loop_state.json` (변수 없으면 `/tmp/review-loop-<리포 디렉터리명>/state.json`). 라운드별 `tree`·`verdict`·`issues` 저장. `issues` 항목 = `{id, sev, file, line, msg(리뷰 원문), parent, out_of_scope, contract, done_when, status, evidence}`. `id` 는 `R1-M3` 식 고정 ID로 라운드가 바뀌어도 유지, `parent` 는 파생된 이전 이슈 ID, `contract` 는 원래 계약 포인터(브리프 §·spec 행), `status` ∈ {open, patched, verified, rebutted, deferred}. `verified` 는 Advisor 가 `evidence` 를 쓴 뒤에만. 동일 미해결은 기존 ID 유지, 수정으로 생긴 별도 결함은 새 ID 와 `parent` 부여. 라운드 1 전 worker 인수 기록 경로는 `worker_acceptance`.
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

{r>=2: ## 이전 라운드 지적과 조치 내역 — 상태 파일 `issues` 에서 이슈별 "ID · 변경 위치 · 해결 근거(evidence) 또는 반박 근거·검증 한계" 로 생성. 각 항목이 해결됐는지 재확인하고, 반박 항목은 근거가 타당한지 판단하라. 재지적이 이전 이슈에서 파생됐으면 그 ID 를 `parent` 로 달고, 범위 밖 설계 제안이면 `out_of_scope: true` 로 표시하라. 늦게 발견한 기존 결함은 범위 안이면 발견 시점과 무관하게 심각도 그대로 매긴다.}

## 출력 형식
- 지적마다: 심각도(critical/major/minor), 파일:라인, 문제, 권장 수정.
{common.md §B 출력 형식 6~9}
- 리뷰의 마지막 줄은 반드시 정확히 다음 중 하나여야 한다:
  - VERDICT: APPROVED
  - VERDICT: NEEDS_CHANGES (critical: yes)
  - VERDICT: NEEDS_CHANGES (critical: no)
- APPROVED 는 critical/major 지적이 하나도 없을 때만(범위 밖 설계 제안 `out_of_scope` 는 집계에서 제외).
- 그 줄 바로 앞에 JSON 블록 하나:
  {"verdict":"APPROVED|NEEDS_CHANGES","critical":true|false,
   "issues":[{"sev":"critical|major|minor","file":"path","line":123,"msg":"한 문장","parent":"이전 이슈 ID 또는 null","out_of_scope":false}]}
```

전송·대기·회수는 §C.

## 4. 판정·수정 (patcher)

- 텍스트 `VERDICT:` 와 JSON이 불일치하면 보수적으로 **NEEDS_CHANGES**. JSON이 없으면 텍스트로 판정하고 지적은 서술에서 추린다. VERDICT 줄도 없으면 NEEDS_CHANGES.
- `issues` 를 상태 파일에 저장(고정 ID 부여·`contract` 기입)하고, 각 지적을 직접 코드로 검증해라 — Codex도 틀린다. 타당하면 `done_when` 을 §0 규칙으로 쓴다.
- 범위 밖 설계 제안(`out_of_scope`)은 별도 보고하며 VERDICT·수정 대상·종료 시 잔존 결함 집계에서 제외한다. 범위 여부는 Advisor 가 원래 작업 계약과 대조해 최종 판단한다(Codex 표시는 참고).
- **APPROVED** → 성공 종료·최종 보고.
- **NEEDS_CHANGES** → 타당하고 범위 안인 항목만 `patcher` 브리프로 넘긴다. 브리프에는 이슈별 `done_when` 과 수정 범위 파일을 적는다(§0). 한 지적이 드러낸 **결함 부류**(예: 레코드 의미가 바뀌면 버전 문자열 갱신, 빌드 분기를 고쳤으면 재개 분기도)는 같은 브리프의 다른 항목과 범위 안 다른 분기에 횡단 적용해 `done_when` 에 쓴다 — 인용 줄 하나만 종결 조건으로 삼지 않는다. 항목별 반영/반박/보류/미완과 근거를 보고받는다. patcher 가 "후속"으로 남긴 항목은 Advisor 가 미완(범위 안 필수)·반박(근거)·범위 밖(별도 보고) 중 하나로 확정한 뒤에만 라운드를 올린다 — Codex 에 심각도 판정을 넘기는 것으로 대신하지 않는다. **미완**이 원래 작업 범위 안의 필수 변경이면 Advisor 가 수정 브리프의 파일 범위를 보완해 재지시하고 인수한 뒤에만 라운드를 올린다. 검증 불가·타당성 이견은 한계를 명시해 재리뷰할 수 있다. 수정 결과는 §0 인수로 항목별 `evidence` 를 쓴 뒤 → 라운드 +1 하고 2로. 반박·검증 한계 항목은 근거와 함께 다음 라운드 프롬프트에 넣는다.

## 5. 종료 조건

1. **APPROVED** → 성공 종료.
2. **5라운드 도달**: 6번째 리뷰 요청은 절대 보내지 않는다.
   - 5번째가 APPROVED → 성공 종료.
   - 5번째에 critical 또는 major 가 남아 있으면(이전 지적 미해결·새 지적 불문) → **미승인 종료, 중단 절차 (common.md §D)**. 한도 뒤 추가 수정을 했더라도 재리뷰하지 않았으면 승인된 것으로 보고하지 않는다 — 최종 보고에 잔존 지적과 리뷰 이후 수정 여부를 그대로 적는다.
   - minor 만 남았으면 → `patcher` 로 반영·§0 인수 후 "리뷰 한도 도달, minor 반영 후 종료"로 마무리.
