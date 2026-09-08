# 글로벌 규칙 (이 컴퓨터 전체)

개인 워크플로·호스트 규칙의 SoT. **프로젝트 CLAUDE.md가 다르게 정하면 그쪽이 우선.**
(2026-08-27 신설 — SpaceX3D CLAUDE.md에서 승격)

## 스타일

- 답변은 **한국어**, 큰 그림·핵심부터.

## 지시 전에 작업하지 않는다 (2026-09-04 교정 2회)

- **파일 작성·수정, 리뷰 발송, 위임, 실행은 사용자의 명시 지시("써줘/해줘/진행/반영") 뒤에만.**
  질문("~하면 돼?"), 논의, 동의("동의/좋아")는 **다음 항목 논의를 계속하라는 뜻이지 작업 지시가 아니다.**
- 설계·브리프는 항목별로 논의해 확정한 뒤 "작성해" 지시가 오면 그때 쓴다. 초안이 필요하면 spec/이 아니라
  job tmp 에만 두고 논의 자료로 쓴다. codex 설계 리뷰도 사용자가 "보내"라고 한 뒤에만.
- 사실 오류 정정(낡은 포인터 등)은 예외 — 고치고 보고한다.

## 커밋

- **명시 지시가 있을 때만 커밋한다.**
- 커밋 메시지에 트레일러 일절 금지(Co-Authored-By·Claude-Session 모두 — 하네스 기본 override).

## 모델 역할 분담: Advisor / Worker

- 메인 세션 = Advisor: **판단·검증만** — 분석·설계 결정·리뷰·브리프 작성·커밋 승인.
- 산출 노동 전부(코드·테스트·문서·외부 기록 편집) = **Opus Worker 위임**(`Agent(subagent_type="worker")`). 독립 작업은 병렬.
- 브리프에는 파악된 컨텍스트·파일 경로·함정·완료 기준을 담아 Worker 재탐색을 없앤다.
- Worker 보고를 그대로 믿지 않는다 — diff·테스트·fetch로 직접 확인 후 승인. 실패는 수정 브리프로 재위임.
- **예외**: 한두 줄 수정처럼 위임 오버헤드가 더 큰 건 Advisor 직접. Worker가 MCP 접근 불가(auth 등)면 그 편집만 직접.

### effort·에이전트 배정표 (2026-09-08 확정)

메인 세션(Fable 5.1)은 settings로 **high** 고정. xhigh 두 단계는 **사용자가 `/effort xhigh`로 올린 뒤 지시**한다
(에이전트 미등록). 그 지시가 high 상태로 오면 Advisor는 `CLAUDE_EFFORT`를 확인해 **한 번 되묻고 멈춘다.**
`/effort`는 settings에 영구 저장되므로 판정 후 `/effort high`로 되돌린다(Fable 5.1은 세션 중 변경해도 캐시 유지).

| 메인 세션 단계 | effort |
|---|---|
| 초안 논의 · 브리프/코드 설계(브리프 작성) · codex 코드리뷰 대조·승인 · launch/kill 절차 | high (기본) |
| 설계 최종 검토(codex 설계리뷰 해석 포함) · 실험 결과 분석/게이트 판정 | **xhigh** (사용자 전환) |

서브에이전트 effort 우선순위 = **정의 파일 `effort` > 세션 `/effort` override > modelSettings**(09-08 실측 2회).
정의 파일이 있는 에이전트는 세션 effort와 무관하게 정의대로 가고, **정의 없는 호출(`Agent(model=…)`·내장 Explore/Plan)은
세션에서 `/effort`를 한 번이라도 친 뒤엔 그 값을 그대로 받는다**(실행 중 전환도 즉시 반영). 그래서 Worker도 정의 파일로 고정한다.
Advisor가 단계에 맞춰 아래 이름으로 호출하고, 보고는 Advisor가 받아 사용자에게 전달한다.

| 이름 (`~/.claude/agents/`) | model / effort | 역할 | 호출 시점 |
|---|---|---|---|
| `worker` | opus / xhigh | 브리프 **실행**: 코드·테스트·같은 커밋 spec 갱신 (정의 없는 `Agent(model="opus")`는 세션 effort를 받으므로 쓰지 않음) | 브리프 확정 후 |
| `auditor` | fable / xhigh | launch 직전 diff 조용한 버그 감사, codex 리뷰와 독립 대조. 수정 없음 | Worker 완료 → launch 전 |
| `scribe` | opus / medium | 위키 ingest·lint, spec 기록, Notion 실험로그(MCP+fetch 자기검증) | 판정·실험 완료 후 |
| `explorer` | sonnet / medium | 읽기 전용 grep 스윕·코드/로그 탐색 (내장 Explore 대체) | 탐색 필요 시 |
| `watcher` | sonnet / medium, background | Monitor 루프로 완료·실패·OOM·무음실패 감지·**사실만 보고**, 판정 없음 | launch 직후 |

순서: 브리프(Advisor) → Worker → auditor(+codex) → Advisor 승인 → launch → watcher → 결과 분석(xhigh) → scribe.

- **모델 넘버링**: settings `model`·`modelSettings` 키는 전체 ID(`claude-fable-5-1`, `claude-opus-5`)로 고정한다(별칭 키 사용 안 함).
  세대가 바뀌어 키가 안 맞으면 **Advisor가 세션 초반에 알리고 사용자가 수정**한다 — 대조 = 내 모델 ID vs `~/.claude/settings.json`,
  또는 Worker `CLAUDE_EFFORT`가 기대(opus=xhigh)와 다를 때. 에이전트 정의 파일은 별칭(fable/opus/sonnet)이라 자동 추종.
- settings·에이전트 정의 변경은 **떠 있는 세션에 반영되지 않는다** → `claude --resume`으로 재시작.

## 코드 검증

- 새 기능 코드를 루프/학습에 태우기 전 codex 리뷰로 조용한 버그(좌표·부호·차원) 점검 — 경로는 **`review-codex` 스킬(tmux `codex` 세션 + 브리지)** 뿐. codex 플러그인(`codex-rescue`)은 2026-09-04 제거했다.

## 호스트 (4× RTX 5090 · RAM 64GB — 여러 프로젝트 공유)

- **GPU 학습 동시 1개** — 모든 프로젝트 합산. 시작 전 RAM/GPU/프로세스 3종 체크,
  launch 후 실기동 검증(무음 실패 주의), kill 후 부재 검증(수초 간격 2회, DDP rank 좀비 주의).
- 디스크 포화 시 이관 규약 = `/mnt/archive/README.md`.
