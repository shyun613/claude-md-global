#!/usr/bin/env python3
"""tmux 세션의 Codex TUI와 통신하는 브리지 (review-loop 스킬용).

서브커맨드:
  resolve --session S --cwd DIR   세션/코덱스 확보 (없으면 read-only로 생성), 상태 JSON 출력
  send    --session S --text-file F [--force]   idle 확인 후 프롬프트 주입+제출, marker JSON 출력
  wait    --session S --marker JSON [--max-seconds N] [--interval N]   완료 대기 (exit 0=완료, 3=아직, 4=오류)
  last    --session S [--rollout F]   마지막 agent_message 전문 출력

검증된 메커니즘:
  - pane_pid → 자손 중 comm==codex → /proc/<pid>/fd 에서 열린 rollout jsonl
  - 여러 rollout 중 첫 줄 session_meta의 source=="cli" 인 것이 메인 TUI 세션 (나머지는 서브에이전트)
  - rollout 파일은 첫 메시지 제출 후에야 생성됨 (신규 세션은 send 후에 잡힘)
  - task_started/task_complete 이벤트로 busy/완료 판정
"""
import argparse
import json
import os
import subprocess
import sys
import time

SESS_DIR = os.path.expanduser("~/.codex/sessions")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def die(msg, code=4):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(code)


def session_exists(name):
    return sh(["tmux", "has-session", "-t", name]).returncode == 0


def find_codex(session):
    """세션의 모든 pane 자손에서 codex 프로세스 탐색. (pane_id, pid) 또는 None."""
    r = sh(["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_id} #{pane_pid}"])
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
        pane_id, pane_pid = line.split()
        queue = [int(pane_pid)]
        while queue:
            p = queue.pop(0)
            if comm.get(p) == "codex":
                return pane_id, p
            queue.extend(children.get(p, []))
    return None


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
            if not any(t in line for t in ('"task_started"', '"task_complete"', '"turn_aborted"')):
                continue
            try:
                t = json.loads(line).get("payload", {}).get("type")
            except ValueError:
                continue
            if t in ("task_started", "task_complete", "turn_aborted"):
                last = t
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
    created = False
    if not session_exists(a.session):
        r = sh(["tmux", "new-session", "-d", "-s", a.session, "-c", a.cwd, "-x", "220", "-y", "50"])
        if r.returncode != 0:
            die("tmux new-session 실패: " + r.stderr.strip())
        sh(["tmux", "send-keys", "-t", a.session, "codex --sandbox read-only", "Enter"])
        created = True
    found = find_codex(a.session)
    if not found and not created:
        # 세션은 있는데 codex가 없음 → 리뷰용 window를 새로 열어 read-only codex 실행
        r = sh(["tmux", "new-window", "-t", a.session, "-n", "review", "-c", a.cwd])
        if r.returncode != 0:
            die("tmux new-window 실패: " + r.stderr.strip())
        sh(["tmux", "send-keys", "-t", a.session + ":review", "codex --sandbox read-only", "Enter"])
        created = True
    if not found:
        found = wait_tui_ready(a.session)
    pane, pid = found
    rollout = main_rollout(pid)
    print(json.dumps({"session": a.session, "pane": pane, "codex_pid": pid,
                      "rollout": rollout, "created_readonly": created,
                      "idle": is_idle(rollout)}, ensure_ascii=False))


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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_resolve)

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

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
