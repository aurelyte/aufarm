# AuFarm — Attack & Defense CTF Exploit Farm

A browser-accessible, Docker-based exploit farm for Attack & Defense CTF competitions.
Run it locally, point it at competition targets, and it repeatedly executes your exploits
every competition tick — automatically, concurrently, and reliably.

```
Browser UI
    ↓
Local Farm (Docker)
    ↓
Tick Scheduler
    ↓
Exploit Runner (subprocess)
    ↓
Remote CTF Targets (network)
```

---

## Architecture

```text
aufarm/
├── backend/
│   ├── main.py          # FastAPI app + WebSocket broadcaster + static serving
│   ├── scheduler.py     # Tick scheduler, process runner, concurrency control
│   ├── submitter.py     # Flag auto-submission engine (HTTP JSON/Form, TCP, dedup)
│   ├── database.py      # aiosqlite async SQLite layer + schema
│   ├── models.py        # Pydantic request/response models
│   ├── routers/
│   │   ├── farm.py      # /api/farm/* (start, stop, pause, config…)
│   │   ├── targets.py   # /api/targets CRUD
│   │   ├── exploits.py  # /api/exploits CRUD + upload
│   │   ├── results.py   # /api/results query + stats
│   │   └── submission.py# /api/submission/* (auto-submission & flags)
│   ├── requirements.txt
│   └── requirements-exploits.txt

├── frontend/
│   └── index.html       # Single-file dashboard (vanilla JS + WebSocket)
├── exploits/            # Exploit scripts (bind-mounted into container)
├── data/                # SQLite database (Docker named volume)
├── logs/                # Log files (Docker named volume)
├── Dockerfile
├── docker-compose.yml
├── test_farm.py         # Farm lifecycle & concurrency integration test
├── test_submission.py   # Auto-submission integration test
├── verify_all_api.py    # Complete REST & WebSocket API audit suite
└── template_farm.py     # Standalone farm template
```

### Key Design Highlights

| Feature | Design |
|---|---|
| **Single Container** | FastAPI, scheduler, submitter, and UI bundled into a single lightweight service. Zero external dependencies (no Redis, Celery, or Postgres). |
| **FastAPI + asyncio** | Fully asynchronous I/O with native WebSocket pub/sub for real-time live execution updates. |
| **Async SQLite** | Embedded SQLite datastore via `aiosqlite`. Zero configuration, persistent across container rebuilds via Docker volume. |
| **Process Isolation** | Exploits execute in separate OS subprocesses with custom timeouts, stream caps (64 KB), and automatic process termination. |
| **Concurrency Control** | Configurable worker pool bounded via `asyncio.Semaphore` to prevent CPU and memory exhaustion. |
| **Auto-Submission** | Background flag submitter parsing exploit stdout via regex, deduplicating flags, and dispatching to CTF scoreboards (HTTP JSON / Form / TCP). |
| **Clock-Anchored Ticks** | Precise tick deadlines (`start + N * interval`) preventing timing drift over long-running rounds. |
| **Host Networking** | Direct host network access (`network_mode: host`) enabling seamless communication with remote CTF networks without NAT. |
| **Hot Script Updates** | Bind-mounted `./exploits/` directory and atomic file saves (`os.replace`) allowing live exploit development without corrupting active runs. |

---

## Docker Startup


```bash
git clone / cd into aufarm/
docker compose up --build
```

The UI is available at: **http://localhost:8000**

> **Note:** `network_mode: host` is used so exploit subprocesses can reach competition
> infrastructure directly. This is intentional and expected for A&D competitions.

### Restart persistence

```bash
docker compose down   # stops container, keeps volumes
docker compose up     # resumes from same DB state
```

Data persists in Docker named volumes:
- `aufarm_data` → SQLite database (targets, exploits, results, config)
- `aufarm_logs` → application logs

Exploit scripts are bind-mounted from `./exploits/` on your host.

---

## UI Access

Open **http://localhost:8000** in your browser.

