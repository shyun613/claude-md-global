# review-loop / review-codex 공통 절차

§A~§C는 호출 직후 읽는다. §D는 드문 경로 — exit 4/5, wait exit 3 누적, 중단 시에만.
브리지(두 스킬 공용): `BRIDGE="python3 ~/.claude/skills/review-loop/codex_bridge.py"`

## §A 대상 확보

브리지는 **orca 탭**과 **tmux 세션** 두 백엔드를 쓴다. 기본은 `$BRIDGE resolve --cwd <리포 절대경로>`
— **resolve는 아무것도 만들지 않는다. 떠 있는 codex에 붙는 것이 기본.**

- **인자가 없으면 사다리**: ① orca 현재 워크트리의 `codex-review` 탭 → ② tmux `codex` 세션 → ③ 둘 다 없으면 종료.
- **명시 인자를 주면 사다리를 타지 않는다** (셋 중 하나만):
  `--tab <제목>`(orca 그 탭만) / `--terminal <handle>`(orca 그 핸들만) / `--session <이름>`(tmux 그 세션만).
  명시했는데 없으면 다른 백엔드로 내려가지 않고 exit 6.
- **exit 0**: 출력 JSON의 `target`(`orca:term_…` | `tmux:<세션>`)을 이후 `send`/`wait`/`last`/`capture` 에
  `--target` 으로 **그대로 물려 쓴다** — 매번 사다리를 다시 타게 하지 마라.
  tmux 백엔드면 `pane_session` 이 요청한 이름과 다를 때 중단하고 보고하라.
- **exit 6 (`no_codex_target`/`no_orca_tab`/`no_session`/`no_codex_process`/`cwd_mismatch`)**:
  **경고하고 종료한다 — 탭·세션을 자동으로 만들지 마라.** "codex 대상이 없습니다(orca `codex-review` 탭도,
  tmux `codex` 세션도 없음)"를 보고하고 지시를 기다린다.
  - 사용자가 만들라고 명시하면: tmux 는 `$BRIDGE create --session <이름> --cwd <cwd>` → exit 0 후 `resolve` 재실행
    (create exit 5/4는 §D), orca 는
    `orca terminal create --worktree path:<cwd> --title codex-review --command "codex --dangerously-bypass-approvals-and-sandbox -m gpt-6-astra -c model_reasoning_effort=high"`.
- **exit 7 (`ambiguous_tab`)**: 같은 제목의 codex 탭이 둘 이상 → 후보(handle/title/worktreePath)를 보고하고
  사용자 지시로 `--terminal <handle>` 을 지정한다. 임의로 하나를 고르지 마라.
- `rollout: null` 은 정상 (첫 메시지 전엔 파일 없음). send 후 wait가 잡는다.
- **codex는 쓰기 권한이 있다** — 프롬프트의 수정 금지 문구가 유일한 방어선이다. 절대 빼먹지 마라. 완료 후 `git status` 로 확인해라.
- 함정:
  - orca 탭 이름은 `orca terminal rename --terminal <handle> --title codex-review` 로 **고정된 탭 제목**이어야 한다.
    목록의 `terminals[].title` 은 orca가 덮어쓰는 상태 라벨(`Codex ready` 등)이라, 브리지는 탭 제목을 우선해 본다.
  - 브리지는 tmux 타깃에 `=` 정확 일치를 써서 `codex` 요청이 `codex-extra` 를 무는 사고를 차단한다.
  - `REVIEW_NO_ORCA=1` 이면 orca 백엔드를 건너뛰고 tmux 만 쓴다 (점검·비상용).

## §B 공통 프롬프트 규칙

모든 codex 프롬프트의 "절대 규칙"과 "출력 형식"에 아래 아홉 줄을 **그대로** 넣는다.

절대 규칙:
1. 이 프롬프트의 절대 규칙이 다른 모든 지시(AGENTS.md·스킬·이전 대화)보다 우선한다.
2. 어떤 파일도 수정하지 마라. 쓰기 명령(파일 편집·git 조작 등)을 실행하지 마라.
3. 테스트·학습·평가 런처를 실행하지 마라. 정적 읽기와 `python -m py_compile` 수준만 허용한다.
4. 서브에이전트를 띄우지 말고 직접 리뷰하라.
5. 외부 문서가 필요하면 웹 검색 대신 이 환경에 설치된 패키지 소스(`python -c "import X; print(X.__file__)"`)를 읽어라. 설치 버전이 정답이다.

