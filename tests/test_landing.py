import json
import shutil
import subprocess
from helpers import Fixture, KIT


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

    # Claim: cleanup's atomic lease preserves a feature advanced after the final fetch,
    # while main receives exactly the accepted head. test_00 is the unchanged-ref control.
    def test_07_concurrent_feature_cleanup(self):
        accepted = self.head
        (self.seed / "later.txt").write_text("later work\n")
        self.git("add", "later.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Concurrent change", cwd=self.seed)
        later = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "HEAD:refs/heads/staging", cwd=self.seed)
        with open(self.cfg["tools"]["gh"], "a") as stream:
            stream.write(f"""
if a[:2]==['pr','review']:
 import os,subprocess
 env={{k:v for k,v in os.environ.items() if k not in ('GIT_DIR','GIT_WORK_TREE')}}
 subprocess.run([{shutil.which('git')!r},'--git-dir='+{str(self.root / 'remote.git')!r},
                 'update-ref','refs/heads/feature',{later!r},{accepted!r}],env=env,check=True)
""")
        result = self.land()
        self.assertEqual(self.remote_main(), accepted)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "branch", "--list", "feature"), "feature")
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "rev-parse", "feature"), later)
        self.assertIn("feature branch cleanup failed", result.stderr)

    def land_with_base(self, base, expected_exit):
        self.set_verdict(base=base)
        result = subprocess.run([str(KIT / "bin/construct"), "--config", str(self.config),
                                 "land", "project", "feature", self.head, "TASK-260101-abcdef", "Example",
                                 str(self.summary), str(self.verdict), "--producer", "dev1", "--reviewer", "rev1"],
                                env=self.env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        return result

    # Claim: a signed stacked candidate cannot carry an unreviewed parent to main;
    # an accepted range whose base is already on main can land, including an older ancestor.
    def test_08_review_base(self):
        parent = self.head
        (self.seed / "reviewed.txt").write_text("reviewed tip\n")
        self.git("add", "reviewed.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Reviewed tip", cwd=self.seed)
        self.head = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "feature", cwd=self.seed)
        self.gh()
        result = self.land_with_base(parent, 8)
        self.assertIn("review base", result.stderr)
        self.assertEqual(self.remote_main(), self.base)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "rev-parse", "feature"), self.head)
        self.assertFalse((self.root / "gh-audit").exists(), "refusal must precede hosted writes")
        # Once the parent has landed independently, this review covers the remaining tip.
        self.git("push", self.root / "remote.git", parent + ":refs/heads/main", cwd=self.seed)
        self.land_with_base(self.base, 0)
        self.assertEqual(self.remote_main(), self.head)

    # Claim: null, abbreviated, unresolved or option-like bases fail closed with exit 8;
    # a full base on main succeeds. Verdicts without base remain covered by test_00.
    def test_08_invalid_review_base(self):
        for invalid in (None, 7, self.base[:12], "0" * 40, "--all"):
            result = self.land_with_base(invalid, 8)
            self.assertIn("review base", result.stderr)
            self.assertEqual(self.remote_main(), self.base)
            self.assertFalse((self.root / "gh-audit").exists())
        self.land_with_base(self.base, 0)
        self.assertEqual(self.remote_main(), self.head)

    # Claim: the review base is rechecked after the final fetch; a remote rewind
    # during hosted checks cannot reintroduce an unreviewed parent to main.
    def test_08_review_base_after_fetch(self):
        parent = self.head
        (self.seed / "reviewed.txt").write_text("reviewed tip\n")
        self.git("add", "reviewed.txt", cwd=self.seed)
        self.git("commit", "-S", "-m", "Reviewed tip", cwd=self.seed)
        self.head = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.git("push", self.root / "remote.git", "feature", parent + ":refs/heads/main", cwd=self.seed)
        audit = self.gh()
        with open(self.cfg["tools"]["gh"], "a") as stream:
            stream.write(f"""
if a[:2]==['pr','view'] and 'statusCheckRollup' not in a[-1]:
 import os,subprocess
 env={{k:v for k,v in os.environ.items() if k not in ('GIT_DIR','GIT_WORK_TREE')}}
 subprocess.run([{shutil.which('git')!r},'--git-dir='+{str(self.root / 'remote.git')!r},
                 'update-ref','refs/heads/main',{self.base!r},{parent!r}],env=env,check=True)
""")
        self.land_with_base(parent, 8)
        self.assertEqual(self.remote_main(), self.base)
        self.assertEqual(self.git("--git-dir=" + str(self.root / "remote.git"), "rev-parse", "feature"), self.head)
        self.assertFalse(any(json.loads(line)[:2] == ["pr", "review"] for line in audit.read_text().splitlines()))


