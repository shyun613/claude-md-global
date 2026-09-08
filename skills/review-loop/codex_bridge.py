#!/usr/bin/env python3
"""tmux 세션의 Codex TUI와 통신하는 브리지 (review-loop / review-codex 스킬용).

서브커맨드:
  resolve --session S --cwd DIR   기존 세션/codex 확보만 한다 (**생성하지 않음**).
                                  세션 없음 / codex 프로세스 없음 → error JSON + exit 6
  create  --session S --cwd DIR   사용자 확인을 받은 뒤에만 호출. CODEX_CMD 로 codex를 새로 띄운다
                                  (이미 떠 있으면 already_running + exit 4)
  send    --session S --text-file F [--force]   idle 확인 후 프롬프트 주입+제출, marker JSON 출력
  wait    --session S --marker JSON [--max-seconds N] [--interval N]   완료 대기 (exit 0=완료, 3=아직, 4=오류)
  last    --session S [--rollout F]   마지막 agent_message 전문 출력
  snapshot --cwd DIR              워킹 트리 스냅샷 트리 객체 생성 (델타 리뷰용). tmux 무관.

생성 명령은 CODEX_CMD 상수 하나로 고정한다 (모델 gpt-6-astra, reasoning effort high,
승인·샌드박스 우회). 이 호스트는 bwrap 샌드박스가 죽어 있어 --sandbox 계열로 띄우면
codex가 파일을 전혀 읽지 못하므로 read-only 생성 경로는 두지 않는다.

검증된 메커니즘:
  - pane_pid → 자손 중 comm==codex → /proc/<pid>/fd 에서 열린 rollout jsonl
  - 여러 rollout 중 첫 줄 session_meta의 source=="cli" 인 것이 메인 TUI 세션 (나머지는 서브에이전트)
  - rollout 파일은 첫 메시지 제출 후에야 생성됨 (신규 세션은 send 후에 잡힘)
  - task_started/task_complete 이벤트로 busy/완료 판정
  - tmux 타깃은 모두 '=' 정확 일치 접두사를 쓴다 (접두사 매칭으로 codex → codex-extra 에
    잘못 붙는 사고를 근본 차단)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

SESS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_CMD = "codex --dangerously-bypass-approvals-and-sandbox -m gpt-6-astra -c model_reasoning_effort=high"


def sh(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def die(msg, code=4):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(code)


def tgt(session, window=None):
    """tmux 타깃 문자열 — '=' 접두사 + 콜론으로 세션 정확 일치만 허용 (콜론 없으면 윈도우 이름으로 해석됨)."""
    return "=%s:%s" % (session, window) if window else "=%s:" % session


def session_exists(name):
    return sh(["tmux", "has-session", "-t", tgt(name)]).returncode == 0


def find_codex(session):
    """세션의 모든 pane 자손에서 codex 프로세스 탐색. (pane_id, pid) 또는 None."""
    r = sh(["tmux", "list-panes", "-s", "-t", tgt(session),
            "-F", "#{session_name} #{pane_id} #{pane_pid}"])
    if r.returncode != 0:
        return None
    children, comm = {}, {}
    for line in sh(["ps", "-eo", "pid=,ppid=,comm="]).stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, c = int(parts[0]), int(parts[1]), parts[2]
        children.setdefault(ppid, []).append(pid)
        comm[pid] = c
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        sess_name, pane_id, pane_pid = fields
        if sess_name != session:  # 이중 안전장치: 접두사 매칭된 다른 세션 배제
            continue
        queue = [int(pane_pid)]
        while queue:
            p = queue.pop(0)
            if comm.get(p) == "codex":
                return pane_id, p
            queue.extend(children.get(p, []))
    return None


def pane_session_name(pane):
    """pane id(%N)가 실제로 속한 세션 이름 (스킬 교차확인용)."""
    r = sh(["tmux", "display", "-p", "-t", pane, "#{session_name}"])
    return r.stdout.strip() if r.returncode == 0 else None


def main_rollout(pid):
    """codex 프로세스가 열고 있는 rollout 중 메인 TUI 세션(source=='cli') 파일."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if not (target.startswith(SESS_DIR) and target.endswith(".jsonl")):
            continue
        try:
            with open(target) as fh:
                first = json.loads(fh.readline())
            if first.get("payload", {}).get("source") == "cli":
                return target
        except (OSError, ValueError):
            continue
    return None


