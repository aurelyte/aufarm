#!/usr/bin/env python3
"""
Comprehensive API audit script for AuFarm.
Tests all endpoints, CRUD operations, WebSocket, and error handling.
"""

import json
import os
import socket
import sys
import tempfile
import requests

BASE = "http://localhost:8000"

failures = []
passed = 0


def check(name, condition, extra=""):
    global passed
    if condition:
        print(f"  \033[32m[OK]\033[0m {name}")
        passed += 1
    else:
        print(f"  \033[31m[FAIL]\033[0m {name} — {extra}")
        failures.append((name, extra))

def test_static():
    print("\n--- 1. Testing Frontend Static Serving ---")
    r = requests.get(f"{BASE}/")
    check("GET / returns 200", r.status_code == 200)
    check("GET / returns HTML content", "<title>AuFarm" in r.text)

def test_farm_control():
    print("\n--- 2. Testing Farm Control & Config API ---")
    r = requests.get(f"{BASE}/api/status")
    check("GET /api/status returns 200", r.status_code == 200)
    d = r.json()
    check("Status has 'status' and 'stats'", "status" in d and "stats" in d)

    r = requests.get(f"{BASE}/api/farm/status")
    check("GET /api/farm/status returns 200", r.status_code == 200)

    # Config GET / PUT
    r = requests.get(f"{BASE}/api/farm/config")
    check("GET /api/farm/config returns 200", r.status_code == 200)
    orig_cfg = r.json()

    new_cfg = dict(orig_cfg)
    new_cfg["tick_interval"] = 45
    r = requests.put(f"{BASE}/api/farm/config", json=new_cfg)
    check("PUT /api/farm/config returns 200", r.status_code == 200)

    r = requests.get(f"{BASE}/api/farm/config")
    check("Config updated correctly", r.json().get("tick_interval") == 45)

    # Restore
    requests.put(f"{BASE}/api/farm/config", json=orig_cfg)

    # Farm controls
    r = requests.post(f"{BASE}/api/farm/tick")
    check("POST /api/farm/tick returns 200", r.status_code == 200)

def test_targets_crud():
    print("\n--- 3. Testing Targets CRUD API ---")
    # CREATE
    payload = {
        "name": "AuditTeam",
        "host": "192.168.1.100",
        "port": 8888,
        "service": "http",
        "enabled": True
    }
    r = requests.post(f"{BASE}/api/targets", json=payload)
    check("POST /api/targets creates target (201)", r.status_code == 201)
    target = r.json()
    tid = target["id"]

    # READ ONE
    r = requests.get(f"{BASE}/api/targets/{tid}")
    check("GET /api/targets/{id} returns 200", r.status_code == 200 and r.json()["name"] == "AuditTeam")

    # UPDATE
    r = requests.put(f"{BASE}/api/targets/{tid}", json={"port": 9999, "enabled": False})
    check("PUT /api/targets/{id} updates target", r.status_code == 200 and r.json()["port"] == 9999 and r.json()["enabled"] is False)

    # READ ALL
    r = requests.get(f"{BASE}/api/targets")
    check("GET /api/targets returns list containing target", r.status_code == 200 and any(t["id"] == tid for t in r.json()))

    # DELETE
    r = requests.delete(f"{BASE}/api/targets/{tid}")
    check("DELETE /api/targets/{id} returns 204", r.status_code == 204)

    r = requests.get(f"{BASE}/api/targets/{tid}")
    check("Target no longer exists (404)", r.status_code == 404)