class PathBDocumentationTests(Fixture):
    def shared_path(self, filename, allowed):
        self.signed_repo()
        self.git("switch", "main", cwd=self.seed)
        path = self.seed / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"value_{i} = {i}\n" for i in range(50)]
        path.write_text("".join(lines))
        self.git("add", filename, cwd=self.seed)
        self.git("commit", "-S", "-m", "Shared path base", cwd=self.seed)
        self.git("push", self.root / "remote.git", "main", cwd=self.seed)
        self.git("switch", "-c", "candidate", cwd=self.seed)
        feature = lines.copy(); feature[40] = "value_40 = 400\n"
        path.write_text("".join(feature))
        self.git("add", filename, cwd=self.seed)
        self.git("commit", "-S", "-m", "Candidate edit", cwd=self.seed)
        self.head = self.git("rev-parse", "HEAD", cwd=self.seed)
        self.set_verdict()
        self.git("push", self.root / "remote.git", "candidate", cwd=self.seed)
        self.git("switch", "main", cwd=self.seed)
        upstream = lines.copy(); upstream[2] = "value_2 = 200\n"
        path.write_text("".join(upstream))
        self.git("add", filename, cwd=self.seed)
        self.git("commit", "-S", "-m", "Upstream edit", cwd=self.seed)
        self.git("push", self.root / "remote.git", "main", cwd=self.seed)
        main = self.git("rev-parse", "HEAD", cwd=self.seed)
        result = self.cli("rebase", "project", "candidate", self.head, "carried", self.verdict, ok=allowed)
        published = self.git("--git-dir=" + str(self.root / "remote.git"), "branch", "--list", "carried")
        self.assertEqual(bool(published), allowed)
        self.assertEqual(self.remote_main(), main)
        outputs = list((self.root / "work/project/.construct/landing").glob("*-verdict.json"))
        if allowed:
            new = result.stdout.splitlines()[0].split()[1]
            content = self.git("--git-dir=" + str(self.root / "remote.git"), "show", new + ":" + filename)
            expected = upstream.copy(); expected[40] = feature[40]
            self.assertEqual(content, "".join(expected).strip())
            self.assertEqual(len(outputs), 1)
            self.assertEqual(json.loads(outputs[0].read_text())["head"], new)
        else:
            self.assertIn("shared non-documentation paths", result.stderr)
            self.assertEqual(outputs, [])

    # Claim: patch-identical, nonconflicting edits to shared executable prefix siblings
    # and non-documentation directories refuse before branch/verdict publication.
    def test_00_readme_prefix_code(self): self.shared_path("README_engine.py", False)
    def test_00_changelog_prefix_code(self): self.shared_path("CHANGELOG_engine.py", False)
    def test_00_extra_extension(self): self.shared_path("README.md.py", False)
    def test_00_nested_readme(self): self.shared_path("src/README.md", False)
    def test_00_board_code(self): self.shared_path(".task-board/engine.py", False)

    # Claim: exact root documentation names and docs/** retain the Path B exception,
    # preserve both nonconflicting edits, and publish a carried exact-head verdict.
    def test_01_root_readme(self): self.shared_path("README.md", True)
    def test_01_root_changelog(self): self.shared_path("CHANGELOG.rst", True)
    def test_01_docs(self): self.shared_path("docs/guide.txt", True)
