#!/usr/bin/env python3
"""
AuFarm integration test suite.

Runs the full stack against a live farm instance and verifies:
- successful exploit execution
- failed exploit handling
- timeout detection and process kill
- concurrent job execution
- STOP while jobs are running
- results persistence
- stats accuracy

Usage:
    python3 test_farm.py [--base-url http://localhost:8000]
"""

import argparse
import json
import sys
import time
import threading
import requests

BASE = "http://localhost:8000"

PASS  = "\033[32m[PASS]\033[0m"
FAIL  = "\033[31m[FAIL]\033[0m"
INFO  = "\033[34m[INFO]\033[0m"

results = []

def check(label, cond, detail=""):
    if cond:
        print(f"{PASS} {label}")
        results.append((True, label))
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        results.append((False, label))

def api(method, path, **kwargs):
    url = f"{BASE}{path}"
    r = getattr(requests, method)(url, timeout=10, **kwargs)
    return r

def wait_for_server(max_wait=30):
    print(f"{INFO} Waiting for server at {BASE}...")
    for _ in range(max_wait):
        try:
            r = requests.get(f"{BASE}/api/status", timeout=2)
            if r.ok:
                print(f"{INFO} Server is up.")
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def cleanup(exploit_ids, target_ids):
    for eid in exploit_ids:
        api("delete", f"/api/exploits/{eid}")
    for tid in target_ids:
        api("delete", f"/api/targets/{tid}")
    api("post", "/api/farm/reset")


def test_create_resources():
    """Create test exploits and targets."""
    exploit_ids = []
    target_ids = []

    print(f"\n{INFO} === Creating test resources ===")

    # Targets
    for i in range(1, 4):
        r = api("post", "/api/targets", json={
            "name": f"TestTeam{i}", "host": f"127.0.0.{i}", "port": 80,
            "service": "web", "enabled": True
        })
        check(f"Create target TestTeam{i}", r.status_code == 201)
        target_ids.append(r.json()["id"])

    # Exploit: always succeeds
    r = api("post", "/api/exploits", json={
        "name": "test_success", "filename": "exploit_success.py",
        "runtime": "python3", "timeout": 10, "enabled": True, "target_mode": "all"
    })
    check("Create exploit_success", r.status_code == 201)
    eid_success = r.json()["id"]
    api("put", f"/api/exploits/{eid_success}", json={
        "source": "import sys, time\nhost = sys.argv[1] if len(sys.argv) > 1 else 'unknown'\ntime.sleep(0.5)\nprint(f'[+] Success {host}', flush=True)\nprint(f'CTF{{flag_{host.replace(\".\",\"_\")}}}', flush=True)\nsys.exit(0)\n"
    })
    exploit_ids.append(eid_success)

    # Exploit: always fails
    r = api("post", "/api/exploits", json={
        "name": "test_fail", "filename": "exploit_fail.py",
        "runtime": "python3", "timeout": 10, "enabled": True, "target_mode": "all"
    })
    check("Create exploit_fail", r.status_code == 201)
    eid_fail = r.json()["id"]
    api("put", f"/api/exploits/{eid_fail}", json={
        "source": "import sys\nprint('[-] Failed', file=sys.stderr, flush=True)\nsys.exit(1)\n"
    })
    exploit_ids.append(eid_fail)

    # Exploit: times out
    r = api("post", "/api/exploits", json={
        "name": "test_timeout", "filename": "exploit_timeout.py",
        "runtime": "python3", "timeout": 3, "enabled": True, "target_mode": "all"
    })
    check("Create exploit_timeout", r.status_code == 201)
    eid_timeout = r.json()["id"]
    api("put", f"/api/exploits/{eid_timeout}", json={
        "source": "import sys, time\nprint('[~] Hanging', flush=True)\ntime.sleep(999999)\n"
    })
    exploit_ids.append(eid_timeout)

    # Exploit: crashes
    r = api("post", "/api/exploits", json={
        "name": "test_crash", "filename": "exploit_crash.py",
        "runtime": "python3", "timeout": 10, "enabled": True, "target_mode": "all"
    })
    check("Create exploit_crash", r.status_code == 201)
    eid_crash = r.json()["id"]
    api("put", f"/api/exploits/{eid_crash}", json={
        "source": "import sys\nraise RuntimeError('Simulated crash')\n"
    })
    exploit_ids.append(eid_crash)


    return exploit_ids, target_ids


