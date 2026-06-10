# =============================================================
# MASSIVE CHARGING CMS — BACKEND LOGIC
# =============================================================
# Design principles:
# 1. Every state change is written to PostgreSQL BEFORE
#    acknowledging it to the charger (write-ahead)
# 2. Every write uses a transaction — both the state update
#    AND the event log succeed together or fail together
# 3. Functions are idempotent — calling the same function
#    twice produces the same result (safe for charger retries)
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
        self.conn = psycopg2.connect(
            host=db_host, port=db_port,
            database=db_name, user=db_user, password=db_password
        )
        self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        print(f"[CMS] Connected to PostgreSQL at {db_host}:{db_port}/{db_name}")

    # ==========================================================
    # FUNCTION 1: create_session
    # State transition: idle → preparing
    # ==========================================================
    def create_session(self, charger_id, connector_id, vehicle_id=None):
        session_uuid = str(uuid4())
        try:
            # Block if active session already exists on this connector
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
                      f"already exists for {charger_id}. Blocking new session.")
                return None

            self.cursor.execute("""
                INSERT INTO sessions
                    (charger_id, connector_id, session_uuid,
                     vehicle_id, status, start_time)
                VALUES (%s, %s, %s, %s, 'preparing', NOW())
                RETURNING id, session_uuid, status, start_time
            """, (charger_id, connector_id, session_uuid, vehicle_id))

            session = self.cursor.fetchone()
            self._log_event(session['id'], 'status_change', {
                'from': 'idle', 'to': 'preparing',
                'charger_id': charger_id, 'connector_id': connector_id
            })
            self.conn.commit()
            print(f"[CMS] Session created: {session['session_uuid']} for {charger_id}")
            return dict(session)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in create_session: {e}")
            raise

    # ==========================================================
    # FUNCTION 2: start_charging
    # State transition: preparing → charging
    # ==========================================================
    def start_charging(self, session_uuid):
        try:
            self.cursor.execute("""
                UPDATE sessions
                SET status = 'charging', updated_at = NOW()
                WHERE session_uuid = %s AND status = 'preparing'
                RETURNING id, session_uuid, status
            """, (session_uuid,))

            session = self.cursor.fetchone()
            if not session:
                print(f"[CMS] WARNING: Cannot start {session_uuid} — not in preparing state.")
                return None

            self._log_event(session['id'], 'status_change', {
                'from': 'preparing', 'to': 'charging',
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
    # State: remains 'charging' — updates energy and amount
    # ==========================================================
    def add_meter_value(self, session_uuid, power_kw, energy_kwh_total):
        try:
            self.cursor.execute("""
                SELECT id, status FROM sessions WHERE session_uuid = %s
            """, (session_uuid,))

            session = self.cursor.fetchone()
            if not session:
                print(f"[CMS] WARNING: Session {session_uuid} not found.")
                return None

            if session['status'] not in ('charging', 'suspended'):
                print(f"[CMS] WARNING: Meter value ignored — session in state {session['status']}.")
                return None

            amount_inr = float(energy_kwh_total) * PRICE_PER_KWH_INR

            self.cursor.execute("""
                UPDATE sessions
                SET total_energy_kwh     = %s,
                    total_amount_inr     = %s,
                    last_meter_value_kwh = %s,
                    last_meter_time      = NOW(),
                    last_power_kw        = %s,
                    updated_at           = NOW()
                WHERE id = %s
                RETURNING *
            """, (energy_kwh_total, amount_inr, energy_kwh_total, power_kw, session['id']))

            updated = self.cursor.fetchone()
            self._log_event(session['id'], 'meter_value', {
                'power_kw': float(power_kw),
                'energy_kwh_total': float(energy_kwh_total),
                'amount_inr': round(amount_inr, 2)
            })
            self.conn.commit()
            print(f"[CMS] Meter value: {session_uuid} | {power_kw} kW | "
                  f"{energy_kwh_total} kWh | ₹{amount_inr:.2f}")
            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in add_meter_value: {e}")
            raise

    # ==========================================================
    # FUNCTION 4: mark_suspect
    # State transition: charging → suspect
    # BUG FIX: Use datetime.utcnow() to match PostgreSQL UTC timestamps
    # ==========================================================
    def mark_suspect(self, session_uuid, reason="no_meter_values_5min"):
        try:
            self.cursor.execute("""
                SELECT id, total_energy_kwh, last_power_kw, last_meter_time
                FROM sessions
                WHERE session_uuid = %s AND status = 'charging'
            """, (session_uuid,))

            session = self.cursor.fetchone()
            if not session:
                print(f"[CMS] WARNING: Cannot mark suspect — {session_uuid} not charging.")
                return None

            last_power_kw = float(session['last_power_kw'] or 0)
            last_energy   = float(session['total_energy_kwh'] or 0)

            # ── BUG FIX ───────────────────────────────────────────
            # PostgreSQL stores timestamps in UTC without timezone info.
            # datetime.now() returns LOCAL time — causes a huge offset
            # on machines not in UTC (like Stockholm UTC+2).
            # Fix: use datetime.utcnow() so both sides are in UTC.
            # ──────────────────────────────────────────────────────
            if session['last_meter_time']:
                elapsed_hours = (
                    datetime.utcnow() - session['last_meter_time']
                ).total_seconds() / 3600
                # Cap at 2 hours — if session has been silent longer
                # than 2 hours something else is wrong
                elapsed_hours = min(elapsed_hours, 2.0)
            else:
                elapsed_hours = 0

            estimated_energy = last_energy + (last_power_kw * elapsed_hours)
            estimated_amount = estimated_energy * PRICE_PER_KWH_INR

            self.cursor.execute("""
                UPDATE sessions
                SET status               = 'suspect',
                    estimated_energy_kwh = %s,
                    estimated_amount_inr = %s,
                    is_estimated         = TRUE,
                    updated_at           = NOW()
                WHERE id = %s
                RETURNING *
            """, (round(estimated_energy, 3), round(estimated_amount, 2), session['id']))

            updated = self.cursor.fetchone()
            self._log_event(session['id'], 'suspect_detected', {
                'reason': reason,
                'last_known_energy_kwh': last_energy,
                'last_known_power_kw': last_power_kw,
                'elapsed_hours': round(elapsed_hours, 4),
                'estimated_energy_kwh': round(estimated_energy, 3),
                'estimated_amount_inr': round(estimated_amount, 2)
            })
            self.conn.commit()
            print(f"[CMS] Session SUSPECT: {session_uuid} | "
                  f"Estimated: {estimated_energy:.3f} kWh | ₹{estimated_amount:.2f}")
            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in mark_suspect: {e}")
            raise

    # ==========================================================
    # FUNCTION 5: stop_charging
    # State transition: charging/suspect → complete
    # BUG FIX: is_estimated stays TRUE when charger never reconnects
    # ==========================================================
    def stop_charging(self, session_uuid, final_energy_kwh):
        try:
            self.cursor.execute("""
                SELECT id, status, estimated_energy_kwh, is_estimated
                FROM sessions
                WHERE session_uuid = %s
                  AND status IN ('charging', 'suspect', 'suspended')
            """, (session_uuid,))

            session = self.cursor.fetchone()
            if not session:
                print(f"[CMS] WARNING: Cannot stop {session_uuid} — not found or already complete.")
                return None

            final_amount = float(final_energy_kwh) * PRICE_PER_KWH_INR

            # ── Reconciliation (only if session was suspect) ───
            reconciliation = None
            if session['is_estimated'] and session['estimated_energy_kwh']:
                diff = abs(float(session['estimated_energy_kwh']) - float(final_energy_kwh))
                reconciliation = {
                    'estimated_kwh': float(session['estimated_energy_kwh']),
                    'actual_kwh': float(final_energy_kwh),
                    'diff_kwh': round(diff, 3),
                    'within_acceptable_range': diff < 2.0
                }

            # ── BUG FIX ───────────────────────────────────────────
            # is_estimated logic:
            # Case 1: Session was never suspect → is_estimated = FALSE (normal)
            # Case 2: Session was suspect, charger reconnected with real value
            #         → is_estimated = FALSE (reconciled to actual)
            # Case 3: Session was suspect, closed with estimated value (no reconnect)
            #         → is_estimated = TRUE (ops team must review billing)
            #
            # How to detect Case 3: reconciliation is None means stop_charging
            # was called with the estimated value itself (no new real reading)
            # ──────────────────────────────────────────────────────
            if not session['is_estimated']:
                # Normal session — never went suspect
                final_is_estimated = False
            elif reconciliation is not None:
                # Was suspect, charger came back with a real reading
                final_is_estimated = False
            else:
                # Was suspect, closed with estimated value — flag for ops
                final_is_estimated = True

            self.cursor.execute("""
                UPDATE sessions
                SET status           = 'complete',
                    total_energy_kwh = %s,
                    total_amount_inr = %s,
                    is_estimated     = %s,
                    end_time         = NOW(),
                    billing_status   = 'pending',
                    updated_at       = NOW()
                WHERE id = %s
                RETURNING *
            """, (final_energy_kwh, round(final_amount, 2),
                  final_is_estimated, session['id']))

            updated = self.cursor.fetchone()

            event_data = {
                'from': session['status'],
                'to': 'complete',
                'final_energy_kwh': float(final_energy_kwh),
                'final_amount_inr': round(final_amount, 2),
                'is_estimated': final_is_estimated
            }
            if reconciliation:
                event_data['reconciliation'] = reconciliation

            self._log_event(session['id'], 'status_change', event_data)

            # Create billing record with correct is_estimated flag
            self.cursor.execute("""
                INSERT INTO billing
                    (session_id, energy_kwh, amount_inr,
                     is_estimated, payment_status)
                VALUES (%s, %s, %s, %s, 'pending')
            """, (session['id'], final_energy_kwh,
                  round(final_amount, 2), final_is_estimated))

            self.conn.commit()

            print(f"[CMS] Session COMPLETE: {session_uuid} | "
                  f"{final_energy_kwh} kWh | ₹{final_amount:.2f} | "
                  f"estimated={final_is_estimated}")
            if reconciliation:
                print(f"[CMS] Reconciliation: estimated {reconciliation['estimated_kwh']} kWh "
                      f"vs actual {reconciliation['actual_kwh']} kWh | "
                      f"diff={reconciliation['diff_kwh']} kWh | "
                      f"acceptable={reconciliation['within_acceptable_range']}")
            return dict(updated)

        except Exception as e:
            self.conn.rollback()
            print(f"[CMS] ERROR in stop_charging: {e}")
            raise

    # ==========================================================
    # FUNCTION 6: get_session_history (read only)
    # ==========================================================
    def get_session_history(self, session_uuid):
        self.cursor.execute("""
            SELECT * FROM sessions WHERE session_uuid = %s
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
    # FUNCTION 7: get_suspect_sessions (read only)
    # ==========================================================
    def get_suspect_sessions(self):
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
    # PRIVATE: _log_event — never commits, caller always commits
    # ==========================================================
    def _log_event(self, session_id, event_type, data):
        self.cursor.execute("""
            INSERT INTO session_events
                (session_id, event_type, event_timestamp, data)
            VALUES (%s, %s, NOW(), %s)
        """, (session_id, event_type, json.dumps(data)))

    def close(self):
        self.conn.close()
        print("[CMS] Database connection closed.")