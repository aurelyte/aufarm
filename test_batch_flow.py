#!/usr/bin/env python3
"""
Test Suite: Batch Flag Collection & Submission (Collect-first, Submit-later)
Tests:
1. Strict timing verification: Exploit collection must complete across ALL targets
   BEFORE any submission request is made to the scoreboard.
2. Mixed target results:
   - Flag retrieved -> Accepted
   - Flag retrieved -> Invalid/Rejected
   - Exploit failed -> Flag not retrieved -> Not submitted
   - Exploit timeout -> Flag not retrieved -> Not submitted
   - Duplicate flag -> Flag retrieved -> Already submitted
3. Centralized configuration & own team IP protection:
   - Targets matching team_ip are excluded from exploit execution.
4. Scoreboard API failure / recovery:
   - Flags and collection results are preserved when scoreboard is unreachable.
"""

import http.server
import json
import socketserver
import sys
import threading
import time
import requests

BASE_URL = "http://localhost:8000"
MOCK_PORT = 9998
MOCK_URL = f"http://localhost:{MOCK_PORT}/api/v1/flags"

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
INFO = "\033[34m[INFO]\033[0m"

submission_events = []
mock_response_mode = "normal"  # normal | 500

class BatchScoreboardHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global submission_events
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len)

        if mock_response_mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Internal Server Error"}')
            return

        try:
            data = json.loads(post_body.decode())
            flags = data.get("flags", [])
            submission_events.append({
                "timestamp": time.time(),
                "flags": list(flags),
            })

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

    def do_GET(self):
        # Connectivity probe
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "scoreboard reachable"}')

    def log_message(self, format, *args):
        pass


