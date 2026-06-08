-- =============================================================
-- MASSIVE CHARGING CMS — DATABASE SCHEMA
-- =============================================================
-- Design principle: PostgreSQL is the single source of truth.
-- Every state change is written here before being acknowledged.
-- No Redis. No Kafka. No cache layer at this scale.
-- =============================================================


-- =============================================================
-- TABLE 1: charger_config
-- One row per physical charger registered in the system.
-- Read frequently, written rarely (only when charger is added).
-- =============================================================
CREATE TABLE charger_config (
    id                    BIGSERIAL PRIMARY KEY,
    charger_id            VARCHAR(50) UNIQUE NOT NULL,  -- e.g. 'CH-001'
    site_name             VARCHAR(100),                 -- e.g. 'Mumbai-Pune Highway KM 45'
    charger_name          VARCHAR(100),                 -- e.g. 'DC Fast Charger 1'
    ocpp_version          VARCHAR(10) DEFAULT '1.6',    -- '1.6' or '2.0'
    power_limit_kw        INTEGER DEFAULT 60,           -- Max power this charger can deliver
    heartbeat_interval_sec INTEGER DEFAULT 30,          -- How often charger sends MeterValues
    created_at            TIMESTAMP DEFAULT NOW(),
    updated_at            TIMESTAMP DEFAULT NOW()
);


-- =============================================================
-- TABLE 2: sessions
-- One row per charging session.
-- Tracks CURRENT STATE only — updated on every MeterValue.
-- Kept sparse: only summary data lives here.
-- Detailed history lives in session_events.
-- =============================================================
CREATE TABLE sessions (
    id                    BIGSERIAL PRIMARY KEY,
    charger_id            VARCHAR(50) NOT NULL,
    connector_id          INTEGER NOT NULL,
    session_uuid          UUID UNIQUE NOT NULL,          -- Shared with mobile app and charger
    vehicle_id            VARCHAR(100),                  -- If known (Plug & Charge)

    -- ── State machine ─────────────────────────────────────────
    -- Values: preparing → charging → suspended → finishing → complete
    -- Special: suspect (lost connection), idle (no session)
    status                VARCHAR(50) NOT NULL DEFAULT 'preparing',

    -- ── Timestamps ────────────────────────────────────────────
    start_time            TIMESTAMP NOT NULL DEFAULT NOW(),
    end_time              TIMESTAMP,                     -- NULL until session completes

    -- ── Energy tracking ───────────────────────────────────────
    total_energy_kwh      DECIMAL(10,3) DEFAULT 0,       -- Updated on every MeterValue
    estimated_energy_kwh  DECIMAL(10,3),                 -- Filled only if session goes suspect
    is_estimated          BOOLEAN DEFAULT FALSE,          -- TRUE = bill based on estimate

    -- ── Last known state (for reconnect recovery) ─────────────
    last_meter_value_kwh  DECIMAL(10,3),                 -- Last confirmed energy reading
    last_meter_time       TIMESTAMP,                     -- When we last heard from charger
    last_power_kw         DECIMAL(8,2),                  -- Last known power level

    -- ── Billing ───────────────────────────────────────────────
    total_amount_inr      DECIMAL(10,2) DEFAULT 0,       -- Running total in rupees
    estimated_amount_inr  DECIMAL(10,2),                 -- Filled only if suspect
    billing_status        VARCHAR(50),                   -- 'pending', 'completed', 'failed'
    payment_id            VARCHAR(100),                  -- Reference from payment gateway

    created_at            TIMESTAMP DEFAULT NOW(),
    updated_at            TIMESTAMP DEFAULT NOW()
);

-- Indexes: what queries will we run most often?
-- 1. "Find active session for this charger" (gateway uses this on every reconnect)
CREATE INDEX idx_sessions_charger_status
    ON sessions(charger_id, status);

-- 2. "Find session by UUID" (mobile app uses this to check session status)
CREATE INDEX idx_sessions_uuid
    ON sessions(session_uuid);

-- 3. "Find all suspect sessions" (alert worker runs this every 60 seconds)
CREATE INDEX idx_sessions_suspect
    ON sessions(status)
    WHERE status = 'suspect';