The sidebar has 6 views:
- **Dashboard** — live status, stats, farm controls (Start / Stop / Pause / One Tick / Reset), and real-time execution log
- **Results** — execution history with filtering (status, tick, exploit, target) and detailed stdout/stderr modal inspector
- **Flags** — flag collection stats (Accepted / Rejected / Queued / Dup / Expired / Error), full flag ledger, and manual submit trigger
- **Exploits** — exploit manager with in-browser code editor, file upload, arguments, and per-exploit timeouts
- **Targets** — target host/port/service registry with quick enable/disable toggles
- **Settings** — farm scheduler tuning (ticks, interval, concurrency) and flag auto-submission configuration


---

## Target Configuration

Go to **Targets → + Add Target**:

| Field | Example |
|---|---|
| Name | `Team 1` |
| Host / IP | `10.10.10.11` |
| Port | `8080` |
| Service | `web` |
| Enabled | Yes |

You can disable individual targets without deleting them.

Targets can also be managed via API:

```bash
# Add a target
curl -X POST http://localhost:8000/api/targets \
  -H 'Content-Type: application/json' \
  -d '{"name":"Team1","host":"10.10.10.11","port":8080,"service":"web","enabled":true}'

# List all targets
curl http://localhost:8000/api/targets
```

---

## Exploit Configuration

Go to **Exploits → + Add Exploit**.

Each exploit has:
- **Name** — display name
- **Filename** — script file in `/exploits/` inside the container (maps to `./exploits/` on host)
- **Runtime** — e.g. `python3`, `ruby`, `perl`, `bash`, or empty for direct execution
- **Extra Args** — additional CLI arguments appended after host and port
- **Timeout** — per-instance timeout in seconds
- **Enabled** — toggle without deleting

### Exploit calling convention

AuFarm calls your exploit as:

```
<runtime> <script> <target_host> [target_port] [extra_args...]
```

Environment variables also set:
```
TARGET_HOST, TARGET_PORT, TARGET_NAME, TARGET_SERVICE, AUFARM_TICK
```

### Paste source

Use the **Paste Source** tab to write or paste Python directly in the browser editor.

### Upload file

Use the **Upload File** tab to upload an existing script from your filesystem.

### Hot update

Edit `./exploits/<script>.py` on your host — changes are visible immediately to the next tick
(the `exploits/` directory is bind-mounted). Running instances are not affected; new subprocesses
pick up the new file.

You can also edit via the browser (Exploits → Edit). Source is saved atomically via `os.replace()`.

---

## Exploit Dependencies & Python Libraries

AuFarm executes exploits in isolated subprocesses inside the Docker container.

### Pre-Installed CTF Toolkit
The container image comes pre-installed with the essential A&D CTF libraries:
- **Python Libraries**: `pwntools`, `requests`, `pycryptodome`, `cryptography`, `beautifulsoup4`, `scapy`, `z3-solver`, `paramiko`
- **System Tools**: `curl`, `wget`, `netcat-openbsd`, `nmap`, `iputils-ping`, `gcc`

### Adding Libraries Live During a Match (Zero Downtime)
If you realize during a competition round that an exploit needs a new Python library (such as `impacket` or `ropper`), **you do not need to stop the farm or rebuild the container**.

Run this command directly in your host terminal:

```bash
docker exec aufarm-aufarm-1 pip install <library_name>
```

*Example:*
```bash
docker exec aufarm-aufarm-1 pip install impacket ropper
```

Because exploits are executed as fresh subprocesses on every tick, the very next tick will automatically find and import the newly installed library without missing a beat.

### Making Custom Libraries Permanent Across Rebuilds
To permanently bake extra libraries into future Docker builds:
1. Append the package name to `backend/requirements-exploits.txt`:
   ```bash
   echo "impacket" >> backend/requirements-exploits.txt
   ```
2. Rebuild whenever convenient:
   ```bash
   docker compose up --build -d
   ```

---


## Tick Configuration

Go to **Settings**:

