"""Stands in for mobile_rpa.worker in tests: echoes a plan, one action, then a result."""
import json
import sys
import time

job = json.loads(sys.stdin.read())
say = lambda **e: print("@@MRPA " + json.dumps(e), flush=True)  # noqa: E731
print("library noise that is not an event", flush=True)
say(kind="plan", text="1. do " + job["instruction"], steps=0)
say(kind="action", text="tap", steps=1)
if "hang" in job["instruction"]:
    time.sleep(60)
say(kind="done", text="finished " + job["serial"], success="fail" not in job["instruction"], steps=1)
