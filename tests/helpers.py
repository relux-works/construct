"""Local-only fixtures; no provider, board, account or network is contacted."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

KIT = Path(os.environ.get("CONSTRUCT_KIT", Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("construct_under_test", KIT / "lib/construct.py")
product = importlib.util.module_from_spec(spec)
spec.loader.exec_module(product)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="construct-", dir=os.environ.get("CONSTRUCT_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"; self.home.mkdir()
        self.config = self.root / "construct.json"
        self.cfg = json.loads((KIT / "construct.example.json").read_text())
        self.cfg.update(root=str(self.root / "work"), git_env=str(self.root / "git-env"),
                        board_config=str(self.root / "board.json"), slots=1,
                        slot_poll_seconds=.02, idle_poll_seconds=.02)
        self.cfg["repos"]["project"].update(url=str(self.root / "remote.git"), github="example/project")
        (self.root / "git-env").write_text("true\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "A2A_", "TASK_BOARD_"))}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1")
        # A child inheriting these would hit an unrelated repository.
        self.env.update(GIT_DIR=str(self.root / "forbidden.git"), GIT_WORK_TREE=str(self.root / "forbidden"))
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.cfg))

    def cli(self, *args, ok=True):
        result = subprocess.run([str(KIT / "bin/construct"), "--config", str(self.config), *map(str, args)],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 2, result.stderr)
        return result

    def git(self, *args, cwd=None):
        env = {k: v for k, v in self.env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
        result = subprocess.run(["git", *map(str, args)], cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fake(self, name, body):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)
        self.cfg["tools"][name] = str(path)
        self.save()
        return path

    def wait_for(self, predicate, timeout=8):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if predicate():
                return
            time.sleep(.01)
        self.fail("fixture deadline exceeded")

    def popen(self, *args):
        p = subprocess.Popen([str(KIT / "bin/construct"), "--config", str(self.config), *map(str, args)],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, p)
        return p

    @staticmethod
    def stop(p):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=7)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
        for stream in (p.stdout, p.stderr):
            if stream:
                stream.close()

    def signed_repo(self):
        """Signing material exists only inside this disposable fixture."""
        key = self.root / "fixture-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        allowed = self.root / "allowed"
        allowed.write_text("author@example.invalid " + key.with_suffix(".pub").read_text())
        envfile = self.root / "git-env"
        # Generated fixture paths are quoted as shell data, never as code.
        import shlex
        settings = {"user.name": "Test Author", "user.email": "author@example.invalid", "gpg.format": "ssh",
                    "user.signingkey": str(key), "gpg.ssh.allowedSignersFile": str(allowed)}
        envfile.write_text("export GIT_CONFIG_COUNT=5\n" + "".join(
            f"export GIT_CONFIG_KEY_{i}={shlex.quote(k)} GIT_CONFIG_VALUE_{i}={shlex.quote(v)}\n"
            for i, (k, v) in enumerate(settings.items())))
        tree = self.root / "seed"
        self.git("init", "-b", "main", tree)
        for keyname, value in settings.items():
            self.git("config", keyname, value, cwd=tree)
        (tree / "README.md").write_text("base\n")
        self.git("add", "README.md", cwd=tree)
        self.git("commit", "-S", "-m", "Base", cwd=tree)
        self.base = self.git("rev-parse", "HEAD", cwd=tree)
        self.git("clone", "--bare", tree, self.root / "remote.git")
        self.git("switch", "-c", "feature", cwd=tree)
        (tree / "feature.txt").write_text("feature\n")
        self.git("add", "feature.txt", cwd=tree)
        self.git("commit", "-S", "-m", "Feature", cwd=tree)
        self.head = self.git("rev-parse", "HEAD", cwd=tree)
        self.git("push", self.root / "remote.git", "feature", cwd=tree)
        self.seed = tree
        self.verdict = self.root / "verdict.json"
        self.set_verdict()
        self.summary = self.root / "summary.md"; self.summary.write_text("Example change.\n")
        self.cli("init")

    def set_verdict(self, **changes):
        self.verdict.write_text(json.dumps({"verdict": "accepted", "head": self.head, "findings": [], **changes}))

    def gh(self, mode="pass"):
        audit = self.root / "gh-audit"
        self.fake("gh", f'''
import json, pathlib, sys
a=sys.argv[1:]
with open({str(audit)!r}, 'a') as f: f.write(json.dumps(a)+'\\n')
mode={mode!r}
if a[:2]==['pr','list']: print('[{{"number":1}}]')
elif a[:2]==['pr','checks'] and '--json' in a:
 print(json.dumps([] if mode=='absent' else [{{'name':'test','bucket':'fail' if mode=='failed' else 'pass','state': 'SKIPPED' if mode=='skipped' else ('FAILURE' if mode=='failed' else 'SUCCESS')}}]))
elif a[:2]==['pr','view']:
 if mode=='error': sys.exit(42)
 d={{'headRefOid': '0'*40 if (mode=='moved' and 'statusCheckRollup' in a[-1]) or (mode=='moved-after' and 'statusCheckRollup' not in a[-1]) else {self.head!r},'baseRefName':'main','state':'OPEN'}}
 if 'statusCheckRollup' in a[-1]: d['statusCheckRollup']=None if mode=='unreadable' else ([] if mode=='absent' else [{{'name':'test'}}])
 print(json.dumps(d))
''')
        return audit

    def land(self, ok=True):
        return self.cli("land", "project", "feature", self.head, "TASK-260101-abcdef", "Example", self.summary,
                        self.verdict, "--producer", "dev1", "--reviewer", "rev1", ok=ok)

    def remote_main(self):
        return self.git("--git-dir=" + str(self.root / "remote.git"), "rev-parse", "main")