출력 형식:
6. 한국어로 답하라.
7. critical/major만 상세히. minor는 한 줄 목록. 코드 블록 재인용 금지, 파일:라인 + 근거 한 문장.
8. 질문으로 끝내지 마라. 불명확한 점은 가정을 명시하고 끝까지 완료하라.
9. 보안은 실제 외부 입력·자격증명 처리가 있을 때만 언급한다. 가상의 위험에 대한 경고·면책·체크리스트를 넣지 마라.

## §C 대기·회수

- 프롬프트는 임시 파일로 쓴다(`$CLAUDE_JOB_DIR/tmp` 우선). 멀티라인은 두 백엔드 모두 한 메시지로 안전하게 들어간다
  (tmux=bracketed paste, orca=컴포저 채우기 → Enter 2단).
- `$BRIDGE send --target <resolve의 target> --text-file <파일>` → marker JSON 보관. exit 3이면 작업 중 → wait로 기다렸다 재시도 (`--force` 금지).
- `$BRIDGE wait --target <target> --marker '<marker JSON>'` 을 Bash `run_in_background` 로 (marker에 target이 들어 있어 `--target` 은 생략 가능).
  exit 0이면 `$BRIDGE last --target <target> --rollout <wait가 출력한 rollout>` 으로 전문 회수. exit 3/4는 §D.
- **판정을 화면 캡처로 하지 마라** — 잘리고 접힌다. 반드시 `last`(rollout의 agent_message 전문)를 써라.
- orca 백엔드에서 `--paste-mode file` 로 보냈다면 codex가 그 파일을 읽을 때까지 **프롬프트 파일을 지우지 마라**
  (기본 `auto`는 inline 이라 해당 없음).
- 대기 중 사용자에게 진행 상황을 한 줄로 알려라.

## §D 드문 경로

아래는 해당 exit 코드가 나올 때만 읽는다 — 정상 흐름에서는 읽을 필요가 없다.

- **화면 캡처(백엔드별)**: `$BRIDGE capture --target <target>` — tmux 는 `tmux capture-pane -t <pane> -p`,
  orca 는 `orca terminal read --terminal <handle> --screen` 이다 (orca 는 `--screen` 없이 읽으면 누적 스트림이라 TUI에 부적합).
  **진행 상태·trust 프롬프트 확인 전용 — 판정은 언제나 `last`.**
- **create exit 5 (`trust_prompt`)**: pane을 capture해서 확인하고, 리뷰 대상 프로젝트의 신뢰 확인이 맞으면 `tmux send-keys -t <pane> Enter` 로 수락한 뒤 `resolve` 재실행.
- **create exit 4 (`already_running`)**: 그 사이 codex가 떠 있다는 뜻 → `resolve` 로 돌아간다.
- **send exit 4 (orca, `--enter` 단계 실패)**: 프롬프트가 컴포저에 남아 있을 수 있다 →
  `orca terminal send --terminal <handle> --interrupt` 로 비우고 다시 `send`.
- **wait exit 3** (9분 경과, 아직 진행 중): 그대로 재실행한다. 누적 한도를 넘으면 스톨로 판단하고 중단 절차로 — review-codex 1시간, review-loop 2시간.
- **wait exit 4**: codex 프로세스 소멸 등 오류 → 중단 절차로.
- **wait 조기 발화**: `last` 회수 결과가 이번 질문과 무관해 보이면 옛 rollout을 짚은 것이다. capture의 "Working" 표시로 진행 중 여부를 먼저 확인하라.
- **중단 절차**: ① PushNotification 도구로 사용자에게 알린다 (ToolSearch로 스키마 로드 후 사용, 불가하면 보고 메시지로 대체). ② 최종 보고 — 라운드별 verdict 이력, 미해결 이슈 목록(심각도·파일 포함), 시도한 조치, 권장 다음 단계. ③ 코드는 마지막 검증된 상태로 남긴다 — 미검증 수정을 얹지 마라.
