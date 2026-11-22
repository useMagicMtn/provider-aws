#!/usr/bin/env python3
"""Exercise the real pipe transport and agent locally, without Kubernetes."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

assert sys.argv[1] == "--kubeconfig"
with open(sys.argv[2]) as source:
    config = json.load(source)
assert config["users"][0]["user"]["token"] == "secret-token"
assert "secret-token" not in str(sys.argv)
assert sys.argv[3] == "--request-timeout=40s"
assert sys.argv[4] == "exec"
assert sys.argv[6:] == ["--container=sandbox", "--stdin", "--", "python3", "/opt/magicmtn-agent.py", "--stdio"]
request = json.load(sys.stdin)
if request["command"] == "oversized":
    sys.stdout.write("x" * (128 * 1024 + 1))
elif request["command"] == "fail":
    sys.stderr.write("secret transport error")
    sys.exit(1)
else:
    path = Path(__file__).resolve().parents[2] / "sandbox/agent.py"
    spec = importlib.util.spec_from_file_location("agent", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    with tempfile.TemporaryDirectory() as directory:
        json.dump(agent.execute(request, cwd=directory), sys.stdout)