def count_completes(rollout):
    if not rollout or not os.path.exists(rollout):
        return 0
    n = 0
    with open(rollout) as fh:
        for line in fh:
            if '"task_complete"' not in line:
                continue
            try:
                if json.loads(line).get("payload", {}).get("type") == "task_complete":
                    n += 1
            except ValueError:
                pass
    return n


def is_idle(rollout):
    """마지막 lifecycle 이벤트가 task_started가 아니면 idle. rollout 없음(첫 메시지 전)도 idle."""
    if not rollout or not os.path.exists(rollout):
        return True
    last = None
    with open(rollout) as fh:
        for line in fh:
            if not any(k in line for k in ('"task_started"', '"task_complete"', '"turn_aborted"')):
                continue
            try:
                ty = json.loads(line).get("payload", {}).get("type")
            except ValueError:
                continue
            if ty in ("task_started", "task_complete", "turn_aborted"):
                last = ty
    return last != "task_started"


def last_agent_message(rollout):
    last = None
    with open(rollout) as fh:
        for line in fh:
            if '"agent_message"' not in line:
                continue
            try:
                p = json.loads(line).get("payload", {})
            except ValueError:
                continue
            if p.get("type") == "agent_message" and "message" in p:
                last = p["message"]
    return last


def pane_text(pane):
    return sh(["tmux", "capture-pane", "-t", pane, "-p"]).stdout


