# Massive Charging CMS — Local Simulation

A working simulation of a **Charge Management System (CMS)** built entirely on PostgreSQL.

Built to demonstrate how a production EV charging backend handles session lifecycle, billing, and failure recovery — without Redis, without Kafka, without over-engineering.

---

## What problem does this solve?

EV chargers on highway corridors face three hard problems:

1. **Unreliable 4G connectivity** — chargers drop mid-session
2. **Billing accuracy** — customers must be billed correctly even when connections fail
3. **Scale** — 200 chargers today, 2,000 tomorrow, without rebuilding the system

This project demonstrates a PostgreSQL-native architecture that solves all three.

---

## Architecture in one diagram

```
Physical Charger (highway)
        │
        │  WebSocket (OCPP 1.6)
        │  wss://cms.massivecharging.com/ocpp/CH-001
        ▼
┌─────────────────────────┐
│   FastAPI Gateway        │   ← In production: EC2 t3.medium
│   (websocket_simulator   │     Accepts OCPP WebSocket connections
│    replaces this locally)│     Routes messages to CMS backend
└──────────┬──────────────┘
           │
           │  Function calls
           ▼
┌─────────────────────────┐
│   CMS Backend            │   ← cms_backend.py
│   State Machine          │     7 functions, each wrapping a
│   Business Logic         │     state transition in a transaction
└──────────┬──────────────┘
           │
           │  SQL (psycopg2)
           ▼
┌─────────────────────────┐
│   PostgreSQL             │   ← In production: AWS RDS t3.medium
│   Single source of truth │     5 tables, indexed for fast reads
│   (Docker locally)       │     67 writes/second at 2,000 sessions
└─────────────────────────┘
```

---

## The state machine

Every charging session follows a strict state machine:

```
idle
  │
  │ vehicle plugs in
  ▼
preparing
  │
  │ payment confirmed
  ▼
charging ──────────────────────────────────► suspect
  │          4G drops, no MeterValues                │
  │          for 5 minutes                           │
  │ vehicle unplugs                                  │ charger reconnects
  ▼          OR alert worker closes after 30 min     │ with real final value
finishing ◄─────────────────────────────────────────┘
  │
  ▼
complete
  │
  ▼
billing triggered (pending → completed / failed)
```

**Suspect state** is the key innovation:
- Session does not close when 4G drops
- System estimates final energy from last known power × time elapsed
- When charger reconnects, estimated value is reconciled against actual
- If charger never reconnects, session closes with estimated billing and ops flag

---

## The five database tables

```
charger_config      One row per physical charger
                    Stores site, power limit, OCPP version
                    Read frequently, written rarely

sessions            One row per charging session
                    Tracks current state, energy, amount
                    Updated on every MeterValues message
                    The real-time heartbeat of the system

session_events      One row per event inside a session
                    Append-only — never updated, never deleted
                    Complete audit trail for billing disputes
                    MeterValues, status changes, errors, reconciliations

command_queue       Commands waiting to be sent to chargers
                    RemoteStop, SetChargingProfile, Reset
                    Gateway reads this and pushes commands over WebSocket

billing             One row per completed session
                    Separate from sessions so billing failure
                    never blocks session closure
```

---

## Three scenarios simulated

### Scenario 1: Happy path
```
Vehicle plugs in → driver pays → charges for 5 minutes → unplugs
Result: session complete, is_estimated=False, billing=pending
```

### Scenario 2: 4G drop — charger reconnects
```
Vehicle plugs in → charges for 3 minutes → 4G drops
→ session marked suspect with estimated energy
→ charger reconnects with real final meter value
→ estimated reconciled against actual
Result: session complete, is_estimated=False (reconciled to real value)
```

### Scenario 3: 4G drop — charger never reconnects
```
Vehicle plugs in → charges for 2 minutes → 4G drops
→ session marked suspect
→ 30 minutes pass, charger still offline
→ alert worker closes session with estimated billing
Result: session complete, is_estimated=True (flagged for ops review)
```

