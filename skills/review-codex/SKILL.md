---
name: review-codex
description: One-shot consult with the local Codex (`codex exec`, non-interactive, new thread each time) — send a single question or task, retrieve the answer, and critically analyze it as Claude (no loop, single round-trip). Default read-only for Codex. Use for a quick second opinion or document/code review (e.g. /review-codex <question or task>).
argument-hint: "[--write] <question or task description>"
---

# review-codex

1회 왕복 자문: 질문/작업을 Codex(`codex exec` 새 스레드)에 보내고, 답을 회수해 **네가(Claude) 비판적으로 분석**해서 보고한다. review-loop와 달리 루프·VERDICT·수정-재리뷰 사이클이 없다.

**사용자의 명시 지시("보내/진행/리뷰 돌려") 없이 이 스킬이 호출됐다면 즉시 중단하고 그 사실만 보고한다.**

`~/.claude/skills/review-loop/common.md` 의 **§A~§C를 읽고 시작한다**(실행 확인·프롬프트 규칙·실행 대기 회수). §D는 run/wait exit 4/5, wait exit 3 누적, 중단 시에만. 브리지는 review-loop 의 `codex_exec.py` 를 경로 참조로 쓴다(자체 브리지 없음).

## 1. 모드 결정 — 읽기 전용이 기본

기본은 **읽기 전용**(Codex는 분석·리뷰·답변만). 사용자가 Codex의 파일 수정을 **명시적으로** 요청한 경우에만 수정 허용 모드. 애매하면 읽기 전용.

수정 허용 모드는 `run --sandbox danger-full-access`(기본 read-only. `workspace-write` 는 이 호스트에서 쓰기 실패 — 09-16 실측). 스레드는 매번 새로 만든다(`--thread` 없음).

## 2. 프롬프트 (§A 실행 확인 후)

```
[1회성 자문 요청]

## 절대 규칙
{common.md §B 절대 규칙 1~5. 수정 허용 모드면 2번 대신 허용 범위를 명시}

## 맥락
{배경. 리포 절대경로와 읽을 파일 목록}

## 요청
{질문/작업을 명확히. 판단 규칙이 있으면 함께 명시}

## 출력 형식
- 주장마다 근거를 파일:라인 수준으로 제시하라.
{common.md §B 출력 형식 6~9}
- 마지막에 "핵심 결론:" 으로 시작하는 한 단락 요약을 붙여라.
```

실행·대기·회수는 §C(스키마 없음).

## 3. 회수·분석 (이 스킬의 핵심)

**그대로 전달 금지.** 3단으로 분석해 보고한다:

1. **요지 요약** — Codex 답변의 핵심 주장.
2. **타당성 평가** — 주장·지적을 코드/문서로 직접 검증. Codex도 틀린다 — 우리 컨텍스트(프로젝트 규칙·정본 문서)에 어긋나는 부분을 짚어라.
3. **채택 제안** — 반영할 것 / 버릴 것 / 추가 확인이 필요한 것 + 후속 액션. 수정에 쓸 지적은 실패 조건·근거·종결 조건을 보존해 브리프로 넘긴다(review-loop §0).

필요하면 원문의 핵심 대목을 인용으로 첨부한다.

## 4. 주의

- **1회 왕복 원칙**: 후속 질문이 필요하면 사용자에게 보고하고 지시를 받아 새로 1회 실행한다. 스스로 루프를 돌지 마라 — 반복 교차 리뷰가 필요하면 review-loop를 써라.
- 수정 허용 모드에서는 완료 후 `git status`/`git diff` 로 실제 변경 내역을 확인하고 분석 보고에 포함하라.
