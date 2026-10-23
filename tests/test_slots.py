import os
from pathlib import Path
import sys
from helpers import Fixture


class SlotTests(Fixture):
    # Claim: no command is not success; an admitted command keeps its exact exit code and environment.
    def test_00_exit_and_environment(self):
        self.assertIn("needs a command", self.cli("slot", ok=False).stderr)
        p = self.popen("slot", sys.executable, "-c", "import os,sys;sys.exit(17 if os.environ['GIT_DIR'].endswith('forbidden.git') else 18)")
        out, err = p.communicate(timeout=8)
        self.assertEqual(p.returncode, 17, err)
        self.assertIn("wait_seconds=", err); self.assertIn("execution_seconds=", err)
        self.assertFalse(list((self.root / "work/.construct-slots/queue").iterdir()))
        self.cli("slot", "true")

    # Claim: later queued callers cannot overtake an earlier live ticket; capacity is held until child exit.
    def test_01_fifo_capacity_and_stale_reclaim(self):
        queue = self.root / "work/.construct-slots/queue"
        queue.mkdir(parents=True)
        (queue / "00000000000000000000-dead").write_text("stale unlocked owner")
        order = self.root / "order"
        gate = self.root / "release"
        script = "import pathlib,time,sys;p=pathlib.Path(sys.argv[1]);p.open('a').write(sys.argv[2]+'\\n');g=pathlib.Path(sys.argv[3]);\nwhile not g.exists():time.sleep(.01)"
        first = self.popen("slot", sys.executable, "-c", script, order, "first", gate)
        self.wait_for(lambda: order.exists())
        self.assertFalse((queue / "00000000000000000000-dead").exists())
        second = self.popen("slot", sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).open('a').write('second\\n')", order)
        self.wait_for(lambda: len(list(queue.iterdir())) == 2)
        third = self.popen("slot", sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).open('a').write('third\\n')", order)
        self.wait_for(lambda: len(list(queue.iterdir())) == 3)
        self.assertEqual(order.read_text(), "first\n")
        gate.touch()
        for p in (first, second, third):
            _, err = p.communicate(timeout=8); self.assertEqual(p.returncode, 0, err)
        self.assertEqual(order.read_text(), "first\nsecond\nthird\n")
        self.assertFalse(list(queue.iterdir()))

    # Claim: two configured slots admit two live commands but a third waits.
    def test_10_two_slots(self):
        self.cfg["slots"] = 2; self.save()
        gate = self.root / "gate"
        script = "import pathlib,time,sys;pathlib.Path(sys.argv[1]).touch();g=pathlib.Path(sys.argv[2]);\nwhile not g.exists():time.sleep(.01)"
        one = self.popen("slot", sys.executable, "-c", script, self.root / "one", gate)
        two = self.popen("slot", sys.executable, "-c", script, self.root / "two", gate)
        self.wait_for(lambda: (self.root / "one").exists() and (self.root / "two").exists())
        three = self.popen("slot", sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", self.root / "three")
        queue = self.root / "work/.construct-slots/queue"
        self.wait_for(lambda: len(list(queue.iterdir())) == 3)
        self.assertFalse((self.root / "three").exists())
        gate.touch()
        for p in (one, two, three):
            _, err = p.communicate(timeout=8); self.assertEqual(p.returncode, 0, err)
        self.assertTrue((self.root / "three").exists())

    # Claim: the admitted child inherits the live ticket descriptor; this checks delivery, not a controller kill/restart.
    def test_11_child_lease(self):
        queue = self.root / "work/.construct-slots/queue"
        script = """import os,pathlib,sys
ticket=next(pathlib.Path(sys.argv[1]).iterdir()).stat()
found=False
for fd in range(3,256):
 try:
  s=os.fstat(fd)
  found=found or (s.st_ino,s.st_dev)==(ticket.st_ino,ticket.st_dev)
 except OSError: pass
sys.exit(0 if found else 19)
"""
        self.cli("slot", sys.executable, "-c", script, queue)
        self.assertFalse(list(queue.iterdir()))
