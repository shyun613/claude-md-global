# review-loop / review-codex 공통 절차

§A~§C는 호출 직후 읽는다. §D는 드문 경로 — exit 4/5, wait exit 3 누적, 중단 시에만.
브리지(두 스킬 공용): `BRIDGE="python3 ~/.claude/skills/review-loop/codex_bridge.py"`

## §A 세션 확보

- `--session <이름>` 인자가 있으면 그 세션, 없으면 `codex` 고정.
- `$BRIDGE resolve --session <이름> --cwd <세션 cwd 절대경로>`. **resolve는 세션을 만들지 않는다 — 떠 있는 세션에 붙는 것이 기본.**
  - **exit 0**: 기존 세션 사용. `pane_session` 이 요청한 이름과 다르면 중단하고 보고하라.
  - **exit 6 (`no_session` / `no_codex_process`)**: **멈추고 보고한 뒤 지시를 기다린다.** 문구 예: "tmux `<이름>` 에 codex가 없습니다. `<cwd>` 에서 `gpt-6-astra` / effort high / `--dangerously-bypass-approvals-and-sandbox` 로 새로 띄울까요?"
    - 확인 받으면 `$BRIDGE create --session <이름> --cwd <cwd>` → exit 0 후 `resolve` 재실행. 거절하면 종료. create exit 5/4는 §D.
- `rollout: null` 은 정상 (첫 메시지 전엔 파일 없음). send 후 wait가 잡는다.
- **codex는 쓰기 권한이 있다** — 프롬프트의 수정 금지 문구가 유일한 방어선이다. 절대 빼먹지 마라. 완료 후 `git status` 로 확인해라.
- 브리지는 tmux 타깃에 `=` 정확 일치를 써서 `codex` 요청이 `codex-extra` 를 무는 사고를 차단한다.

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

- 프롬프트는 임시 파일로 쓴다(`$CLAUDE_JOB_DIR/tmp` 우선) — bracketed paste로 멀티라인 안전 전송.
- `$BRIDGE send --session <이름> --text-file <파일>` → marker JSON 보관. exit 3이면 작업 중 → wait로 기다렸다 재시도 (`--force` 금지).
- `$BRIDGE wait --session <이름> --marker '<marker JSON>'` 을 Bash `run_in_background` 로. exit 0이면 `$BRIDGE last --session <이름> --rollout <wait가 출력한 rollout>` 으로 전문 회수. exit 3/4는 §D.
- **판정을 화면 캡처로 하지 마라** — 잘리고 접힌다. 반드시 `last`(rollout의 agent_message 전문)를 써라.
- 대기 중 사용자에게 진행 상황을 한 줄로 알려라.

## §D 드문 경로

아래는 해당 exit 코드가 나올 때만 읽는다 — 정상 흐름에서는 읽을 필요가 없다.

- **create exit 5 (`trust_prompt`)**: pane을 capture해서 확인하고, 리뷰 대상 프로젝트의 신뢰 확인이 맞으면 `tmux send-keys -t <pane> Enter` 로 수락한 뒤 `resolve` 재실행.
- **create exit 4 (`already_running`)**: 그 사이 codex가 떠 있다는 뜻 → `resolve` 로 돌아간다.
- **wait exit 3** (9분 경과, 아직 진행 중): 그대로 재실행한다. 누적 한도를 넘으면 스톨로 판단하고 중단 절차로 — review-codex 1시간, review-loop 2시간.
- **wait exit 4**: codex 프로세스 소멸 등 오류 → 중단 절차로.
- **wait 조기 발화**: `last` 회수 결과가 이번 질문과 무관해 보이면 옛 rollout을 짚은 것이다. pane capture의 "Working" 표시로 진행 중 여부를 먼저 확인하라.
- **중단 절차**: ① PushNotification 도구로 사용자에게 알린다 (ToolSearch로 스키마 로드 후 사용, 불가하면 보고 메시지로 대체). ② 최종 보고 — 라운드별 verdict 이력, 미해결 이슈 목록(심각도·파일 포함), 시도한 조치, 권장 다음 단계. ③ 코드는 마지막 검증된 상태로 남긴다 — 미검증 수정을 얹지 마라.
