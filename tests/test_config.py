import json
from helpers import Fixture


class ConfigTests(Fixture):
    # Claims: malformed config is rejected before layout writes; valid controls stay usable.
    def test_00_reject_invalid_config(self):
        original = json.loads(json.dumps(self.cfg))
        cases = [("slots", 0, "slots must be"), ("slots", True, "slots must be"),
                 ("lane", "../escape", "invalid lane"), ("identity_prefix", "Bad Name", "invalid identity_prefix"),
                 ("room", "other", "invalid room"), ("root", "", "missing root"),
                 ("repos", {}, "repos must"), ("slot_poll_seconds", 0, "invalid slot_poll"),
                 ("sandbox", "anything", "invalid sandbox"), ("allow_no_checks", "yes", "invalid allow_no_checks")]
        cases.extend([("roles", {"dev": {"model": "", "effort": "high"},
                                 "rev": {"model": "review-model", "effort": "medium"}}, "missing model"),
                      ("roles", {}, "missing model"), ("tools", {"git": ""}, "invalid tools")])
        for key, value, error in cases:
            with self.subTest(key=key, value=value):
                self.cfg = {**original, key: value}; self.save()
                self.assertIn(error, self.cli("init", ok=False).stderr)
                self.assertFalse((self.root / "work").exists())
        self.cfg = original; self.save()
        self.cli("init")
        self.assertTrue((self.root / "work/project/.construct/runs").is_dir())

    # Claim: repository names, URLs and base refs cannot escape layout or become git options.
    def test_02_repo_guards(self):
        original = dict(self.cfg["repos"]["project"])
        for repos, message in (({"../escape": original}, "invalid repo"),
                               ({"project": {**original, "url": "--evil"}}, "invalid repo URL"),
                               ({"project": {**original, "base": "../main"}}, "invalid repo base")):
            self.cfg["repos"] = repos; self.save()
            self.assertIn(message, self.cli("init", ok=False).stderr)
            self.assertFalse((self.root / "work").exists())
        self.cfg["repos"] = {"project": original}; self.save(); self.cli("init")

    # Claim: only medium, xhigh and max reviewer efforts pass; developer high remains valid.
    def test_01_reviewer_effort_guard(self):
        for effort in ("low", "high"):
            self.cfg["roles"]["rev"]["effort"] = effort; self.save()
            self.assertIn("reviewer effort", self.cli("config-check", ok=False).stderr)
        for effort in ("medium", "xhigh", "max"):
            self.cfg["roles"]["rev"]["effort"] = effort; self.save()
            self.cli("config-check")

    # Claim: JSON shell-looking strings remain data; relative config paths resolve beside config.
    def test_10_no_shell_evaluation(self):
        self.cfg["root"] = "$(touch injected) space"
        self.save(); self.cli("init")
        self.assertTrue((self.root / "$(touch injected) space/project").is_dir())
        self.assertFalse((self.root / "injected").exists())