| Setting | Default | Description |
|---|---|---|
| Tick Count | 100 | Number of ticks to run (0 = use Continuous mode) |
| Tick Interval | 60s | Seconds between tick starts |
| Exploit Timeout | 30s | Per-exploit-instance timeout |
| Concurrency | 20 | Max parallel jobs across all exploits/targets |
| Continuous Mode | No | Run ticks forever until manually stopped |

---

## Running One Tick

Click **⚡ ONE TICK** in the dashboard, or:

```bash
curl -X POST http://localhost:8000/api/farm/tick
```

This fires a single tick immediately, regardless of farm state.

---

## Running the Farm

1. **Add targets** (Targets page)
2. **Add exploits** (Exploits page)
3. **Configure** tick interval and concurrency (Settings)
4. **Click START** (Dashboard)

The farm will:
1. Fire tick 1 at `t=0`
2. Launch all `(exploit × target)` combinations concurrently (up to concurrency limit)
3. Collect stdout/stderr, exit code, duration for each job
4. Save results to SQLite
5. Broadcast live updates over WebSocket to all connected browsers
6. Fire tick 2 at `t=tick_interval` (clock-anchored, not drift-accumulating)
7. Continue until tick count reached or STOP is pressed

**Closing the browser does NOT stop the farm.** The scheduler runs entirely in the container.

---

## Continuous Mode

Enable **Continuous Mode** in Settings. The farm will run ticks indefinitely until
you press STOP. Tick Count is ignored.

---

## Flag Auto-Submission

AuFarm has a built-in flag harvesting and submission engine inspired by real CTF A&D setups (CTFd, Siberlab, ForcAD, etc.):

```
Exploit stdout
      ↓
Regex Extraction (configurable pattern)
      ↓
Deduplication via SQLite `flags` table
      ↓
Submitter Loop (independent periodic task)
      ↓
Competition Scoring Server (HTTP JSON / HTTP Form / TCP)
```

### Supported Submission Modes

1. **HTTP JSON** (`http_json`) — Default for platforms like Siberlab (`polriCTF`) and CTFd:
   - Request: `POST {"flags": ["flag1", "flag2"]}`
   - Headers: `Authorization: Bearer <TOKEN>` and `X-Team-Token: <TOKEN>`
   - Response parsing: parses `submitFlagResults` or `results` array, or checks for `valid: true` / status strings (`STATUS_OK`, `ACCEPTED`, `DUP`, `REJECTED`).
2. **HTTP Form** (`http_form`) — Simple web form:
   - Request: `POST flag=<flag>`
3. **TCP** (`tcp`) — Raw TCP socket (e.g., Destructive Farm / ForcAD):
   - Connects to gameserver host:port, sends line-by-line flags, parses response.

### Configuration via UI

Go to **Settings → Flag Auto-Submission**:
- **Enabled**: Toggle auto-submission on/off.
- **Mode**: Choose `HTTP JSON`, `HTTP Form`, or `TCP`.
- **Submit URL / Token**: Target scoreboard URL and your team's API token.
- **Flag Regex**: Custom regex pattern (e.g. `polriCTF\{[^}]+\}` or `[A-Za-z0-9_]{2,16}\{[A-Za-z0-9_+=\-/\.]{8,}\}`). If blank, uses a robust default.
- **Submit Interval**: How often (seconds) the submitter checks for and dispatches queued flags.
- **Flag Lifetime**: Auto-expire stale queued flags if older than N seconds.
- **⚡ Submit Now**: Trigger an immediate submission round without waiting for the interval.

### Flag Monitoring Dashboard

Click **🚩 Flags** in the sidebar:
- Live counter badge showing unsubmitted queued flags.
- Stats breakdown: Total, Queued, Accepted, Rejected, Duplicate, Expired, Error.
- Real-time flag history table with filter by status.
- Instant feedback in the live terminal log on submission acceptance/rejection.


---

## Network Requirements

AuFarm uses `network_mode: host` in Docker Compose.

