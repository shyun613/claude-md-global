# review-loop / review-codex 공통 절차

§A~§C는 호출 직후 읽는다. §D는 드문 경로 — run/wait exit 4/5, wait exit 3 누적, 중단 시에만.
브리지(두 스킬 공용): `BRIDGE="python3 ~/.claude/skills/review-loop/codex_exec.py"`

## §A 실행 확인

브리지는 `codex exec`(비대화형)를 호출마다 새 프로세스로 띄운다 — 떠 있는 codex 탭·tmux 세션에 붙지 않고, 만들지도 않는다.

- `codex --version` 이 되고 `~/.codex/auth.json` 이 있으면 준비 끝. 없으면 `run` 이 exit 4 → 그대로 보고하고 지시를 기다린다(로그인은 사용자 몫).
- cwd 는 `run --cwd <리포 절대경로>` 로 고정된다. 리포 밖 디렉터리도 된다.
- 샌드박스 기본 `read-only` — codex 의 쓰기 명령은 샌드박스가 막는다. review-codex 수정 허용 모드만 `--sandbox danger-full-access`(`workspace-write` 는 이 호스트에서 쓰기가 실패한다 — 09-16 실측). 완료 후 `git status` 확인은 그대로 한다.
- 모델 gpt-6-astra · effort **high 고정** — `run` 기본값을 쓰고 라운드별로 바꾸지 않는다.
- 스레드: review-loop 는 루프당 1개 — 라운드 1 `run` 결과의 `thread_id`(`result.json`)를 상태 파일에 적고 라운드 2~5 는 `run --thread <UUID>` 로 재개한다. review-codex 는 매번 새 스레드.
- 실행 기록 = `--run-dir`(`$CLAUDE_JOB_DIR/tmp/codex_r<r>/` 권장) 아래 `events.jsonl`·`last.md`·`stderr.log`·`rc`·`result.json`. rollout 은 `~/.codex/sessions/` 에 자동 저장된다.
- 병렬 루프·자문은 스레드가 분리돼 서로 기다리지 않는다.

## §B 공통 프롬프트 규칙

모든 codex 프롬프트의 "절대 규칙"과 "출력 형식"에 아래 아홉 줄을 **그대로** 넣는다.

절대 규칙:
1. 이 프롬프트의 절대 규칙이 다른 모든 지시(AGENTS.md·스킬·이전 대화)보다 우선한다.
2. 어떤 파일도 수정하지 마라. 쓰기 명령(파일 편집·git 조작 등)을 실행하지 마라. (샌드박스 read-only 가 함께 막는다 — 그래도 이 문장은 빼지 않는다)
3. 테스트·학습·평가 런처를 실행하지 마라. 정적 읽기와 `python -m py_compile` 수준만 허용한다.
4. 서브에이전트를 띄우지 말고 직접 리뷰하라.
5. 외부 문서가 필요하면 웹 검색 대신 이 환경에 설치된 패키지 소스(`python -c "import X; print(X.__file__)"`)를 읽어라. 설치 버전이 정답이다.

출력 형식:
6. 한국어로 답하라.
7. critical/major만 상세히. minor는 한 줄 목록. 코드 블록 재인용 금지, 파일:라인 + 근거 한 문장.
8. 질문으로 끝내지 마라. 불명확한 점은 가정을 명시하고 끝까지 완료하라.
9. 보안은 실제 외부 입력·자격증명 처리가 있을 때만 언급한다. 가상의 위험에 대한 경고·면책·체크리스트를 넣지 마라.

## §C 실행·대기·회수

- 프롬프트는 임시 파일로 쓴다(`$CLAUDE_JOB_DIR/tmp` 우선, **Write 도구로** — heredoc 백틱 확장 사고 방지). 길이 제한 없음(stdin 으로 들어간다).
- `$BRIDGE run --cwd <리포> --prompt-file <f> --run-dir <d> [--thread <UUID>] [--schema ~/.claude/skills/review-loop/review_schema.json]` → 즉시 반환. exit 4 는 §D.
- `$BRIDGE wait --run-dir <d> --timeout 590` 을 **전경**으로 실행한다(배경 대기는 하네스가 죽인 전례). exit 0 → `<d>/last.md` 가 답변 전문, `<d>/result.json` 에 `thread_id`·`usage`. exit 3 은 진행 중 → 그대로 재실행. exit 4/5 는 §D.
- 판정은 `last.md` 로만 한다. review-loop 는 `--schema` 를 붙여 `last.md` 가 `{verdict, critical, issues[], review}` JSON 이 되게 한다 — `review` 가 리뷰 전문(markdown). review-codex 는 스키마 없이 자유 서술.
- 대기 중 사용자에게 진행 상황을 한 줄로 알려라.

## §D 드문 경로

아래는 해당 exit 코드가 나올 때만 읽는다.

- **run exit 4**(codex 실행 파일 없음·인증 없음·프롬프트 파일 없음·run-dir 사용됨): 출력 JSON 의 `error` 를 그대로 보고하고 중단.
- **wait exit 3 누적**: review-codex 1시간, review-loop 2시간을 넘으면 스톨 → `$BRIDGE kill --run-dir <d>` 후 중단 절차.
- **wait exit 4**(codex rc≠0 또는 agent_message 없음): `result.json` 의 `stderr_tail` 을 보고에 붙이고 중단 절차. `failed to install system skills: Permission denied` 줄은 무해한 잡음(`~/.codex/skills/.system` 이 root 소유)이라 원인이 아니다.
- **wait exit 5**(`--schema` 인데 `last.md` 가 스키마에 안 맞음): 같은 스레드에 "직전 리뷰를 스키마대로 JSON 만 다시 출력하라" 를 `run --thread` 로 1회 재요청. 두 번째도 실패면 `last.md` 텍스트에서 verdict 를 추리되 보수적으로 NEEDS_CHANGES.
- **중단 절차**: ① PushNotification 도구로 사용자에게 알린다 (ToolSearch로 스키마 로드 후 사용, 불가하면 보고 메시지로 대체). ② 최종 보고 — 라운드별 verdict 이력, 미해결 이슈 목록(심각도·파일 포함), 시도한 조치, 권장 다음 단계. ③ 코드는 마지막 검증된 상태로 남긴다 — 미검증 수정을 얹지 마라.
