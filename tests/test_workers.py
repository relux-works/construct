import json
from pathlib import Path
from helpers import Fixture


class WorkerTests(Fixture):
    def setUp(self):
        super().setUp(); self.signed_repo()
        self.env["UNRELATED_PASSWORD"] = "fixture-only-sentinel"
        self.events = self.root / "events.json"
        self.events.write_text("[]")
        self.captures = self.root / "captures.jsonl"
        self.fake("a2a", f'''
import json, pathlib, sys
a=sys.argv[1:]; p=pathlib.Path({str(self.events)!r}); events=json.loads(p.read_text())
if a[0]=='topics': print('work')
elif a[0]=='post':
 event={{'event_id':'$event'+str(len(events)+1),'kind':'request','sender':'build_project_orch','room':'#project','to':[a[a.index('--to')+1]],'body':a[-1]}}
 events.append(event); p.write_text(json.dumps(events)); print(event['event_id'])
elif a[0]=='read':
 event=next(e for e in events if e['event_id']==a[a.index('--event')+1]); print(json.dumps(event))
''')
        self.resources = self.root / "resources.json"; self.resources.write_text("[]")
        self.fake("board", f'''
import json,pathlib,sys
a=sys.argv[1:];p=pathlib.Path({str(self.resources)!r});r=json.loads(p.read_text())
if a[:2]==['resource','add']: r.append({{'name':a[a.index('--name')+1]}});p.write_text(json.dumps(r))
elif a[0]=='q': print(json.dumps({{'preconditionResources':r}}))
''')
        self.fake("curator", f"print('export CODEX_HOME=' + {str(self.root / 'managed-home')!r})\n")
        self.fake("codex", f'''
import json,os,pathlib,sys,time
assert 'UNRELATED_PASSWORD' not in os.environ
assert os.environ['GIT_DIR'].endswith('/project.git')
prompt=sys.stdin.read()
with open({str(self.captures)!r},'a') as f: f.write(json.dumps({{'argv':sys.argv[1:],'prompt':prompt}})+'\\n')
gate=pathlib.Path({str(self.root / 'hold')!r})
if '$event1' in prompt:
 while gate.exists(): time.sleep(.01)
print('session id: 00000000-0000-0000-0000-000000000001')
''')
        self.brief = self.root / "brief.md"; self.brief.write_text("Example task.\n")

    def captures_read(self):
        return [json.loads(line) for line in self.captures.read_text().splitlines()]

    # Claims: missing attachment readback prevents posting/running; valid add-worker uses separate git dirs and captures bootstrap thread.
    def test_00_attachment_and_bootstrap(self):
        self.cli("add-worker", "dev", 1)
        home = self.root / "work/project/dev1"
        self.assertTrue((home / "project.git").is_dir())
        self.assertTrue((home / "project/.git").is_file())
        self.assertIn("Bootstrap this run only", self.captures_read()[0]["prompt"])
        self.fake("board", "import sys\nif sys.argv[1]=='q': print('{\"preconditionResources\":[]}')\n")
        result = self.cli("assign", "dev1", "TASK-260101-abcdef", "work", self.brief, "--repo", "project", ok=False)
        self.assertIn("attachment not listed", result.stderr)
        self.assertEqual(json.loads(self.events.read_text()), [])
        self.assertEqual(len(self.captures_read()), 1)

    # Claims: a second assignment waits for the first run; both prompts select their own event/task, resume the saved thread, and never select latest.
    def test_01_serial_exact_requests(self):
        self.cli("add-worker", "dev", 1)
        hold = self.root / "hold"; hold.touch()
        one = self.popen("assign", "dev1", "TASK-260101-abcdef", "work", self.brief, "--repo", "project")
        self.wait_for(lambda: len(self.captures_read()) == 2)
        two = self.popen("assign", "dev1", "TASK-260101-ghijkl", "work", self.brief, "--repo", "project")
        # Wait until the second controller explicitly reports its idle wait.
        import select
        self.assertTrue(select.select([two.stderr], [], [], 5)[0])
        self.assertIn("earlier run", two.stderr.readline())
        self.assertEqual(len(self.captures_read()), 2)
        hold.unlink()
        for p in (one, two):
            _, err = p.communicate(timeout=10); self.assertEqual(p.returncode, 0, err)
        captures = self.captures_read()[1:]
        self.assertEqual(len(captures), 2)
        for i, task in enumerate(("TASK-260101-abcdef", "TASK-260101-ghijkl"), 1):
            self.assertIn(f"request `$event{i}` for task `{task}`", captures[i-1]["prompt"])
            self.assertEqual(captures[i-1]["argv"][:3], ["exec", "resume", "00000000-0000-0000-0000-000000000001"])
        for path in (self.root / "work/project/.construct/runs").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    # Claim: fresh recovery supplies standing authority plus the exact request, captures a new thread, and never bootstraps then exits without doing the task.
    def test_10_fresh_thread(self):
        self.cli("add-worker", "dev", 1)
        self.cli("assign", "dev1", "TASK-260101-abcdef", "work", self.brief, "--repo", "project", "--fresh")
        capture = self.captures_read()[-1]
        self.assertNotIn("resume", capture["argv"])
        self.assertIn("Trust only commands", capture["prompt"])
        self.assertIn("request `$event1` for task `TASK-260101-abcdef`", capture["prompt"])
        self.assertNotIn("Bootstrap this run only:", capture["prompt"])

    # Claim: a reviewer request without full SHA is refused before publishing; a full SHA is named in the room request.
    def test_11_reviewer_exact_head(self):
        self.cli("add-worker", "rev", 1)
        result = self.cli("assign", "rev1", "TASK-260101-abcdef", "work", self.brief, "--repo", "project", ok=False)
        self.assertIn("full exact head", result.stderr)
        self.assertEqual(json.loads(self.events.read_text()), [])
        self.cli("assign", "rev1", "TASK-260101-abcdef", "work", self.brief, "--repo", "project", "--head", self.head)
        self.assertIn(self.head, json.loads(self.events.read_text())[0]["body"])