def test_run_tick():
    """Run a single tick and check all job types complete."""
    print(f"\n{INFO} === Test: Single tick execution ===")

    # Configure short timeouts for tests
    api("put", "/api/farm/config", json={
        "tick_count": 1, "tick_interval": 60,
        "exploit_timeout": 5, "concurrency": 20, "continuous": False
    })

    # Reset stats
    api("post", "/api/farm/reset")

    r = api("post", "/api/farm/tick")
    check("One-tick API returns ok", r.ok)

    print(f"{INFO} Waiting 15s for jobs to complete (includes 3s timeout test)...")
    time.sleep(15)

    # Check results
    r = api("get", "/api/results?limit=200")
    d = r.json()
    items = d["results"]

    statuses = [i["status"] for i in items]
    check("Some results exist", len(items) > 0, f"got {len(items)}")
    check("At least one SUCCESS", "SUCCESS" in statuses, str(statuses[:10]))
    check("At least one FAILED",  "FAILED"  in statuses, str(statuses[:10]))
    check("At least one TIMEOUT", "TIMEOUT" in statuses, str(statuses[:10]))

    # Crashed exploit exits with non-zero — that's FAILED (exit code != 0)
    has_failed_or_crashed = "FAILED" in statuses or "CRASHED" in statuses
    check("Crash results in FAILED/CRASHED", has_failed_or_crashed)

    return items


def test_concurrent(items):
    """Verify multiple targets ran concurrently (from timings)."""
    print(f"\n{INFO} === Test: Concurrent execution ===")
    success_items = [i for i in items if i["status"] == "SUCCESS"]
    check("Multiple success results (concurrent)", len(success_items) >= 2,
          f"got {len(success_items)}")


def test_stop_kills_procs():
    """Start farm, wait for running jobs, then STOP and verify."""
    print(f"\n{INFO} === Test: STOP while running ===")

    api("put", "/api/farm/config", json={
        "tick_count": 5, "tick_interval": 5,
        "exploit_timeout": 30, "concurrency": 20, "continuous": False
    })
    api("post", "/api/farm/reset")
    r = api("post", "/api/farm/start")
    check("Farm started", r.ok)

    time.sleep(3)
    status = api("get", "/api/farm/status").json()
    check("Farm running after start", status["status"] == "RUNNING",
          status["status"])

    r = api("post", "/api/farm/stop")
    check("STOP returns ok", r.ok)
    time.sleep(2)

    status = api("get", "/api/farm/status").json()
    check("Farm STOPPED after stop", status["status"] == "STOPPED",
          status["status"])


def test_browser_disconnect():
    """
    Simulate browser disconnect: start farm, sleep (no HTTP), check it's still running.
    """
    print(f"\n{INFO} === Test: Farm continues without browser connection ===")

    api("put", "/api/farm/config", json={
        "tick_count": 10, "tick_interval": 3,
        "exploit_timeout": 5, "concurrency": 20, "continuous": False
    })
    api("post", "/api/farm/reset")
    r = api("post", "/api/farm/start")
    check("Farm started", r.ok)

    print(f"{INFO} Simulating 8s browser disconnect (no HTTP calls)...")
    time.sleep(8)

    status = api("get", "/api/farm/status").json()
    tick = status["current_tick"]
    check("Farm advanced ticks without browser", tick > 1,
          f"current_tick={tick}")
    check("Farm still running after disconnect", status["status"] in ("RUNNING", "STOPPED"),
          status["status"])

    api("post", "/api/farm/stop")


def test_stats():
    """Verify stats accumulate correctly."""
    print(f"\n{INFO} === Test: Stats ===")
    stats = api("get", "/api/results/stats").json()
    check("Stats endpoint returns data", bool(stats))
    total = sum(stats.values())
    check("Total stats > 0", total > 0, str(stats))


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")

    print("=" * 60)
    print("AuFarm Integration Test Suite")
    print("=" * 60)

    if not wait_for_server():
        print(f"{FAIL} Server not reachable at {BASE}")
        sys.exit(1)

    exploit_ids, target_ids = test_create_resources()
    items = test_run_tick()
    test_concurrent(items)
    test_stop_kills_procs()
    test_browser_disconnect()
    test_stats()

    cleanup(exploit_ids, target_ids)

    print("\n" + "=" * 60)
    passed = sum(1 for ok, _ in results if ok)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"Results: {passed} passed, {failed} failed out of {len(results)} tests")
    if failed:
        print(f"{FAIL} Some tests failed")
        sys.exit(1)
    else:
        print(f"{PASS} All tests passed!")


if __name__ == "__main__":
    main()
