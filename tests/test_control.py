"""The control daemon end to end over its Unix socket, with a fake Session (no probes, no models).

    python3 -m unittest discover -s loop/tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

LOOP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOOP / "aqua"))

import control  # noqa: E402
from session import SessionError  # noqa: E402

SCRIPT = LOOP / "skills" / "aqua" / "scripts" / "aqua"


class FakeSession:
    def __init__(self):
        self.run_id, self.closed, self.driver, self.calls = None, False, "x", []

    def start(self, rounds, fresh):
        self.run_id = "run1"
        self.run_dir = Path("/tmp/run1")
        return {"run_id": self.run_id, "rounds": rounds}

    def status(self):
        return {"run_id": self.run_id}

    def delegate(self, role, *rest):
        time.sleep(2)                          # slower than the (minimum 1 s) inline wait
        return {"role": role, "brief": rest[0]}

    def commit(self, cid):
        raise SessionError("not approved")

    def close(self):
        self.closed = True


class ControlTest(unittest.TestCase):
    def test_script_talks_to_daemon(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sock = Path(tmp.name) / "a.sock"
        ctl = control.Controller(FakeSession)
        control.INLINE_WAIT = 0.2
        thread = threading.Thread(target=control.serve, args=(ctl, sock, 60), daemon=True)
        thread.start()
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.05)
        env = dict(os.environ, AQUA_SOCKET=str(sock))

        def aqua(*args, stdin=None):
            r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, input=stdin,
                               capture_output=True, text=True, timeout=30)
            return r.returncode, json.loads(r.stdout)

        self.assertEqual(aqua("start", "--rounds", "2"), (0, {"run_id": "run1", "rounds": 2}))
        code, out = aqua("delegate", "reviewer", "--brief", "-", stdin="multi\nline")
        self.assertEqual((code, out["status"]), (0, "running"))              # slow call -> job
        job = out["job"]
        code, out = aqua("commit", "c1")                                   # write job while the delegate runs
        self.assertEqual(code, 1)
        self.assertIn("aqua wait", out["error"])
        code, out = aqua("wait", job)
        self.assertEqual(out[job], {"role": "reviewer", "brief": "multi\nline"})
        code, out = aqua("commit", "c1")
        self.assertEqual((code, out), (1, {"error": "not approved"}))        # refusals exit 1
        self.assertEqual(aqua("stop")[0], 0)
        thread.join(10)
        self.assertTrue(ctl.s.closed)                                       # cleanup on exit
        self.assertFalse(sock.exists())


if __name__ == "__main__":
    unittest.main()