This means:
- The farm is accessible at `http://localhost:8000`
- Exploit subprocesses have **full access to your host's network interfaces**
- No NAT or port mapping between exploit and competition targets
- Exploits can connect to `10.x.x.x` competition IPs directly

If you need bridge networking instead, replace `network_mode: host` with:
```yaml
ports:
  - "8000:8000"
```
Note: exploit processes will then share the container's network namespace, which
typically still has access to external hosts via the bridge and host routing.

---

## API Reference

```
GET    /api/status                  Farm status + stats

POST   /api/farm/start              Start the farm
POST   /api/farm/stop               Stop the farm
POST   /api/farm/pause              Pause between ticks
POST   /api/farm/resume             Resume from pause
POST   /api/farm/tick               Run one tick immediately
POST   /api/farm/reset              Stop + clear all results
GET    /api/farm/config             Get current config
PUT    /api/farm/config             Update config

GET    /api/targets                 List targets
POST   /api/targets                 Create target
GET    /api/targets/{id}            Get target
PUT    /api/targets/{id}            Update target
DELETE /api/targets/{id}            Delete target

GET    /api/exploits                List exploits
POST   /api/exploits                Create exploit
GET    /api/exploits/{id}           Get exploit
GET    /api/exploits/{id}/source    Get exploit source code
PUT    /api/exploits/{id}           Update exploit (+ source)
POST   /api/exploits/{id}/upload    Upload exploit file
DELETE /api/exploits/{id}           Delete exploit

GET    /api/results                 List results (filterable)
GET    /api/results/stats           Summary stats
GET    /api/results/{id}            Get single result with stdout/stderr

GET    /api/submission/config       Get auto-submission settings
PUT    /api/submission/config       Update auto-submission settings
GET    /api/submission/flags        List captured flags with status
GET    /api/submission/flags/stats  Flag collection stats (accepted, rejected, etc.)
POST   /api/submission/submit_now   Trigger immediate submission run

WS     /ws                          Live WebSocket feed

```

---

## Troubleshooting

**Container won't start:**
```bash
docker compose logs aufarm
```

**Port 8000 already in use:**
Edit `docker-compose.yml`, change from `network_mode: host` to:
```yaml
ports:
  - "8001:8000"
```
Then access at `http://localhost:8001`.

**Exploit not found:**
- Check that the filename in the Exploit record matches the actual file in `./exploits/`
- The container maps `./exploits/` to `/exploits/` inside the container

**Exploit times out immediately:**
- Check that the runtime is installed in the container (Python 3 is included by default)
- For other runtimes, add them to the `Dockerfile` and rebuild

**No results appearing:**
- Verify targets are enabled (green dot in Targets list)
- Verify exploits are enabled
- Click ⚡ ONE TICK and check the live log for errors

**Results show SKIPPED:**
- The exploit script file does not exist in `/exploits/`

**WebSocket shows "Disconnected":**
- Reload the page — it reconnects automatically every 3 seconds

---

## Testing

```bash
python3 test_farm.py
python3 test_submission.py
```

The test suites run against a live farm instance and verify:
- Successful exploit execution
- Failed exploit (non-zero exit code)
- Timeout detection and process kill
- Crash handling
- Concurrent execution across multiple targets
- STOP while jobs are running (child process cleanup)
- Browser disconnect survival (farm continues without HTTP)
- Stats accumulation accuracy
- Flag extraction from stdout via regex
- Flag deduplication via SQLite unique constraint
- Scoring server submission (HTTP JSON, Form, TCP)
- Status feedback tracking (ACCEPTED, REJECTED, DUPLICATE, EXPIRED)


---

## Exploit Template

```python
#!/usr/bin/env python3
"""
Example AuFarm exploit.
Called as: python3 exploit.py <host> [port]
"""
import sys
import socket

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 80

try:
    # Your exploit logic here
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
    data = s.recv(4096)
    print(data.decode(errors="replace"), flush=True)
    s.close()
    sys.exit(0)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr, flush=True)
    sys.exit(1)
```
