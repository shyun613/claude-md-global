#!/usr/bin/env python3
"""Codex 비대화형 실행 브리지 (review-loop / review-codex 스킬 공용) — `codex exec` 기반.

떠 있는 TUI 에 프롬프트를 주입하던 옛 브리지를 대체한다. 매 턴을 `codex exec`(신규 스레드) 또는
`codex exec resume <UUID>`(이전 턴 기억 유지)로 실행하고, 이벤트 JSONL 을 파싱해 결과를 돌려준다.
프로세스는 새 세션(start_new_session)으로 분리 실행하므로 호출자는 즉시 반환받고 wait 로 폴링한다.

서브커맨드:
  snapshot --cwd DIR
      워킹 트리 전체(untracked 포함, .gitignore 준수)의 트리 객체 생성 — 델타 리뷰용.
      임시 GIT_INDEX_FILE 에서만 작업하므로 실제 인덱스·워킹 트리·refs 무변경. 출력 {"tree","cwd"}.
  run --cwd REPO --prompt-file F --run-dir D [--thread UUID] [--effort high]
      [--model gpt-6-astra] [--sandbox read-only|workspace-write|danger-full-access] [--schema FILE]
      codex exec 를 분리 실행하고 즉시 {"run_dir","pid","thread_requested"} 반환.
      D 안에 events.jsonl(이벤트 JSONL) · last.md(마지막 메시지) · stderr.log · meta.json · rc 가 쌓인다.
      --thread 를 주면 resume 경로(샌드박스는 -c sandbox_mode, cwd 는 프로세스 cwd 로 전달).
  wait --run-dir D [--timeout 590] [--interval 5]
      전경 폴링. 완료 판정 = rc 파일 존재(PID 재사용 회피). 완료 시 events.jsonl 을 파싱해
      D/result.json = {thread_id, rc, last_message_path, last_message, usage, duration_s, errors[]}
      을 쓰고 같은 JSON 을 stdout 에 출력(last_message 는 앞 300 자만).
  kill --run-dir D
      meta 의 pgid 에 SIGTERM → 5 초 → SIGKILL. rc 가 없으면 rc=137 로 기록.

exit: 0=정상, 2=인자 오류, 3=아직 실행 중(wait 타임아웃), 4=오류, 5=스키마 위반.

주의: codex stderr 에 `failed to install system skills: Permission denied` 가 반복 출력되나
무해한 잡음이다(~/.codex/skills/.system 소유권). stderr.log 에만 남기고 판정에 쓰지 않는다.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

CODEX_BIN = "codex"
AUTH_JSON = os.path.expanduser("~/.codex/auth.json")
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_EFFORT = "high"
DEFAULT_SANDBOX = "read-only"
STDOUT_MSG_CHARS = 300      # stdout 에 실어 보내는 last_message 길이 상한
STDERR_TAIL_LINES = 20


def sh(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def die(msg, code=4, **extra):
    payload = {"error": msg}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def parse_utc(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


# ─────────────────────────────── snapshot ───────────────────────────────

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
    tmp_index = tempfile.mktemp(prefix="codex-exec-index-")
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


# ─────────────────────────────── run ───────────────────────────────

def build_cmd(a, run_dir):
    """codex exec 명령 리스트를 만든다. --thread 가 있으면 resume 경로.

    resume 서브커맨드에는 -C/-s 가 없다 → cwd 는 Popen(cwd=), 샌드박스는 -c sandbox_mode 로 준다.
    """
    last_md = os.path.join(run_dir, "last.md")
    if a.thread:
        cmd = [CODEX_BIN, "exec", "resume", a.thread,
               "-m", a.model,
               "-c", "model_reasoning_effort=%s" % a.effort,
               "-c", 'sandbox_mode="%s"' % a.sandbox,
               "--skip-git-repo-check", "--json", "-o", last_md]
    else:
        cmd = [CODEX_BIN, "exec",
               "-C", a.cwd,
               "-s", a.sandbox,
               "-m", a.model,
               "-c", "model_reasoning_effort=%s" % a.effort,
               "--skip-git-repo-check", "--json", "-o", last_md]
    if a.schema:
        cmd += ["--output-schema", a.schema]
    cmd.append("-")          # 프롬프트는 stdin 에서 읽는다
    return cmd


def cmd_run(a):
    if not shutil.which(CODEX_BIN):
        die("codex 실행 파일 없음 (PATH)")
    if not os.path.isfile(AUTH_JSON):
        die("codex 인증 파일 없음: " + AUTH_JSON)
    if not os.path.isfile(a.prompt_file):
        die("프롬프트 파일 없음: " + a.prompt_file)
    if not os.path.isdir(a.cwd):
        die("cwd 디렉터리가 아님: " + a.cwd)
    if a.schema and not os.path.isfile(a.schema):
        die("스키마 파일 없음: " + a.schema)

    run_dir = os.path.abspath(a.run_dir)
    try:
        os.makedirs(run_dir, exist_ok=True)
    except OSError as e:
        die("run-dir 생성 실패: %s (%s)" % (run_dir, e))
    rc_path = os.path.join(run_dir, "rc")
    if os.path.exists(rc_path):
        die("run-dir 사용됨 (rc 존재): " + run_dir)

    cmd = build_cmd(a, run_dir)
    # 셸 래퍼가 종료 코드를 rc 파일로 남긴다 → wait 는 PID 가 아니라 rc 존재로 완료를 판정한다.
    shell_line = "%s; echo $? > %s" % (shlex.join(cmd), shlex.quote(rc_path))

    events = os.path.join(run_dir, "events.jsonl")
    errlog = os.path.join(run_dir, "stderr.log")
    with open(a.prompt_file, "rb") as fin, \
            open(events, "wb") as fout, \
            open(errlog, "wb") as ferr:
        p = subprocess.Popen(["sh", "-c", shell_line],
                             stdin=fin, stdout=fout, stderr=ferr,
                             cwd=a.cwd, start_new_session=True)
    try:
        pgid = os.getpgid(p.pid)
    except OSError:
        pgid = p.pid

    meta = {
        "pid": p.pid,
        "pgid": pgid,
        "started_utc": utcnow(),
        "cmd": cmd,
        "cwd": os.path.abspath(a.cwd),
        "thread_requested": a.thread,
        "effort": a.effort,
        "model": a.model,
        "sandbox": a.sandbox,
        "schema": os.path.abspath(a.schema) if a.schema else None,
        "prompt_file": os.path.abspath(a.prompt_file),
    }
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps({"run_dir": run_dir, "pid": p.pid,
                      "thread_requested": a.thread}, ensure_ascii=False))


# ─────────────────────────────── wait ───────────────────────────────

def read_meta(run_dir):
    path = os.path.join(run_dir, "meta.json")
    if not os.path.isfile(path):
        die("meta.json 없음: " + path)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        die("meta.json 파싱 실패: %s (%s)" % (path, e))


def parse_events(path):
    """events.jsonl → (thread_id, last_agent_message, usage, errors[]).

    JSONL 이 아닌 줄(잡음)은 조용히 건너뛴다.
    """
    thread_id, last_msg, usage, errors = None, None, None, []
    if not os.path.isfile(path):
        return thread_id, last_msg, usage, errors
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type", "")
            if t == "thread.started":
                thread_id = ev.get("thread_id") or (ev.get("thread") or {}).get("id") or thread_id
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("item_type") == "agent_message" or item.get("type") == "agent_message":
                    txt = item.get("text")
                    if txt is not None:
                        last_msg = txt
            elif t == "turn.completed":
                usage = ev.get("usage") or usage
            if t == "error" or t.endswith(".error") or t == "turn.failed" or "error" in ev:
                errors.append(ev)
    return thread_id, last_msg, usage, errors


def tail_lines(path, n):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def check_schema(last_md, schema_path):
    """last.md 가 유효 JSON 이고 최상위 키가 스키마 required 를 모두 포함하는지 검사.

    문제가 있으면 사유 문자열, 없으면 None 을 돌려준다.
    """
    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except (OSError, ValueError) as e:
        return "스키마 파일 읽기 실패: %s (%s)" % (schema_path, e)
    required = schema.get("required") or []
    try:
        with open(last_md, errors="replace") as f:
            raw = f.read().strip()
    except OSError as e:
        return "last.md 읽기 실패: %s" % e
    if not raw:
        return "last.md 가 비어 있음"
    try:
        obj = json.loads(raw)
    except ValueError as e:
        return "last.md JSON 파싱 실패: %s" % e
    if not isinstance(obj, dict):
        return "last.md 최상위가 객체가 아님: %s" % type(obj).__name__
    missing = [k for k in required if k not in obj]
    if missing:
        return "필수 키 누락: %s" % ", ".join(missing)
    return None


def cmd_wait(a):
    run_dir = os.path.abspath(a.run_dir)
    meta = read_meta(run_dir)
    rc_path = os.path.join(run_dir, "rc")
    started = parse_utc(meta.get("started_utc")) or time.time()

    deadline = time.time() + a.timeout
    while not os.path.exists(rc_path):
        if time.time() >= deadline:
            print(json.dumps({"status": "running",
                              "elapsed_s": round(time.time() - started, 1)},
                             ensure_ascii=False))
            sys.exit(3)
        time.sleep(min(a.interval, max(0.1, deadline - time.time())))

    try:
        with open(rc_path) as f:
            rc = int(f.read().strip() or "-1")
    except (OSError, ValueError):
        rc = -1
    try:
        duration = round(os.path.getmtime(rc_path) - started, 1)
    except OSError:
        duration = round(time.time() - started, 1)

    last_md = os.path.join(run_dir, "last.md")
    thread_id, last_msg, usage, errors = parse_events(os.path.join(run_dir, "events.jsonl"))
    missing_msg = last_msg is None
    if missing_msg and os.path.isfile(last_md):
        # 이벤트에 agent_message 가 없을 때도 진단용으로 -o 파일 내용을 실어 준다 (판정은 그대로 실패).
        try:
            with open(last_md, errors="replace") as f:
                last_msg = f.read()
        except OSError:
            pass

    result = {
        "thread_id": thread_id,
        "rc": rc,
        "last_message_path": last_md,
        "last_message": last_msg,
        "usage": usage,
        "duration_s": duration,
        "errors": errors,
    }

    failed = rc != 0 or missing_msg
    if failed:
        result["stderr_tail"] = tail_lines(os.path.join(run_dir, "stderr.log"), STDERR_TAIL_LINES)
        if missing_msg:
            result.setdefault("errors", []).append({"type": "no_agent_message"})

    schema_error = None
    if not failed and meta.get("schema"):
        schema_error = check_schema(last_md, meta["schema"])
        if schema_error:
            result["schema_error"] = schema_error

    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    out = dict(result)
    if isinstance(out.get("last_message"), str) and len(out["last_message"]) > STDOUT_MSG_CHARS:
        out["last_message"] = out["last_message"][:STDOUT_MSG_CHARS]
    print(json.dumps(out, ensure_ascii=False))
    if failed:
        sys.exit(4)
    if schema_error:
        sys.exit(5)


# ─────────────────────────────── kill ───────────────────────────────

def pg_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def cmd_kill(a):
    run_dir = os.path.abspath(a.run_dir)
    meta = read_meta(run_dir)
    pgid = meta.get("pgid")
    rc_path = os.path.join(run_dir, "rc")
    signals = []
    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
            signals.append("SIGTERM")
        except OSError:
            pass
        deadline = time.time() + 5
        while pg_alive(pgid) and time.time() < deadline:
            time.sleep(0.2)
        if pg_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
                signals.append("SIGKILL")
            except OSError:
                pass
            time.sleep(0.3)
    if not os.path.exists(rc_path):
        # 셸 래퍼까지 죽으면 `echo $? > rc` 가 돌지 않는다 → 직접 기록해 wait 가 완료를 보게 한다.
        with open(rc_path, "w") as f:
            f.write("137\n")
    print(json.dumps({"run_dir": run_dir, "pgid": pgid, "signals": signals,
                      "alive": pg_alive(pgid) if pgid else False}, ensure_ascii=False))


# ─────────────────────────────── main ───────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="워킹 트리 스냅샷 트리 객체 생성 (델타 리뷰용)")
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("run", help="codex exec 분리 실행 (즉시 반환)")
    p.add_argument("--cwd", required=True, help="리포 절대경로 (에이전트 작업 루트)")
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--thread", help="이어 붙일 thread_id(UUID) — 주면 resume 경로")
    p.add_argument("--effort", default=DEFAULT_EFFORT,
                   choices=("low", "medium", "high", "xhigh"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--sandbox", default=DEFAULT_SANDBOX,
                   choices=("read-only", "workspace-write", "danger-full-access"))
    p.add_argument("--schema", help="--output-schema 로 넘길 JSON Schema 파일")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("wait", help="완료 폴링 후 result.json 작성")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--timeout", type=float, default=590)
    p.add_argument("--interval", type=float, default=5)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("kill", help="프로세스 그룹 종료 (SIGTERM → 5s → SIGKILL)")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(fn=cmd_kill)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