-- =============================================================
-- TABLE 3: session_events
-- One row per event inside a session.
-- APPEND ONLY — rows are never updated, only inserted.
-- This is your audit trail. Never delete from this table.
-- =============================================================
CREATE TABLE session_events (
    id                    BIGSERIAL PRIMARY KEY,
    session_id            BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    -- What type of event is this?
    -- Values: status_change, meter_value, command_sent, 
    --         suspect_detected, reconciled, error
    event_type            VARCHAR(50) NOT NULL,

    event_timestamp       TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Flexible JSON payload — different for each event type
    -- status_change:    {"from": "preparing", "to": "charging", "reason": "..."}
    -- meter_value:      {"power_kw": 45.2, "energy_kwh": 2.5, "voltage": 400}
    -- suspect_detected: {"reason": "no_meter_values_5min", "estimated_kwh": 15.2}
    -- reconciled:       {"estimated_kwh": 15.2, "actual_kwh": 16.8, "diff": 1.6}
    data                  JSONB NOT NULL,

    created_at            TIMESTAMP DEFAULT NOW()
);

-- Index: always filter by session_id when querying events
CREATE INDEX idx_session_events_session_id
    ON session_events(session_id);

-- Index: find all events of a specific type (e.g. all errors today)
CREATE INDEX idx_session_events_type_time
    ON session_events(event_type, event_timestamp);


-- =============================================================
-- TABLE 4: command_queue
-- Commands waiting to be delivered to chargers.
-- Gateway reads this continuously and sends pending commands.
-- =============================================================
CREATE TABLE command_queue (
    id                    BIGSERIAL PRIMARY KEY,
    session_id            BIGINT REFERENCES sessions(id),
    charger_id            VARCHAR(50) NOT NULL,

    -- What command to send?
    -- Values: RemoteStop, SetChargingProfile, Reset
    command_type          VARCHAR(50) NOT NULL,
    command_data          JSONB NOT NULL,               -- Command parameters

    -- Lifecycle: pending → sent → acknowledged (or failed)
    status                VARCHAR(50) DEFAULT 'pending',
    retry_count           INTEGER DEFAULT 0,            -- How many times we tried
    created_at            TIMESTAMP DEFAULT NOW(),
    updated_at            TIMESTAMP DEFAULT NOW()
);

-- Index: gateway queries "pending commands for connected chargers" continuously
CREATE INDEX idx_command_queue_charger_status
    ON command_queue(charger_id, status);


-- =============================================================
-- TABLE 5: billing
-- One row per completed session.
-- Separate from sessions so billing failure never blocks
-- session closure. Session closes cleanly, billing retries
-- independently via River Queue.
-- =============================================================
CREATE TABLE billing (
    id                    BIGSERIAL PRIMARY KEY,
    session_id            BIGINT UNIQUE NOT NULL REFERENCES sessions(id),

    energy_kwh            DECIMAL(10,3),
    amount_inr            DECIMAL(10,2),
    is_estimated          BOOLEAN DEFAULT FALSE,        -- Was this bill based on estimate?

    payment_status        VARCHAR(50),                  -- 'completed', 'failed', 'pending'
    payment_id            VARCHAR(100),                 -- Payment gateway reference
    receipt_sent          BOOLEAN DEFAULT FALSE,        -- Was receipt sent to driver?

    created_at            TIMESTAMP DEFAULT NOW(),
    updated_at            TIMESTAMP DEFAULT NOW()
);


-- =============================================================
-- SEED DATA: Two test chargers
-- =============================================================
INSERT INTO charger_config 
    (charger_id, site_name, charger_name, ocpp_version, power_limit_kw)
VALUES
    ('CH-001', 'Mumbai-Pune Highway KM 45', 'DC Fast Charger 1', '1.6', 60),
    ('CH-002', 'Mumbai-Pune Highway KM 45', 'DC Fast Charger 2', '1.6', 60),
    ('CH-003', 'Bangalore-Chennai Highway KM 120', 'DC Fast Charger 1', '1.6', 50);