#!/usr/bin/env python3
"""Codex TUI 브리지 (review-loop / review-codex 스킬 공용) — **orca 탭 / tmux 세션** 두 백엔드.

서브커맨드:
  resolve [--tab T | --terminal H | --session S] --cwd DIR
          리뷰 대상 codex를 확보만 한다 (**생성하지 않음**). 인자가 없으면 사다리를 탄다:
            ① orca: 현재 워크트리의 title=='codex-review' codex 탭
            ② tmux: 'codex' 세션
            ③ 둘 다 없으면 no_codex_target + exit 6 (자동 생성 금지 — 사용자에게 보고)
          출력의 `target` 토큰을 send/wait/last/capture 에 그대로 물려 쓴다 (사다리 재탐색 방지).
  create  --session S --cwd DIR   (tmux 전용) 사용자 확인을 받은 뒤에만 호출. CODEX_CMD 로 codex를
                                  새로 띄운다 (이미 떠 있으면 already_running + exit 4).
                                  orca 탭 생성은 브리지가 하지 않는다 — common.md §A의 orca terminal create 안내 참조.
  send    --target TOK --text-file F [--force] [--paste-mode auto|inline|file]  idle 확인 후 주입+제출, marker JSON
  wait    --target TOK --marker JSON [--max-seconds N] [--interval N]   완료 대기 (exit 0=완료, 3=아직, 4=오류)
  last    --target TOK [--rollout F]   마지막 agent_message 전문 출력
  capture --target TOK            화면 캡처 (trust 프롬프트·진행 상태 확인 전용. **판정에는 쓰지 마라**)
  snapshot --cwd DIR              워킹 트리 스냅샷 트리 객체 생성 (델타 리뷰용). 백엔드 무관.

target 토큰: 'orca:<handle>' | 'tmux:<session>'. `--session S` 만 준 경우 'tmux:S' 로 해석한다(하위호환).
exit: 2=인자 오류, 3=busy/미완, 4=오류, 5=trust 프롬프트, 6=대상 없음, 7=후보 중복.

생성 명령은 CODEX_CMD 상수 하나로 고정한다 (모델 gpt-6-astra, reasoning effort high,
승인·샌드박스 우회). 이 호스트는 bwrap 샌드박스가 죽어 있어 --sandbox 계열로 띄우면
codex가 파일을 전혀 읽지 못하므로 read-only 생성 경로는 두지 않는다.

검증된 메커니즘:
  - [공통] rollout jsonl 기반 판정 — 화면 캡처로 완료를 판정하지 않는다
      · codex 프로세스의 /proc/<pid>/fd 에서 열린 rollout jsonl
      · 여러 rollout 중 첫 줄 session_meta의 source=="cli" 인 것이 메인 TUI 세션 (나머지는 서브에이전트)
      · rollout 파일은 첫 메시지 제출 후에야 생성됨 (신규 세션은 send 후에 잡힘)
      · task_started/task_complete 이벤트로 busy/완료 판정
  - [tmux] pane_pid → 자손 중 comm==codex. 타깃은 모두 '=' 정확 일치 접두사 (접두사 매칭으로
      codex → codex-extra 에 잘못 붙는 사고를 근본 차단)
  - [orca] orca가 각 터미널 셸에 ORCA_TERMINAL_HANDLE 을 export하고 codex가 이를 상속한다 →
      /proc/<pid>/environ 에 그 핸들이 있는 comm=="codex" 프로세스가 그 탭의 codex다.
      PID 매핑 성공이 곧 동일 호스트 증명 (원격 호스트 탭이면 매핑이 실패한다).
      codex는 자식으로 codex-code-mode-host 를 띄우므로 comm 정확 일치로 거르고,
      그래도 복수면 부모가 후보에 없는 것(TUI 루트)을 고른다.
      orca CLI stdout 앞에는 '[relay-connect] Handshake OK ...' preamble 한 줄이 붙는다 → 첫 '{' 부터 파싱.
      전송은 2단(--text 로 컴포저 채우기 → --enter 로 제출)이다. 한 호출에 --text 와 --enter 를 같이 주면
      agent_prompt_blocked 로 거부된다. --text 의 개행은 조기 제출을 일으키지 않는다(멀티라인 안전).
      탭 제목은 rename 으로 custom title 을 박아 두지 않으면 orca 가 상태 라벨('Codex ready' 등)로
      덮어쓴다 → 사다리 1단이 쓰는 'codex-review' 는 `orca terminal rename` 으로 고정된 제목이어야 한다.
      REVIEW_NO_ORCA=1 이면 orca 백엔드를 통째로 비활성화한다 (tmux 경로 점검·비상용).
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

ORCA_BIN = "orca"
ORCA_TAB_NAME = "codex-review"   # 사다리 1단이 찾는 orca 탭 제목 (탭 이름 정확 일치, 상태 라벨도 대조)
TMUX_SESSION = "codex"           # 사다리 2단에서 찾는 tmux 세션 이름
ORCA_TIMEOUT = 30                # orca CLI 호출 타임아웃(초). 호출당 ~1초.
# orca 는 --text 를 컴포저에 담아 두었다가 --enter 에서 한 메시지로 제출한다 (실측: 3줄 프롬프트가
# 조기 제출 없이 그대로 남았고 --enter 한 번에 한 메시지로 도착) → 멀티라인 inline 전송 안전.
ORCA_MULTILINE_SAFE = True


def sh(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def die(msg, code=4, **extra):
    payload = {"error": msg}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


# ─────────────────────────────── 플랫폼 shim (Linux=/proc, macOS=ps/lsof) ───────────────────────────────
# tail-srv(Linux)와 Mac 양쪽에서 같은 파일을 쓴다. Linux 경로는 그대로, darwin 만 ps/lsof 로 대체.

IS_LINUX = sys.platform.startswith("linux")


def _lsof_names(pid, extra=()):
    """lsof -Fn 출력의 'n<path>' 줄들. 자기 프로세스만 조회하므로 권한 문제 없음."""
    r = sh(["lsof", "-a", "-p", str(pid), "-Fn"] + list(extra))
    if r.returncode != 0 and not r.stdout:
        return []
    return [line[1:] for line in r.stdout.splitlines() if line.startswith("n")]


def _open_files(pid):
    if IS_LINUX:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            return []
        out = []
        for fd in fds:
            try:
                out.append(os.readlink(os.path.join(fd_dir, fd)))
            except OSError:
                continue
        return out
    return _lsof_names(pid)


def _proc_environ(pid):
    """환경변수 바이트열 목록 (b'K=V'). darwin 은 ps -E (자기 프로세스만 보임)."""
    if IS_LINUX:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                return fh.read().split(b"\0")
        except OSError:
            return None
    r = sh(["ps", "-Eww", "-o", "command=", "-p", str(pid)])
    if r.returncode != 0:
        return None
    return [t.encode() for t in r.stdout.split()]


def _proc_comm(pid):
    if IS_LINUX:
        try:
            with open(f"/proc/{pid}/comm") as fh:
                return fh.read().strip()
        except OSError:
            return None
    r = sh(["ps", "-o", "comm=", "-p", str(pid)])
    return os.path.basename(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _all_pids():
    if IS_LINUX:
        return [int(n) for n in os.listdir("/proc") if n.isdigit()]
    r = sh(["ps", "-axo", "pid="])
    return [int(x) for x in r.stdout.split() if x.isdigit()]


# ─────────────────────────────── 공통: rollout 판정 ───────────────────────────────

def _is_cli_rollout(path):
    try:
        with open(path) as fh:
            first = json.loads(fh.readline())
        return first.get("payload", {}).get("source") == "cli", first.get("payload", {})
    except (OSError, ValueError):
        return False, {}


def main_rollout(pid):
    """codex 프로세스가 열고 있는 rollout 중 메인 TUI 세션(source=='cli') 파일.

    darwin 폴백: codex 가 rollout fd 를 상시 열어두지 않을 수 있어, fd 에서 못 찾으면
    SESS_DIR 에서 첫 줄 source=='cli' 이고 cwd 가 그 프로세스 cwd 와 같은 파일 중 최신을 고른다.
    """
    for target in _open_files(pid):
        if not (target.startswith(SESS_DIR) and target.endswith(".jsonl")):
            continue
        ok, _ = _is_cli_rollout(target)
        if ok:
            return target
    if IS_LINUX:
        return None
    cwd = proc_cwd(pid)
    started = _proc_start_epoch(pid)
    if not cwd or started is None:
        return None
    # 프로세스 시작 이전 파일은 제외 — 같은 cwd 의 옛 세션 rollout 을 집는 사고 방지
    best, best_m = None, started - 5
    for root, _dirs, files in os.walk(SESS_DIR):
        for f in files:
            if not (f.startswith("rollout-") and f.endswith(".jsonl")):
                continue
            path = os.path.join(root, f)
            try:
                m = os.path.getmtime(path)
            except OSError:
                continue
            if m <= best_m:
                continue
            ok, meta = _is_cli_rollout(path)
            if ok and os.path.realpath(str(meta.get("cwd", ""))) == os.path.realpath(cwd):
                best, best_m = path, m
    return best


def _proc_start_epoch(pid):
    """darwin: ps etime([[dd-]hh:]mm:ss) 로 프로세스 시작 시각(epoch) 추정."""
    r = sh(["ps", "-o", "etime=", "-p", str(pid)])
    s = r.stdout.strip()
    if r.returncode != 0 or not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return time.time() - (days * 86400 + h * 3600 + m * 60 + sec)


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
    """마지막 어시스턴트 메시지 전문. codex 버전별 rollout 형식을 모두 본다:
      · ≤0.153: event_msg payload.type=='agent_message' + payload.message
      · 0.154+: event_msg 'task_complete' 의 payload.last_agent_message,
                event_msg 'item_completed' 의 item.type=='AgentMessage' (item.text),
                response_item 'message' role=='assistant' (content[].text)
    파일 순서상 가장 뒤의 것을 채택한다."""
    last = None
    with open(rollout) as fh:
        for line in fh:
            if not any(k in line for k in ('"agent_message"', '"task_complete"', '"AgentMessage"', '"assistant"')):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload", {})
            if not isinstance(p, dict):
                continue
            ty, msg = p.get("type"), None
            if ty == "agent_message" and "message" in p:
                msg = p["message"]
            elif ty == "task_complete" and p.get("last_agent_message"):
                msg = p["last_agent_message"]
            elif ty == "item_completed" and isinstance(p.get("item"), dict) \
                    and p["item"].get("type") == "AgentMessage":
                msg = p["item"].get("text") or p["item"].get("message")
            elif d.get("type") == "response_item" and ty == "message" and p.get("role") == "assistant":
                msg = "".join(x.get("text", "") for x in p.get("content", []) if isinstance(x, dict)) or None
            if msg:
                last = msg
    return last


def proc_cwd(pid):
    if IS_LINUX:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    names = _lsof_names(pid, ["-d", "cwd"])
    return names[0] if names else None


# ─────────────────────────────── tmux 백엔드 ───────────────────────────────

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


# ─────────────────────────────── orca 백엔드 ───────────────────────────────

def orca_enabled():
    return os.environ.get("REVIEW_NO_ORCA", "") in ("", "0", "false", "False")


def orca_json(args, timeout=ORCA_TIMEOUT):
    """orca CLI 호출 → 파싱된 JSON dict, 실패/비활성 시 None.

    stdout 맨 앞의 '[relay-connect] Handshake OK at version=...' preamble 때문에 첫 '{' 부터 잘라 파싱한다.
    """
    if not orca_enabled():
        return None
    try:
        r = subprocess.run([ORCA_BIN] + args + ["--json"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    i = r.stdout.find("{")
    if i < 0:
        return None
    try:
        d = json.loads(r.stdout[i:])
    except ValueError:
        return None
    return d if d.get("ok") else None


def orca_worktree_selector(cwd):
    """워크트리 selector — $ORCA_WORKTREE_ID 우선, 없으면 git toplevel, 그것도 실패면 cwd.

    ('active' 는 UI 포커스 기준이라 쓰지 않는다.)
    """
    wt = os.environ.get("ORCA_WORKTREE_ID")
    if wt:
        return "id:" + wt
    r = sh(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    top = r.stdout.strip() if r.returncode == 0 else ""
    return "path:" + (top or os.path.abspath(cwd))


def _walk_tabs(node, out):
    """visualLayouts 트리에서 탭 노드(tabId + panes)를 모은다 (그룹 중첩 무관)."""
    if isinstance(node, list):
        for x in node:
            _walk_tabs(x, out)
    elif isinstance(node, dict):
        if "tabId" in node and "panes" in node:
            out.append(node)
        for v in node.values():
            _walk_tabs(v, out)


def _pane_handles(node, out):
    if isinstance(node, list):
        for x in node:
            _pane_handles(x, out)
    elif isinstance(node, dict):
        if node.get("type") == "terminal" and node.get("handle"):
            out.append(node["handle"])
        for v in node.values():
            if isinstance(v, (list, dict)):
                _pane_handles(v, out)


def orca_list(selector):
    """(terminals[], {handle: 탭 제목}) 한 번의 호출로. None = orca 사용 불가.

    **terminals[].title 은 탭 이름이 아니라 상태 라벨이다** ('Codex ready' / '◐ 이름' 처럼 orca가
    덮어쓴다). 사용자가 rename 으로 붙인 탭 이름은 visualLayouts 의 탭 노드 title 에 있다.
    """
    d = orca_json(["terminal", "list", "--worktree", selector, "--include-visual-layouts"])
    if not d:
        return None
    res = d.get("result", {})
    tabs, titles = [], {}
    _walk_tabs(res.get("visualLayouts"), tabs)
    for tab in tabs:
        handles = []
        _pane_handles(tab.get("panes"), handles)
        for h in handles:
            titles[h] = tab.get("title")
    return res.get("terminals") or [], titles


def orca_codex_tabs(selector, title):
    """현재 워크트리에서 제목 정확 일치 + agentIdentity=='codex' + connected 인 탭들. None=orca 사용 불가.

    제목은 탭 이름(visualLayouts) 우선, 상태 라벨(terminals[].title)도 함께 본다.
    """
    listed = orca_list(selector)
    if listed is None:
        return None
    terms, tab_titles = listed
    out = []
    for t in terms:
        if t.get("agentIdentity") != "codex" or not t.get("connected"):
            continue
        tab_title = tab_titles.get(t.get("handle"))
        if _title_match(tab_title, title) or _title_match(t.get("title"), title):
            out.append(dict(t, tabTitle=tab_title))
    return out


def _title_match(shown, wanted):
    """탭 제목 정확 일치. 단 orca(Mac 빌드)가 붙이는 ' | <워크트리명>' 꼬리는 무시한다
    ('codex-review | BitBot' 도 'codex-review' 로 본다. 'codex-review-2' 같은 접두 매칭은 여전히 불허)."""
    if not shown:
        return False
    return shown == wanted or shown.startswith(wanted + " | ")


def orca_terminal_info(handle):
    d = orca_json(["terminal", "show", "--terminal", handle])
    if not d:
        return None
    return d.get("result", {}).get("terminal")


def ppid_of(pid):
    if not IS_LINUX:
        r = sh(["ps", "-o", "ppid=", "-p", str(pid)])
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return None
    try:  # comm 에 공백·괄호가 있을 수 있으므로 마지막 ')' 뒤부터 파싱: state, ppid, ...
        return int(data[data.rindex(")") + 1:].split()[1])
    except (ValueError, IndexError):
        return None


def find_codex_orca(handle):
    """ORCA_TERMINAL_HANDLE=<handle> 을 상속한 comm=='codex' 프로세스의 PID(TUI 루트) 또는 None."""
    key = ("ORCA_TERMINAL_HANDLE=" + handle).encode()
    cands = {}
    for pid in _all_pids():
        if _proc_comm(pid) != "codex":   # 자식 codex-code-mode-host 배제
            continue
        env = _proc_environ(pid)   # PermissionError / ProcessLookupError → None
        if env is None or key not in env:   # 정확 일치 (접두 매칭 방지)
            continue
        cands[pid] = ppid_of(pid)
    if not cands:
        return None
    roots = [p for p, pp in cands.items() if pp not in cands]
    return min(roots) if roots else min(cands)


def orca_send_text(handle, text=None, enter=False, interrupt=False):
    """orca terminal send 1회. **agent 탭에서는 text 와 enter 를 한 호출에 같이 주면 안 된다**
    (실측: --text ... --enter 는 ok=false / agent_prompt_blocked 로 거부된다).
    """
    args = ["terminal", "send", "--terminal", handle]
    if text is not None:
        args += ["--text", text]
    if enter:
        args.append("--enter")
    if interrupt:
        args.append("--interrupt")
    d = orca_json(args)
    if not d or not d.get("result", {}).get("send", {}).get("accepted"):
        return None
    return d["result"]["send"]


def orca_submit(handle, text):
    """2단 전송: ① --text 로 컴포저를 채우고 ② --enter 로 제출 (tmux 의 paste-buffer + Enter 와 같은 모양).

    실측: ①의 개행은 조기 제출을 일으키지 않고 여러 줄이 그대로 컴포저에 남으며, ②에서 한 번에
    한 메시지로 제출된다. 한 호출에 --text 와 --enter 를 같이 주면 agent_prompt_blocked 로 거부된다.
    """
    if not orca_send_text(handle, text=text):
        return "orca terminal send(--text) 실패 (handle=%s)" % handle
    time.sleep(1)
    if not orca_send_text(handle, enter=True):
        return "orca terminal send(--enter) 실패 — 컴포저에 프롬프트가 남아 있을 수 있다 (handle=%s)" % handle
    return None


def orca_screen_text(handle):
    """렌더된 화면 (--screen). --screen 없이 읽으면 이스케이프가 제거된 누적 스트림이라 TUI에 부적합."""
    d = orca_json(["terminal", "read", "--terminal", handle, "--screen"])
    if not d:
        return None
    term = d.get("result", {}).get("terminal", {})
    return "\n".join(term.get("tail") or [])


# ─────────────────────────────── target 토큰 ───────────────────────────────

def parse_target(a):
    """(backend, ref) — 'orca:<handle>' / 'tmux:<session>'. --session 만 주면 tmux 로 해석(하위호환)."""
    tok = getattr(a, "target", None)
    sess = getattr(a, "session", None)
    if tok and sess and tok != "tmux:" + sess:
        die("--target 과 --session 이 불일치한다 (%s vs tmux:%s)" % (tok, sess), 2)
    if not tok:
        if not sess:
            die("--target 또는 --session 이 필요하다 (resolve 출력의 target 을 그대로 써라)", 2)
        tok = "tmux:" + sess
    backend, _, ref = tok.partition(":")
    if backend not in ("orca", "tmux") or not ref:
        die("target 형식 오류: %r (orca:<handle> 또는 tmux:<session>)" % tok, 2)
    return backend, ref


def resolve_pid(backend, ref):
    """백엔드별 현재 codex PID. tmux 는 (pane, pid), orca 는 (None, pid). 없으면 (None, None)."""
    if backend == "tmux":
        found = find_codex(ref)
        return (found[0], found[1]) if found else (None, None)
    pid = find_codex_orca(ref)
    return (None, pid) if pid else (None, None)


# ─────────────────────────────── resolve ───────────────────────────────

def orca_result(handle, title, pid):
    rollout = main_rollout(pid)
    return {"backend": "orca", "target": "orca:" + handle, "handle": handle, "title": title,
            "codex_pid": pid, "cwd": proc_cwd(pid), "rollout": rollout, "idle": is_idle(rollout)}


def check_orca_cwd(pid, worktree_path):
    """codex 프로세스 cwd 가 그 탭의 워크트리 경로 아래인지 (엉뚱한 리포를 리뷰하는 사고 차단)."""
    if not worktree_path:
        return
    cwd = proc_cwd(pid)
    if not cwd:
        return
    wp = os.path.realpath(worktree_path)
    rc = os.path.realpath(cwd)
    if rc != wp and not rc.startswith(wp.rstrip("/") + "/"):
        die("cwd_mismatch", 6, codex_pid=pid, codex_cwd=cwd, worktree_path=worktree_path,
            hint="이 orca 탭의 codex 가 워크트리 밖에서 돌고 있다 — 탭을 확인하라")


def orca_resolve_by_title(title, cwd, strict):
    """orca 탭 제목으로 확보. strict=False(사다리)면 못 찾았을 때 (None, 사유) 반환, True면 exit 6."""
    sel = orca_worktree_selector(cwd)
    tabs = orca_codex_tabs(sel, title)
    if tabs is None:
        if strict:
            die("orca_unavailable", 6, tab=title,
                hint="orca CLI 를 쓸 수 없다 (미설치·실패·타임아웃 또는 REVIEW_NO_ORCA=1)")
        return None, "orca:unavailable"
    if len(tabs) > 1:
        print(json.dumps({"error": "ambiguous_tab", "tab": title, "worktree": sel,
                          "candidates": [{"handle": t.get("handle"),
                                          "title": t.get("tabTitle") or t.get("title"),
                                          "pane_label": t.get("title"),
                                          "worktreePath": t.get("worktreePath")} for t in tabs],
                          "hint": "--terminal <handle> 로 하나를 지정하라"}, ensure_ascii=False))
        sys.exit(7)
    if not tabs:
        if strict:
            die("no_orca_tab", 6, tab=title, worktree=sel,
                hint="이 워크트리에 title=='%s' 인 codex 탭이 없다" % title)
        return None, "orca:no_tab(%s)" % title
    t = tabs[0]
    handle = t.get("handle")
    pid = find_codex_orca(handle)
    if pid is None:
        if strict:
            die("no_codex_process", 6, handle=handle, tab=title,
                hint="이 탭의 codex 프로세스를 이 호스트에서 찾지 못했다 (원격 호스트 탭이거나 종료됨)")
        return None, "orca:no_pid(%s)" % handle
    check_orca_cwd(pid, t.get("worktreePath"))
    return orca_result(handle, t.get("tabTitle") or t.get("title"), pid), None


def orca_resolve_by_handle(handle, cwd):
    pid = find_codex_orca(handle)
    if pid is None:
        die("no_codex_process", 6, handle=handle,
            hint="이 핸들의 codex 프로세스를 이 호스트에서 찾지 못했다 (원격 호스트 탭이거나 종료됨)")
    info, title = {}, None
    listed = orca_list(orca_worktree_selector(cwd))
    if listed:
        terms, tab_titles = listed
        info = next((t for t in terms if t.get("handle") == handle), {})
        title = tab_titles.get(handle)
    if not info:   # 다른 워크트리의 탭
        info = orca_terminal_info(handle) or {}
    check_orca_cwd(pid, info.get("worktreePath"))
    return orca_result(handle, title or info.get("title"), pid)


def tmux_resolve(session, cwd, strict):
    hint = "사용자 확인 후 `create --session %s --cwd %s`" % (session, cwd)
    if not session_exists(session):
        if strict:
            die("no_session", 6, session=session, cwd=cwd, hint=hint)
        return None, "tmux:no_session(%s)" % session
    found = find_codex(session)
    if not found:
        if strict:
            die("no_codex_process", 6, session=session, cwd=cwd, hint=hint)
        return None, "tmux:no_codex_process(%s)" % session
    pane, pid = found
    rollout = main_rollout(pid)
    return {"backend": "tmux", "target": "tmux:" + session, "session": session, "pane": pane,
            "pane_session": pane_session_name(pane), "codex_pid": pid,
            "cwd": proc_cwd(pid) or cwd, "rollout": rollout, "idle": is_idle(rollout)}, None


def cmd_resolve(a):
    """기존 codex를 찾기만 한다. 생성은 하지 않는다 (create 서브커맨드 / orca terminal create 가 담당)."""
    given = [x for x in (a.terminal, a.tab, a.session) if x]
    if len(given) > 1:
        die("--terminal / --tab / --session 중 하나만 지정하라", 2)
    if a.terminal:   # 명시 handle — 사다리 안 탄다
        print(json.dumps(orca_resolve_by_handle(a.terminal, a.cwd), ensure_ascii=False))
        return
    if a.tab:        # 명시 탭 제목 — tmux 로 내려가지 않는다
        out, _ = orca_resolve_by_title(a.tab, a.cwd, strict=True)
        print(json.dumps(out, ensure_ascii=False))
        return
    if a.session:    # 명시 tmux 세션 — 기존 동작 그대로
        out, _ = tmux_resolve(a.session, a.cwd, strict=True)
        print(json.dumps(out, ensure_ascii=False))
        return
    tried = []
    out, why = orca_resolve_by_title(ORCA_TAB_NAME, a.cwd, strict=False)   # ① orca
    if out:
        print(json.dumps(out, ensure_ascii=False))
        return
    tried.append(why)
    out, why = tmux_resolve(TMUX_SESSION, a.cwd, strict=False)             # ② tmux
    if out:
        print(json.dumps(out, ensure_ascii=False))
        return
    tried.append(why)
    print(json.dumps({"error": "no_codex_target", "tried": tried, "cwd": a.cwd,   # ③ 종료
                      "hint": "orca 탭 '%s' 도, tmux 세션 '%s' 도 없다 — 사용자에게 보고하고 지시를 받아라 (자동 생성 금지)"
                              % (ORCA_TAB_NAME, TMUX_SESSION)}, ensure_ascii=False))
    sys.exit(6)


def cmd_create(a):
    """사용자 확인을 받은 뒤에만 호출: CODEX_CMD 로 tmux 에 codex TUI를 새로 띄운다."""
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
    print(json.dumps({"backend": "tmux", "target": "tmux:" + a.session, "session": a.session,
                      "pane": pane, "codex_pid": pid, "rollout": main_rollout(pid),
                      "created": True, "cwd": a.cwd,
                      "model": "gpt-6-astra", "effort": "high"}, ensure_ascii=False))


# ─────────────────────────────── send / wait / last / capture ───────────────────────────────

def cmd_send(a):
    backend, ref = parse_target(a)
    pane, pid = resolve_pid(backend, ref)
    if not pid:
        die("%s 대상 %s 에 codex 프로세스가 없음" % (backend, ref))
    rollout = main_rollout(pid)
    if not a.force and not is_idle(rollout):
        die("codex가 작업 중 (task_started 후 task_complete 없음). 기다렸다가 재시도", 3)
    marker = {"target": "%s:%s" % (backend, ref), "rollout": rollout,
              "completes": count_completes(rollout), "sent_at": int(time.time())}
    if backend == "tmux":
        # bracketed paste — 개행이 조기 제출되지 않게 버퍼로 붙여 넣고 Enter 를 따로 보낸다.
        sh(["tmux", "load-buffer", "-b", "rlbuf", a.text_file])
        sh(["tmux", "paste-buffer", "-p", "-d", "-b", "rlbuf", "-t", pane])
        time.sleep(1)
        sh(["tmux", "send-keys", "-t", pane, "Enter"])
    else:
        path = os.path.abspath(a.text_file)
        try:
            with open(path) as fh:
                payload = fh.read().rstrip("\n")
        except OSError as e:
            die("프롬프트 파일을 읽지 못함: %s" % e)
        mode = a.paste_mode
        if mode == "auto":
            mode = "inline" if (ORCA_MULTILINE_SAFE or "\n" not in payload) else "file"
        if mode == "file":
            # 파일 모드: codex 가 그 파일을 읽을 때까지 파일을 지우면 안 된다.
            payload = "다음 파일에 담긴 지시를 그대로 수행하라(파일 내용이 곧 요청이다): " + path
        err = orca_submit(ref, payload)
        if err:
            die(err)
        marker["paste_mode"] = mode
        if mode == "file":
            marker["prompt_file"] = path
    print(json.dumps(marker, ensure_ascii=False))


def cmd_wait(a):
    marker = json.loads(a.marker)
    if not getattr(a, "target", None) and not getattr(a, "session", None):
        a.target = marker.get("target")
    backend, ref = parse_target(a)
    deadline = time.time() + a.max_seconds
    rollout = marker.get("rollout")
    while time.time() < deadline:
        _, pid = resolve_pid(backend, ref)
        if not pid:
            die("codex 프로세스가 사라짐")
        if not rollout:  # 신규 세션: 첫 메시지 후 생성된 파일을 fd로 잡는다
            rollout = main_rollout(pid)
        if rollout and count_completes(rollout) > marker.get("completes", 0):
            print(json.dumps({"done": True, "rollout": rollout}))
            return
        time.sleep(a.interval)
    print(json.dumps({"done": False, "rollout": rollout}))
    sys.exit(3)


def cmd_last(a):
    rollout = a.rollout
    if not rollout:
        backend, ref = parse_target(a)
        _, pid = resolve_pid(backend, ref)
        if not pid:
            die("%s 대상 %s 에 codex 프로세스가 없음" % (backend, ref))
        rollout = main_rollout(pid)
    if not rollout:
        die("rollout 파일을 찾지 못함")
    msg = last_agent_message(rollout)
    if msg is None:
        die("rollout에 agent_message가 없음")
    print(msg)


def cmd_capture(a):
    """화면 캡처 — trust 프롬프트·진행 상태 확인 전용. 완료 판정은 반드시 rollout(last)으로 하라."""
    backend, ref = parse_target(a)
    if backend == "tmux":
        pane, pid = resolve_pid(backend, ref)
        if not pid:
            die("tmux 세션 %s 에 codex 프로세스가 없음" % ref)
        sys.stdout.write(pane_text(pane))
        return
    text = orca_screen_text(ref)
    if text is None:
        die("orca terminal read 실패 (handle=%s)" % ref)
    sys.stdout.write(text + "\n")


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
    p.add_argument("--tab", help="orca 탭 제목 (기본 사다리는 '%s')" % ORCA_TAB_NAME)
    p.add_argument("--terminal", help="orca 터미널 핸들 (term_UUID)")
    p.add_argument("--session", help="tmux 세션 이름 (기본 사다리는 '%s')" % TMUX_SESSION)
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("create")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("send")
    p.add_argument("--target", help="resolve 가 출력한 target 토큰 (orca:<handle> | tmux:<session>)")
    p.add_argument("--session", help="하위호환: tmux 세션 이름 (= --target tmux:<name>)")
    p.add_argument("--text-file", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--paste-mode", choices=("auto", "inline", "file"), default="auto",
                   help="orca 백엔드 전용. auto = ORCA_MULTILINE_SAFE 에 따름 (tmux 는 무시)")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("wait")
    p.add_argument("--target")
    p.add_argument("--session")
    p.add_argument("--marker", required=True)
    p.add_argument("--max-seconds", type=int, default=540)
    p.add_argument("--interval", type=int, default=10)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("last")
    p.add_argument("--target")
    p.add_argument("--session")
    p.add_argument("--rollout")
    p.set_defaults(fn=cmd_last)

    p = sub.add_parser("capture")
    p.add_argument("--target")
    p.add_argument("--session")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("snapshot")
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_snapshot)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