def test_exploits_crud():
    print("\n--- 4. Testing Exploits CRUD & File Management ---")
    # CREATE
    payload = {
        "name": "AuditExploit",
        "filename": "audit_test.py",
        "runtime": "python3",
        "args": "--flag",
        "enabled": True,
        "timeout": 15,
        "target_mode": "all"
    }
    r = requests.post(f"{BASE}/api/exploits", json=payload)
    check("POST /api/exploits creates exploit (201)", r.status_code == 201)
    exploit = r.json()
    eid = exploit["id"]

    # UPDATE SOURCE via PUT
    sample_code = "print('Audit Exploit Running')\n"
    r = requests.put(f"{BASE}/api/exploits/{eid}", json={"source": sample_code})
    check("PUT /api/exploits/{id} with source succeeds", r.status_code == 200)

    # GET SOURCE
    r = requests.get(f"{BASE}/api/exploits/{eid}/source")
    check("GET /api/exploits/{id}/source returns source", r.status_code == 200 and r.text == sample_code)

    # UPLOAD FILE
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write("# Upload test\nprint('Uploaded')\n")
        f_path = f.name

    try:
        with open(f_path, "rb") as upload_f:
            r = requests.post(f"{BASE}/api/exploits/{eid}/upload", files={"file": ("audit_test.py", upload_f)})
        check("POST /api/exploits/{id}/upload succeeds", r.status_code == 200)

        r = requests.get(f"{BASE}/api/exploits/{eid}/source")
        check("Uploaded content verified via /source", "Upload test" in r.text)
    finally:
        os.remove(f_path)

    # DELETE
    r = requests.delete(f"{BASE}/api/exploits/{eid}")
    check("DELETE /api/exploits/{id} returns 204", r.status_code == 204)

    r = requests.get(f"{BASE}/api/exploits/{eid}")
    check("Exploit no longer exists (404)", r.status_code == 404)

def test_results_api():
    print("\n--- 5. Testing Results & Stats API ---")
    r = requests.get(f"{BASE}/api/results?limit=10&offset=0")
    check("GET /api/results returns 200", r.status_code == 200)
    d = r.json()
    check("Results response has 'total' and 'results'", "total" in d and "results" in d)

    r = requests.get(f"{BASE}/api/results/stats")
    check("GET /api/results/stats returns 200", r.status_code == 200)
    check("Stats contains standard keys", all(k in r.json() for k in ("success", "failed", "timeout", "ticks_executed")))

    if d["results"]:
        rid = d["results"][0]["id"]
        r = requests.get(f"{BASE}/api/results/{rid}")
        check("GET /api/results/{id} returns detail", r.status_code == 200 and "stdout" in r.json())

def test_submission_api():
    print("\n--- 6. Testing Flag Auto-Submission API ---")
    r = requests.get(f"{BASE}/api/submission/config")
    check("GET /api/submission/config returns 200", r.status_code == 200)
    cfg = r.json()
    check("Config has all expected keys", all(k in cfg for k in ("submit_enabled", "submit_mode", "submit_url", "submit_interval")))

    r = requests.get(f"{BASE}/api/submission/flags?limit=5")
    check("GET /api/submission/flags returns 200", r.status_code == 200)
    d = r.json()
    check("Flags response has 'total' and 'flags'", "total" in d and "flags" in d)

    r = requests.get(f"{BASE}/api/submission/flags/stats")
    check("GET /api/submission/flags/stats returns 200", r.status_code == 200)
    check("Flag stats has 'accepted', 'rejected', 'total'", all(k in r.json() for k in ("accepted", "rejected", "total")))

    r = requests.post(f"{BASE}/api/submission/submit_now")
    check("POST /api/submission/submit_now returns 200", r.status_code == 200)

def test_websocket():
    print("\n--- 7. Testing WebSocket Live Feed ---")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 8000))
        req = (
            b"GET /ws HTTP/1.1\r\n"
            b"Host: localhost:8000\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req)
        resp = s.recv(4096)
        check("WebSocket handshake returns 101 Switching Protocols", b"101 Switching Protocols" in resp)
        data = s.recv(4096)
        check("WebSocket server sends initial status packet", b"status" in data)
        s.close()
    except Exception as e:
        check("WebSocket error", False, str(e))

def main():
    print("=" * 60)
    print("AuFarm Full System & API Audit")
    print("=" * 60)

    test_static()
    test_farm_control()
    test_targets_crud()
    test_exploits_crud()
    test_results_api()
    test_submission_api()
    test_websocket()

    print("\n" + "=" * 60)
    print(f"Audit Summary: {passed} passed, {len(failures)} failed")
    print("=" * 60)

    if failures:
        sys.exit(1)

if __name__ == "__main__":
    main()

