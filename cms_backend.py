# =============================================================
# MASSIVE CHARGING CMS — BACKEND LOGIC
# =============================================================
# This file is the brain of the system.
# It contains the state machine logic for charging sessions.
#
# Design principles:
# 1. Every state change is written to PostgreSQL BEFORE
#    acknowledging it to the charger (write-ahead)
# 2. Every write uses a transaction — both the state update
#    AND the event log succeed together or fail together
# 3. Functions are idempotent where possible — calling the
#    same function twice produces the same result
# =============================================================

import psycopg2
from psycopg2.extras import RealDictCursor
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

# Price per kWh in Indian Rupees
PRICE_PER_KWH_INR = 20.0


class MassiveChargingCMS:

    def __init__(self, db_host='localhost', db_port=5432,
                 db_name='massive_charging', db_user='postgres',
                 db_password='postgres'):
        """
        Connect to PostgreSQL on startup.
        RealDictCursor means rows come back as dictionaries
        instead of tuples — much easier to work with.
        """
        self.conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password
        )
        # RealDictCursor: row['charger_id'] instead of row[0]
        self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        print(f"[CMS] Connected to PostgreSQL at {db_host}:{db_port}/{db_name}")

    # ==========================================================
    # FUNCTION 1: create_session
    # Triggered when: vehicle plugs in, charger sends
    # StatusNotification: Preparing
    # State transition: idle → preparing
    # ==========================================================
    def create_session(self, charger_id, connector_id, vehicle_id=None):
        """
        Create a new charging session.
        Returns the new session record.
        """
        session_uuid = str(uuid4())

        try:
            # Step 1: Check for existing active session on this connector
            # (Edge case 4.2: double session on reconnect)
            self.cursor.execute("""
                SELECT id, session_uuid, status
                FROM sessions
                WHERE charger_id = %s
                  AND connector_id = %s
                  AND status NOT IN ('complete', 'finishing')
            """, (charger_id, connector_id))

            existing = self.cursor.fetchone()
            if existing:
                print(f"[CMS] WARNING: Active session {existing['session_uuid']} "
                      f"already exists for {charger_id} connector {connector_id}. "
                      f"Blocking new session.")
                return None

            # Step 2: Create the session record
            self.cursor.execute("""
                INSERT INTO sessions
                    (charger_id, connector_id, session_uuid,
                     vehicle_id, status, start_time)
                VALUES (%s, %s, %s, %s, 'preparing', NOW())
                RETURNING id, session_uuid, status, start_time
            """, (charger_id, connector_id, session_uuid, vehicle_id))

            session = self.cursor.fetchone()

            # Step 3: Log the event
            # Both steps commit together — atomicity
            self._log_event(session['id'], 'status_change', {
                'from': 'idle',
                'to': 'preparing',
                'charger_id': charger_id,
                'connector_id': connector_id
            })

            # Step 4: Commit both writes together
            self.conn.commit()

            print(f"[CMS] Session created: {session['session_uuid']} "
                  f"for {charger_id} connector {connector_id}")
            return dict(session)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in create_session: {e}")
            raise

    # ==========================================================
    # FUNCTION 2: start_charging
    # Triggered when: payment confirmed, charger sends
    # StartTransaction
    # State transition: preparing → charging
    # ==========================================================
    def start_charging(self, session_uuid):
        """
        Move session from preparing to charging.
        Only allowed if session is currently in 'preparing' state.
        """
        try:
            # Note the WHERE clause: status = 'preparing'
            # If session is not in preparing state, UPDATE affects 0 rows
            # This is idempotency — wrong state = no change
            self.cursor.execute("""
                UPDATE sessions
                SET status = 'charging',
                    updated_at = NOW()
                WHERE session_uuid = %s
                  AND status = 'preparing'
                RETURNING id, session_uuid, status
            """, (session_uuid,))

            session = self.cursor.fetchone()

            if not session:
                print(f"[CMS] WARNING: Cannot start charging for {session_uuid}. "
                      f"Session not found or not in preparing state.")
                return None

            self._log_event(session['id'], 'status_change', {
                'from': 'preparing',
                'to': 'charging',
                'reason': 'authorization_confirmed'
            })

            self.conn.commit()
            print(f"[CMS] Charging started: {session_uuid}")
            return dict(session)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in start_charging: {e}")
            raise

    # ==========================================================
    # FUNCTION 3: add_meter_value
    # Triggered when: charger sends MeterValues (every 30 sec)
    # State: remains 'charging' — just updates energy and amount
    # ==========================================================
    def add_meter_value(self, session_uuid, power_kw, energy_kwh_total):
        """
        Record a MeterValues update from the charger.
        Updates running energy total and calculates amount in INR.
        """
        try:
            # Read current session state
            self.cursor.execute("""
                SELECT id, status, total_energy_kwh, last_power_kw
                FROM sessions
                WHERE session_uuid = %s
            """, (session_uuid,))

            session = self.cursor.fetchone()

            if not session:
                print(f"[CMS] WARNING: Session {session_uuid} not found.")
                return None

            if session['status'] not in ('charging', 'suspended'):
                print(f"[CMS] WARNING: Received meter value for session "
                      f"{session_uuid} in state {session['status']}. Ignoring.")
                return None

            # Calculate amount in INR based on energy delivered
            amount_inr = float(energy_kwh_total) * PRICE_PER_KWH_INR

            # Update session with latest meter value
            self.cursor.execute("""
                UPDATE sessions
                SET total_energy_kwh   = %s,
                    total_amount_inr   = %s,
                    last_meter_value_kwh = %s,
                    last_meter_time    = NOW(),
                    last_power_kw      = %s,
                    updated_at         = NOW()
                WHERE id = %s
                RETURNING *
            """, (energy_kwh_total, amount_inr,
                  energy_kwh_total, power_kw, session['id']))

            updated = self.cursor.fetchone()

            # Log meter value event
            self._log_event(session['id'], 'meter_value', {
                'power_kw': float(power_kw),
                'energy_kwh_total': float(energy_kwh_total),
                'amount_inr': round(amount_inr, 2)
            })

            self.conn.commit()
            print(f"[CMS] Meter value: {session_uuid} | "
                  f"{power_kw} kW | {energy_kwh_total} kWh | ₹{amount_inr:.2f}")
            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in add_meter_value: {e}")
            raise

    # ==========================================================
    # FUNCTION 4: mark_suspect
    # Triggered when: no MeterValues received for 5 minutes
    # State transition: charging → suspect
    # This is the 4G drop scenario
    # ==========================================================
    def mark_suspect(self, session_uuid, reason="no_meter_values_5min"):
        """
        Mark a session as suspect when connection is lost.
        Calculates estimated final energy based on last known state.
        """
        try:
            self.cursor.execute("""
                SELECT id, total_energy_kwh, last_power_kw, last_meter_time
                FROM sessions
                WHERE session_uuid = %s
                  AND status = 'charging'
            """, (session_uuid,))

            session = self.cursor.fetchone()

            if not session:
                print(f"[CMS] WARNING: Cannot mark suspect — session "
                      f"{session_uuid} not found or not charging.")
                return None

            # ── Estimate final energy ──────────────────────────
            # Formula: last known energy + (last known power × time elapsed)
            # This is the best estimate we can make with available data
            last_power_kw = float(session['last_power_kw'] or 0)
            last_energy = float(session['total_energy_kwh'] or 0)

            if session['last_meter_time']:
                # PostgreSQL stores timestamps as UTC without timezone info
                # Use utcnow() to compare correctly, avoiding local timezone offset
                elapsed_hours = (
                    datetime.utcnow() - session['last_meter_time']
                ).total_seconds() / 3600
                estimated_delta = last_power_kw * elapsed_hours
            else:
                estimated_delta = 0

            estimated_energy = last_energy + estimated_delta
            estimated_amount = estimated_energy * PRICE_PER_KWH_INR

            # Update session to suspect
            self.cursor.execute("""
                UPDATE sessions
                SET status               = 'suspect',
                    estimated_energy_kwh = %s,
                    estimated_amount_inr = %s,
                    is_estimated         = TRUE,
                    updated_at           = NOW()
                WHERE id = %s
                RETURNING *
            """, (round(estimated_energy, 3),
                  round(estimated_amount, 2),
                  session['id']))

            updated = self.cursor.fetchone()

            self._log_event(session['id'], 'suspect_detected', {
                'reason': reason,
                'last_known_energy_kwh': last_energy,
                'last_known_power_kw': last_power_kw,
                'estimated_energy_kwh': round(estimated_energy, 3),
                'estimated_amount_inr': round(estimated_amount, 2)
            })

            self.conn.commit()
            print(f"[CMS] Session marked SUSPECT: {session_uuid} | "
                  f"Estimated energy: {estimated_energy:.3f} kWh | "
                  f"₹{estimated_amount:.2f}")
            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in mark_suspect: {e}")
            raise

    # ==========================================================
    # FUNCTION 5: stop_charging
    # Triggered when: charger sends StopTransaction
    # Works from both 'charging' state (normal) and
    # 'suspect' state (charger reconnected with final value)
    # State transition: charging/suspect → complete
    # ==========================================================
    def stop_charging(self, session_uuid, final_energy_kwh):
        """
        Close a session with the final confirmed energy value.
        If session was suspect, reconciles estimated vs actual.
        Triggers billing.
        """
        try:
            self.cursor.execute("""
                SELECT id, status, estimated_energy_kwh, is_estimated
                FROM sessions
                WHERE session_uuid = %s
                  AND status IN ('charging', 'suspect', 'suspended')
            """, (session_uuid,))

            session = self.cursor.fetchone()

            if not session:
                print(f"[CMS] WARNING: Cannot stop — session {session_uuid} "
                      f"not found or already complete.")
                return None

            final_amount = float(final_energy_kwh) * PRICE_PER_KWH_INR

            # ── Reconciliation (if session was suspect) ────────
            reconciliation = None
            if session['is_estimated'] and session['estimated_energy_kwh']:
                diff = abs(float(session['estimated_energy_kwh'])
                           - float(final_energy_kwh))
                reconciliation = {
                    'estimated_kwh': float(session['estimated_energy_kwh']),
                    'actual_kwh': float(final_energy_kwh),
                    'diff_kwh': round(diff, 3),
                    'within_acceptable_range': diff < 2.0  # 2 kWh tolerance
                }

            # If session was suspect and we're closing with the same estimated value
            # keep is_estimated = TRUE so ops team knows this needs review
            # Only set is_estimated = FALSE when charger provided a real final reading
            was_reconciled = (
                reconciliation is not None and
                reconciliation['within_acceptable_range']
            )
            # If charger never reconnected, final_energy == estimated_energy → still estimated
            still_estimated = (
                session['is_estimated'] and reconciliation is None
            ) or (
                session['is_estimated'] and
                reconciliation is not None and
                not reconciliation['within_acceptable_range'] == False
            )
            final_is_estimated = session['is_estimated'] and reconciliation is None

            # Mark session complete with real final values
            self.cursor.execute("""
                UPDATE sessions
                SET status             = 'complete',
                    total_energy_kwh   = %s,
                    total_amount_inr   = %s,
                    is_estimated       = %s,
                    end_time           = NOW(),
                    billing_status     = 'pending',
                    updated_at         = NOW()
                WHERE id = %s
                RETURNING *
            """, (final_energy_kwh, round(final_amount, 2),
                  final_is_estimated, session['id']))

            updated = self.cursor.fetchone()

            # Log completion event
            event_data = {
                'from': session['status'],
                'to': 'complete',
                'final_energy_kwh': float(final_energy_kwh),
                'final_amount_inr': round(final_amount, 2)
            }
            if reconciliation:
                event_data['reconciliation'] = reconciliation

            self._log_event(session['id'], 'status_change', event_data)

            # Create billing record
            self.cursor.execute("""
                INSERT INTO billing
                    (session_id, energy_kwh, amount_inr,
                     is_estimated, payment_status)
                VALUES (%s, %s, %s, FALSE, 'pending')
            """, (session['id'], final_energy_kwh, round(final_amount, 2)))

            self.conn.commit()

            print(f"[CMS] Session COMPLETE: {session_uuid} | "
                  f"{final_energy_kwh} kWh | ₹{final_amount:.2f}")
            if reconciliation:
                print(f"[CMS] Reconciliation: estimated {reconciliation['estimated_kwh']} kWh "
                      f"vs actual {reconciliation['actual_kwh']} kWh | "
                      f"diff = {reconciliation['diff_kwh']} kWh")

            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in stop_charging: {e}")
            raise

    # ==========================================================
    # FUNCTION 6: get_session_history
    # Read-only — returns complete audit trail for one session
    # Used by: ops dashboard, customer dispute resolution
    # ==========================================================
    def get_session_history(self, session_uuid):
        """
        Return complete session record + all events in order.
        This is your audit trail for billing disputes.
        """
        self.cursor.execute("""
            SELECT * FROM sessions
            WHERE session_uuid = %s
        """, (session_uuid,))

        session = self.cursor.fetchone()
        if not session:
            return None

        self.cursor.execute("""
            SELECT event_type, event_timestamp, data
            FROM session_events
            WHERE session_id = %s
            ORDER BY event_timestamp ASC
        """, (session['id'],))

        events = self.cursor.fetchall()

        return {
            'session': dict(session),
            'events': [dict(e) for e in events]
        }

    # ==========================================================
    # FUNCTION 7: get_suspect_sessions
    # Read-only — returns all suspect sessions
    # Used by: alert worker (runs every 60 seconds)
    # ==========================================================
    def get_suspect_sessions(self):
        """
        Return all sessions currently in suspect state.
        Alert worker uses this to decide which sessions to close
        with estimated billing.
        """
        self.cursor.execute("""
            SELECT id, session_uuid, charger_id, connector_id,
                   last_meter_time, estimated_energy_kwh,
                   estimated_amount_inr, updated_at
            FROM sessions
            WHERE status = 'suspect'
            ORDER BY last_meter_time ASC
        """)
        return [dict(r) for r in self.cursor.fetchall()]

    # ==========================================================
    # PRIVATE HELPER: _log_event
    # Called by every function above to write to session_events
    # Never called directly from outside this class
    # ==========================================================
    def _log_event(self, session_id, event_type, data):
        """
        Append an event to session_events table.
        Called inside every state transition — before commit.
        This means if the commit fails, the event is also rolled back.
        """
        self.cursor.execute("""
            INSERT INTO session_events
                (session_id, event_type, event_timestamp, data)
            VALUES (%s, %s, NOW(), %s)
        """, (session_id, event_type, json.dumps(data)))
        # Note: NO commit here — caller commits after both
        # the state update AND the event log are written

    def close(self):
        """Close database connection cleanly."""
        self.conn.close()
        print("[CMS] Database connection closed.")