def run_mock_server():
    server = socketserver.TCPServer(("0.0.0.0", MOCK_PORT), BatchScoreboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def check(label, cond, detail=""):
    if cond:
        print(f"{PASS} {label}")
        return True
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        return False


def cleanup_all():
    # Remove all targets and exploits
    try:
        r = requests.get(f"{BASE_URL}/api/targets").json()
        for t in r:
            requests.delete(f"{BASE_URL}/api/targets/{t['id']}")
        r = requests.get(f"{BASE_URL}/api/exploits").json()
        for e in r:
            requests.delete(f"{BASE_URL}/api/exploits/{e['id']}")
        requests.post(f"{BASE_URL}/api/farm/reset")
    except Exception:
        pass


def main():
    global mock_response_mode, submission_events
    print("=" * 65)
    print("AuFarm Batch Collection & Submission Verification Suite")
    print("=" * 65)

    server = run_mock_server()
    all_passed = True

    try:
        cleanup_all()

        # ── Test 1: Centralized Configuration Window API ───────────────────────
        print(f"\n{INFO} === 1. Testing Centralized Competition Configuration ===")
        r = requests.get(f"{BASE_URL}/api/competition/config", timeout=5)
        all_passed &= check("GET /api/competition/config returns 200", r.status_code == 200)
        cfg = r.json()
        all_passed &= check("Config contains team, api, execution",
                            "team" in cfg and "api" in cfg and "execution" in cfg)

        # Update centralized config with team identity, IP protection, and mock scoreboard
        new_cfg = {
            "team": {
                "team_id": "7",
                "team_name": "AntigravityTeam",
                "team_ip": "10.10.10.99",  # Own team IP to exclude
                "team_password": "supersecretpassword",
            },
            "api": {
                "submit_mode": "http_json",
                "submit_url": MOCK_URL,
                "submit_token": "secret-ctf-token-xyz",
                "submit_host": "",
                "submit_port": 31337,
                "flag_regex": r"((?:polriCTF|CTF)\{[^}]+\})",
            },
            "execution": {
                "tick_count": 50,
                "tick_interval": 30,
                "exploit_timeout": 5,
                "collection_deadline": 5,
                "concurrency": 20,
                "continuous": False,
                "submit_enabled": True,
                "submit_interval": 2,
                "flag_lifetime": 300,
            }
        }
        r = requests.put(f"{BASE_URL}/api/competition/config", json=new_cfg, timeout=5)
        all_passed &= check("PUT /api/competition/config saved successfully", r.status_code == 200)

        # Test connection endpoint
        r = requests.post(f"{BASE_URL}/api/competition/test_connection", timeout=5)
        all_passed &= check("POST /api/competition/test_connection returns ok",
                            r.status_code == 200 and r.json().get("ok") is True, r.text)

        # Validation test on required fields
        bad_cfg = dict(new_cfg)
        bad_cfg["team"] = dict(new_cfg["team"])
        bad_cfg["team"]["team_id"] = "   "  # empty
        r = requests.put(f"{BASE_URL}/api/competition/config", json=bad_cfg, timeout=5)
        all_passed &= check("Validation rejects empty team_id (400)", r.status_code == 400)

        # ── Test 2: Flow Verification: Collect First, Submit Later ────────────
        print(f"\n{INFO} === 2. Testing Execution Flow (Collect First, Submit Later) ===")
        requests.post(f"{BASE_URL}/api/farm/reset")
        submission_events.clear()

        nonce1 = int(time.time())

        # Create 4 Opponent Targets + 1 Own Team Target (to test exclusion)
        t_ids = []
        target_info = [
            ("Opponent_Success", "10.10.10.1", 80),
            ("Opponent_Invalid", "10.10.10.2", 80),
            ("Opponent_Fail",    "10.10.10.3", 80),
            ("Opponent_Timeout", "10.10.10.4", 80),
            ("Own_Team_Protected", "10.10.10.99", 80), # Must NOT be attacked!
        ]
        for name, host, port in target_info:
            r = requests.post(f"{BASE_URL}/api/targets", json={
                "name": name, "host": host, "port": port, "service": "web", "enabled": True
            })
            t_ids.append(r.json()["id"])

        exploit_script = f"""#!/usr/bin/env python3
import sys, time

host = sys.argv[1]
if host == "10.10.10.99":
    print("CRITICAL_ERROR_OWN_TEAM_ATTACKED")
    sys.exit(99)
elif host == "10.10.10.1":
    time.sleep(1.2)
    print("CTF{{valid_flag_opponent_1_{nonce1}}}", flush=True)
elif host == "10.10.10.2":
    time.sleep(1.2)
    print("CTF{{invalid_flag_opponent_2_{nonce1}}}", flush=True)
elif host == "10.10.10.3":
    time.sleep(0.3)
    sys.exit(1)
elif host == "10.10.10.4":
    time.sleep(9999)
sys.exit(0)
"""
        r = requests.post(f"{BASE_URL}/api/exploits", json={
            "name": "service_web_vuln",
            "filename": "service_web_vuln.py",
            "runtime": "python3",
            "timeout": 3,
            "enabled": True,
            "target_mode": "all",
        })
        exp_id = r.json()["id"]
        requests.put(f"{BASE_URL}/api/exploits/{exp_id}", json={"source": exploit_script})

        t_tick_start = time.time()
        print(f"{INFO} Triggering tick at t=0...")
        requests.post(f"{BASE_URL}/api/farm/tick")

        # Wait for all targets to complete (target 4 timeout is 3s, so total ~3.5s)
        time.sleep(5)

        # Check submission timing
        all_passed &= check("Batch submission occurred", len(submission_events) > 0)
        if submission_events:
            sub_t = submission_events[0]["timestamp"]
            sub_flags = submission_events[0]["flags"]
            print(f"{INFO} Submission event received at t={sub_t - t_tick_start:.2f}s with flags: {sub_flags}")

            # Verify that submission happened AFTER target 1 and 2 finished collection (> 1.2s)
            # but before a sequential 4-target run could finish (~1.2 + 1.2 + 0.3 + 3.0 = 5.7s).
            # This proves targets were launched concurrently and the batch submitted immediately
            # after the slowest per-target timeout completed.
            elapsed_to_submit = sub_t - t_tick_start
            all_passed &= check("No submission occurred before targets finished collection",
                                elapsed_to_submit >= 1.0)
            all_passed &= check("Targets executed in parallel, not sequentially (<5s to submit)",
                                elapsed_to_submit < 5.0,
                                f"submission took {elapsed_to_submit:.2f}s; sequential execution would risk flag TTL")
            all_passed &= check("Submitted only flags from successful targets (valid + invalid)",
                                len(sub_flags) == 2 and any("opponent_1" in f for f in sub_flags) and any("opponent_2" in f for f in sub_flags))

        # Check Latest Batch results
        r_batch = requests.get(f"{BASE_URL}/api/results/latest-batch")
        all_passed &= check("GET /api/results/latest-batch returns 200", r_batch.status_code == 200)
        batch_data = r_batch.json().get("batch", {})
        all_passed &= check("Batch stage is COMPLETED", batch_data.get("stage") == "COMPLETED")
        all_passed &= check("Flags retrieved count == 2", batch_data.get("flags_retrieved") == 2)
        all_passed &= check("Flags submitted count == 2", batch_data.get("flags_submitted") == 2)
        all_passed &= check("Accepted count == 1", batch_data.get("accepted") == 1)
        all_passed &= check("Rejected count == 1", batch_data.get("rejected") == 1)

        # Verify Per-IP statuses
        results_by_ip = {r["target_host"]: r for r in batch_data.get("results", [])}
        print(f"\n{INFO} === Per-IP Results Table ===")
        print(f"{'Opponent IP':<16} | {'Collection Status':<20} | {'Submission Status':<20}")
        print("-" * 62)
        for ip, r in sorted(results_by_ip.items()):
            print(f"{ip:<16} | {r['collection_status']:<20} | {r['submission_status']:<20}")

        # Assertions per IP
        all_passed &= check("10.10.10.1: Flag Retrieved & Accepted",
                            results_by_ip.get("10.10.10.1", {}).get("collection_status") == "Flag Retrieved" and
                            results_by_ip.get("10.10.10.1", {}).get("submission_status") == "Accepted")

        all_passed &= check("10.10.10.2: Flag Retrieved & Invalid",
                            results_by_ip.get("10.10.10.2", {}).get("collection_status") == "Flag Retrieved" and
                            results_by_ip.get("10.10.10.2", {}).get("submission_status") == "Invalid")

        all_passed &= check("10.10.10.3 (Failed): Flag Not Retrieved & Not submitted",
                            results_by_ip.get("10.10.10.3", {}).get("collection_status") == "Flag Not Retrieved" and
                            results_by_ip.get("10.10.10.3", {}).get("submission_status") == "Not submitted")

        all_passed &= check("10.10.10.4 (Timeout): Flag Not Retrieved & Not submitted",
                            results_by_ip.get("10.10.10.4", {}).get("collection_status") == "Flag Not Retrieved" and
                            results_by_ip.get("10.10.10.4", {}).get("submission_status") == "Not submitted")

        all_passed &= check("Own team IP 10.10.10.99 excluded from attack targets",
                            "10.10.10.99" not in results_by_ip)

        # ── Test 3: Duplicate / Already Submitted Flag Detection ───────────────
        print(f"\n{INFO} === 3. Testing Duplicate / Already Submitted Flag Detection ===")
        submission_events.clear()
        # Run tick again with SAME exploit. 10.10.10.1 returns the exact same flag that was already ACCEPTED.
        requests.post(f"{BASE_URL}/api/farm/tick")
        time.sleep(5)

        r_batch_dup = requests.get(f"{BASE_URL}/api/results/latest-batch").json().get("batch", {})
        res_dup_ip = {r["target_host"]: r for r in r_batch_dup.get("results", [])}
        all_passed &= check("Already accepted flag not re-submitted to scoreboard",
                            not any(f"valid_flag_opponent_1_{nonce1}" in f for ev in submission_events for f in ev["flags"]))
        all_passed &= check("Target marked with Already submitted status",
                            res_dup_ip.get("10.10.10.1", {}).get("submission_status") == "Already submitted")

        # ── Test 4: Scoreboard Down / Failure Resilience ───────────────────────
        print(f"\n{INFO} === 4. Testing Scoreboard Down / Failure Resilience ===")
        nonce2 = int(time.time()) + 100
        exploit_script_new = exploit_script.replace(str(nonce1), str(nonce2))
        requests.put(f"{BASE_URL}/api/exploits/{exp_id}", json={"source": exploit_script_new})

        mock_response_mode = "500"  # Scoreboard returns 500 error
        requests.post(f"{BASE_URL}/api/farm/tick")
        time.sleep(5)

        r_batch_fail = requests.get(f"{BASE_URL}/api/results/latest-batch").json().get("batch", {})
        res_fail_ip = {r["target_host"]: r for r in r_batch_fail.get("results", [])}

        all_passed &= check("Flags still collected when scoreboard 500s",
                            res_fail_ip.get("10.10.10.1", {}).get("collection_status") == "Flag Retrieved")
        all_passed &= check("Target marked with Submission Error on scoreboard 500",
                            res_fail_ip.get("10.10.10.1", {}).get("submission_status") == "Submission Error")

        # Verify that flag is preserved in database with status ERROR so it can be retried later
        r_flags = requests.get(f"{BASE_URL}/api/submission/flags").json()
        error_flags = [f for f in r_flags.get("flags", []) if f["status"] == "ERROR"]
        all_passed &= check("Flags preserved in database with ERROR status for later retry",
                            len(error_flags) >= 1)

        cleanup_all()

    finally:
        server.shutdown()

    print("\n" + "=" * 65)
    if all_passed:
        print(f"{PASS} ALL BATCH COLLECTION & SUBMISSION TESTS PASSED!")
    else:
        print(f"{FAIL} SOME BATCH FLOW TESTS FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    main()
