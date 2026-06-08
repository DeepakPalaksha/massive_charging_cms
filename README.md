# Massive Charging CMS — Local Development Setup

A working simulation of a Charge Management System (CMS) built on PostgreSQL.
Demonstrates session state machine, edge case handling, and billing logic.

---

## What this project demonstrates

- OCPP session state machine: `preparing → charging → suspended → finishing → complete`
- Edge case: 4G drop mid-session with reconnect and reconciliation
- Edge case: 4G drop with no reconnect and estimated billing
- Audit trail: complete event log for every session
- PostgreSQL as single source of truth (no Redis, no Kafka)

---

## Prerequisites

Make sure you have these installed:

| Tool | Version | Check |
|------|---------|-------|
| Docker Desktop | Latest | `docker --version` |
| Python | 3.11+ | `python --version` |
| uv | Latest | `uv --version` |

---

## Project structure

```
massive_charging_cms/
├── docker-compose.yml       # PostgreSQL + pgAdmin containers
├── schema.sql               # Database tables (auto-runs on first start)
├── cms_backend.py           # State machine logic
├── websocket_simulator.py   # Simulates charger behavior (3 scenarios)
├── pyproject.toml           # Python dependencies (managed by uv)
└── README.md                # This file
```

---

## Setup (one time only)

### Step 1: Clone or create the project folder

```bash
mkdir massive_charging_cms
cd massive_charging_cms
```

### Step 2: Initialize Python project with uv

```bash
uv init
uv add psycopg2-binary python-dateutil
```

### Step 3: Start PostgreSQL and pgAdmin

```bash
docker-compose up -d
```

Expected output:
```
✔ Container massive_charging_db       Healthy
✔ Container massive_charging_pgadmin  Running
```

### Step 4: Verify database tables were created

```bash
docker exec -it massive_charging_db psql -U postgres -d massive_charging
```

Inside psql:
```sql
\dt
```

Expected output:
```
 billing
 charger_config
 command_queue
 session_events
 sessions
```

Check seed data:
```sql
SELECT charger_id, site_name FROM charger_config;
```

Expected output:
```
 CH-001 | Mumbai-Pune Highway KM 45
 CH-002 | Mumbai-Pune Highway KM 45
 CH-003 | Bangalore-Chennai Highway KM 120
```

Exit psql:
```sql
\q
```

---

## Running the simulator

```bash
uv run python websocket_simulator.py
```

### What you will see:

**Scenario 1 — Happy Path:**
```
[STEP 1] Vehicle plugs into CH-001
[STEP 2] Authorization confirmed
[STEP 3] MeterValues every 30 seconds (10 messages)
[STEP 4] StopTransaction — session complete
Status: complete | Energy: X kWh | ₹XXX | Is estimated: False
```

**Scenario 2 — 4G Drop, Charger Reconnects:**
```
[STEP 1] Vehicle plugs into CH-002
[STEP 2] Authorization confirmed
[STEP 3] MeterValues for 3 minutes (normal)
[STEP 4] CONNECTION DROPS → session marked suspect
         Estimated energy: X kWh | ₹XXX
[STEP 5] Charger reconnects with real final value
         Reconciled: estimated vs actual diff = X kWh
Status: complete | Is estimated: False ← reconciled to actual
```

**Scenario 3 — 4G Drop, Never Reconnects:**
```
[STEP 1] Vehicle plugs into CH-003
[STEP 2] Authorization confirmed
[STEP 3] MeterValues for 2 minutes
[STEP 4] CONNECTION DROPS → session marked suspect
[STEP 5] Alert worker closes session with estimate
Status: complete | Is estimated: True ← flagged for ops review
```

---

## Inspecting the database visually

Open pgAdmin in your browser:

```
URL:      http://localhost:5050
Email:    admin@massivecharging.com
Password: admin
```

### Connect pgAdmin to your database:

1. Right click **Servers** → **Register** → **Server**
2. **Name:** massive_charging
3. Click **Connection** tab:
   - **Host:** massive_charging_db
   - **Port:** 5432
   - **Database:** massive_charging
   - **Username:** postgres
   - **Password:** postgres
4. Click **Save**

### Useful queries to run in pgAdmin Query Tool:

**See all sessions:**
```sql
SELECT session_uuid, charger_id, status, 
       total_energy_kwh, total_amount_inr, is_estimated
FROM sessions
ORDER BY created_at DESC;
```

**See complete event history for a session:**
```sql
SELECT event_type, event_timestamp, data
FROM session_events
WHERE session_id = 1
ORDER BY event_timestamp ASC;
```

**See all suspect sessions:**
```sql
SELECT session_uuid, charger_id, 
       estimated_energy_kwh, estimated_amount_inr
FROM sessions
WHERE status = 'suspect';
```

**See billing records:**
```sql
SELECT s.session_uuid, b.energy_kwh, 
       b.amount_inr, b.payment_status, b.is_estimated
FROM billing b
JOIN sessions s ON s.id = b.session_id;
```

---

## Resetting the database (fresh start)

To wipe all data and start again:

```bash
docker-compose down -v
docker-compose up -d
```

The `-v` flag removes the PostgreSQL volume.
All tables and seed data are recreated automatically.

---

## Key concepts demonstrated

### State machine
Every session follows a strict state machine.
Invalid transitions are rejected (idempotency).

### Atomicity
Every function wraps its database writes in a transaction.
Session state update + event log either both succeed or both fail.

### Idempotency
Every state transition checks current state before updating.
Duplicate messages from chargers never corrupt the database.

### Audit trail
Every state change is logged to `session_events`.
Complete reconstruction of any session is always possible.

### Estimated billing
When a charger drops, the system estimates billing from last known state.
When charger reconnects, estimated value is reconciled against actual.
Estimated sessions are flagged for ops team review.

---

## Production architecture

This local setup maps to production as follows:

| Local | Production |
|-------|-----------|
| Docker PostgreSQL | AWS RDS PostgreSQL t3.medium |
| Direct Python calls | FastAPI WebSocket gateway |
| Manual function calls | River Queue background workers |
| pgAdmin | Grafana + CloudWatch dashboard |

---

## Stopping the project

```bash
docker-compose down
```

Data is preserved in the Docker volume.
Next `docker-compose up -d` restores everything.