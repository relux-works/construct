#!/usr/bin/env python3
"""Persistent worker lanes. Configuration is data; all subprocess arguments are arrays."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import tempfile
import uuid

KIT = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"[a-z][a-z0-9_-]*\Z")
TASK = re.compile(r"(?:TASK|BUG)-[0-9]{6}-[a-z0-9]{6}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
THREAD = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


class Refusal(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    write_private(path, json.dumps(value, indent=2) + "\n")


def clean_env():
    # Explicit core environment, rather than a blacklist of secret spellings.
    return {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TZ") if k in os.environ}


def run(argv, *, env=None, cwd=None, capture=True):
    result = subprocess.run([str(a) for a in argv], env=env, cwd=cwd, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        # Do not echo arguments or tool output: either may contain private input.
        raise Refusal(f"{Path(str(argv[0])).name} failed (exit {result.returncode})")
    return result.stdout or ""


def child_wait(argv, *, env=None, cwd=None, stdin=None, stdout=None, stderr=None, pass_fds=()):
    """A terminated controller stops and reaps only its own process group."""
    child = subprocess.Popen(argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout,
                             stderr=stderr, start_new_session=True, pass_fds=pass_fds)
    try:
        return child.wait()
    except BaseException:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        raise


@contextlib.contextmanager
def lock(path, poll=30):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        announced = False
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    print("waiting for the worker's earlier run", file=sys.stderr)
                    announced = True
                time.sleep(poll)
        try:
            yield stream.fileno()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Config:
    def __init__(self, filename):
        self.file = Path(filename).resolve()
        self.data = json.loads(self.file.read_text(encoding="utf-8"))
        d = self.data
        require(isinstance(d, dict), "config must be an object")
        for key in ("lane", "identity_prefix"):
            require(isinstance(d.get(key), str) and SLUG.fullmatch(d[key]), f"invalid {key}")
        require(isinstance(d.get("room"), str) and d["room"].startswith("#") and
                not re.search(r"\s", d["room"]), "invalid room")
        for key in ("root", "board_config", "git_env"):
            require(isinstance(d.get(key), str) and bool(d[key]), f"missing {key}")
        require(type(d.get("slots")) is int and 1 <= d["slots"] <= 64, "slots must be 1..64")
        require(isinstance(d.get("repos"), dict) and bool(d["repos"]), "repos must be nonempty")
        for name, repo in d["repos"].items():
            require(SLUG.fullmatch(name) and isinstance(repo, dict), "invalid repo")
            require(isinstance(repo.get("url"), str) and repo["url"] and
                    not repo["url"].startswith("-"), "invalid repo URL")
            require(isinstance(repo.get("base"), str) and valid_branch(repo["base"]), "invalid repo base")
        require(isinstance(d.get("roles"), dict), "missing roles")
        for role in ("dev", "rev"):
            r = d["roles"].get(role, {})
            require(isinstance(r, dict) and isinstance(r.get("model"), str) and bool(r["model"]), "missing model")
            require(r.get("effort") in ("low", "medium", "high", "xhigh", "max"), "invalid effort")
        require(d["roles"]["rev"]["effort"] in ("medium", "xhigh", "max"), "reviewer effort must be medium, xhigh or max")
        for key in ("slot_poll_seconds", "idle_poll_seconds"):
            value = d.get(key, 5 if key.startswith("slot") else 30)
            require(type(value) in (int, float) and value > 0, f"invalid {key}")
        require(d.get("sandbox", "workspace-write") in ("workspace-write", "trusted"), "invalid sandbox")
        for key in ("allow_no_checks", "memories"):
            require(type(d.get(key, False)) is bool, f"invalid {key}")
        require(isinstance(d.get("tools", {}), dict) and all(isinstance(v, str) and v for v in d.get("tools", {}).values()), "invalid tools")
        self.root = self.path(d["root"])
        self.lane = self.root / d["lane"]
        self.state = self.lane / ".construct"
        self.env = clean_env()
        self.env["TASK_BOARD_CONFIG"] = str(self.path(d["board_config"]))
        self.env["A2A_AGENT"] = self.identity("orch")
        if d.get("signing_socket"):
            self.env["CONSTRUCT_SIGNING_SOCKET"] = str(self.path(d["signing_socket"]))

    def path(self, value):
        p = Path(value).expanduser()
        return (self.file.parent / p).resolve() if not p.is_absolute() else p.resolve()

    def tool(self, name):
        return self.data.get("tools", {}).get(name, "task-board" if name == "board" else name)

    def identity(self, worker):
        return f'{self.data["identity_prefix"]}_{self.data["lane"]}_{worker}'

    def call(self, tool, *args):
        return run([self.tool(tool), *args], env=self.env)

    def git(self, gitdir, *args, tree=None):
        argv = [self.tool("git"), f"--git-dir={gitdir}"]
        if self.data.get("signing_socket"):
            argv += ["-c", f"gpg.ssh.program={KIT / 'bin/ssh-keygen-agent'}"]
        if tree:
            argv.append(f"--work-tree={tree}")
        # Source the configured signing/transport policy without printing its environment.
        return run([self.tool("zsh"), "-c", 'source "$1" || exit; shift; exec "$@"',
                    "construct-git", self.path(self.data["git_env"]), *argv, *args], env=self.env)

    def plain_git(self, *args):
        return run([self.tool("zsh"), "-c", 'source "$1" || exit; shift; exec "$@"',
                    "construct-git", self.path(self.data["git_env"]), self.tool("git"), *args], env=self.env)


def valid_branch(value):
    return (bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value)) and
            ".." not in value and "//" not in value and not value.endswith(("/", ".", ".lock")) and
            all(not p.startswith(".") for p in value.split("/")))


def worker_name(role, number):
    require(role in ("dev", "rev") and number > 0, "worker must be dev|rev and a positive number")
    return f"{role}{number}"


def worker(c, name):
    require(re.fullmatch(r"(?:dev|rev)[1-9][0-9]*", name), "invalid worker")
    path = c.state / "workers" / f"{name}.json"
    require(path.is_file(), "worker is not registered; use add-worker")
    return json.loads(path.read_text(encoding="utf-8"))


def render(c, name, template, **extra):
    values = {"ID": c.identity(name), "ORCH": c.identity("orch"), "ROOM": c.data["room"],
              "WORK": str(c.lane / name), "ROLE": "developer" if name.startswith("dev") else "reviewer",
              "GIT_ENV": shlex.quote(str(c.path(c.data["git_env"]))),
              "SLOT_COMMAND": shlex.join([str(KIT / "bin/construct"), "--config", str(c.file), "slot", "--"]),
              "REPOS": ", ".join(c.data["repos"]), **extra}
    text = (KIT / "templates" / template).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"@{key}@", str(value))
    require(not re.search(r"@[A-Z_]+@", text), "unresolved template field")
    return text


def initialize(c):
    for part in ("runs", "workers", "prompts", "locks", "landing"):
        (c.state / part).mkdir(parents=True, exist_ok=True)
    print(c.lane)


def add_worker(c, args):
    name = worker_name(args.role, args.number)
    initialize(c)
    with lock(c.state / "locks" / name, c.data.get("idle_poll_seconds", 30)) as lease:
        path = c.state / "workers" / f"{name}.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        home = c.lane / name
        home.mkdir(parents=True, exist_ok=True)
        for repo, settings in c.data["repos"].items():
            tree, gitdir = home / repo, home / f"{repo}.git"
            if not gitdir.exists():
                require(not tree.exists(), "clone destination already exists")
                c.plain_git("clone", f"--separate-git-dir={gitdir}", settings["url"], tree)
            require(tree.is_dir() and gitdir.is_dir(), "incomplete separate git clone")
        if not state.get("registered"):
            c.call("a2a", "register", c.identity(name))
            state["registered"] = True
            write_json(path, state)
        if not state.get("invited"):
            c.call("a2a", "invite", c.data["room"], c.identity(name))
            state["invited"] = True
            write_json(path, state)
        bootstrap = c.state / "prompts" / f"{name}.bootstrap.md"
        write_private(bootstrap, render(c, name, "bootstrap.md"))
        if not state.get("thread"):
            state["thread"] = launch(c, name, next(iter(c.data["repos"])), bootstrap, None, lease_fd=lease)
            write_json(path, state)
        print(name)


def validate_request(event, *, event_id, task, sender, room, recipient):
    require(isinstance(event, dict), "request must be an event object")
    require(event.get("event_id") == event_id, "event id mismatch")
    require(event.get("kind") == "request", "not a request")
    require(event.get("sender") == sender, "untrusted request sender")
    require(event.get("room") == room, "wrong request room")
    require(isinstance(event.get("to"), list) and recipient in event["to"], "request not addressed to worker")
    require(task in re.findall(r"\b(?:TASK|BUG)-[0-9]{6}-[a-z0-9]{6}\b", event.get("body", "")), "task id absent from request")
    return event


def assign(c, args):
    require(TASK.fullmatch(args.task), "invalid task id")
    require(SLUG.fullmatch(args.topic), "invalid topic")
    state = worker(c, args.worker)
    require(args.repo in c.data["repos"], "unknown repo")
    if args.worker.startswith("rev"):
        require(args.head and SHA.fullmatch(args.head), "review needs the full exact head")
    resources = [Path(p).resolve() for p in [args.brief, *args.resources]]
    require(all(p.is_file() and p.stat().st_size for p in resources), "missing or empty resource")
    require(len({p.name for p in resources}) == len(resources), "duplicate resource name")
    # Hold from board mutation through thread capture: a second assignment cannot overlap a run.
    with lock(c.state / "locks" / args.worker, c.data.get("idle_poll_seconds", 30)) as lease:
        state = worker(c, args.worker)
        identity = c.identity(args.worker)
        if args.worker.startswith("dev"):
            c.call("board", "m", f"assign({args.task}, agent={identity})")
            c.call("board", "m", f"set_status({args.task}, status=development)")
        else:
            note = c.state / "prompts" / f"reviewer-{args.task}.md"
            write_private(note, f"Reviewer for {args.task}: {identity}, named by {c.identity('orch')}.\n")
            resources.append(note)
            c.call("board", "m", f'set_notes({args.task}, text="Reviewer: {identity} (review B).")')
        for resource in resources:
            c.call("board", "resource", "add", args.task, resource, "--type", "precondition", "--name", resource.name)
            listed = json.loads(c.call("board", "q", f"get({args.task}) {{ preconditionResources }}"))
            require(any(r.get("name") == resource.name for r in listed.get("preconditionResources", [])), "attachment not listed")
        # Topic discovery is read-only. Creation failures are never silently ignored.
        topics = c.call("a2a", "topics", c.data["room"])
        if args.topic not in topics.split():
            c.call("a2a", "topic", "new", c.data["room"], args.topic,
                   f"[{args.task}] {resources[0].stem}", "--body", f"Assigned by {c.identity('orch')} to {identity}.")
        body = f"Task {args.task} for {identity}. Brief: {resources[0].name} (board precondition resource)."
        if args.head:
            require(SHA.fullmatch(args.head), "invalid exact head")
            body += f" Review the exact head {args.head} in {args.repo}."
        body += " Follow the standing worker protocol; hand over in this topic."
        posted = c.call("a2a", "post", c.data["room"], "--topic", args.topic,
                        "--kind", "request", "--to", identity, body).strip()
        require(re.fullmatch(r"\$[A-Za-z0-9_-]+", posted), "post did not return an event id; do not repost blindly")
        event = json.loads(c.call("a2a", "read", c.data["room"], "--event", posted, "--json"))
        validate_request(event, event_id=posted, task=args.task, sender=c.identity("orch"), room=c.data["room"], recipient=identity)
        prompt = c.state / "prompts" / f"{args.worker}-{args.task}-{uuid.uuid4().hex}.md"
        text = render(c, args.worker, "next.md", EVENT=posted, TASK=args.task, TOPIC=args.topic)
        if args.fresh or not state.get("thread"):
            text = render(c, args.worker, "bootstrap.md").split("Bootstrap this run only:")[0] + text
        write_private(prompt, text)
        # Receipt precedes runtime execution; failures leave the exact event available for recovery.
        receipt = c.state / "runs" / f"request-{posted[1:]}.json"
        write_json(receipt, {"event_id": posted, "task": args.task, "worker": args.worker, "prompt": str(prompt)})
        print(f"request {posted} task {args.task}", flush=True)
        thread = launch(c, args.worker, args.repo, prompt, None if args.fresh else state.get("thread"), lease_fd=lease)
        state["thread"] = thread
        write_json(c.state / "workers" / f"{args.worker}.json", state)


def launch(c, name, repo, prompt, thread, *, lease_fd):
    tag = f"{name}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    prefix = c.state / "runs" / tag
    argv = [c.tool("zsh"), "-c",
            'source "$1" || exit; shift; resolved=$("$1" env resolve "$2" --format shell) || exit; '
            'eval "$resolved" || exit; shift 2; exec "$@"',
            "construct-run", str(c.path(c.data["git_env"])), c.tool("curator"),
            c.data.get("curator_runtime", "codex_cli"), sys.executable, str(KIT / "lib" / "construct.py"),
            "--config", str(c.file), "_exec", name, repo, str(prompt), str(prefix), thread or "", "--lease-fd", str(lease_fd)]
    rc = child_wait(argv, env=c.env, cwd=c.lane / name / repo, pass_fds=(lease_fd,))
    require(rc == 0, f"worker run failed (exit {rc}); inspect its private run log and request receipt")
    log = prefix.with_suffix(".log").read_text(encoding="utf-8", errors="replace")
    if thread:
        return thread
    matches = re.findall(r"session id: ([0-9a-f-]+)", log)
    require(bool(matches) and THREAD.fullmatch(matches[0]), "new run did not return a valid thread id; do not resume the previous thread")
    return matches[0]


def execute_worker(c, args):
    name, repo = args.worker, args.repo
    require(os.environ.get("CODEX_HOME"), "curator did not resolve CODEX_HOME")
    require(not args.thread or THREAD.fullmatch(args.thread), "invalid saved thread id")
    w = c.lane / name
    tree = w / repo
    cache = w / f".gocache-{uuid.uuid4().hex}"
    cache.mkdir()
    for part in (".tmp", ".gopath"):
        (w / part).mkdir(exist_ok=True)
    env = clean_env()
    # Only these signing and transport values cross the agent boundary.
    for key in ("CODEX_HOME", "SSH_AUTH_SOCK", "CONSTRUCT_SIGNING_SOCKET", "GIT_SSH_COMMAND", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_CONFIG_COUNT"):
        if key in os.environ:
            env[key] = os.environ[key]
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    require(0 <= count <= 32, "invalid signing config count")
    for i in range(count):
        for kind in ("KEY", "VALUE"):
            key = f"GIT_CONFIG_{kind}_{i}"
            require(key in os.environ, "incomplete signing config")
            env[key] = os.environ[key]
    if c.data.get("signing_socket"):
        env[f"GIT_CONFIG_KEY_{count}"] = "gpg.ssh.program"
        env[f"GIT_CONFIG_VALUE_{count}"] = str(KIT / "bin/ssh-keygen-agent")
        env["GIT_CONFIG_COUNT"] = str(count + 1)
    env.update(PATH=c.data.get("worker_path", env.get("PATH", os.defpath)),
               TMPDIR=str(w / ".tmp"), A2A_AGENT=c.identity(name), A2A_SESSION_ID=f"construct-{c.identity(name)}",
               TASK_BOARD_CONFIG=str(c.path(c.data["board_config"])), GIT_DIR=str(w / f"{repo}.git"),
               GIT_WORK_TREE=str(tree), GOCACHE=str(cache), GOPATH=str(w / ".gopath"), GOFLAGS="-trimpath")
    if c.data.get("go"):
        env["CURATOR_GO"] = str(c.path(c.data["go"]))
    role = c.data["roles"]["dev" if name.startswith("dev") else "rev"]
    argv = [c.tool("codex"), "exec"]
    if args.thread:
        argv += ["resume", args.thread]
    argv += ["--skip-git-repo-check", "-m", role["model"], "-c", f'model_reasoning_effort={json.dumps(role["effort"])}',
             "-c", f'service_tier={json.dumps(c.data.get("service_tier", "standard"))}',
             "-c", f'features.memories={str(c.data.get("memories", False)).lower()}',
             "-c", 'shell_environment_policy.inherit="core"']
    for key, value in env.items():
        argv += ["-c", f"shell_environment_policy.set.{key}={json.dumps(value)}"]
    if c.data.get("sandbox", "workspace-write") == "trusted":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        argv += ["-c", 'sandbox_mode="workspace-write"', "-c", "sandbox_workspace_write.network_access=true",
                 "-c", f"sandbox_workspace_write.writable_roots={json.dumps([str(w), str(Path.home() / '.config/a2a-swarm')])}"]
    prefix = Path(args.prefix)
    argv += ["-o", str(prefix.with_suffix(".last.txt")), "-"]
    write_json(prefix.with_suffix(".argv"), argv)
    write_private(prefix.with_suffix(".log"), "")
    rc = 1
    try:
        with open(args.prompt, "r", encoding="utf-8") as prompt, prefix.with_suffix(".log").open("a", encoding="utf-8") as log:
            rc = child_wait(argv, env=env, cwd=tree, stdin=prompt, stdout=log, stderr=subprocess.STDOUT, pass_fds=(args.lease_fd,))
    finally:
        import shutil
        shutil.rmtree(cache)
        write_private(prefix.with_suffix(".exit"), str(rc) + "\n")
        last = prefix.with_suffix(".last.txt")
        if last.exists():
            last.chmod(0o600)
    return rc


def slot(c, command):
    require(bool(command), "slot needs a command")
    root = c.path(c.data["slot_root"]) if c.data.get("slot_root") else c.root / ".construct-slots"
    root.mkdir(parents=True, exist_ok=True)
    queue = root / "queue"
    queue.mkdir(exist_ok=True)
    started = time.monotonic()
    ticket = None
    handle = None
    try:
        with lock(root / "lock", .01):
            counter = root / "sequence"
            seq = int(counter.read_text()) + 1 if counter.exists() else 1
            write_private(counter, str(seq))
            ticket = queue / f"{seq:020d}-{uuid.uuid4().hex}"
            handle = ticket.open("x+")
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
        while True:
            with lock(root / "lock", .01):
                for item in sorted(queue.iterdir()):
                    if item == ticket:
                        continue
                    with item.open("r+") as other:
                        try:
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        item.unlink()  # No held kernel lock means a dead owner, including PID reuse.
                live = sorted(queue.iterdir())
                admitted = ticket in live[:c.data["slots"]]
            if admitted:
                break
            time.sleep(c.data.get("slot_poll_seconds", 5))
        print(f"slot wait_seconds={time.monotonic() - started:.3f}", file=sys.stderr, flush=True)
        executing = time.monotonic()
        try:
            # The command keeps the lease if its controller is killed abruptly.
            return child_wait(command, pass_fds=(handle.fileno(),))
        finally:
            print(f"slot execution_seconds={time.monotonic() - executing:.3f}", file=sys.stderr)
    finally:
        if ticket and handle:
            with lock(root / "lock", .01):
                ticket.unlink(missing_ok=True)
                handle.close()


def verdict(path, head):
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    data = json.loads(blocks[-1] if blocks else text)
    require(data.get("verdict") == "accepted", "review is not accepted")
    require(data.get("head") == head and SHA.fullmatch(head), "verdict must name the full exact head")
    findings = data.get("findings")
    require(isinstance(findings, list) and all(isinstance(f, dict) and f.get("severity") in ("P0", "P1", "P2") for f in findings), "invalid findings")
    require(not any(f["severity"] in ("P0", "P1") for f in findings), "blocking findings remain")
    return data


def repository(c, name):
    require(name in c.data["repos"], "unknown repo")
    gitdir = c.state / "landing" / f"{name}.git"
    if not gitdir.exists():
        c.plain_git("clone", "--bare", c.data["repos"][name]["url"], gitdir)
    return gitdir


def fetch_candidate(c, gitdir, repo, branch, head):
    require(valid_branch(branch) and branch != c.data["repos"][repo]["base"], "invalid feature branch")
    require(SHA.fullmatch(head), "full head required")
    base = c.data["repos"][repo]["base"]
    c.git(gitdir, "fetch", "origin", f"+refs/heads/{base}:refs/heads/{base}", f"+refs/heads/{branch}:refs/heads/{branch}")
    require(c.git(gitdir, "rev-parse", f"refs/heads/{branch}").strip() == head, "branch differs from reviewed head")
    return base


def signed_range(c, gitdir, base, head):
    commits = c.git(gitdir, "rev-list", f"{base}..{head}").split()
    require(bool(commits), "no introduced commits")
    for commit in commits:
        require(c.git(gitdir, "log", "-1", "--format=%G?", commit).strip() == "G", "introduced commit is not signed G")


def landing(c, args):
    require(TASK.fullmatch(args.task), "invalid task id")
    require(re.fullmatch(r"dev[1-9][0-9]*", args.producer) and re.fullmatch(r"rev[1-9][0-9]*", args.reviewer), "independent producer and reviewer required")
    require(c.identity(args.producer) != c.identity(args.reviewer), "reviewer must differ from producer")
    verdict(args.verdict, args.head)
    gitdir = repository(c, args.repo)
    with lock(c.state / "locks" / f"land-{args.repo}"):
        base = fetch_candidate(c, gitdir, args.repo, args.branch, args.head)
        c.git(gitdir, "merge-base", "--is-ancestor", base, args.head)
        signed_range(c, gitdir, base, args.head)
        remote = c.data["repos"][args.repo].get("github")
        require(isinstance(remote, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", remote), "repo.github owner/name required for landing")
        summary = Path(args.summary).read_text(encoding="utf-8")
        body = c.state / "landing" / f"{args.task}-body.md"
        write_private(body, summary + f"\n\nTask: {args.task}. Independent review: {c.identity(args.reviewer)} at {args.head}.\n")
        prs = json.loads(c.call("gh", "pr", "list", "--repo", remote, "--head", args.branch, "--base", base, "--json", "number"))
        if not prs:
            c.call("gh", "pr", "create", "--repo", remote, "--base", base, "--head", args.branch, "--title", args.title, "--body-file", body)
            prs = json.loads(c.call("gh", "pr", "list", "--repo", remote, "--head", args.branch, "--base", base, "--json", "number"))
        require(len(prs) == 1, "expected exactly one open PR")
        pr = str(prs[0]["number"])
        before = json.loads(c.call("gh", "pr", "view", pr, "--repo", remote, "--json", "headRefOid,baseRefName,state,statusCheckRollup"))
        require(all(before.get(k) == v for k, v in {"headRefOid": args.head, "baseRefName": base, "state": "OPEN"}.items()), "PR differs from reviewed candidate")
        require(isinstance(before.get("statusCheckRollup"), list), "unreadable check rollup")
        if before["statusCheckRollup"]:
            c.call("gh", "pr", "checks", pr, "--repo", remote, "--watch", "--interval", "30")
            checks = json.loads(c.call("gh", "pr", "checks", pr, "--repo", remote, "--json", "name,state,bucket"))
        else:
            checks = []  # A successful structured read proves absence; a CLI error never does.
        require(isinstance(checks, list) and (checks or c.data.get("allow_no_checks", False)), "checks absent or unreadable")
        require(all(isinstance(check, dict) and check.get("bucket") == "pass" and check.get("state") == "SUCCESS" for check in checks), "checks are not all successful")
        view = json.loads(c.call("gh", "pr", "view", pr, "--repo", remote, "--json", "headRefOid,baseRefName,state"))
        require(view == {"headRefOid": args.head, "baseRefName": base, "state": "OPEN"}, "PR moved after checks")
        # Re-fetch both refs, repeat all commit checks, and use a normal non-forced push.
        fetch_candidate(c, gitdir, args.repo, args.branch, args.head)
        c.git(gitdir, "merge-base", "--is-ancestor", base, args.head)
        signed_range(c, gitdir, base, args.head)
        c.call("gh", "pr", "review", pr, "--repo", remote, "--comment", "--body",
               f"Independent review B by {c.identity(args.reviewer)} accepted exact head {args.head}; all checks pass and introduced commits are signed. Landing by fast-forward.")
        c.git(gitdir, "push", "origin", f"{args.head}:refs/heads/{base}")
        print(f"landed PR {pr} {args.head}")


def rebase(c, args):
    accepted = verdict(args.verdict, args.head)
    require(valid_branch(args.new_branch) and args.new_branch not in (args.branch, c.data["repos"][args.repo]["base"]), "invalid new feature branch")
    gitdir = repository(c, args.repo)
    with lock(c.state / "locks" / f"land-{args.repo}"):
        base = fetch_candidate(c, gitdir, args.repo, args.branch, args.head)
        signed_range(c, gitdir, c.git(gitdir, "merge-base", base, args.head).strip(), args.head)
        ancestor = c.git(gitdir, "merge-base", base, args.head).strip()
        upstream = set(c.git(gitdir, "diff", "--name-only", ancestor, base).splitlines())
        changed = set(c.git(gitdir, "diff", "--name-only", ancestor, args.head).splitlines())
        shared = sorted(p for p in upstream & changed if not re.match(r"^(README|CHANGELOG)|^docs/|^\.task-board/", p))
        require(not shared, "Path B refused: shared non-documentation paths; return to producer")
        import tempfile
        import shutil
        tree = Path(tempfile.mkdtemp(prefix="rebase-", dir=c.state / "landing"))
        local = tree / "repo"
        try:
            c.plain_git("clone", "--no-checkout", gitdir, local)
            lg = local / ".git"
            c.git(lg, "checkout", "-b", args.new_branch, base, tree=local)
            for commit in c.git(gitdir, "rev-list", "--reverse", f"{ancestor}..{args.head}").split():
                c.git(lg, "cherry-pick", "-S", commit, tree=local)
            diff = c.git(lg, "range-diff", "--no-color", f"{ancestor}..{args.head}", f"{base}..HEAD", tree=local)
            require(bool(diff.strip()) and all(re.fullmatch(r"\s*\d+:\s+[0-9a-f]+ =\s+\d+:\s+[0-9a-f]+.*", line) for line in diff.splitlines()), "Path B refused: patches are not identical")
            new_head = c.git(lg, "rev-parse", "HEAD", tree=local).strip()
            signed_range(c, lg, base, new_head)
            c.git(lg, "push", c.data["repos"][args.repo]["url"], f"HEAD:refs/heads/{args.new_branch}", tree=local)
            carried = {**accepted, "head": new_head, "accepted_head": args.head, "path": "B"}
            output = c.state / "landing" / f"{new_head}-verdict.json"
            write_json(output, carried)
            print(f"head {new_head}\nverdict {output}")
        finally:
            shutil.rmtree(tree)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=os.environ.get("CONSTRUCT_CONFIG", "construct.json"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("config-check")
    a = sub.add_parser("add-worker")
    a.add_argument("role", choices=("dev", "rev")); a.add_argument("number", type=int)
    a = sub.add_parser("assign")
    for name in ("worker", "task", "topic", "brief"):
        a.add_argument(name)
    a.add_argument("resources", nargs="*"); a.add_argument("--repo", required=True)
    a.add_argument("--head"); a.add_argument("--fresh", action="store_true")
    a = sub.add_parser("slot"); a.add_argument("argv", nargs=argparse.REMAINDER)
    a = sub.add_parser("land")
    for name in ("repo", "branch", "head", "task", "title", "summary", "verdict"):
        a.add_argument(name)
    a.add_argument("--producer", required=True); a.add_argument("--reviewer", required=True)
    a = sub.add_parser("rebase")
    for name in ("repo", "branch", "head", "new_branch", "verdict"):
        a.add_argument(name)
    a = sub.add_parser("request-check", help="validate a saved a2a JSON event without network access")
    for name in ("file", "event", "task", "worker"):
        a.add_argument(name)
    a = sub.add_parser("_exec", help=argparse.SUPPRESS)
    for name in ("worker", "repo", "prompt", "prefix", "thread"):
        a.add_argument(name)
    a.add_argument("--lease-fd", required=True, type=int)
    return p


def main():
    args = parser().parse_args()
    c = Config(args.config)
    if args.command == "config-check":
        print("config valid")
    elif args.command == "init":
        initialize(c)
    elif args.command == "add-worker":
        add_worker(c, args)
    elif args.command == "assign":
        assign(c, args)
    elif args.command == "slot":
        return slot(c, args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
    elif args.command == "land":
        landing(c, args)
    elif args.command == "rebase":
        rebase(c, args)
    elif args.command == "request-check":
        require(TASK.fullmatch(args.task), "invalid task id")
        validate_request(json.loads(Path(args.file).read_text(encoding="utf-8")), event_id=args.event,
                         task=args.task, sender=c.identity("orch"), room=c.data["room"], recipient=c.identity(args.worker))
        print("exact request verified")
    elif args.command == "_exec":
        return execute_worker(c, args)
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except (Refusal, ValueError, KeyError, TypeError, OSError) as error:
        print(f"construct: {error}", file=sys.stderr)
        sys.exit(2)
