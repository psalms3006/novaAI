"""Minimal test — verify LiveManager API without nova import chain."""
import sys, os

# Test 1: Import
from desk.live_session import LiveManager, LiveState
print("1. Import OK")

# Test 2: Instantiate
mgr = LiveManager()
print("2. Instantiate OK")

# Test 3: Status
s = mgr.status()
assert s["state"] == "idle", f"expected idle, got {s['state']}"
assert "model" in s and "voice" in s
print(f"3. Status OK: state={s['state']} model={s['model']} voice={s['voice']}")

# Test 4: Start without key
orig = os.environ.get("GEMINI_API_KEY")
os.environ.pop("GEMINI_API_KEY", None)
r = mgr.start()
assert not r["ok"], f"expected ok=False, got {r}"
assert "reason" in r
print(f"4. Start no key OK: reason={r['reason']}")
if orig:
    os.environ["GEMINI_API_KEY"] = orig

# Test 5: Subscribe/unsubscribe
q = mgr.subscribe()
assert not q.empty() is False
mgr.unsubscribe(q)
mgr.unsubscribe(q)
print("5. Subscribe/unsubscribe OK")

# Test 6: Stop idle
r = mgr.stop()
assert r["ok"]
print("6. Stop idle OK")

# Test 7: Send text idle
r = mgr.send_text("hello")
assert not r["ok"]
print("7. Send text idle OK")

print("\nALL TESTS PASSED")
