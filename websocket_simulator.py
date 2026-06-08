# =============================================================
# MASSIVE CHARGING CMS — WEBSOCKET SIMULATOR
# =============================================================
# This file simulates charger behavior.
# In production, chargers send OCPP messages over WebSocket.
# Here we simulate those messages as direct function calls.
#
# Three scenarios:
# Scenario 1: Happy path (normal session start to finish)
# Scenario 2: 4G drop mid-session (charger reconnects with real value)
# Scenario 3: 4G drop mid-session (charger never reconnects → estimate)
# =============================================================

import time
from cms_backend import MassiveChargingCMS


def print_separator(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_step(step, description):
    print(f"\n[STEP {step}] {description}")
    print("-" * 40)


def simulate_meter_values(cms, session_uuid, count, power_kw, start_energy):
    """
    Simulate 'count' MeterValue messages from a charger.
    Each message arrives 30 seconds apart (simulated instantly).
    Energy increases with each reading.
    """
    energy = start_energy
    for i in range(count):
        energy += (power_kw / 1000) * (30 / 3600)  # kWh per 30 seconds
        cms.add_meter_value(session_uuid, power_kw, round(energy, 3))
        time.sleep(0.2)  # Small delay so output is readable
    return round(energy, 3)


# =============================================================
# SCENARIO 1: Happy Path
# Vehicle plugs in → pays → charges → unplugs → billed correctly
# =============================================================
def scenario_1_happy_path(cms):
    print_separator("SCENARIO 1: Happy Path — Normal Session")

    print_step(1, "Vehicle plugs into CH-001 connector 1")
    session = cms.create_session(
        charger_id='CH-001',
        connector_id=1,
        vehicle_id='EV-TATA-NEXON-001'
    )
    session_uuid = session['session_uuid']
    print(f"    Session UUID: {session_uuid}")
    print(f"    Status: {session['status']}")

    print_step(2, "Driver pays on mobile app — authorization confirmed")
    result = cms.start_charging(session_uuid)
    print(f"    Status: {result['status']}")

    print_step(3, "Charger sends MeterValues every 30 seconds (simulating 5 minutes)")
    final_energy = simulate_meter_values(
        cms, session_uuid,
        count=10,        # 10 meter value messages
        power_kw=45.0,   # Charging at 45 kW
        start_energy=0.0
    )
    print(f"    Final energy after 5 minutes: {final_energy} kWh")

    print_step(4, "Driver unplugs — charger sends StopTransaction")
    result = cms.stop_charging(session_uuid, final_energy_kwh=final_energy)
    print(f"    Status: {result['status']}")
    print(f"    Total energy: {result['total_energy_kwh']} kWh")
    print(f"    Total amount: ₹{result['total_amount_inr']}")
    print(f"    Is estimated: {result['is_estimated']}")

    print(f"\n✓ Scenario 1 complete. Session UUID for inspection: {session_uuid}")
    return session_uuid


# =============================================================
# SCENARIO 2: 4G Drop — Charger Reconnects with Real Value
# =============================================================
def scenario_2_drop_and_reconnect(cms):
    print_separator("SCENARIO 2: 4G Drop — Charger Reconnects")

    print_step(1, "Vehicle plugs into CH-002 connector 1")
    session = cms.create_session(
        charger_id='CH-002',
        connector_id=1,
        vehicle_id='EV-MG-ZS-001'
    )
    session_uuid = session['session_uuid']
    print(f"    Session UUID: {session_uuid}")

    print_step(2, "Authorization confirmed — charging starts")
    cms.start_charging(session_uuid)

    print_step(3, "Charger sends MeterValues for 3 minutes (normal charging)")
    last_energy = simulate_meter_values(
        cms, session_uuid,
        count=6,         # 6 messages = 3 minutes
        power_kw=50.0,   # 50 kW charging
        start_energy=0.0
    )
    print(f"    Last confirmed energy: {last_energy} kWh")

    print_step(4, "4G CONNECTION DROPS — no more MeterValues from charger")
    print("    [5 minutes pass with no signal...]")
    print("    Alert worker detects no MeterValues for 5 minutes")
    result = cms.mark_suspect(
        session_uuid,
        reason="no_meter_values_5min"
    )
    print(f"    Status: {result['status']}")
    print(f"    Estimated energy: {result['estimated_energy_kwh']} kWh")
    print(f"    Estimated amount: ₹{result['estimated_amount_inr']}")
    print(f"    Is estimated: {result['is_estimated']}")

    print_step(5, "CHARGER RECONNECTS — sends StopTransaction with real final value")
    print("    Charger sends actual final meter reading...")
    # Real final value is slightly different from estimate (normal)
    real_final_energy = last_energy + 2.1  # Charging continued during 4G outage
    result = cms.stop_charging(session_uuid, final_energy_kwh=real_final_energy)
    print(f"    Status: {result['status']}")
    print(f"    Final energy (actual): {result['total_energy_kwh']} kWh")
    print(f"    Final amount: ₹{result['total_amount_inr']}")
    print(f"    Is estimated: {result['is_estimated']} ← reconciled to actual value")

    print(f"\n✓ Scenario 2 complete. Session UUID for inspection: {session_uuid}")
    return session_uuid


# =============================================================
# SCENARIO 3: 4G Drop — Charger Never Reconnects
# Session closed with estimated billing
# =============================================================
def scenario_3_drop_never_reconnects(cms):
    print_separator("SCENARIO 3: 4G Drop — Charger Never Reconnects")

    print_step(1, "Vehicle plugs into CH-003 connector 1")
    session = cms.create_session(
        charger_id='CH-003',
        connector_id=1,
        vehicle_id='EV-HYUNDAI-IONIQ-001'
    )
    session_uuid = session['session_uuid']
    print(f"    Session UUID: {session_uuid}")

    print_step(2, "Authorization confirmed — charging starts")
    cms.start_charging(session_uuid)

    print_step(3, "Charger sends MeterValues for 2 minutes")
    last_energy = simulate_meter_values(
        cms, session_uuid,
        count=4,         # 4 messages = 2 minutes
        power_kw=40.0,
        start_energy=0.0
    )
    print(f"    Last confirmed energy: {last_energy} kWh")

    print_step(4, "4G CONNECTION DROPS — charger goes completely offline")
    print("    [5 minutes pass with no signal...]")
    result = cms.mark_suspect(
        session_uuid,
        reason="no_meter_values_5min"
    )
    print(f"    Status: {result['status']}")
    print(f"    Estimated energy: {result['estimated_energy_kwh']} kWh")
    print(f"    Estimated amount: ₹{result['estimated_amount_inr']}")

    print_step(5, "30 minutes pass — charger still offline")
    print("    Alert worker decides: close session with estimated billing")
    print("    [In production: River Queue alert worker triggers this]")
    print("    [Here: we call it manually to simulate]")

    # Use estimated energy as final value (charger never came back)
    estimated_energy = float(result['estimated_energy_kwh'])
    final_result = cms.stop_charging(
        session_uuid,
        final_energy_kwh=estimated_energy
    )
    print(f"    Status: {final_result['status']}")
    print(f"    Final energy (estimated): {final_result['total_energy_kwh']} kWh")
    print(f"    Final amount: ₹{final_result['total_amount_inr']}")
    print(f"    Billing flagged for ops review: {final_result['is_estimated']}")

    print(f"\n✓ Scenario 3 complete. Session UUID for inspection: {session_uuid}")
    return session_uuid


# =============================================================
# INSPECTION: Show full session history from database
# =============================================================
def inspect_session(cms, session_uuid, label):
    print_separator(f"INSPECTION: {label}")

    history = cms.get_session_history(session_uuid)
    session = history['session']
    events = history['events']

    print(f"\nSession Summary:")
    print(f"  UUID:          {session['session_uuid']}")
    print(f"  Charger:       {session['charger_id']} connector {session['connector_id']}")
    print(f"  Status:        {session['status']}")
    print(f"  Energy:        {session['total_energy_kwh']} kWh")
    print(f"  Amount:        ₹{session['total_amount_inr']}")
    print(f"  Is estimated:  {session['is_estimated']}")
    print(f"  Billing:       {session['billing_status']}")

    print(f"\nEvent Log ({len(events)} events):")
    for e in events:
        print(f"  [{e['event_timestamp']}] {e['event_type']}")
        print(f"    {e['data']}")


# =============================================================
# MAIN: Run all three scenarios
# =============================================================
if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  MASSIVE CHARGING CMS — SIMULATOR")
    print("  Simulating 3 charging scenarios")
    print("=" * 60)

    # Connect to PostgreSQL
    cms = MassiveChargingCMS()

    try:
        # Run all three scenarios
        uuid_1 = scenario_1_happy_path(cms)
        uuid_2 = scenario_2_drop_and_reconnect(cms)
        uuid_3 = scenario_3_drop_never_reconnects(cms)

        # Inspect each session's full history
        print("\n\n")
        inspect_session(cms, uuid_1, "Scenario 1 — Happy Path")
        inspect_session(cms, uuid_2, "Scenario 2 — Drop and Reconnect")
        inspect_session(cms, uuid_3, "Scenario 3 — Drop, Never Reconnects")

        # Show suspect sessions (should be empty — all resolved)
        print_separator("SUSPECT SESSIONS CHECK")
        suspect = cms.get_suspect_sessions()
        if not suspect:
            print("\n✓ No suspect sessions — all sessions resolved correctly.")
        else:
            print(f"\n⚠ {len(suspect)} suspect session(s) still open:")
            for s in suspect:
                print(f"  {s['session_uuid']} — {s['charger_id']}")

    finally:
        cms.close()

    print("\n" + "=" * 60)
    print("  SIMULATION COMPLETE")
    print("  Check pgAdmin at localhost:5050 to inspect the database")
    print("=" * 60 + "\n")