---

## Key engineering decisions

### Why PostgreSQL only (no Redis)?

2,000 sessions × 1 MeterValues per 30 seconds = **67 writes per second**.

AWS RDS PostgreSQL on a `db.t3.medium` handles **1,000–3,000 writes per second** comfortably. We are at 5% of capacity.

Redis solves a problem we don't have. It would add operational overhead — another service to monitor, another failure point — without solving anything real at this scale.

**Scaling trigger:** When PostgreSQL CPU hits 70% sustained, add a read replica. Workers and dashboard read from the replica. Gateway writes to primary.

### Why two tables (sessions + session_events)?

`sessions` is sparse and fast — it holds only the current state. Dashboard and workers query this table.

`session_events` is the audit trail — append-only, one row per event. Used only when you need to reconstruct what happened (billing dispute, charger debugging, ops investigation).

A customer dispute about billing can be resolved by replaying the complete event log for that session. Without this, you're guessing.

### Why no Kafka?

Kafka is the right tool at 50,000+ sessions with 5+ engineering teams. At 2,000 sessions with one team, Kafka adds:
- A new service to operate and monitor
- A new failure mode
- Operational expertise your team doesn't have yet

PostgreSQL LISTEN/NOTIFY gives real-time behaviour from the database you're already running.

### Why PgBouncer matters more than instance size?

PostgreSQL has a default limit of 100 simultaneous connections. With 2,000 chargers sending messages simultaneously, you'd exhaust this instantly.

PgBouncer is a connection pooler that sits between FastAPI and PostgreSQL. It maintains 20–30 real database connections and queues the rest. PostgreSQL never sees more than 30 connections.

This is a configuration change, not a hardware upgrade.

---

## Project structure

```
massive_charging_cms/
├── docker-compose.yml        PostgreSQL (port 5432) + pgAdmin (port 5050)
├── schema.sql                Five tables with indexes and seed data
│                             Auto-runs when Docker starts
├── cms_backend.py            State machine logic
│                             7 functions, each a state transition
│                             Every write wrapped in a transaction
├── websocket_simulator.py    Simulates three charger scenarios
│                             Calls cms_backend functions directly
│                             (replaces FastAPI gateway locally)
├── pyproject.toml            Python dependencies managed by uv
├── uv.lock                   Exact dependency versions locked
└── README.md                 This file
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Docker Desktop | Latest | https://docker.com |
| Python | 3.11+ | https://python.org |
| uv | Latest | `pip install uv` |
| Git | Any | https://git-scm.com |

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/DeepakPalaksha/massive_charging_cms.git
cd massive_charging_cms
```

### 2. Install Python dependencies

```bash
uv sync
```

This reads `pyproject.toml`, creates a virtual environment in `.venv`, and installs all dependencies. No manual pip install needed.

### 3. Start PostgreSQL and pgAdmin

```bash
docker-compose up -d
```

Expected output:
```
✔ Container massive_charging_db       Healthy
✔ Container massive_charging_pgadmin  Running
```

`schema.sql` runs automatically on first start. All five tables and seed data are created.

### 4. Verify the database

```bash
docker exec -it massive_charging_db psql -U postgres -d massive_charging
```

Inside psql:

```sql
-- Check tables exist
\dt

-- Check seed data
SELECT charger_id, site_name, power_limit_kw FROM charger_config;

-- Exit
\q
```

---

## Running the simulator

```bash
uv run python websocket_simulator.py
```

The simulator runs all three scenarios and prints:
- Session UUID for each scenario
- Status at each step
- Energy and billing amounts
- Reconciliation result for suspect sessions
- Complete event log for each session
- Final check: zero suspect sessions remaining

---

## Inspecting the database visually

Open pgAdmin at `http://localhost:5050`

```
Email:    admin@massivecharging.com
Password: admin
```

### Connect pgAdmin to PostgreSQL:

