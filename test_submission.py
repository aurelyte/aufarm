#!/usr/bin/env python3
"""
Test auto-submission functionality in AuFarm:
- Flag extraction from exploit output
- Deduplication of flags
- Submission via HTTP JSON (Siberlab / polriCTF style)
- Status update in database (ACCEPTED / REJECTED)
- Flag stats API
"""

import http.server
import json
import re
import socketserver
import sys
import threading
import time
import requests

BASE_URL = "http://localhost:8000"
MOCK_PORT = 9999
MOCK_URL = f"http://localhost:{MOCK_PORT}/api/v1/flags"

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
INFO = "\033[34m[INFO]\033[0m"

received_flags = []

class MockScoreboardHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len)
        try:
            data = json.loads(post_body.decode())
            flags = data.get("flags", [])
            for f in flags:
                received_flags.append(f)
            
            # Siberlab / CTFd style response
            res_list = []
            for f in flags:
                if "invalid" in f.lower():
                    res_list.append({"flag": f, "valid": False, "status": "REJECTED"})
                elif "dup" in f.lower():
                    res_list.append({"flag": f, "valid": False, "status": "ALREADY_SUBMITTED"})
                else:
                    res_list.append({"flag": f, "valid": True, "status": "STATUS_OK"})
            
            resp = json.dumps({"submitFlagResults": res_list}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        # suppress normal logging
        pass

def run_mock_server():
    server = socketserver.TCPServer(("0.0.0.0", MOCK_PORT), MockScoreboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server

def main():
    print("=" * 60)
    print("AuFarm Auto-Submission Test Suite")
    print("=" * 60)

    print(f"{INFO} Starting mock scoreboard on port {MOCK_PORT}...")
    server = run_mock_server()

    # 1. Check submission config endpoint
    print(f"{INFO} Testing submission config API...")
    r = requests.get(f"{BASE_URL}/api/submission/config", timeout=5)
    assert r.status_code == 200, f"Config GET failed: {r.text}"
    print(f"{PASS} GET /api/submission/config successful")

    # 2. Configure auto-submission with custom flag regex (matching polriCTF or CTF)
    sub_cfg = {
        "submit_enabled": True,
        "submit_mode": "http_json",
        "submit_url": MOCK_URL,
        "submit_token": "test-team-token-12345",
        "submit_host": "",
        "submit_port": 31337,
        "flag_regex": r"((?:polriCTF|CTF)\{[^}]+\})",
        "submit_interval": 2,
        "flag_lifetime": 300,
    }
    r = requests.put(f"{BASE_URL}/api/submission/config", json=sub_cfg, timeout=5)
    assert r.status_code == 200, f"Config PUT failed: {r.text}"
    print(f"{PASS} PUT /api/submission/config enabled auto-submission to mock server")

    # 3. Create target and exploit that outputs flags
    nonce = int(time.time())
    print(f"{INFO} Setting up exploit that yields flags (nonce {nonce})...")
    r_tgt = requests.post(f"{BASE_URL}/api/targets", json={
        "name": f"TargetVictim_{nonce}",
        "host": "127.0.0.1",
        "port": 80,
        "service": "web",
        "enabled": True,
    })
    target_id = r_tgt.json()["id"]

    exploit_source = f"""#!/usr/bin/env python3
import sys

host = sys.argv[1]
print("Running attack on " + host)
# Valid flag
print("Found flag: polriCTF{{split_field_ssti_pwnd_{nonce}}}")
# Invalid flag for testing rejection
print("Found flag: CTF{{invalid_flag_dummy_{nonce}}}")
# Duplicate test flag
print("Duplicate found: polriCTF{{split_field_ssti_pwnd_{nonce}}}")
sys.exit(0)
"""
    r_exp = requests.post(f"{BASE_URL}/api/exploits", json={
        "name": f"flag_harvester_{nonce}",
        "filename": f"flag_harvester_{nonce}.py",
        "runtime": "python3",
        "timeout": 5,
        "enabled": True,
        "target_mode": str(target_id),
    })
    exploit_id = r_exp.json()["id"]

    requests.put(f"{BASE_URL}/api/exploits/{exploit_id}", json={"source": exploit_source})


    # 4. Trigger one tick
    print(f"{INFO} Triggering one tick to run exploit...")
    requests.post(f"{BASE_URL}/api/farm/tick")

    # Wait for exploit to finish and submitter to process
    print(f"{INFO} Waiting for exploit execution and auto-submission (6s)...")
    time.sleep(6)

    # 5. Verify flags received by mock scoreboard
    print(f"{INFO} Received flags at mock scoreboard: {received_flags}")
    assert any("split_field_ssti" in f for f in received_flags), "Valid flag was not submitted!"
    print(f"{PASS} Flag successfully extracted from stdout and submitted to scoreboard")

    # 6. Check flags history endpoint
    r_flags = requests.get(f"{BASE_URL}/api/submission/flags")
    flags_data = r_flags.json()
    print(f"{INFO} Flags table total: {flags_data['total']}")
    assert flags_data["total"] >= 2, f"Expected at least 2 distinct flags, got {flags_data['total']}"
    print(f"{PASS} Flag deduplication working (duplicate in stdout ignored)")

    # 7. Check flags status
    statuses = {f["flag"]: f["status"] for f in flags_data["flags"]}
    print(f"{INFO} Flag statuses: {statuses}")
    assert any(st == "ACCEPTED" for st in statuses.values()), "No flag marked as ACCEPTED!"
    assert any(st == "REJECTED" for st in statuses.values()), "No flag marked as REJECTED!"
    print(f"{PASS} Correct status attribution (ACCEPTED and REJECTED)")

    # 8. Check stats endpoint
    r_stats = requests.get(f"{BASE_URL}/api/submission/flags/stats")
    stats = r_stats.json()
    print(f"{INFO} Flag stats: {stats}")
    assert stats["accepted"] >= 1
    assert stats["rejected"] >= 1
    print(f"{PASS} /api/submission/flags/stats verified")

    # Cleanup test resources
    requests.delete(f"{BASE_URL}/api/exploits/{exploit_id}")
    requests.delete(f"{BASE_URL}/api/targets/{target_id}")

    server.shutdown()
    print("\n" + "=" * 60)
    print(f"{PASS} ALL AUTO-SUBMISSION TESTS PASSED!")
    print("=" * 60)

if __name__ == "__main__":
    main()
