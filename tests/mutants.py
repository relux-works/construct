#!/usr/bin/env python3
"""Bounded source mutations, only in disposable kit copies; no production test switches."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

KIT = Path(__file__).resolve().parents[1]
CONFIG = "test_config.ConfigTests."
REQUEST = "test_request.RequestTests.test_00_request_authority"
LAND = "test_landing.LandingTests."
WORK = "test_workers.WorkerTests."
SLOT = "test_slots.SlotTests."
# Each plant removes one gate. The associated negative-first test must fail as an assertion,
# with no setup error or skipped test; its adjacent positive control runs on the clean kit.
GATES = [
    ("slots must be 1..64", CONFIG + "test_00_reject_invalid_config"),
    ("invalid room", CONFIG + "test_00_reject_invalid_config"),
    ("repos must be nonempty", CONFIG + "test_00_reject_invalid_config"),
    ("missing model", CONFIG + "test_00_reject_invalid_config"),
    ("invalid sandbox", CONFIG + "test_00_reject_invalid_config"),
    ("invalid tools", CONFIG + "test_00_reject_invalid_config"),
    ("reviewer effort must be medium, xhigh or max", CONFIG + "test_01_reviewer_effort_guard"),
    ("invalid repo", CONFIG + "test_02_repo_guards"),
    ("invalid repo URL", CONFIG + "test_02_repo_guards"),
    ("invalid repo base", CONFIG + "test_02_repo_guards"),
    ("event id mismatch", REQUEST), ("not a request", REQUEST),
    ("untrusted request sender", REQUEST), ("wrong request room", REQUEST),
    ("request not addressed to worker", REQUEST), ("task id absent from request", REQUEST),
    ("review is not accepted", LAND + "test_00_verdict_gates"),
    ("verdict must name the full exact head", LAND + "test_00_verdict_gates"),
    ("blocking findings remain", LAND + "test_00_verdict_gates"),
    ("checks absent or unreadable", LAND + "test_01_hosted_check_gates"),
    ("checks are not all successful", LAND + "test_01_hosted_check_gates"),
    ("PR differs from reviewed candidate", LAND + "test_01_hosted_check_gates"),
    ("PR moved after checks", LAND + "test_01_hosted_check_gates"),
    ("unreadable check rollup", LAND + "test_01_hosted_check_gates"),
    ("introduced commit is not signed G", LAND + "test_02_unsigned"),
    ("default branch was not advanced", LAND + "test_09_late_main_at_head"),
    ("branch differs from reviewed head", LAND + "test_03_ref_drift"),
    ("invalid feature branch", LAND + "test_03_empty_and_default_branch"),
    ("no introduced commits", LAND + "test_03_empty_and_default_branch"),
    ("Path B refused: shared non-documentation paths; return to producer", LAND + "test_04_path_b"),
    ("Path B refused: patches are not identical", LAND + "test_05_changed_patch"),
    ("attachment not listed", WORK + "test_00_attachment_and_bootstrap"),
    ("review needs the full exact head", WORK + "test_11_reviewer_exact_head"),
]
LITERALS = [
    ("landed branch cleanup", 'c.git(gitdir, "push", "origin", f"--force-with-lease=refs/heads/{args.branch}:{args.head}", "--delete", args.branch)',
     'None', LAND + "test_00_verdict_gates"),
    ("atomic cleanup lease", 'f"--force-with-lease=refs/heads/{args.branch}:{args.head}", "--delete"',
     '"--delete"', LAND + "test_07_concurrent_feature_cleanup"),
    ("atomic main lease", 'f"--force-with-lease=refs/heads/{base}:{expected_main}", ',
     '', LAND + "test_09_late_main_rewind"),
    ("final fast-forward ancestry", 'c.git(gitdir, "merge-base", "--is-ancestor", expected_main, args.head)',
     'None', LAND + "test_09_final_ancestry"),
    ("documentation prefix bypass", 'not documentation_path(p)',
     'not re.match(r"^(README|CHANGELOG)|^docs/|^\\.task-board/", p)',
     "test_landing.PathBDocumentationTests.test_00_readme_prefix_code"),
    ("review base ancestry", 'c.git(gitdir, "merge-base", "--is-ancestor", reviewed, base)',
     'None', LAND + "test_08_review_base"),
    ("full review base", 'isinstance(reviewed, str) and SHA.fullmatch(reviewed)',
     'isinstance(reviewed, str)', LAND + "test_08_invalid_review_base"),
    ("initial review base check", 'base = fetch_candidate(c, gitdir, args.repo, args.branch, args.head)\n        check_review_base(c, gitdir, base, accepted)',
     'base = fetch_candidate(c, gitdir, args.repo, args.branch, args.head)', LAND + "test_08_review_base"),
    ("final review base check", 'check_review_base(c, gitdir, expected_main, accepted)',
     'None', LAND + "test_08_review_base_after_fetch"),
    ("slot requires command", 'require(bool(command), "slot needs a command")',
     'if not command: return 0', SLOT + "test_00_exit_and_environment"),
    ("exact prompt selector", 'EVENT=posted, TASK=args.task', 'EVENT="$latest", TASK=args.task', WORK + "test_10_fresh_thread"),
    ("worker serialization", 'with lock(c.state / "locks" / args.worker, c.data.get("idle_poll_seconds", 30)) as lease:',
     'with contextlib.nullcontext(1) as lease:', WORK + "test_01_serial_exact_requests"),
    ("FIFO capacity", 'admitted = ticket in live[:c.data["slots"]]', 'admitted = True', SLOT + "test_01_fifo_capacity_and_stale_reclaim"),
    ("stale reclaim", 'item.unlink()  # No held kernel lock means a dead owner, including PID reuse.',
     'pass  # planted stale reclaim violation', SLOT + "test_01_fifo_capacity_and_stale_reclaim"),
    ("child slot lease", 'return child_wait(command, pass_fds=(handle.fileno(),))',
     'return child_wait(command)', SLOT + "test_11_child_lease"),
]


class RemoveGate(ast.NodeTransformer):
    def __init__(self, message):
        self.message, self.changed = message, 0

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "require" and len(node.args) == 2:
            message = node.args[1]
            if isinstance(message, ast.Constant) and message.value == self.message:
                node.args[0] = ast.Constant(value=True); self.changed += 1
        return node


def main():
    root = Path(os.environ.get("CONSTRUCT_TEST_TMP", KIT / ".temp/tests"))
    root.mkdir(parents=True, exist_ok=True)
    source = (KIT / "lib/construct.py").read_text()
    records = []
    for name, test, old, new in ([(m, t, None, None) for m, t in GATES] + [(n, t, o, v) for n, o, v, t in LITERALS]):
        if old is None:
            transform = RemoveGate(name)
            tree = transform.visit(ast.parse(source))
            if transform.changed != 1:
                raise RuntimeError(f"plant not unique: {name}: {transform.changed}")
            mutated = ast.unparse(ast.fix_missing_locations(tree))
        else:
            if source.count(old) != 1:
                raise RuntimeError(f"plant not unique: {name}")
            mutated = source.replace(old, new)
        with tempfile.TemporaryDirectory(prefix="mutant-", dir=root) as temporary:
            target = Path(temporary) / "kit"
            shutil.copytree(KIT, target, ignore=shutil.ignore_patterns(".git", ".temp", "__pycache__"))
            (target / "lib/construct.py").write_text(mutated)
            env = dict(os.environ, CONSTRUCT_KIT=str(target), CONSTRUCT_TEST_TMP=temporary, PYTHONDONTWRITEBYTECODE="1")
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", test], cwd=KIT / "tests", env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=55)
            if result.returncode != 1 or "FAIL:" not in result.stdout or "ERROR:" in result.stdout or "skipped" in result.stdout.lower():
                print(result.stdout, file=sys.stderr)
                raise RuntimeError(f"plant did not cause the intended assertion failure: {name}")
            record = {"plant": name, "test": test, "exit": result.returncode, "detected": True,
                      "seconds": round(time.monotonic() - started, 3)}
            records.append(record)
            print(json.dumps(record), flush=True)
    print(f"{len(records)} narrowing mutants detected")


if __name__ == "__main__":
    main()