def wait_tui_ready(session, timeout=60):
    """codex 프로세스 등장 + 입력 프롬프트(›) 대기. trust 프롬프트 감지 시 exit 5."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = find_codex(session)
        if found:
            cap = pane_text(found[0])
            if any(l.lstrip().startswith("›") for l in cap.splitlines()):
                return found
            if "trust" in cap.lower():
                print(json.dumps({"error": "trust_prompt", "pane": found[0],
                                  "hint": "pane을 capture해서 확인 후, 이 프로젝트가 맞으면 Enter로 수락하고 resolve 재실행"},
                                 ensure_ascii=False))
                sys.exit(5)
        time.sleep(2)
    die("codex TUI가 60초 내에 준비되지 않음")


def cmd_resolve(a):
    """기존 세션의 codex를 찾기만 한다. 생성은 하지 않는다 (create 서브커맨드가 담당)."""
    hint = "사용자 확인 후 `create --session %s --cwd %s`" % (a.session, a.cwd)
    if not session_exists(a.session):
        print(json.dumps({"error": "no_session", "session": a.session, "cwd": a.cwd,
                          "hint": hint}, ensure_ascii=False))
        sys.exit(6)
    found = find_codex(a.session)
    if not found:
        print(json.dumps({"error": "no_codex_process", "session": a.session, "cwd": a.cwd,
                          "hint": hint}, ensure_ascii=False))
        sys.exit(6)
    pane, pid = found
    rollout = main_rollout(pid)
    print(json.dumps({"session": a.session, "pane": pane, "pane_session": pane_session_name(pane),
                      "codex_pid": pid, "rollout": rollout,
                      "idle": is_idle(rollout)}, ensure_ascii=False))


def cmd_create(a):
    """사용자 확인을 받은 뒤에만 호출: CODEX_CMD 로 codex TUI를 새로 띄운다."""
    exists = session_exists(a.session)
    if exists:
        found = find_codex(a.session)
        if found:
            print(json.dumps({"error": "already_running", "session": a.session,
                              "pane": found[0], "codex_pid": found[1],
                              "hint": "이미 codex가 떠 있다 — create 대신 resolve를 써라"},
                             ensure_ascii=False))
            sys.exit(4)
        r = sh(["tmux", "new-window", "-t", tgt(a.session), "-n", "review", "-c", a.cwd,
                "-P", "-F", "#{pane_id}"])
        if r.returncode != 0:
            die("tmux new-window 실패: " + r.stderr.strip())
    else:
        r = sh(["tmux", "new-session", "-d", "-s", a.session, "-c", a.cwd, "-x", "220", "-y", "50",
                "-P", "-F", "#{pane_id}"])
        if r.returncode != 0:
            die("tmux new-session 실패: " + r.stderr.strip())
    # tmux 3.4의 target-pane은 '=S'(콜론 없는 형태)를 해석하지 못한다 → 갓 만든 pane id로 직접 보낸다.
    target = r.stdout.strip()
    if not target.startswith("%"):
        die("새 pane id를 얻지 못함: " + repr(r.stdout))
    sh(["tmux", "send-keys", "-t", target, CODEX_CMD, "Enter"])
    pane, pid = wait_tui_ready(a.session)
    print(json.dumps({"session": a.session, "pane": pane, "codex_pid": pid,
                      "rollout": main_rollout(pid), "created": True, "cwd": a.cwd,
                      "model": "gpt-6-astra", "effort": "high"}, ensure_ascii=False))


def cmd_send(a):
    found = find_codex(a.session)
    if not found:
        die("세션 %s 에 codex 프로세스가 없음" % a.session)
    pane, pid = found
    rollout = main_rollout(pid)
    if not a.force and not is_idle(rollout):
        die("codex가 작업 중 (task_started 후 task_complete 없음). 기다렸다가 재시도", 3)
    marker = {"rollout": rollout, "completes": count_completes(rollout), "sent_at": int(time.time())}
    sh(["tmux", "load-buffer", "-b", "rlbuf", a.text_file])
    sh(["tmux", "paste-buffer", "-p", "-d", "-b", "rlbuf", "-t", pane])
    time.sleep(1)
    sh(["tmux", "send-keys", "-t", pane, "Enter"])
    print(json.dumps(marker))


def cmd_wait(a):
    marker = json.loads(a.marker)
    deadline = time.time() + a.max_seconds
    rollout = marker.get("rollout")
    while time.time() < deadline:
        found = find_codex(a.session)
        if not found:
            die("codex 프로세스가 사라짐")
        if not rollout:  # 신규 세션: 첫 메시지 후 생성된 파일을 fd로 잡는다
            rollout = main_rollout(found[1])
        if rollout and count_completes(rollout) > marker.get("completes", 0):
            print(json.dumps({"done": True, "rollout": rollout}))
            return
        time.sleep(a.interval)
    print(json.dumps({"done": False, "rollout": rollout}))
    sys.exit(3)


def cmd_last(a):
    rollout = a.rollout
    if not rollout:
        found = find_codex(a.session)
        if not found:
            die("세션 %s 에 codex 프로세스가 없음" % a.session)
        rollout = main_rollout(found[1])
    if not rollout:
        die("rollout 파일을 찾지 못함")
    msg = last_agent_message(rollout)
    if msg is None:
        die("rollout에 agent_message가 없음")
    print(msg)


def cmd_snapshot(a):
    """워킹 트리 전체(untracked 포함, .gitignore 준수)의 트리 객체를 만든다 — 델타 리뷰용.

    임시 GIT_INDEX_FILE 에서만 add 하므로 실제 인덱스·워킹 트리·refs·브랜치는 무변경이다.
    라운드 r 직전에 호출해 tree_r 을 기록해 두면 `git diff <tree_{r-1}> <tree_r>` 이 그 라운드의 델타다.
    """
    d = a.cwd
    if not os.path.isdir(d):
        die("디렉터리가 아님: " + d)
    if sh(["git", "-C", d, "rev-parse", "--git-dir"]).returncode != 0:
        die("git 리포가 아님: " + d)
    tmp_index = tempfile.mktemp(prefix="codex-bridge-index-")
    env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
    try:
        # HEAD 가 있으면 그 트리로 임시 인덱스를 채운 뒤 워킹 트리를 얹는다 (최초 커밋 전이면 건너뜀).
        if sh(["git", "-C", d, "rev-parse", "--verify", "-q", "HEAD"]).returncode == 0:
            r = sh(["git", "-C", d, "read-tree", "HEAD"], env)
            if r.returncode != 0:
                die("git read-tree 실패: " + r.stderr.strip())
        r = sh(["git", "-C", d, "add", "-A"], env)
        if r.returncode != 0:
            die("git add -A 실패: " + r.stderr.strip())
        r = sh(["git", "-C", d, "write-tree"], env)
        if r.returncode != 0:
            die("git write-tree 실패: " + r.stderr.strip())
        print(json.dumps({"tree": r.stdout.strip(), "cwd": os.path.abspath(d)}))
    finally:
        for p in (tmp_index, tmp_index + ".lock"):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("create")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("send")
    p.add_argument("--session", required=True)
    p.add_argument("--text-file", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--session", required=True)
    p.add_argument("--marker", required=True)
    p.add_argument("--max-seconds", type=int, default=540)
    p.add_argument("--interval", type=int, default=10)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("last")
    p.add_argument("--session", required=True)
    p.add_argument("--rollout")
    p.set_defaults(fn=cmd_last)

    p = sub.add_parser("snapshot")
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_snapshot)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
