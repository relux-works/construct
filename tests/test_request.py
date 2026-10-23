import json
from helpers import Fixture


class RequestTests(Fixture):
    # Claim: exact event selection checks all authority fields and task before accepting it.
    def test_00_request_authority(self):
        expected = {"event_id": "$earlier", "sender": "build_project_orch", "room": "#project",
                    "kind": "request", "to": ["build_project_dev1"], "body": "Task TASK-260101-abcdef"}
        file = self.root / "event.json"
        for key, value, error in (("event_id", "$latest", "event id mismatch"), ("sender", "other", "untrusted request sender"),
                                  ("room", "#other", "wrong request room"), ("kind", "notice", "not a request"),
                                  ("to", ["other"], "not addressed"), ("body", "Task TASK-260101-ghijkl", "task id absent")):
            with self.subTest(key=key):
                file.write_text(json.dumps({**expected, key: value}))
                self.assertIn(error, self.cli("request-check", file, "$earlier", "TASK-260101-abcdef", "dev1", ok=False).stderr)
        file.write_text(json.dumps(expected))
        self.cli("request-check", file, "$earlier", "TASK-260101-abcdef", "dev1")