1. Right click **Servers** → **Register** → **Server**
2. **General tab** → Name: `massive_charging`
3. **Connection tab:**
   - Host: `massive_charging_db`
   - Port: `5432`
   - Database: `massive_charging`
   - Username: `postgres`
   - Password: `postgres`
4. Click **Save**

### Useful queries (Tools → Query Tool):

**All sessions with status and billing:**
```sql
SELECT session_uuid, charger_id, status,
       total_energy_kwh, total_amount_inr,
       is_estimated, billing_status
FROM sessions
ORDER BY created_at DESC;
```

**Complete event history for one session:**
```sql
SELECT event_type, event_timestamp, data
FROM session_events
WHERE session_id = 1
ORDER BY event_timestamp ASC;
```

**Sessions flagged for ops review (estimated billing):**
```sql
SELECT session_uuid, charger_id,
       total_energy_kwh, total_amount_inr
FROM sessions
WHERE is_estimated = TRUE;
```

**Billing records:**
```sql
SELECT s.session_uuid, s.charger_id,
       b.energy_kwh, b.amount_inr,
       b.is_estimated, b.payment_status
FROM billing b
JOIN sessions s ON s.id = b.session_id
ORDER BY b.created_at DESC;
```

---

## Resetting to a clean state

```bash
docker-compose down -v
docker-compose up -d
```

The `-v` flag removes the PostgreSQL data volume. All tables and seed data are recreated automatically on next start.

---

## What is NOT in this repo (production additions)

This is a local simulation. A production deployment would add:

| Component | Purpose |
|-----------|---------|
| FastAPI gateway | Accept real OCPP WebSocket connections from chargers |
| River Queue workers | Automatically monitor sessions, trigger suspect detection, retry billing |
| PgBouncer | Connection pooler between gateway and PostgreSQL |
| AWS RDS | Managed PostgreSQL with automated backups and read replicas |
| AWS ALB | Load balancer distributing charger connections across gateway instances |
| Grafana + CloudWatch | Dashboard for session monitoring, charger health, billing alerts |
| CI/CD pipeline | GitHub Actions → Docker build → ECR → ECS deployment |

---

## Production architecture (reference)

```
Chargers (highway)
    │ wss:// OCPP WebSocket
    ▼
AWS ALB (load balancer)
    │
    ├──► Gateway EC2 t3.medium (holds ~1,000 WebSocket connections)
    └──► Gateway EC2 t3.medium (holds ~1,000 WebSocket connections)
              │
              │ psycopg2
              ▼
         PgBouncer (connection pooler)
              │
              ▼
         AWS RDS PostgreSQL db.t3.medium (primary — writes)
              │
              └──► RDS Read Replica (workers and dashboard reads)
              
River Queue Workers (separate EC2 or ECS tasks)
    ├── Session Monitor Worker  (detects no MeterValues → mark suspect)
    ├── Alert Worker            (closes suspect sessions after 30 min)
    ├── Billing Worker          (processes payments, retries on failure)
    └── Command Dispatcher      (reads command_queue, pushes to chargers)

Mobile App / Dashboard
    │ REST API
    ▼
FastAPI API Server (separate from gateway)
    │
    ▼
Same PostgreSQL RDS
```

---

## Concepts demonstrated

| Concept | Where in code |
|---------|--------------|
| State machine | `cms_backend.py` — every function checks current state before transitioning |
| Atomicity | Every function: state update + event log commit together or both roll back |
| Idempotency | Every UPDATE has a WHERE clause checking current state — safe for charger retries |
| Audit trail | `session_events` table — append-only, complete reconstruction always possible |
| Estimated billing | `mark_suspect()` — calculates energy from last known power × elapsed time |
| Reconciliation | `stop_charging()` — compares estimated vs actual, flags large differences |
| Connection safety | `create_session()` — blocks duplicate sessions on same connector |

---

## Authors

Built by Deepak Palaksha as a consulting deliverable for Massive Charging.