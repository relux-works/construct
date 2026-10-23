import json
from helpers import Fixture


class LandingTests(Fixture):
    def setUp(self):
        super().setUp(); self.signed_repo(); self.gh()

    # Claims: rejected/abbreviated/mismatched/blocking verdicts cannot write main; an exact accepted control lands.
    def test_00_verdict_gates(self):
        for change, message in (({"verdict": "changes_requested"}, "not accepted"),
                                ({"head": self.head[:12]}, "full exact head"),
                                ({"head": "0" * 40}, "full exact head"),
                                ({"findings": [{"severity": "P1"}]}, "blocking findings")):
            self.set_verdict(**change)
            self.assertIn(message, self.land(ok=False).stderr)
            self.assertEqual(self.remote_main(), self.base)
        self.set_verdict(findings=[{"severity": "P2"}]); self.gh()
        self.land(); self.assertEqual(self.remote_main(), self.head)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "branch", "--list", "feature"), "")

    # Claim: unknown/failed checks and PR-head drift leave main untouched; successful checks land.
    def test_01_hosted_check_gates(self):
        for mode, message in (("absent", "checks absent"), ("failed", "not all successful"), ("skipped", "not all successful"),
                              ("unreadable", "unreadable check rollup"), ("moved", "PR differs"), ("moved-after", "PR moved after checks")):
            self.cfg["allow_no_checks"] = mode == "unreadable"; self.save()
            self.gh(mode)
            self.assertIn(message, self.land(ok=False).stderr)
            self.assertEqual(self.remote_main(), self.base)
        self.gh(); self.land(); self.assertEqual(self.remote_main(), self.head)

    # Claim: explicit no-check policy accepts verified empty rollup, while provider read failure is never treated as absence.
    def test_01_no_check_policy(self):
        self.cfg["allow_no_checks"] = True; self.save(); self.gh("error")
        self.assertIn("gh failed (exit 42)", self.land(ok=False).stderr)
        self.assertEqual(self.remote_main(), self.base)
        self.gh("absent"); self.land(); self.assertEqual(self.remote_main(), self.head)

    # Claim: an unsigned introduced commit is rejected even when review and hosted checks would pass.
    def test_02_unsigned(self):
        (self.seed / "unsigned.txt").write_text("unsigned\n")
        self.git("add", "unsigned.txt", cwd=self.seed)
        self.git("commit", "--no-gpg-sign", "-m", "Unsigned", cwd=self.seed)
        self.head = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "feature", cwd=self.seed)
        self.set_verdict(); self.gh()
        self.assertIn("not signed G", self.land(ok=False).stderr)
        self.assertEqual(self.remote_main(), self.base)

    # Claim: feature drift is refused before hosting actions; a default-branch advance prevents landing.
    def test_03_ref_drift(self):
        old = self.head
        (self.seed / "other.txt").write_text("other candidate\n")
        self.git("add", "other.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Other candidate", cwd=self.seed)
        self.head = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "HEAD:refs/heads/other", cwd=self.seed)
        self.set_verdict(); self.gh()
        self.assertIn("branch differs", self.land(ok=False).stderr)
        self.head = old; self.set_verdict(); self.gh()
        self.git("switch", "main", cwd=self.seed)
        (self.seed / "upstream.txt").write_text("upstream\n")
        self.git("add", "upstream.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Upstream", cwd=self.seed)
        advanced = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "main", cwd=self.seed)
        self.land(ok=False); self.assertEqual(self.remote_main(), advanced)

    # Claim: the default branch cannot be passed as a feature, and an empty candidate range cannot land.
    def test_03_empty_and_default_branch(self):
        result = self.cli("land", "project", "main", self.head, "TASK-260101-abcdef", "Example", self.summary,
                          self.verdict, "--producer", "dev1", "--reviewer", "rev1", ok=False)
        self.assertIn("invalid feature branch", result.stderr)
        self.git("--git-dir=" + str(self.root / "remote.git"), "update-ref", "refs/heads/feature", self.base)
        self.head = self.base; self.set_verdict(); self.gh()
        self.assertIn("no introduced commits", self.land(ok=False).stderr)
        self.assertEqual(self.remote_main(), self.base)

    # Claim: Path B refuses shared code; disjoint signed patches rebase identically onto current main.
    def test_04_path_b(self):
        self.git("switch", "main", cwd=self.seed)
        (self.seed / "feature.txt").write_text("upstream conflicting\n")
        self.git("add", "feature.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Conflicting", cwd=self.seed)
        self.git("push", self.root / "remote.git", "main", cwd=self.seed)
        result = self.cli("rebase", "project", "feature", self.head, "rebased", self.verdict, ok=False)
        self.assertIn("shared non-documentation paths", result.stderr)
        # Advance through a new independent bare remote instead of resetting user state.
        clean = self.root / "clean.git"
        self.git("clone", "--bare", self.root / "remote.git", clean)
        self.git("--git-dir=" + str(clean), "update-ref", "refs/heads/main", self.base)
        self.git("switch", "-c", "disjoint", self.base, cwd=self.seed)
        (self.seed / "independent.txt").write_text("upstream\n")
        self.git("add", "independent.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Independent upstream", cwd=self.seed)
        self.git("push", clean, "HEAD:refs/heads/main", cwd=self.seed)
        upstream = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.cfg["repos"]["project"]["url"] = str(clean); self.save()
        # The cached landing clone deliberately follows the previous URL; use a fresh lane.
        self.cfg["lane"] = "clean"; self.save(); self.cli("init")
        result = self.cli("rebase", "project", "feature", self.head, "rebased", self.verdict)
        new = result.stdout.splitlines()[0].split()[1]
        self.assertEqual(self.git("--git-dir=" + str(clean), "rev-parse", "rebased"), new)
        self.assertEqual(self.git("-c", "gpg.format=ssh", "-c", "gpg.ssh.allowedSignersFile=" + str(self.root / "allowed"),
                                  "--git-dir=" + str(clean), "log", "-1", "--format=%G?", new), "G")
        self.assertEqual(self.git("--git-dir=" + str(clean), "rev-parse", new + "^"), upstream)
        self.assertEqual(self.git("--git-dir=" + str(clean), "show", new + ":feature.txt"), "feature")
        self.assertEqual(self.git("--git-dir=" + str(clean), "show", new + ":independent.txt"), "upstream")

    # Claim: a silently changed cherry-pick is refused by the patch-identity gate before publication.
    def test_05_changed_patch(self):
        import shutil
        real_git = shutil.which("git")
        self.fake("git", f'''
import pathlib,subprocess,sys
a=sys.argv[1:]; rc=subprocess.call([{real_git!r},*a])
if rc==0 and 'cherry-pick' in a:
 tree=pathlib.Path(next(x.split('=',1)[1] for x in a if x.startswith('--work-tree=')))
 (tree/'unexpected.txt').write_text('changed patch\\n')
 prefix=a[:a.index('cherry-pick')]
 subprocess.run([{real_git!r},*prefix,'add','unexpected.txt'],check=True)
 subprocess.run([{real_git!r},*prefix,'commit','--amend','-S','--no-edit'],check=True,stdout=subprocess.DEVNULL)
sys.exit(rc)
''')
        result = self.cli("rebase", "project", "feature", self.head, "rebased", self.verdict, ok=False)
        self.assertIn("patches are not identical", result.stderr)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "branch", "--list", "rebased"), "")
        self.cfg["tools"]["git"] = real_git; self.save()
        self.cli("rebase", "project", "feature", self.head, "rebased", self.verdict)

    # Claim: cleanup failure is reported without hiding a successful signed landing; test_00 is the normal deletion control.
    def test_06_cleanup_failure(self):
        import shutil
        real_git = shutil.which("git")
        self.fake("git", f'''
import subprocess,sys
sys.exit(9 if '--delete' in sys.argv else subprocess.call([{real_git!r},*sys.argv[1:]]))
''')
        result = self.land()
        self.assertIn("feature branch cleanup failed", result.stderr)
        self.assertEqual(self.remote_main(), self.head)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "rev-parse", "feature"), self.head)
