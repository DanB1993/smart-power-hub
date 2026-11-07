import asyncio
import base64
import json
import math
import os
import socket
import sqlite3
import struct
import threading
import time
import urllib.request
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from discovery import load_config, save_config, scan_network_for_devices, DNO_REGIONS, TARIFF_TEMPLATES, BATTERY_PRESETS

try:
    from tapo import ApiClient
    HAS_TAPO = True
except ImportError:
    HAS_TAPO = False

try:
    from kasa import Discover, Credentials
    HAS_KASA = True
except ImportError:
    HAS_KASA = False

app_config = load_config()

INVERTER_HOST = os.environ.get("INVERTER_HOST", app_config.get("inverter", {}).get("host", "192.168.1.100"))
INVERTER_PORT = int(os.environ.get("INVERTER_PORT", str(app_config.get("inverter", {}).get("port", 502))))
INVERTER_UNIT = int(os.environ.get("INVERTER_UNIT", str(app_config.get("inverter", {}).get("unit_id", 247))))
BATTERY_CAPACITY_KWH = float(app_config.get("battery", {}).get("gross_capacity_kwh", 9.84))
BATTERY_USABLE_KWH = float(app_config.get("battery", {}).get("usable_capacity_kwh", 8.86))
ADAPTIVE_HEADROOM_ENABLED = bool(app_config.get("battery", {}).get("adaptive_headroom_enabled", True))
ADAPTIVE_BUFFER_PCT = int(app_config.get("battery", {}).get("adaptive_buffer_pct", 10))
ADAPTIVE_TOPUP_ENABLED = bool(app_config.get("battery", {}).get("adaptive_topup_enabled", True))
ADAPTIVE_TOPUP_DROP_PCT = int(app_config.get("battery", {}).get("adaptive_topup_drop_pct", 3))
HUE_HOST = os.environ.get("HUE_HOST", app_config.get("devices", {}).get("hue", {}).get("host", "192.168.1.150"))
HUE_API_KEY = os.environ.get("HUE_API_KEY", app_config.get("devices", {}).get("hue", {}).get("api_key", ""))
OCTOPUS_API_KEY = os.environ.get("OCTOPUS_API_KEY", app_config.get("tariff", {}).get("api_key", ""))
OCTOPUS_ACCOUNT = os.environ.get("OCTOPUS_ACCOUNT", app_config.get("tariff", {}).get("account_number", ""))
OCTOPUS_PRODUCT_CODE = os.environ.get("OCTOPUS_PRODUCT", app_config.get("tariff", {}).get("product_code", "GO-FIX-12M-26-10-14"))
OCTOPUS_TARIFF_CODE = os.environ.get("OCTOPUS_TARIFF", app_config.get("tariff", {}).get("import_tariff_code", "E-1R-GO-FIX-12M-26-10-14-H"))
OCTOPUS_EXPORT_RATE_P_KWH = float(os.environ.get("OCTOPUS_EXPORT_RATE", str(app_config.get("tariff", {}).get("fixed_export_rate_p_kwh", 12.0))))
OCTOPUS_EXPORT_ACTIVE = os.environ.get("OCTOPUS_EXPORT_ACTIVE", "false").lower() == "true"
LATITUDE = float(os.environ.get("LATITUDE", str(app_config.get("location", {}).get("latitude", 51.5074))))
LONGITUDE = float(os.environ.get("LONGITUDE", str(app_config.get("location", {}).get("longitude", -0.1278))))
TAPO_DEFAULT_IP = os.environ.get("TAPO_IP", "192.168.1.200")
TAPO_ENV_USER = os.environ.get("TAPO_USERNAME", "")
TAPO_ENV_PASS = os.environ.get("TAPO_PASSWORD", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8050"))
DB_PATH = os.environ.get("DB_PATH", "/data/solar_history.db")

# Hardware power profiles (standby_w, min_on_w, max_on_w) based on Signify / Powercalc LUTs
HUE_POWER_PROFILES = {
    "LCX001": (0.40, 2.2, 20.0),  # Hue Play Gradient Lightstrip
    "LWA018": (0.40, 1.4, 15.5),  # Hue White 1600lm High Lumen
    "LWB010": (0.40, 1.1, 9.0),   # Hue White 800lm
    "LTA001": (0.35, 1.0, 8.5),   # Hue White Ambiance 800lm
    "LWA012": (0.40, 1.0, 8.5),   # Hue White 800lm
}
DEFAULT_HUE_PROFILE = (0.40, 1.2, 9.0)

STATE_MAP = {
    0: "Self-Test",
    1: "Waiting",
    2: "Checking",
    3: "On-Grid (Normal)",
    4: "Off-Grid / EPS",
    5: "Recoverable Fault",
    6: "Unrecoverable Fault",
}

WORK_MODE_MAP = {
    0: "Self-Use",
    1: "Feed-in Priority",
    2: "Back-up",
    3: "Peak Shaving",
    4: "Force Charge",
    5: "Force Discharge",
}

lock = threading.Lock()
latest_data = {
    "status": "initializing",
    "last_updated": None,
    "poll_interval_sec": POLL_INTERVAL,
    "error": None,
}
latest_hue = {
    "status": "initializing",
    "bridge_ip": HUE_HOST,
    "total_watts": 0.0,
    "max_watts": 87.5,
    "lights_on_count": 0,
    "lights_total_count": 0,
    "rooms": [],
    "lights": [],
}
latest_tapo = {
    "status": "needs_auth",
    "configured": False,
    "total_watts": 0.0,
    "plugs": [
        {
            "ip": TAPO_DEFAULT_IP,
            "alias": "Desk Plug (P110M)",
            "model": "P110M(UK)",
            "mac": "AC-A7-F1-CC-92-5A",
            "is_on": False,
            "watts": 0.0,
            "today_kwh": 0.0,
            "month_kwh": 0.0,
            "authenticated": False,
        }
    ],
    "error": None,
}
latest_shelly = {
    "status": "online",
    "total_watts": 0.0,
    "plugs": [],
}
latest_octopus = {
    "status": "initializing",
    "tariff_name": "Octopus Go 12M Fixed",
    "tariff_code": OCTOPUS_TARIFF_CODE,
    "current_rate_p_kwh": 36.56,
    "is_off_peak": False,
    "off_peak_rate_p_kwh": 9.40,
    "peak_rate_p_kwh": 36.56,
    "off_peak_window": "00:30 - 05:30",
    "next_transition": None,
    "last_fetched": None,
    # Octopus Agile Outgoing Export Feed & Shadow Tracker
    "agile_outgoing": {
        "status": "initializing",
        "current_export_p_kwh": 15.0,
        "next_export_p_kwh": None,
        "next_rate_time": None,
        "peak_window_today": "16:00 - 19:00",
        "peak_rate_today_p_kwh": 32.0,
        "min_rate_today_p_kwh": 8.0,
        "realized_rate_p_kwh": 0.0,
        "today_shadow_earned_gbp": 0.0,
        "standard_export_earned_gbp": 0.0,
        "agile_uplift_gbp": 0.0,
        "top_slots_today": [],
        "export_strategy": None,
        "half_hourly_rates": [],
        "half_hourly_tomorrow": [],
        "last_updated": None,
    },
}
latest_weather = {
    "status": "initializing",
    "city": "Reading / Local",
    "current_temp_c": 15.0,
    "current_weathercode": 0,
    "current_weather_desc": "Clear",
    "today_solar_kwh": 0.0,
    "tomorrow_solar_kwh": 0.0,
    "tomorrow_temp_max_c": 15.0,
    "tomorrow_temp_min_c": 8.0,
    "tomorrow_weathercode": 0,
    "tomorrow_weather_desc": "Sunny",
    "hourly_forecast": [],
    "last_updated": None,
}
latest_adaptive = {
    "enabled": True,
    "calculated_target_soc": 100,
    "active_register_max_soc": 100,
    "reason": "Initializing",
    "tomorrow_solar_kwh": 0.0,
    "night_mode_active": False,
    "last_evaluated": None,
}
latest_ai_insight = {
    "status": "ready",
    "title": "Smart Energy Advisor",
    "summary": "Real-time tariff & solar optimization engine",
    "bullets": [],
    "last_updated": None,
    "generating": False,
}
tapo_automation_runtime_by_ip = {}
DEFAULT_TAPO_AUTO_RULES = {
    "enabled": False,
    "idle_cutoff_enabled": False,
    "idle_threshold_w": 15.0,
    "idle_minutes": 15,
    "battery_reserve_cutoff_enabled": False,
    "battery_reserve_soc_pct": 12,
    "hue_room_sync_enabled": False,
    "hue_room_id": "84",
    "workday_morning_on_enabled": False,
    "workday_on_time": "06:50",            # Weekday power on time (e.g. 06:50)
    "weekend_morning_on_enabled": False,   # Weekend power on enabled
    "weekend_on_time": "10:00",            # Weekend power on time (e.g. 10:00)
    "weekdays_only": False,
    # Dishwasher Smart Auto-Arm & Off-Peak / Solar Trigger:
    "dishwasher_mode_enabled": False,
    "dishwasher_arm_watts": 10.0,          # Watts jump indicating cycle was started & door closed (sensitive to drain pumps)
    "dishwasher_offpeak_trigger": True,    # Run automatically when Octopus Go off-peak begins (00:30)
    "dishwasher_solar_trigger": True,      # Run automatically if daytime solar surplus >= 1200W
    "dishwasher_solar_surplus_w": 1200.0,
    "dishwasher_cycle_finish_cutoff": False,
    "dishwasher_finish_watts": 3.0,
    "dishwasher_finish_minutes": 10,
    # Media / TV Overnight Deep-Sleep Schedule:
    "night_sleep_enabled": False,
    "night_sleep_off_time": "01:00",       # Power off time if inactive
    "night_sleep_wake_time": "07:00",      # Power on time every morning
    "night_sleep_max_active_w": 25.0,      # If drawing >25W at sleep time, someone is still watching: don't cut power!
}
TAPO_DEVICE_AUTO_RULES = {
    "192.168.1.200": {
        "enabled": True,
        "idle_cutoff_enabled": True,
        "idle_threshold_w": 15.0,
        "idle_minutes": 20,
        "battery_reserve_cutoff_enabled": False,
        "battery_reserve_soc_pct": 12,
        "hue_room_sync_enabled": False,
        "hue_room_id": "84",
        "workday_morning_on_enabled": True,
        "workday_on_time": "06:50",
        "weekend_morning_on_enabled": True,
        "weekend_on_time": "10:00",
        "weekdays_only": False,
    }
}
recent_history = deque(maxlen=8640)  # 24 hours at 10s intervals
static_device_info = {
    "manufacturer": "FoxESS",
    "model": "H1-3.7-E-G2-WL",
    "serial": "60EAM50067",
    "datalogger_hostname": "mDNS-605H372065KD132.local",
    "rated_power_w": 3700,
    "master_ver": None,
    "slave_ver": None,
    "manager_ver": None,
}


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # High performance non-blocking WAL mode
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            ts INTEGER PRIMARY KEY,
            pv_power INTEGER,
            pv1_power INTEGER,
            pv2_power INTEGER,
            load_power INTEGER,
            grid_power INTEGER,
            battery_power INTEGER,
            battery_soc INTEGER,
            inverter_power INTEGER,
            pv1_voltage REAL,
            pv2_voltage REAL,
            grid_voltage REAL,
            battery_voltage REAL,
            battery_temp REAL,
            inverter_temp REAL
        )
        """
    )
    # Composite indices for fast financial & time range queries
    c.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts_grid ON telemetry(ts, grid_power)")

    # Permanent daily summary table (1 row per calendar day)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT PRIMARY KEY,
            solar_kwh REAL DEFAULT 0.0,
            import_kwh REAL DEFAULT 0.0,
            export_kwh REAL DEFAULT 0.0,
            load_kwh REAL DEFAULT 0.0,
            off_peak_import_kwh REAL DEFAULT 0.0,
            peak_import_kwh REAL DEFAULT 0.0,
            cost_gbp REAL DEFAULT 0.0,
            savings_gbp REAL DEFAULT 0.0,
            export_credit_gbp REAL DEFAULT 0.0,
            peak_solar_w INTEGER DEFAULT 0,
            peak_load_w INTEGER DEFAULT 0,
            min_soc INTEGER DEFAULT 100,
            max_soc INTEGER DEFAULT 0,
            radiation_mj REAL DEFAULT 0.0,
            points_count INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """
    )
    # Ensure radiation_mj column exists if migrating an existing table
    try:
        c.execute("ALTER TABLE daily_summary ADD COLUMN radiation_mj REAL DEFAULT 0.0")
    except Exception:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summary_date ON daily_summary(date)")

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_appliances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            room TEXT NOT NULL,
            watts REAL NOT NULL,
            is_on INTEGER NOT NULL DEFAULT 1,
            icon TEXT NOT NULL DEFAULT 'plug'
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    # Seed default estimated baseline appliances if table is empty
    c.execute("SELECT COUNT(*) FROM custom_appliances")
    if c.fetchone()[0] == 0:
        defaults = [
            ("Router, Hue Bridge & Mesh Wi-Fi", "Infrastructure", 18.0, 1, "router"),
            ("Home Server / Docker Host", "Infrastructure", 35.0, 1, "server"),
            ("Fridge / Freezer (Avg Cycle)", "Kitchen", 45.0, 1, "fridge"),
            ("TV & Entertainment Standby", "Living room", 12.0, 1, "tv"),
        ]
        c.executemany(
            "INSERT INTO custom_appliances (name, room, watts, is_on, icon) VALUES (?, ?, ?, ?, ?)",
            defaults,
        )
    conn.commit()

    # Load last 24 hours into memory buffer on startup
    cutoff = int(time.time()) - 86400
    c.execute(
        """
        SELECT ts, pv_power, pv1_power, pv2_power, load_power, grid_power,
               battery_power, battery_soc, inverter_power, pv1_voltage,
               pv2_voltage, grid_voltage, battery_voltage, battery_temp, inverter_temp
        FROM telemetry WHERE ts >= ? ORDER BY ts ASC
        """,
        (cutoff,),
    )
    for row in c.fetchall():
        recent_history.append(
            {
                "ts": row[0],
                "iso": datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat(),
                "pv_power": row[1],
                "pv1_power": row[2],
                "pv2_power": row[3],
                "load_power": row[4],
                "grid_power": row[5],
                "battery_power": row[6],
                "battery_soc": row[7],
                "inverter_power": row[8],
                "pv1_voltage": row[9],
                "pv2_voltage": row[10],
                "grid_voltage": row[11],
                "battery_voltage": row[12],
                "battery_temp": row[13],
                "inverter_temp": row[14],
            }
        )
    conn.close()


def rollup_daily_summary(target_date_str=None):
    """Aggregate raw telemetry for a given day (YYYY-MM-DD) into the daily_summary table."""
    now = datetime.now()
    if target_date_str is None:
        target_date_str = now.strftime("%Y-%m-%d")

    try:
        t_start = datetime.strptime(target_date_str + " 00:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
        t_end = datetime.strptime(target_date_str + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT ts, pv_power, load_power, grid_power, battery_soc
            FROM telemetry
            WHERE ts >= ? AND ts <= ?
            ORDER BY ts ASC
            """,
            (t_start, t_end),
        )
        rows = c.fetchall()
        if not rows:
            conn.close()
            return

        import_cost_p = 0.0
        baseline_cost_p = 0.0
        export_kwh = 0.0
        import_kwh = 0.0
        off_peak_kwh = 0.0
        peak_import_kwh = 0.0
        load_kwh = 0.0
        solar_kwh = 0.0
        peak_solar_w = 0
        peak_load_w = 0
        min_soc = 100
        max_soc = 0

        with lock:
            peak_rate = latest_octopus.get("peak_rate_p_kwh", 36.5644)
            off_peak_rate = latest_octopus.get("off_peak_rate_p_kwh", 9.40)

        for i in range(len(rows)):
            pv_w = rows[i][1] or 0
            load_w = rows[i][2] or 0
            grid_w = rows[i][3] or 0
            soc = rows[i][4] if rows[i][4] is not None else 10

            peak_solar_w = max(peak_solar_w, pv_w)
            peak_load_w = max(peak_load_w, load_w)
            min_soc = min(min_soc, soc)
            max_soc = max(max_soc, soc)

            if i > 0:
                dt_h = (rows[i][0] - rows[i - 1][0]) / 3600.0
                if 0 < dt_h <= 0.05:
                    ts_dt = datetime.fromtimestamp(rows[i][0])
                    mins = ts_dt.hour * 60 + ts_dt.minute
                    is_cheap = (30 <= mins < 330)
                    rate = off_peak_rate if is_cheap else peak_rate

                    solar_kwh += (pv_w * dt_h) / 1000.0
                    load_kwh += (load_w * dt_h) / 1000.0

                    if grid_w > 0:  # Import
                        kwh = (grid_w * dt_h) / 1000.0
                        import_kwh += kwh
                        import_cost_p += kwh * rate
                        if is_cheap:
                            off_peak_kwh += kwh
                        else:
                            peak_import_kwh += kwh
                    elif grid_w < 0:  # Export
                        export_kwh += (abs(grid_w) * dt_h) / 1000.0

                    baseline_cost_p += ((load_w * dt_h) / 1000.0) * 26.0

        standing_charge_p = 48.85
        cost_gbp = round((import_cost_p + standing_charge_p) / 100.0, 2)
        savings_gbp = round(max(0.0, (baseline_cost_p - import_cost_p) / 100.0), 2)
        export_credit_gbp = round((export_kwh * OCTOPUS_EXPORT_RATE_P_KWH) / 100.0, 2)

        c.execute(
            """
            INSERT INTO daily_summary (
                date, solar_kwh, import_kwh, export_kwh, load_kwh,
                off_peak_import_kwh, peak_import_kwh, cost_gbp, savings_gbp,
                export_credit_gbp, peak_solar_w, peak_load_w, min_soc, max_soc,
                radiation_mj, points_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                solar_kwh=excluded.solar_kwh,
                import_kwh=excluded.import_kwh,
                export_kwh=excluded.export_kwh,
                load_kwh=excluded.load_kwh,
                off_peak_import_kwh=excluded.off_peak_import_kwh,
                peak_import_kwh=excluded.peak_import_kwh,
                cost_gbp=excluded.cost_gbp,
                savings_gbp=excluded.savings_gbp,
                export_credit_gbp=excluded.export_credit_gbp,
                peak_solar_w=excluded.peak_solar_w,
                peak_load_w=excluded.peak_load_w,
                min_soc=excluded.min_soc,
                max_soc=excluded.max_soc,
                radiation_mj=COALESCE(daily_summary.radiation_mj, excluded.radiation_mj),
                points_count=excluded.points_count,
                updated_at=excluded.updated_at
            """,
            (
                target_date_str,
                round(solar_kwh, 2),
                round(import_kwh, 2),
                round(export_kwh, 2),
                round(load_kwh, 2),
                round(off_peak_kwh, 2),
                round(peak_import_kwh, 2),
                cost_gbp,
                savings_gbp,
                export_credit_gbp,
                peak_solar_w,
                peak_load_w,
                min_soc,
                max_soc,
                float(latest_weather.get("today_radiation_mj", 0.0) if target_date_str == now.strftime("%Y-%m-%d") else 0.0),
                len(rows),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Rollup Daily Summary Error] {e}")


def prune_old_telemetry(retention_days=14):
    """Prune raw 10-second telemetry older than retention_days, preserving aggregated daily summaries."""
    try:
        cutoff = int(time.time()) - (retention_days * 86400)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,))
        deleted = c.rowcount
        conn.commit()
        if deleted > 0:
            c.execute("PRAGMA optimize")
            print(f"[DB Maintenance] Pruned {deleted} raw telemetry points older than {retention_days} days.")
        conn.close()
    except Exception as e:
        print(f"[DB Pruning Error] {e}")


def get_telemetry_history(hours=6):
    """Query telemetry points for the given number of hours from SQLite, with downsampling if needed."""
    try:
        cutoff = int(time.time() - hours * 3600)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT ts, pv_power, pv1_power, pv2_power, load_power, grid_power,
                   battery_power, battery_soc, inverter_power, pv1_voltage,
                   pv2_voltage, grid_voltage, battery_voltage, battery_temp, inverter_temp
            FROM telemetry
            WHERE ts >= ?
            ORDER BY ts ASC
            """,
            (cutoff,),
        )
        rows = c.fetchall()
        conn.close()

        # If points exceed ~1200, downsample evenly to keep browser Chart.js rendering fast
        step = max(1, len(rows) // 1200)
        selected_rows = rows[::step] if step > 1 else rows

        cols = [
            "ts", "pv_power", "pv1_power", "pv2_power", "load_power", "grid_power",
            "battery_power", "battery_soc", "inverter_power", "pv1_voltage",
            "pv2_voltage", "grid_voltage", "battery_voltage", "battery_temp", "inverter_temp"
        ]
        return [dict(zip(cols, r)) for r in selected_rows]
    except Exception as e:
        print(f"[Telemetry History Error] {e}")
        return []


def get_daily_history(days=30):
    """Query daily_summary table for past N days."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT date, solar_kwh, import_kwh, export_kwh, load_kwh,
                   off_peak_import_kwh, peak_import_kwh, cost_gbp, savings_gbp,
                   export_credit_gbp, peak_solar_w, peak_load_w, min_soc, max_soc
            FROM daily_summary
            ORDER BY date DESC
            LIMIT ?
            """,
            (days,),
        )
        cols = [
            "date", "solar_kwh", "import_kwh", "export_kwh", "load_kwh",
            "off_peak_import_kwh", "peak_import_kwh", "cost_gbp", "savings_gbp",
            "export_credit_gbp", "peak_solar_w", "peak_load_w", "min_soc", "max_soc"
        ]
        results = [dict(zip(cols, row)) for row in c.fetchall()]
        conn.close()
        return list(reversed(results))
    except Exception as e:
        print(f"[Daily History Query Error] {e}")
        return []


def predict_daily_load_ml(target_date=None, target_temp_min_c=None, target_temp_max_c=None):
    """
    Self-learning time-series regression model to predict next day's electricity consumption (kWh).
    Architecture:
      1. Day-of-Week (DOW) profile weights (e.g. Saturday vs Tuesday habits) with exponential recency decay.
      2. Ambient temperature correlation coefficient (heating / seasonal sensitivity).
      3. Bayesian shrinkage towards contract baseline (3,476 kWh/yr = ~9.52 kWh/day total, ~7.8 kWh daytime)
         when historical samples are young, auto-transitioning to 100% learned weights as data matures.
    """
    if target_date is None:
        target_date = datetime.now() + timedelta(days=1)
    target_dow = target_date.weekday() # 0 = Monday, 6 = Sunday

    # Baseline anchor tuned to actual household telemetry (~6.0 kWh/day actual)
    baseline_total_kwh = 6.00
    baseline_daytime_kwh = 4.80
    dow_multipliers = {
        0: 0.98, # Monday
        1: 0.96, # Tuesday
        2: 0.96, # Wednesday
        3: 0.98, # Thursday
        4: 1.02, # Friday
        5: 1.06, # Saturday (gentle weekend adjustment +6%)
        6: 1.04, # Sunday
    }

    # Weather thermal sensitivity adjustment:
    # Baseline comfort band is ~15°C. For each 1°C colder, UK domestic demand increases by ~1.4%.
    avg_temp = 12.0
    if target_temp_min_c is not None and target_temp_max_c is not None:
        avg_temp = (float(target_temp_min_c) + float(target_temp_max_c)) / 2.0
    elif target_temp_min_c is not None:
        avg_temp = float(target_temp_min_c)

    thermal_delta = 15.0 - avg_temp
    temp_factor = 1.0 + (thermal_delta * 0.014 if thermal_delta > 0 else thermal_delta * 0.005)
    temp_factor = max(0.85, min(1.35, temp_factor))

    # Query historical daily summaries from SQLite
    history_samples = []
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT date, load_kwh FROM daily_summary WHERE load_kwh > 1.0 ORDER BY date DESC LIMIT 60")
        rows = c.fetchall()
        conn.close()
        for r in rows:
            try:
                d_obj = datetime.strptime(r[0], "%Y-%m-%d")
                history_samples.append({
                    "date": r[0],
                    "dt": d_obj,
                    "dow": d_obj.weekday(),
                    "load_kwh": float(r[1])
                })
            except Exception:
                pass
    except Exception as e:
        print(f"[ML Load Predictor Error] {e}")

    sample_count = len(history_samples)

    if sample_count == 0:
        # Cold start: Pure Bayesian contract prior
        pred_total = round(baseline_total_kwh * dow_multipliers.get(target_dow, 1.0) * temp_factor, 2)
        pred_daytime = round(baseline_daytime_kwh * dow_multipliers.get(target_dow, 1.0) * temp_factor, 2)
        confidence = "Contract Baseline"
        method = "Prior Benchmark"
    else:
        # Exponentially-weighted moving average (EWMA) with half-life of 14 days
        now_dt = datetime.now()
        total_weight = 0.0
        weighted_load_sum = 0.0
        dow_samples = []

        for s in history_samples:
            days_ago = max(0, (now_dt - s["dt"]).days)
            # Exponential decay: recent days have higher weight
            w = 0.5 ** (days_ago / 14.0)
            # Extra boost if it matches the target day of week
            if s["dow"] == target_dow:
                w *= 2.2
                dow_samples.append(s["load_kwh"])
            total_weight += w
            weighted_load_sum += s["load_kwh"] * w

        ewma_load = weighted_load_sum / total_weight if total_weight > 0 else baseline_total_kwh

        # Bayesian blending: Credibility factor scales quickly with real telemetry
        # Reaches 70% credibility by day 3, 90% by day 5
        credibility = min(0.95, sample_count / 5.0)
        prior_load = baseline_total_kwh * dow_multipliers.get(target_dow, 1.0)

        blended_load = (credibility * ewma_load) + ((1.0 - credibility) * prior_load)
        # Apply temperature regression
        pred_total = round(blended_load * temp_factor, 2)
        # Daytime is typically ~82% of total 24h consumption
        pred_daytime = round(pred_total * 0.82, 2)
        confidence = f"High ({sample_count}d trained)" if sample_count >= 10 else f"Learning ({sample_count}d sampled)"
        method = f"EWMA + DOW + Thermal Regression ({temp_factor:.2f}x)"

    return {
        "target_date": target_date.strftime("%Y-%m-%d"),
        "target_day_name": target_date.strftime("%A"),
        "predicted_total_kwh": pred_total,
        "predicted_daytime_kwh": pred_daytime,
        "temperature_factor": round(temp_factor, 2),
        "confidence": confidence,
        "method": method,
        "samples_trained": sample_count,
    }


def get_learned_hourly_baseload():
    """
    ML Base Load Model: Computes the 24-hour baseline (standby/vampire) power profile
    from recent telemetry using the 20th percentile of load power for each hour of the day.
    Filtering by the 20th percentile isolates true continuous baseline consumption (fridge,
    router, idle PCs, standby devices) from transient spikes (kettles, showers, cooking).
    """
    learned_base = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT 
                cast(strftime('%H', datetime(ts, 'unixepoch', 'localtime')) as int) as hr,
                load_power
            FROM telemetry
            WHERE ts >= strftime('%s', 'now', '-14 days')
            """
        )
        hr_loads = defaultdict(list)
        for hr, load in c.fetchall():
            if load is not None and load > 0:
                hr_loads[hr].append(load)
        conn.close()

        for hr in range(24):
            vals = hr_loads.get(hr, [])
            if len(vals) >= 20:
                vals.sort()
                p20 = vals[int(len(vals) * 0.20)]
                learned_base[hr] = round(p20)
            else:
                learned_base[hr] = 130
    except Exception as e:
        print(f"[ML Baseload Warning] {e}")
        for hr in range(24):
            learned_base[hr] = 130

    return learned_base


def calculate_adaptive_target_soc(tomorrow_solar_kwh):
    """Compute the optimal overnight target SoC and solar headroom using ML load prediction,
    learned hourly baseload profiling, and tomorrow's solar forecast curve.
    Contract specification: Fox EQ5000 (9.84 kWh gross capacity, ~8.86 kWh usable 10%-100%)."""
    bat_total_kwh = 9.84
    bat_usable_kwh = 8.86
    
    # Run ML Load Prediction for tomorrow (aggregate 24h & daytime)
    with lock:
        t_min = latest_weather.get("tomorrow_temp_min_c")
        t_max = latest_weather.get("tomorrow_temp_max_c")

    target_date = datetime.now() + timedelta(days=1)
    ml = predict_daily_load_ml(
        target_date=target_date,
        target_temp_min_c=t_min,
        target_temp_max_c=t_max
    )
    predicted_total_kwh = ml["predicted_total_kwh"]

    # 1. Fetch ML-learned 24-hour baseload profile
    learned_base = get_learned_hourly_baseload()
    ml["hourly_baseload_watts"] = {f"{h:02d}:00": learned_base.get(h, 130) for h in range(24)}

    # 2. Compute hourly solar generation curve for tomorrow using calibrated physics model
    calibrated_factor = get_calibrated_solar_factor()
    hourly_watt_scale = round((calibrated_factor / 0.80) * 2.88, 2)
    tomorrow_str = target_date.strftime("%Y-%m-%d")

    projected_solar_surplus_kwh = 0.0
    first_solar_breakeven_hr = 9  # default to 09:00 if not found
    try:
        url_om = (
            f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}"
            "&hourly=shortwave_radiation&timezone=Europe%2FLondon&forecast_days=3"
        )
        req = urllib.request.Request(url_om, headers={"User-Agent": "SmartPowerDashboard/2.0 (contact: user@example.com)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            om_data = json.loads(resp.read().decode("utf-8"))

        times = om_data.get("hourly", {}).get("time", [])
        rads = om_data.get("hourly", {}).get("shortwave_radiation", [])

        found_breakeven = False
        for t_iso, w_m2 in zip(times, rads):
            if t_iso.startswith(tomorrow_str):
                hr = int(t_iso.split("T")[1].split(":")[0])
                pred_solar_w = round(w_m2 * hourly_watt_scale)
                hr_base_w = learned_base.get(hr, 130)
                # Determine first hour where solar begins offsetting the house baseline
                if pred_solar_w >= hr_base_w and not found_breakeven and hr >= 6:
                    first_solar_breakeven_hr = hr
                    found_breakeven = True
                # Any solar exceeding the learned baseload is surplus that needs battery storage
                if pred_solar_w > hr_base_w:
                    projected_solar_surplus_kwh += (pred_solar_w - hr_base_w) / 1000.0
    except Exception as e:
        print(f"[Adaptive Solar Curve Warning] {e}")
        # Robust fallback: solar during peak midday hours above baseline
        projected_solar_surplus_kwh = max(0.0, tomorrow_solar_kwh - 1.2)
        first_solar_breakeven_hr = 9

    # 3. Model Morning Pre-Solar Drain (05:30 until solar covers base load):
    # The battery must power the home during this entire pre-solar window.
    # Because the battery naturally discharges to cover this morning gap, that discharge
    # creates "free headroom" inside the battery before peak solar hits!
    morning_pre_solar_kwh = (learned_base.get(5, 100) * 0.5) / 1000.0  # 05:30 to 06:00
    for hr in range(6, max(6, first_solar_breakeven_hr)):
        morning_pre_solar_kwh += learned_base.get(hr, 120) / 1000.0

    morning_pre_solar_kwh = round(morning_pre_solar_kwh, 2)
    ml["morning_pre_solar_kwh"] = morning_pre_solar_kwh
    ml["first_solar_breakeven_hour"] = f"{first_solar_breakeven_hr:02d}:00"

    projected_solar_surplus_kwh = round(projected_solar_surplus_kwh, 2)
    ml["predicted_solar_surplus_kwh"] = projected_solar_surplus_kwh

    # 4. Net Headroom Required at 05:30:
    # Net headroom to reserve overnight = Max(0, Solar Surplus - Morning Pre-Solar Drain)
    # If the morning house load will naturally empty 0.5 kWh out of the battery before the sun
    # is high enough to produce surplus, we only need to reserve additional overnight headroom
    # if solar surplus exceeds what the morning drain already frees up.
    net_overnight_headroom_needed = max(0.0, projected_solar_surplus_kwh - morning_pre_solar_kwh)
    net_overnight_headroom_needed = round(net_overnight_headroom_needed, 2)
    ml["net_overnight_headroom_needed_kwh"] = net_overnight_headroom_needed

    # 5. Decision Matrix with User Safety Buffer:
    buffer_pct = max(0, min(30, ADAPTIVE_BUFFER_PCT))
    ml["applied_buffer_pct"] = buffer_pct

    if net_overnight_headroom_needed >= 0.3:
        # Cap headroom reservation to not drop battery below 40% (ensuring evening peak security)
        headroom_kwh = min(net_overnight_headroom_needed, bat_usable_kwh * 0.60)
        raw_target_soc = int(round(100 - (headroom_kwh / bat_total_kwh) * 100))
        # Add the user-configured safety buffer on top of the calculated target
        target_soc = min(100, max(40, raw_target_soc + buffer_pct))
        headroom_kwh = ((100 - target_soc) / 100.0) * bat_total_kwh
        ml["raw_target_soc"] = raw_target_soc
    else:
        target_soc = 100
        headroom_kwh = 0.0
        ml["raw_target_soc"] = 100

    target_charge_kwh = round((target_soc / 100.0) * bat_total_kwh, 1)
    return target_soc, target_charge_kwh, round(headroom_kwh, 1), ml


def generate_energy_insights():
    """Deterministic, math-driven savings & battery optimization engine."""
    with lock:
        curr_load = latest_data.get("power", {}).get("load_w", 0)
        pv_w = latest_data.get("power", {}).get("pv_total_w", 0)
        grid_w = latest_data.get("power", {}).get("grid_net_w", 0)
        soc = latest_data.get("battery", {}).get("soc_pct", 10)
        bat_w = latest_data.get("battery", {}).get("power_w", 0)
        today_pv = latest_data.get("energy", {}).get("today", {}).get("solar_pv_kwh", 0.0)
        today_imp = latest_data.get("energy", {}).get("today", {}).get("grid_import_kwh", 0.0)
        tomorrow_solar = latest_weather.get("tomorrow_solar_kwh", 0.0)
        tomorrow_desc = latest_weather.get("tomorrow_weather_desc", "Sunny")
        off_peak_rate = latest_octopus.get("off_peak_rate_p_kwh", 9.4)
        peak_rate = latest_octopus.get("peak_rate_p_kwh", 36.56)
        is_off_peak = latest_octopus.get("is_off_peak", False)

    bullets = []
    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    off_peak_start = 30  # 00:30 is 30 mins
    off_peak_end = 330   # 05:30 is 330 mins
    # Contract specification: Fox EQ5000 9.84 kWh gross capacity
    bat_total_kwh = 9.84
    min_soc = 10.0       # 10% reserve cut-off

    # 1. Battery Run-Time & Peak-Bridging Calculation
    usable_kwh = max(0.0, (soc - min_soc) / 100.0 * bat_total_kwh)
    net_house_demand_w = max(0, curr_load - pv_w)

    if is_off_peak:
        bullets.append(
            f"Super Off-Peak window active ({off_peak_rate:.1f}p/kWh until 05:30). "
            f"Running high-demand appliances now saves {(peak_rate - off_peak_rate):.1f}p per kWh compared to daytime."
        )
    else:
        # Time remaining until 00:30
        if now_minutes >= off_peak_start:
            mins_until_cheap = (24 * 60 - now_minutes) + off_peak_start
        else:
            mins_until_cheap = off_peak_start - now_minutes

        hours_until_cheap = mins_until_cheap / 60.0

        if net_house_demand_w > 50:
            bat_hours_left = usable_kwh / (net_house_demand_w / 1000.0)
            if bat_hours_left >= hours_until_cheap:
                bullets.append(
                    f"Battery at {soc}% ({usable_kwh:.1f} kWh usable) will easily bridge the remaining "
                    f"{hours_until_cheap:.1f}h until the 00:30 cheap rate with ~{((bat_hours_left - hours_until_cheap) * (net_house_demand_w / 1000.0)):.1f} kWh to spare."
                )
            else:
                deficit_kwh = (hours_until_cheap - bat_hours_left) * (net_house_demand_w / 1000.0)
                cost_import = deficit_kwh * (peak_rate / 100.0)
                deplete_time = (now + timedelta(hours=bat_hours_left)).strftime("%H:%M")
                bullets.append(
                    f"At current {curr_load}W load, battery reaches reserve (10%) at ~{deplete_time} "
                    f"({bat_hours_left:.1f}h runtime). Grid will supply ~{deficit_kwh:.2f} kWh at peak rate (~£{cost_import:.2f}) before 00:30."
                )
        else:
            bullets.append(
                f"Battery at {soc}% ({usable_kwh:.1f} kWh usable). House load is minimal ({curr_load}W)."
            )

    # 2. Tomorrow Solar Forecast vs Overnight Battery Pre-Charge Recommendation
    target_soc, needed_grid_kwh, headroom_kwh, ml_pred = calculate_adaptive_target_soc(tomorrow_solar)
    if headroom_kwh > 0.1:
        strategy_desc = f"Optimal overnight charge target is {target_soc}% ({needed_grid_kwh:.1f} kWh), leaving {headroom_kwh:.1f} kWh solar headroom."
    else:
        strategy_desc = f"Optimal overnight charge target is {target_soc}% ({needed_grid_kwh:.1f} kWh) to avoid 36.6p peak power (all solar absorbed directly by home)."

    bullets.append(
        f"Tomorrow's forecast is {tomorrow_solar:.1f} kWh ({tomorrow_desc}) against an ML predicted home demand of {ml_pred['predicted_total_kwh']} kWh "
        f"({ml_pred['target_day_name']} load profile · {ml_pred['confidence']}). "
        f"{strategy_desc}"
    )

    # 3. Financial Arbitrage Metric
    full_cycle_savings = (bat_total_kwh * 0.9) * ((peak_rate - off_peak_rate) / 100.0)
    bullets.append(
        f"Shifting full battery capacity ({bat_total_kwh * 0.9:.1f} kWh usable) from peak (36.6p) to off-peak (9.4p) delivers "
        f"£{full_cycle_savings:.2f}/day in direct tariff arbitrage savings."
    )

    now_str = datetime.now().strftime("%H:%M")
    with lock:
        latest_ai_insight.update({
            "status": "online",
            "title": f"Energy Optimizer · {now_str}",
            "bullets": bullets[:3],
            "last_updated": now_str,
            "generating": False,
        })

    return latest_ai_insight


def get_custom_appliances():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, name, room, watts, is_on, icon FROM custom_appliances ORDER BY id ASC")
        rows = [
            {
                "id": r[0],
                "name": r[1],
                "room": r[2],
                "watts": round(float(r[3]), 1),
                "is_on": bool(r[4]),
                "active_watts": round(float(r[3]), 1) if r[4] else 0.0,
                "icon": r[5],
            }
            for r in c.fetchall()
        ]
        conn.close()
        return rows
    except Exception as e:
        print(f"[Appliances DB Error] {e}")
        return []


def estimate_hue_light_watts(modelid, state):
    standby_w, min_on_w, max_on_w = HUE_POWER_PROFILES.get(modelid, DEFAULT_HUE_PROFILE)
    if not state.get("reachable", True):
        return 0.0, max_on_w, standby_w
    if not state.get("on", False):
        return standby_w, max_on_w, standby_w
    bri = max(1, min(254, int(state.get("bri", 254))))
    # Non-linear LED driver power curve (gamma ~ 1.45)
    ratio = (bri / 254.0) ** 1.45
    watts = min_on_w + (max_on_w - min_on_w) * ratio
    return round(watts, 2), max_on_w, standby_w


def poll_hue_once():
    url = f"http://{HUE_HOST}/api/{HUE_API_KEY}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=3.5) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    raw_lights = data.get("lights", {})
    raw_groups = data.get("groups", {})

    # Map light_id -> room_name
    light_to_room = {}
    rooms_out = []
    for gid, g in raw_groups.items():
        if g.get("type") in ("Room", "Zone"):
            rname = g.get("name", f"Room {gid}")
            for lid in g.get("lights", []):
                light_to_room[str(lid)] = rname

    lights_out = []
    total_light_watts = 0.0
    total_max_watts = 0.0
    on_count = 0

    for lid, l in sorted(raw_lights.items(), key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
        st = l.get("state", {})
        modelid = l.get("modelid", "")
        watts, max_w, standby_w = estimate_hue_light_watts(modelid, st)
        is_on = bool(st.get("on", False))
        if is_on:
            on_count += 1
        bri = int(st.get("bri", 254))
        bri_pct = round((bri / 254.0) * 100) if is_on else 0
        total_light_watts += watts
        total_max_watts += max_w

        lights_out.append(
            {
                "id": str(lid),
                "name": l.get("name", f"Light {lid}"),
                "room": light_to_room.get(str(lid), "Other / Unassigned"),
                "modelid": modelid,
                "productname": l.get("productname", "Hue Light"),
                "type": l.get("type", ""),
                "on": is_on,
                "bri": bri,
                "bri_pct": bri_pct,
                "reachable": bool(st.get("reachable", True)),
                "watts": watts,
                "max_watts": max_w,
                "standby_watts": standby_w,
            }
        )

    # Build room summaries
    for gid, g in sorted(raw_groups.items(), key=lambda x: x[1].get("name", "")):
        if g.get("type") in ("Room", "Zone"):
            lids = [str(x) for x in g.get("lights", [])]
            r_lights = [lt for lt in lights_out if lt["id"] in lids]
            r_watts = round(sum(lt["watts"] for lt in r_lights), 2)
            r_max = round(sum(lt["max_watts"] for lt in r_lights), 2)
            st = g.get("state", {})
            rooms_out.append(
                {
                    "id": str(gid),
                    "name": g.get("name", f"Room {gid}"),
                    "class": g.get("class", "Other"),
                    "light_ids": lids,
                    "any_on": bool(st.get("any_on", False)),
                    "all_on": bool(st.get("all_on", False)),
                    "watts": r_watts,
                    "max_watts": r_max,
                }
            )

    # Include bridge itself (~1.8W)
    bridge_watts = 1.8
    total_hue_watts = round(total_light_watts + bridge_watts, 2)

    hue_payload = {
        "status": "online",
        "bridge_ip": HUE_HOST,
        "bridge_watts": bridge_watts,
        "lights_watts": round(total_light_watts, 2),
        "total_watts": total_hue_watts,
        "max_watts": round(total_max_watts + bridge_watts, 1),
        "lights_on_count": on_count,
        "lights_total_count": len(lights_out),
        "rooms": rooms_out,
        "lights": lights_out,
    }

    with lock:
        latest_hue.clear()
        latest_hue.update(hue_payload)


def set_hue_light_state(light_id, state_body):
    url = f"http://{HUE_HOST}/api/{HUE_API_KEY}/lights/{light_id}/state"
    req = urllib.request.Request(url, data=json.dumps(state_body).encode("utf-8"), method="PUT")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=3.5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_hue_group_action(group_id, action_body):
    url = f"http://{HUE_HOST}/api/{HUE_API_KEY}/groups/{group_id}/action"
    req = urllib.request.Request(url, data=json.dumps(action_body).encode("utf-8"), method="PUT")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=3.5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_octopus_rates():
    """Fetch live rates from Octopus Energy API and check if current time is within cheap off-peak window."""
    now_dt = datetime.now()
    now_utc = datetime.now(timezone.utc)
    
    # Standard Octopus Go off-peak is 00:30 to 05:30 UK local time
    # Check if local clock is currently in that window
    current_mins = now_dt.hour * 60 + now_dt.minute
    is_go_window = (0 * 60 + 30) <= current_mins < (5 * 60 + 30)
    
    current_rate = 9.40 if is_go_window else 36.56
    off_peak_rate = 9.40
    peak_rate = 36.56

    try:
        url = f"https://api.octopus.energy/v1/products/{OCTOPUS_PRODUCT_CODE}/electricity-tariffs/{OCTOPUS_TARIFF_CODE}/standard-unit-rates/"
        auth_header = "Basic " + base64.b64encode(f"{OCTOPUS_API_KEY}:".encode("utf-8")).decode("utf-8")
        req = urllib.request.Request(url)
        req.add_header("Authorization", auth_header)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", [])
            for r in results:
                vf = r.get("valid_from")
                vt = r.get("valid_to")
                val = float(r.get("value_inc_vat", 0.0))
                if val < 15.0:
                    off_peak_rate = val
                else:
                    peak_rate = val
                if vf and vt:
                    t_from = datetime.fromisoformat(vf.replace("Z", "+00:00"))
                    t_to = datetime.fromisoformat(vt.replace("Z", "+00:00"))
                    if t_from <= now_utc < t_to:
                        current_rate = val
                        is_go_window = val < 15.0
    except Exception as e:
        print(f"[Octopus Rates Warning] {e}")

    # Poll live Octopus Agile Outgoing Export rates for Region H (Southern England)
    try:
        url_agile = "https://api.octopus.energy/v1/products/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-H/standard-unit-rates/?page_size=100"
        req_agile = urllib.request.Request(url_agile, headers={"User-Agent": "SmartPowerDashboard/2.0"})
        with urllib.request.urlopen(req_agile, timeout=6) as resp_agile:
            data_agile = json.loads(resp_agile.read().decode("utf-8"))
            agile_results = data_agile.get("results", [])

        curr_agile_p = 15.0
        next_agile_p = None
        next_agile_from = None
        today_date_str = now_dt.strftime("%Y-%m-%d")
        tomorrow_date_str = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        half_hourly_today = []
        half_hourly_tomorrow = []

        for r in agile_results:
            vf = r.get("valid_from")
            vt = r.get("valid_to")
            val = float(r.get("value_inc_vat", 0.0))
            if vf and vt:
                t_from = datetime.fromisoformat(vf.replace("Z", "+00:00")).astimezone()
                t_to = datetime.fromisoformat(vt.replace("Z", "+00:00")).astimezone()
                if t_from <= now_dt.astimezone() < t_to:
                    curr_agile_p = val
                elif now_dt.astimezone() < t_from <= now_dt.astimezone() + timedelta(minutes=30):
                    next_agile_p = round(val, 2)
                    next_agile_from = t_from.strftime("%H:%M")
                if t_from.strftime("%Y-%m-%d") == today_date_str:
                    half_hourly_today.append({
                        "from": t_from.strftime("%H:%M"),
                        "to": t_to.strftime("%H:%M"),
                        "rate_p_kwh": round(val, 2),
                    })
                elif t_from.strftime("%Y-%m-%d") == tomorrow_date_str:
                    half_hourly_tomorrow.append({
                        "from": t_from.strftime("%H:%M"),
                        "to": t_to.strftime("%H:%M"),
                        "rate_p_kwh": round(val, 2),
                    })

        # Sort earliest to latest
        half_hourly_today.sort(key=lambda x: x["from"])
        half_hourly_tomorrow.sort(key=lambda x: x["from"])
        today_rates_vals = [x["rate_p_kwh"] for x in half_hourly_today]
        max_rate = max(today_rates_vals) if today_rates_vals else 30.0
        min_rate = min(today_rates_vals) if today_rates_vals else 5.0

        # Top 3 highest earning export half-hour slots today
        sorted_by_rate = sorted(half_hourly_today, key=lambda x: x["rate_p_kwh"], reverse=True)
        top_slots = sorted_by_rate[:3]

        # Best automated discharge window recommendation
        export_strategy = None
        if top_slots:
            best_slot = top_slots[0]
            # Identify high value window (slots with rate >= 25p or top 2 slots)
            high_slots = [s for s in half_hourly_today if s["rate_p_kwh"] >= 25.0]
            if not high_slots:
                high_slots = sorted_by_rate[:2]
            high_slots.sort(key=lambda x: x["from"])
            start_window = high_slots[0]["from"]
            end_window = high_slots[-1]["to"]
            avg_win_rate = round(sum(s["rate_p_kwh"] for s in high_slots) / len(high_slots), 2)
            export_strategy = {
                "recommended_window": f"{start_window} - {end_window}",
                "avg_rate_p_kwh": avg_win_rate,
                "peak_rate_p_kwh": max_rate,
                "estimated_export_kwh": 4.5,
                "potential_earnings_gbp": round((4.5 * avg_win_rate) / 100.0, 2),
                "arbitrage_profit_gbp": round((4.5 * (avg_win_rate - 9.40)) / 100.0, 2),
                "has_tomorrow_rates": len(half_hourly_tomorrow) > 0,
            }

        # Calculate exact weighted realized export rate and shadow earnings from today's telemetry
        realized_rate_p = 0.0
        shadow_earned_gbp = 0.0
        standard_earned_gbp = 0.0
        agile_uplift_gbp = 0.0

        try:
            midnight_ts = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT ts, grid_power FROM telemetry WHERE ts >= ? ORDER BY ts ASC",
                (midnight_ts,),
            )
            telem_rows = c.fetchall()
            conn.close()

            total_exp_kwh = 0.0
            total_earned_p = 0.0

            if len(telem_rows) > 1 and half_hourly_today:
                # Pre-map timestamp ranges to agile rates
                rates_map = []
                for s in half_hourly_today:
                    h_start, m_start = map(int, s["from"].split(":"))
                    h_end, m_end = map(int, s["to"].split(":"))
                    t1 = datetime(now_dt.year, now_dt.month, now_dt.day, h_start, m_start).timestamp()
                    # If slot ends at 00:00, it's midnight tomorrow
                    if h_end == 0 and m_end == 0:
                        t2 = t1 + 1800.0
                    else:
                        t2 = datetime(now_dt.year, now_dt.month, now_dt.day, h_end, m_end).timestamp()
                    rates_map.append((t1, t2, s["rate_p_kwh"]))

                for i in range(1, len(telem_rows)):
                    dt_hours = (telem_rows[i][0] - telem_rows[i - 1][0]) / 3600.0
                    if dt_hours <= 0 or dt_hours > 0.05:
                        continue
                    grid_w = telem_rows[i][1]
                    if grid_w < -10:
                        exp_kwh = (-grid_w * dt_hours) / 1000.0
                        ts = telem_rows[i][0]
                        slot_rate = curr_agile_p
                        for r_start, r_end, r_val in rates_map:
                            if r_start <= ts < r_end:
                                slot_rate = r_val
                                break
                        total_exp_kwh += exp_kwh
                        total_earned_p += exp_kwh * slot_rate

                if total_exp_kwh > 0.001:
                    realized_rate_p = round(total_earned_p / total_exp_kwh, 2)
                    shadow_earned_gbp = round(total_earned_p / 100.0, 2)
                    standard_earned_gbp = round((total_exp_kwh * 15.0) / 100.0, 2)  # Standard fixed 15p SEG
                    agile_uplift_gbp = round(shadow_earned_gbp - standard_earned_gbp, 2)
                else:
                    realized_rate_p = round(curr_agile_p, 2)
        except Exception as e_calc:
            print(f"[Agile Earnings Telemetry Warning] {e_calc}")

        with lock:
            latest_octopus["agile_outgoing"].update({
                "status": "online",
                "current_export_p_kwh": round(curr_agile_p, 2),
                "next_export_p_kwh": next_agile_p,
                "next_rate_time": next_agile_from,
                "peak_window_today": "16:00 - 19:00",
                "peak_rate_today_p_kwh": round(max_rate, 2),
                "min_rate_today_p_kwh": round(min_rate, 2),
                "realized_rate_p_kwh": realized_rate_p,
                "today_shadow_earned_gbp": shadow_earned_gbp,
                "standard_export_earned_gbp": standard_earned_gbp,
                "agile_uplift_gbp": agile_uplift_gbp,
                "top_slots_today": top_slots,
                "export_strategy": export_strategy,
                "half_hourly_rates": half_hourly_today,
                "half_hourly_tomorrow": half_hourly_tomorrow,
                "last_updated": now_dt.strftime("%H:%M:%S"),
            })
    except Exception as e_agile:
        print(f"[Octopus Agile Outgoing Warning] {e_agile}")

    with lock:
        latest_octopus["status"] = "online"
        latest_octopus["current_rate_p_kwh"] = round(current_rate, 2)
        latest_octopus["is_off_peak"] = is_go_window
        latest_octopus["off_peak_rate_p_kwh"] = round(off_peak_rate, 2)
        latest_octopus["peak_rate_p_kwh"] = round(peak_rate, 2)
        latest_octopus["last_fetched"] = now_dt.strftime("%H:%M:%S")


WMO_WEATHER_CODES = {
    0: ("Clear Sky", "☀️"),
    1: ("Mainly Clear", "🌤️"),
    2: ("Partly Cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Depositing Rime Fog", "🌫️"),
    51: ("Light Drizzle", "🌦️"),
    53: ("Moderate Drizzle", "🌧️"),
    55: ("Dense Drizzle", "🌧️"),
    61: ("Slight Rain", "🌦️"),
    63: ("Moderate Rain", "🌧️"),
    65: ("Heavy Rain", "🌧️"),
    71: ("Slight Snow", "🌨️"),
    73: ("Moderate Snow", "🌨️"),
    75: ("Heavy Snow", "❄️"),
    80: ("Rain Showers", "🌦️"),
    81: ("Moderate Showers", "🌧️"),
    82: ("Violent Showers", "⛈️"),
    95: ("Thunderstorm", "⛈️"),
}


def get_calibrated_solar_factor():
    """
    ML Calibration: Computes dynamic solar yield factor (kWh per MJ/m² radiation)
    based on historical pairs of observed inverter generation vs recorded solar radiation.
    Takes into account roof geometry (20° tilt, south-facing) and seasonal diffuse light.
    """
    # Baseline for 3.7kWp south-facing array with 20° tilt in UK late autumn
    # High summer = ~0.75-0.80. Autumn/Winter low-altitude diffuse = ~0.50-0.55
    default_factor = 0.54

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT radiation_mj, solar_kwh 
            FROM daily_summary 
            WHERE radiation_mj > 1.0 AND solar_kwh > 0.3 
            ORDER BY date DESC LIMIT 14
            """
        )
        rows = c.fetchall()
        conn.close()

        if rows and len(rows) >= 2:
            ratios = [kwh / rad for rad, kwh in rows if rad > 0]
            if ratios:
                # Weighted average favoring the most recent 7 days
                weights = [1.0 / (i + 1) for i in range(len(ratios))]
                learned_factor = sum(r * w for r, w in zip(ratios, weights)) / sum(weights)
                # Clamp within realistic physical boundaries for a 3.7kWp system (0.35 to 0.90)
                return round(max(0.35, min(0.90, learned_factor)), 3)
    except Exception as e:
        print(f"[Solar Calibration ML Warning] {e}")

    return default_factor


def _calculate_clearsky_ghi(lat_deg, day_of_year, hour_float):
    """Astronomical clear-sky global horizontal irradiance (W/m²) based on solar geometry."""
    lat = math.radians(lat_deg)
    decl = math.radians(23.45 * math.sin(math.radians((360.0 / 365.0) * (day_of_year - 81))))
    omega = math.radians((hour_float - 12.0) * 15.0)
    sin_alpha = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(omega)
    if sin_alpha <= 0.02:
        return 0.0
    alpha = math.asin(sin_alpha)
    air_mass = 1.0 / (math.sin(alpha) + 0.50572 * math.pow(math.degrees(alpha) + 6.07995, -1.6364))
    dni = 1361.0 * math.pow(0.7, math.pow(air_mass, 0.678))
    return max(0.0, dni * sin_alpha + (1361.0 * 0.1 * sin_alpha))


def poll_weather_forecast():
    """Fetch weather & solar radiation forecast. Uses Open-Meteo with automatic fallback to Met.no."""
    calibrated_factor = get_calibrated_solar_factor()
    now_dt = datetime.now()
    doy = now_dt.timetuple().tm_yday

    # Primary: Open-Meteo
    url_om = (
        f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}"
        "&current=temperature_2m,weathercode,cloudcover"
        "&hourly=temperature_2m,shortwave_radiation,cloudcover,weathercode"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,shortwave_radiation_sum"
        "&timezone=Europe%2FLondon&forecast_days=3"
    )

    try:
        try:
            req = urllib.request.Request(url_om, headers={"User-Agent": "SmartPowerDashboard/2.0 (contact: user@example.com)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            current = data.get("current", {})
            daily = data.get("daily", {})
            hourly = data.get("hourly", {})

            curr_temp = current.get("temperature_2m", 12.0)
            curr_code = current.get("weathercode", 0)
            curr_desc, curr_icon = WMO_WEATHER_CODES.get(curr_code, ("Clear", "☀️"))

            rad_sums = daily.get("shortwave_radiation_sum", [4.5, 5.5, 5.5])
            today_rad = rad_sums[0] if len(rad_sums) > 0 else 4.5
            tomorrow_rad = rad_sums[1] if len(rad_sums) > 1 else 5.5

            today_predicted_kwh = round(today_rad * calibrated_factor, 1)
            tomorrow_predicted_kwh = round(tomorrow_rad * calibrated_factor, 1)

            t_maxs = daily.get("temperature_2m_max", [15.0, 15.0])
            t_mins = daily.get("temperature_2m_min", [8.0, 8.0])
            w_codes = daily.get("weathercode", [0, 0])
            tomorrow_t_max = t_maxs[1] if len(t_maxs) > 1 else 15.0
            tomorrow_t_min = t_mins[1] if len(t_mins) > 1 else 8.0
            tomorrow_code = w_codes[1] if len(w_codes) > 1 else 0
            tomorrow_desc, tomorrow_icon = WMO_WEATHER_CODES.get(tomorrow_code, ("Partly Cloudy", "⛅"))

            hourly_watt_scale = round((calibrated_factor / 0.80) * 2.88, 2)
            hourly_times = hourly.get("time", [])
            hourly_rad = hourly.get("shortwave_radiation", [])
            hourly_temps = hourly.get("temperature_2m", [])
            hourly_codes = hourly.get("weathercode", [])

            now_hour_str = now_dt.strftime("%Y-%m-%dT%H:00")
            start_idx = 0
            for idx, t_str in enumerate(hourly_times):
                if t_str >= now_hour_str:
                    start_idx = idx
                    break

            hourly_forecast = []
            for i in range(start_idx, min(len(hourly_times), start_idx + 24)):
                t_obj = datetime.fromisoformat(hourly_times[i])
                w_m2 = hourly_rad[i] if i < len(hourly_rad) else 0.0
                pred_watts = round(w_m2 * hourly_watt_scale)
                code = hourly_codes[i] if i < len(hourly_codes) else 0
                desc, icon = WMO_WEATHER_CODES.get(code, ("Clear", "☀️"))
                hourly_forecast.append({
                    "time": t_obj.strftime("%H:00"),
                    "date": t_obj.strftime("%a"),
                    "watts": pred_watts,
                    "temp_c": round(hourly_temps[i], 1) if i < len(hourly_temps) else 15.0,
                    "icon": icon,
                    "desc": desc,
                })

        except Exception as e_om:
            # Fallback to Norwegian Meteorological Institute (Met.no) when Open-Meteo is overloaded
            try:
                url_met = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={LATITUDE}&lon={LONGITUDE}"
                req_met = urllib.request.Request(url_met, headers={"User-Agent": "SmartPowerDashboard/2.0 (contact: user@example.com)"})
                with urllib.request.urlopen(req_met, timeout=7) as resp:
                    data_met = json.load(resp)

                timeseries = data_met.get("properties", {}).get("timeseries", [])
                if not timeseries:
                    raise ValueError("Empty timeseries from Met.no")

                curr_entry = timeseries[0]["data"]["instant"]["details"]
                curr_temp = curr_entry.get("air_temperature", 11.0)
                curr_code = 1
                curr_desc, curr_icon = ("Fair", "🌤️")

                # Calculate today & tomorrow solar using clear-sky physics attenuated by cloud cover
                today_str = now_dt.strftime("%Y-%m-%d")
                tomorrow_str = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")

                today_mj = 0.0
                tomorrow_mj = 0.0
                hourly_forecast = []

                for entry in timeseries:
                    t_iso = entry.get("time", "")
                    t_obj = datetime.fromisoformat(t_iso.replace("Z", "+00:00")).astimezone()
                    dt_str = t_obj.strftime("%Y-%m-%d")
                    hour_f = t_obj.hour + t_obj.minute / 60.0
                    day_num = t_obj.timetuple().tm_yday

                    cloud_pct = entry.get("data", {}).get("instant", {}).get("details", {}).get("cloud_area_fraction", 50.0)
                    temp_c = entry.get("data", {}).get("instant", {}).get("details", {}).get("air_temperature", 10.0)
                    # Cloud attenuation: (1 - 0.75 * cloud_fraction)
                    cloud_factor = max(0.15, 1.0 - (0.75 * (cloud_pct / 100.0)))
                    clear_w = _calculate_clearsky_ghi(LATITUDE, day_num, hour_f)
                    ghi_w = clear_w * cloud_factor

                    if dt_str == today_str:
                        today_mj += (ghi_w * 3600.0) / 1_000_000.0
                    elif dt_str == tomorrow_str:
                        tomorrow_mj += (ghi_w * 3600.0) / 1_000_000.0

                    if len(hourly_forecast) < 24 and t_obj >= now_dt.astimezone() - timedelta(minutes=30):
                        # Inverter AC Watts = GHI * 3.7kWp * (calibrated_factor / 0.80 * 2.88)
                        pred_w = round(ghi_w * ((calibrated_factor / 0.80) * 2.88))
                        symbol = entry.get("data", {}).get("next_1_hours", {}).get("summary", {}).get("symbol_code", "partlycloudy_day")
                        icon = "☀️" if "clearsky" in symbol else ("⛅" if "fair" in symbol or "partly" in symbol else "☁️")
                        hourly_forecast.append({
                            "time": t_obj.strftime("%H:00"),
                            "date": t_obj.strftime("%a"),
                            "watts": pred_w,
                            "temp_c": round(temp_c, 1),
                            "icon": icon,
                            "desc": symbol.replace("_", " ").title(),
                        })

                today_rad = round(today_mj, 1)
                tomorrow_rad = round(tomorrow_mj, 1)
                today_predicted_kwh = round(today_rad * calibrated_factor, 1)
                tomorrow_predicted_kwh = round(tomorrow_rad * calibrated_factor, 1)
                tomorrow_t_max = curr_temp
                tomorrow_t_min = curr_temp - 4.0
                tomorrow_desc, tomorrow_icon = ("Partly Cloudy", "⛅")

            except Exception as e_met:
                print(f"[Weather Dual-Provider Error] OM: {e_om} | Met.no: {e_met}")
                return

        with lock:
            latest_weather["status"] = "online"
            latest_weather["current_temp_c"] = round(curr_temp, 1)
            latest_weather["current_weathercode"] = curr_code
            latest_weather["current_weather_desc"] = curr_desc
            latest_weather["current_icon"] = curr_icon
            latest_weather["today_radiation_mj"] = today_rad
            latest_weather["calibration_factor"] = calibrated_factor
            latest_weather["today_solar_kwh"] = today_predicted_kwh
            latest_weather["tomorrow_solar_kwh"] = tomorrow_predicted_kwh
            latest_weather["tomorrow_temp_max_c"] = round(tomorrow_t_max, 1)
            latest_weather["tomorrow_temp_min_c"] = round(tomorrow_t_min, 1)
            latest_weather["tomorrow_weathercode"] = 0
            latest_weather["tomorrow_weather_desc"] = tomorrow_desc
            latest_weather["tomorrow_icon"] = tomorrow_icon
            latest_weather["hourly_forecast"] = hourly_forecast
            latest_weather["last_updated"] = datetime.now().strftime("%H:%M:%S")

    except Exception as e:
        print(f"[Weather Forecast Warning] {e}")

    except Exception as e:
        print(f"[Weather Forecast Warning] {e}")


def calculate_daily_financials():
    """Calculate today's electricity cost on Octopus Go and savings generated by solar + battery."""
    now = datetime.now()
    midnight_ts = datetime(now.year, now.month, now.day).timestamp()
    
    with lock:
        peak_rate = latest_octopus.get("peak_rate_p_kwh", 36.5644)
        off_peak_rate = latest_octopus.get("off_peak_rate_p_kwh", 9.40)
        curr_load_w = latest_data.get("power", {}).get("load_w", 0)
        curr_grid_net_w = latest_data.get("power", {}).get("grid_net_w", 0)
        curr_rate = latest_octopus.get("current_rate_p_kwh", peak_rate)

    # Live burn rates (pence per hour)
    live_cost_p_hr = round((max(0, curr_grid_net_w) / 1000.0) * curr_rate, 2)
    standard_flat_rate = 26.0  # Ofgem standard price cap reference rate (p/kWh)
    live_savings_p_hr = round(max(0.0, ((curr_load_w / 1000.0) * standard_flat_rate) - live_cost_p_hr), 2)

    import_cost_p = 0.0
    baseline_cost_p = 0.0
    standing_charge_p = 48.85  # Octopus Go daily standing charge

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT ts, grid_power, load_power FROM telemetry WHERE ts >= ? ORDER BY ts ASC",
            (midnight_ts,),
        )
        rows = c.fetchall()
        conn.close()

        for i in range(1, len(rows)):
            dt_hours = (rows[i][0] - rows[i - 1][0]) / 3600.0
            if dt_hours <= 0 or dt_hours > 0.05:
                continue
            grid_w = rows[i][1]
            load_w = rows[i][2]

            ts_dt = datetime.fromtimestamp(rows[i][0])
            mins = ts_dt.hour * 60 + ts_dt.minute
            is_cheap = (30 <= mins < 330)
            rate = off_peak_rate if is_cheap else peak_rate

            if grid_w > 0:
                import_kwh = (grid_w * dt_hours) / 1000.0
                import_cost_p += import_kwh * rate

            load_kwh = (load_w * dt_hours) / 1000.0
            baseline_cost_p += load_kwh * standard_flat_rate
    except Exception as e:
        print(f"[Financials Calc Warning] {e}")

    today_cost_pounds = round((import_cost_p + standing_charge_p) / 100.0, 2)
    today_import_only_pounds = round(import_cost_p / 100.0, 2)
    today_savings_pounds = round(max(0.0, (baseline_cost_p - import_cost_p) / 100.0), 2)

    # Weekly and monthly estimates based on cumulative energy totals
    with lock:
        tot_pv_kwh = latest_data.get("energy", {}).get("total", {}).get("solar_pv_kwh", 0.0)
        tot_import_kwh = latest_data.get("energy", {}).get("total", {}).get("grid_import_kwh", 0.0)
        tot_load_kwh = latest_data.get("energy", {}).get("total", {}).get("load_kwh", 0.0)

    # Estimated cumulative savings from solar self-consumption
    total_estimated_savings_gbp = round(max(0.0, (tot_pv_kwh * 0.26)), 2)

    # Battery Time to Empty / Full calculation
    with lock:
        soc_pct = latest_data.get("battery", {}).get("soc_pct", 10)
        min_soc_pct = latest_data.get("battery", {}).get("min_soc_pct", 10)
        bat_w = latest_data.get("power", {}).get("battery_raw_w", 0) # pos = discharge, neg = charge
        pv_w = latest_data.get("power", {}).get("pv_total_w", 0)
        load_w = latest_data.get("power", {}).get("load_w", 0)
        grid_w = latest_data.get("power", {}).get("grid_net_w", 0)
        is_off_peak = latest_octopus.get("is_off_peak", False)

    # Contract specification: Fox EQ5000 9.84 kWh gross capacity
    BATTERY_CAPACITY_KWH = 9.84
    usable_soc = max(0, soc_pct - min_soc_pct)
    usable_kwh_left = (usable_soc / 100.0) * BATTERY_CAPACITY_KWH
    kwh_to_full = (max(0, 100 - soc_pct) / 100.0) * BATTERY_CAPACITY_KWH

    battery_countdown = {
        "mode": "idle",
        "label": "Battery Idle",
        "eta_minutes": None,
        "eta_clock": None,
        "detail": "Holding at reserve",
    }

    if bat_w > 20:  # Actively discharging to power the house
        hours_left = usable_kwh_left / (bat_w / 1000.0)
        mins_left = int(hours_left * 60)
        if mins_left <= 0:
            battery_countdown = {
                "mode": "reserve",
                "label": "Reserve Reached",
                "eta_minutes": 0,
                "eta_clock": "Now",
                "detail": f"Protected at {min_soc_pct}% reserve",
            }
        elif mins_left > 1440:  # > 24 hours
            battery_countdown = {
                "mode": "discharging",
                "label": "> 24h Left",
                "eta_minutes": mins_left,
                "eta_clock": "> 24h",
                "detail": f"Supplying {bat_w}W house load",
            }
        else:
            eta_dt = now.timestamp() + mins_left * 60
            eta_clock = datetime.fromtimestamp(eta_dt).strftime("%H:%M")
            h = mins_left // 60
            m = mins_left % 60
            battery_countdown = {
                "mode": "discharging",
                "label": f"Empty in {h}h {m}m",
                "eta_minutes": mins_left,
                "eta_clock": eta_clock,
                "detail": f"Will reach {min_soc_pct}% reserve at approx. {eta_clock}",
            }
    elif bat_w < -20:  # Actively charging (from solar or grid)
        chg_w = abs(bat_w)
        hours_to_full = kwh_to_full / (chg_w / 1000.0)
        mins_to_full = int(hours_to_full * 60)
        if mins_to_full <= 5:
            battery_countdown = {
                "mode": "full",
                "label": "Fully Charged",
                "eta_minutes": 0,
                "eta_clock": "Now",
                "detail": "Battery at 100% SoC",
            }
        else:
            eta_dt = now.timestamp() + mins_to_full * 60
            eta_clock = datetime.fromtimestamp(eta_dt).strftime("%H:%M")
            h = mins_to_full // 60
            m = mins_to_full % 60
            battery_countdown = {
                "mode": "charging",
                "label": f"Full in {h}h {m}m",
                "eta_minutes": mins_to_full,
                "eta_clock": eta_clock,
                "detail": f"Charging at {chg_w}W · full at {eta_clock}",
            }
    else:
        if soc_pct <= min_soc_pct:
            battery_countdown = {
                "mode": "reserve",
                "label": f"At Reserve ({min_soc_pct}%)",
                "eta_minutes": 0,
                "eta_clock": "Holding",
                "detail": f"Grid charging scheduled for 00:30 (9.4p)",
            }
        else:
            battery_countdown = {
                "mode": "idle",
                "label": f"Stable ({soc_pct}%)",
                "eta_minutes": None,
                "eta_clock": "Holding",
                "detail": f"{round(usable_kwh_left, 1)} kWh usable energy stored",
            }

    # Advanced Battery Analytics & 00:30 Off-Peak Bridging Calculation
    bridging_analytics = {
        "status": "calculating",
        "usable_kwh_stored": round(usable_kwh_left, 2),
        "total_kwh_capacity": BATTERY_CAPACITY_KWH,
        "soh_pct": 100,
        "est_cycles": 14,
        "min_reserve_pct": min_soc_pct,
        "temp_c": 20.0,
        "voltage_v": 132.0,
        "will_bridge_to_offpeak": True,
        "surplus_kwh_at_offpeak": 0.0,
        "deficit_kwh_before_offpeak": 0.0,
        "bridge_confidence": "High",
        "bridging_message": "Battery is fully on track to bridge until 00:30 off-peak rate.",
    }

    try:
        with lock:
            bat_temp = latest_data.get("temperatures", {}).get("battery_c", 20.0)
            bat_volt = latest_data.get("battery", {}).get("voltage_v", 132.0)
            tot_disch_kwh = latest_data.get("energy", {}).get("total", {}).get("battery_discharge_kwh", 0.0)
            tot_chg_kwh = latest_data.get("energy", {}).get("total", {}).get("battery_charge_kwh", 0.0)

        # Cycles = total discharge / usable capacity (8.86 kWh)
        cycles = round(tot_disch_kwh / (BATTERY_CAPACITY_KWH * 0.9), 1) if tot_disch_kwh > 0 else 12.0
        # Round trip efficiency %
        rte_pct = round((tot_disch_kwh / tot_chg_kwh) * 100.0, 1) if tot_chg_kwh > 1.0 else 92.5

        # Calculate exact hours remaining until 00:30 tonight
        current_time_dt = datetime.now()
        target_0030 = datetime(current_time_dt.year, current_time_dt.month, current_time_dt.day, 0, 30)
        if current_time_dt >= target_0030:
            target_0030 += timedelta(days=1)
        hours_to_offpeak = max(0.1, (target_0030 - current_time_dt).total_seconds() / 3600.0)

        # Baseline consumption rate from now until 00:30
        baseload_profile = get_learned_hourly_baseload()
        curr_hour = current_time_dt.hour
        predicted_evening_drain_kwh = 0.0
        # Sum hourly expected demand
        for h in range(int(hours_to_offpeak) + 1):
            eval_hour_str = f"{(curr_hour + h) % 24:02d}:00"
            hour_w = baseload_profile.get(eval_hour_str, 160)
            # Add dinner cooking/TV bump for evening hours 17:00-21:00
            if 17 <= (curr_hour + h) % 24 <= 21:
                hour_w = max(hour_w, 220)
            predicted_evening_drain_kwh += hour_w / 1000.0
        # Adjust for remaining fractional hour
        predicted_evening_drain_kwh = round(predicted_evening_drain_kwh * (hours_to_offpeak / (int(hours_to_offpeak) + 1)), 2)

        net_margin = round(usable_kwh_left - predicted_evening_drain_kwh, 2)
        will_bridge = net_margin >= 0

        bridging_analytics = {
            "status": "active",
            "usable_kwh_stored": round(usable_kwh_left, 2),
            "total_kwh_capacity": BATTERY_CAPACITY_KWH,
            "soh_pct": 100,
            "est_cycles": cycles,
            "round_trip_efficiency_pct": min(98.0, max(85.0, rte_pct)),
            "min_reserve_pct": min_soc_pct,
            "temp_c": bat_temp,
            "voltage_v": bat_volt,
            "hours_to_offpeak": round(hours_to_offpeak, 1),
            "predicted_evening_demand_kwh": predicted_evening_drain_kwh,
            "will_bridge_to_offpeak": will_bridge,
            "surplus_kwh_at_offpeak": max(0.0, net_margin),
            "deficit_kwh_before_offpeak": max(0.0, -net_margin),
            "bridge_confidence": "High (95%+)" if net_margin > 1.0 else ("Moderate" if will_bridge else "Warning"),
            "bridging_message": (
                f"Battery will easily bridge until 00:30 ({round(hours_to_offpeak, 1)}h away) with +{net_margin} kWh surplus."
                if will_bridge else
                f"May hit {min_soc_pct}% reserve ~{round(abs(net_margin)/(load_w/1000.0 if load_w > 50 else 0.2), 1)}h before 00:30."
            )
        }
    except Exception as e_br:
        print(f"[Bridging Analytics Warning] {e_br}")

    # Appliance Advisory (Dishwasher, Washing Machine, Tumble Dryer)
    # Profiles:
    # - Dishwasher: ~1.2 kWh / cycle (temp heat spike ~2kW)
    # - Washing Machine: ~1.0 kWh / 40°C cycle (heater spike ~2kW)
    # - Tumble Dryer: ~2.5 kWh / cotton cycle (sustained ~2.2kW heat pump / condenser)

    hourly_fc = latest_weather.get("hourly_forecast", [])
    surplus_hours = []
    for h in hourly_fc:
        try:
            h_time = h.get("time", "")
            h_watts = float(h.get("watts", 0))
            if h_watts >= 900:
                surplus_hours.append((h_time, h_watts))
        except Exception:
            pass

    has_solar_window = bool(surplus_hours and (soc_pct >= 60 or latest_weather.get("today_solar_kwh", 0) >= 6.0))
    if has_solar_window:
        start_h = surplus_hours[0][0]
        end_h = surplus_hours[-1][0]
        max_h = max(surplus_hours, key=lambda x: x[1])
        solar_window_label = f"Today {start_h} – {end_h} (Peak {max_h[0]})"
    else:
        solar_window_label = None

    # Calculate optimal windows & costs per appliance
    # Dishwasher (~1.2 kWh)
    dw_rec = {
        "name": "Dishwasher",
        "icon": "🍽️",
        "kwh": 1.2,
        "best_time": solar_window_label if has_solar_window else "00:30 Tonight",
        "cost_str": "0.0p (Free Solar)" if has_solar_window else "11.3p (9.4p Off-Peak)",
        "peak_cost_str": "43.9p",
        "savings_str": "Save 44p" if has_solar_window else "Save 33p",
        "ready_now": bool(pv_w >= load_w + 800 or is_off_peak or soc_pct >= 75),
    }

    # Washing Machine (~1.0 kWh)
    wm_rec = {
        "name": "Washing Machine",
        "icon": "🧺",
        "kwh": 1.0,
        "best_time": solar_window_label if has_solar_window else "00:30 Tonight",
        "cost_str": "0.0p (Free Solar)" if has_solar_window else "9.4p (9.4p Off-Peak)",
        "peak_cost_str": "36.6p",
        "savings_str": "Save 37p" if has_solar_window else "Save 27p",
        "ready_now": bool(pv_w >= load_w + 800 or is_off_peak or soc_pct >= 75),
    }

    # Tumble Dryer (~2.5 kWh) - heavy load!
    td_solar_ok = bool(has_solar_window and max_h[1] >= 1800 and soc_pct >= 85)
    td_rec = {
        "name": "Tumble Dryer",
        "icon": "💨",
        "kwh": 2.5,
        "best_time": (f"Today {max_h[0]}" if td_solar_ok else "00:30 Tonight"),
        "cost_str": ("0.0p (High Solar)" if td_solar_ok else "23.5p (9.4p Off-Peak)"),
        "peak_cost_str": "91.4p",
        "savings_str": ("Save 91p" if td_solar_ok else "Save 68p"),
        "ready_now": bool(pv_w >= load_w + 1500 or is_off_peak),
    }

    # Primary banner headline based on system conditions
    if pv_w >= load_w + 1200 or (pv_w > 1400 and soc_pct >= 85):
        surplus = max(0, pv_w - load_w)
        appliance_advisory = {
            "status": "free_solar",
            "badge": "Run Now · 100% Free Solar",
            "color": "#10b981",
            "icon": "☀️",
            "title": "Ideal Time to Run Appliances",
            "action": f"Solar surplus +{surplus}W: Perfect time for Dishwasher, Washer, or Dryer",
            "savings_note": "0p electricity cost · Pure solar",
            "appliances": [dw_rec, wm_rec, td_rec],
        }
    elif is_off_peak:
        appliance_advisory = {
            "status": "cheap_offpeak",
            "badge": "Run Now · Super Off-Peak (9.4p)",
            "color": "#38bdf8",
            "icon": "⚡",
            "title": "Off-Peak Window Active (9.4p/kWh)",
            "action": "Run Dishwasher, Washing Machine & Dryer before 05:30",
            "savings_note": "Saves ~74% vs peak 36.6p rate",
            "appliances": [dw_rec, wm_rec, td_rec],
        }
    elif soc_pct >= 80 and bat_w >= 0 and grid_w < 50:
        appliance_advisory = {
            "status": "battery_buffered",
            "badge": "Safe to Run · Battery Buffered",
            "color": "#f59e0b",
            "icon": "🔋",
            "title": "Battery Can Cover Wash Cycles",
            "action": f"Battery at {soc_pct}% can cover a Dishwasher or Washer without grid import",
            "savings_note": "Avoiding 36.6p grid peak",
            "appliances": [dw_rec, wm_rec, td_rec],
        }
    else:
        best_overall = solar_window_label if has_solar_window else "00:30 Tonight"
        appliance_advisory = {
            "status": "scheduled_optimal",
            "badge": f"Best Time: {best_overall}",
            "color": "#38bdf8" if has_solar_window else "#a855f7",
            "icon": "☀️" if has_solar_window else "🌙",
            "title": f"Best Time to Run: {best_overall}",
            "action": f"Plan wet appliances for {best_overall} to avoid 36.6p peak rate",
            "savings_note": "Saves up to ~68p per appliance run",
            "appliances": [dw_rec, wm_rec, td_rec],
        }

    # Export metrics (simulated or active)
    with lock:
        today_export_kwh = latest_data.get("energy", {}).get("today", {}).get("grid_export_kwh", 0.0)
        curr_grid_export_w = latest_data.get("power", {}).get("grid_export_w", 0)

    today_export_credit_pounds = round((today_export_kwh * OCTOPUS_EXPORT_RATE_P_KWH) / 100.0, 2)
    live_export_p_hr = round((curr_grid_export_w / 1000.0) * OCTOPUS_EXPORT_RATE_P_KWH, 2)

    export_summary = {
        "rate_p_kwh": OCTOPUS_EXPORT_RATE_P_KWH,
        "is_active": OCTOPUS_EXPORT_ACTIVE,
        "today_export_kwh": today_export_kwh,
        "today_credit_gbp": today_export_credit_pounds,
        "live_export_p_hr": live_export_p_hr,
        "status_label": "Live Export" if OCTOPUS_EXPORT_ACTIVE else "Simulated Export",
    }

    # Annual consumption projection based on daily usage
    with lock:
        today_load_kwh = latest_data.get("energy", {}).get("today", {}).get("load_kwh", 0.0)

    # Baseline annual energy demand from Project Solar contract: 3,476 kWh/yr (~9.52 kWh/day)
    avg_daily_load_kwh = today_load_kwh if today_load_kwh > 2.0 else 9.52
    db_days_count = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT AVG(load_kwh), COUNT(*) FROM daily_summary WHERE load_kwh > 1.0")
        row = c.fetchone()
        conn.close()
        if row and row[0] and row[1] > 0:
            db_days_count = int(row[1])
            # If multiple days are recorded, use DB average, else blend today's load with baseline
            avg_daily_load_kwh = float(row[0])
    except Exception:
        pass

    projected_yearly_kwh = round(avg_daily_load_kwh * 365.25, 0)
    baseline_target_kwh = 3476.0
    yearly_diff_pct = round(((projected_yearly_kwh - baseline_target_kwh) / baseline_target_kwh) * 100.0, 1)

    yearly_projection = {
        "avg_daily_kwh": round(avg_daily_load_kwh, 2),
        "projected_yearly_kwh": int(projected_yearly_kwh),
        "baseline_quote_kwh": int(baseline_target_kwh),
        "diff_vs_quote_pct": yearly_diff_pct,
        "days_sampled": max(1, db_days_count),
    }

    return {
        "today_cost_gbp": today_cost_pounds,
        "today_import_gbp": today_import_only_pounds,
        "today_savings_gbp": today_savings_pounds,
        "total_estimated_savings_gbp": total_estimated_savings_gbp,
        "standing_charge_pence": standing_charge_p,
        "live_cost_p_hr": live_cost_p_hr,
        "live_savings_p_hr": live_savings_p_hr,
        "standard_flat_rate_p_kwh": standard_flat_rate,
        "battery_countdown": battery_countdown,
        "appliance_advisory": appliance_advisory,
        "bridging_analytics": bridging_analytics,
        "export": export_summary,
        "yearly_projection": yearly_projection,
    }


def set_foxess_work_mode(mode_code):
    """Write work mode to Holding Register 41000 via Modbus FC06."""
    try:
        sock = socket.create_connection((INVERTER_HOST, INVERTER_PORT), timeout=4.5)
        # TransID(2) + Proto(2) + Len(2) + Unit(1) + Func(1) + Reg(2) + Val(2)
        payload = struct.pack(">HHHBBHH", 88, 0, 6, INVERTER_UNIT, 6, 41000, int(mode_code))
        sock.sendall(payload)
        resp = sock.recv(16)
        sock.close()
        return True
    except Exception as e:
        print(f"[FoxESS Work Mode Write Warning] {e}")
        return False


def set_foxess_battery_reserves(min_soc, max_soc, min_soc_on_grid=None, max_charge_a=None, max_discharge_a=None):
    """Write battery safety thresholds and current limits (Registers 41007-41011) via Modbus FC16."""
    try:
        sock = socket.create_connection((INVERTER_HOST, INVERTER_PORT), timeout=4.5)
        # Read current register values first
        regs = read_regs(sock, 3, 41007, 5, unit=INVERTER_UNIT, tx_id=89)
        cur_chg_a = regs[0] if regs else 400
        cur_dis_a = regs[1] if regs else 400
        cur_min = regs[2] if regs else 10
        cur_max = regs[3] if regs else 100
        cur_grid_min = regs[4] if regs else 10

        new_chg_a = int(float(max_charge_a) * 10) if max_charge_a is not None else cur_chg_a
        new_dis_a = int(float(max_discharge_a) * 10) if max_discharge_a is not None else cur_dis_a
        new_min = int(min_soc) if min_soc is not None else cur_min
        new_max = int(max_soc) if max_soc is not None else cur_max
        new_grid_min = int(min_soc_on_grid) if min_soc_on_grid is not None else (new_min if min_soc is not None else cur_grid_min)

        vals = [new_chg_a, new_dis_a, new_min, new_max, new_grid_min]
        count = len(vals)
        byte_count = count * 2
        payload = struct.pack(f">HHHBBHHB{count}H", 90, 0, 7 + byte_count, INVERTER_UNIT, 16, 41007, count, byte_count, *vals)
        sock.sendall(payload)
        resp = sock.recv(16)
        sock.close()
        return True
    except Exception as e:
        print(f"[FoxESS Reserves Write Warning] {e}")
        return False


def set_foxess_force_charge_schedule(enabled, start_hour=0, start_minute=30, end_hour=5, end_minute=30, p2_enabled=False, p2_start_hour=0, p2_start_minute=0, p2_end_hour=0, p2_end_minute=0):
    """Write Period 1 and Period 2 force charge schedules to registers 41001-41006 via Modbus FC16."""
    try:
        sock = socket.create_connection((INVERTER_HOST, INVERTER_PORT), timeout=4.5)
        p1_start_val = (start_hour * 256) + start_minute
        p1_end_val = (end_hour * 256) + end_minute
        p2_start_val = (p2_start_hour * 256) + p2_start_minute
        p2_end_val = (p2_end_hour * 256) + p2_end_minute

        vals = [
            1 if enabled else 0,
            p1_start_val,
            p1_end_val,
            1 if p2_enabled else 0,
            p2_start_val,
            p2_end_val,
        ]
        count = len(vals)
        byte_count = count * 2
        payload = struct.pack(f">HHHBBHHB{count}H", 77, 0, 7 + byte_count, INVERTER_UNIT, 16, 41001, count, byte_count, *vals)
        sock.sendall(payload)
        resp = sock.recv(16)
        sock.close()
        return True
    except Exception as e:
        print(f"[FoxESS Schedule Write Warning] {e}")
        return False


def sync_foxess_octopus_schedule():
    """Evaluate adaptive target SoC based on tomorrow's solar forecast and manage night force-charge window."""
    # 1. Adaptive Target SoC evaluation
    try:
        with lock:
            tomorrow_solar = latest_weather.get("tomorrow_solar_kwh", 0.0)

        target_soc, needed_grid_kwh, headroom_kwh, ml_pred = calculate_adaptive_target_soc(tomorrow_solar)
        if headroom_kwh > 0.1:
            headroom_note = f"leaving {headroom_kwh:.1f} kWh solar headroom"
        else:
            headroom_note = "solar absorbed by house, full 100% off-peak buffer needed"

        reason = (
            f"ML predicts {ml_pred['predicted_total_kwh']} kWh demand ({ml_pred['target_day_name']} profile) "
            f"vs {tomorrow_solar:.1f} kWh solar -> target {target_soc}% ({headroom_note})"
        )

        now = datetime.now()
        current_minute = now.hour * 60 + now.minute
        is_night_window = (current_minute >= 22 * 60) or (current_minute < 5 * 60 + 35)

        with lock:
            latest_adaptive.update({
                "enabled": ADAPTIVE_HEADROOM_ENABLED,
                "calculated_target_soc": target_soc,
                "active_register_max_soc": target_soc if (is_night_window and ADAPTIVE_HEADROOM_ENABLED) else 100,
                "reason": reason if ADAPTIVE_HEADROOM_ENABLED else "Adaptive Headroom disabled: charging to 100% capacity",
                "tomorrow_solar_kwh": tomorrow_solar,
                "ml_prediction": ml_pred,
                "night_mode_active": is_night_window,
                "last_evaluated": now.strftime("%H:%M:%S"),
                "topup_enabled": ADAPTIVE_TOPUP_ENABLED,
                "topup_drop_pct": ADAPTIVE_TOPUP_DROP_PCT,
                "buffer_pct": ADAPTIVE_BUFFER_PCT,
            })
    except Exception as e:
        print(f"[FoxESS Adaptive SoC Warning] {e}")


def enforce_inverter_night_charge_cutoff():
    """Actively monitors battery SoC during the off-peak tariff window.
    - If Adaptive Headroom is enabled:
        * Cuts off Period 1 force charge once battery reaches calculated target SoC.
        * If battery falls below target (default: by 3% or more) during off-peak due to household loads,
          automatically resumes force charge to top back up to the target SoC.
    - If Adaptive Headroom is disabled:
        * Keeps force charge active throughout the entire off-peak window to charge to 100% capacity."""
    now = datetime.now()
    cur_mins = now.hour * 60 + now.minute

    # Parse tariff off-peak start and end times
    try:
        start_str = app_config.get("tariff", {}).get("off_peak_start", "00:30")
        end_str = app_config.get("tariff", {}).get("off_peak_end", "05:30")
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
    except Exception:
        start_mins = 30
        end_mins = 330

    if start_mins < end_mins:
        in_offpeak = (start_mins <= cur_mins < end_mins)
    else:
        # Crosses midnight (e.g. 23:30 to 05:30)
        in_offpeak = (cur_mins >= start_mins) or (cur_mins < end_mins)

    # Prep window: ~30 mins before off-peak begins
    prep_start = (start_mins - 30) % 1440
    if prep_start < start_mins:
        in_prep = (prep_start <= cur_mins < start_mins)
    else:
        in_prep = (cur_mins >= prep_start) or (cur_mins < start_mins)

    with lock:
        current_soc = latest_data.get("battery", {}).get("soc_pct", 100)
        target_soc = latest_adaptive.get("calculated_target_soc", 40)
        sched = latest_data.get("settings", {}).get("grid_charge_schedule", {})
        p1_enabled = sched.get("period1", {}).get("enabled", True)
        headroom_enabled = ADAPTIVE_HEADROOM_ENABLED
        topup_enabled = ADAPTIVE_TOPUP_ENABLED
        topup_drop = ADAPTIVE_TOPUP_DROP_PCT

    if in_prep:
        # Prior to the off-peak window, ensure the schedule is enabled and ready to charge
        if not p1_enabled:
            print(f"[FoxESS Auto-Charge] Off-peak prep window: Re-enabling force charge schedule.")
            set_foxess_force_charge_schedule(enabled=True)
    elif in_offpeak:
        if not headroom_enabled:
            # If adaptive headroom is disabled by user, guarantee charging stays enabled to 100%
            if not p1_enabled:
                print(f"[FoxESS Auto-Charge] Full 100% Charge Mode: Ensuring force charge remains enabled throughout off-peak.")
                set_foxess_force_charge_schedule(enabled=True)
        else:
            # Adaptive mode active
            if current_soc >= target_soc and p1_enabled:
                print(f"[FoxESS Auto-Charge] Target SoC reached ({current_soc}% >= {target_soc}%). Pausing force charge to preserve solar headroom.")
                set_foxess_force_charge_schedule(enabled=False)
            elif topup_enabled and current_soc <= (target_soc - topup_drop) and not p1_enabled:
                # Top up back to adaptive target if house load drained battery during off-peak
                print(f"[FoxESS Auto-Charge] Top-Up Triggered: SoC dropped to {current_soc}% (<= target {target_soc}% - {topup_drop}%). Resuming force charge.")
                set_foxess_force_charge_schedule(enabled=True)
    else:
        # Outside off-peak window: Ensure schedule is re-enabled for upcoming night cycle
        if not p1_enabled:
            set_foxess_force_charge_schedule(enabled=True)


def get_tapo_credentials():
    if TAPO_ENV_USER and TAPO_ENV_PASS:
        return TAPO_ENV_USER, TAPO_ENV_PASS
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM app_config WHERE key = 'tapo_username'")
        r1 = c.fetchone()
        c.execute("SELECT value FROM app_config WHERE key = 'tapo_password'")
        r2 = c.fetchone()
        conn.close()
        if r1 and r2 and r1[0] and r2[0]:
            return r1[0], r2[0]
    except Exception:
        pass
    return None, None


def save_tapo_credentials(username, password):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('tapo_username', ?)", (username,))
    c.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('tapo_password', ?)", (password,))
    conn.commit()
    conn.close()


def get_tapo_known_ips():
    ips = [TAPO_DEFAULT_IP]
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM app_config WHERE key = 'tapo_known_ips'")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            saved = json.loads(row[0])
            if isinstance(saved, list) and saved:
                for ip in saved:
                    if ip and ip not in ips:
                        ips.append(str(ip).strip())
    except Exception:
        pass
    return ips


def add_tapo_known_ip(ip):
    ip = str(ip).strip()
    if not ip:
        return get_tapo_known_ips()
    ips = get_tapo_known_ips()
    if ip not in ips:
        ips.append(ip)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('tapo_known_ips', ?)", (json.dumps(ips),))
        conn.commit()
        conn.close()
    return ips


def scan_tapo_subnet():
    """Sweep 192.168.86.1..254 on UDP port 20002 to auto-discover any new Tapo plugs on the LAN."""
    prefix = ".".join(TAPO_DEFAULT_IP.split(".")[:3])
    probe = bytes.fromhex("020000010000000000000000463cb5d3")
    found_ips = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.6)
    try:
        for i in range(1, 255):
            s.sendto(probe, (f"{prefix}.{i}", 20002))
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(2048)
                if len(data) > 16 and addr[0] not in found_ips:
                    found_ips.append(addr[0])
            except socket.timeout:
                break
    except Exception as e:
        print(f"[Tapo Auto-Discovery Warning] {e}")
    finally:
        s.close()

    for ip in found_ips:
        add_tapo_known_ip(ip)
    return found_ips


def get_tapo_runtime(ip):
    if ip not in tapo_automation_runtime_by_ip:
        tapo_automation_runtime_by_ip[ip] = {
            "idle_since_ts": None,
            "idle_elapsed_sec": 0,
            "last_hue_room_on": False,
            "last_schedule_trigger_date": "",
            "last_sleep_trigger_date": "",
            "last_wake_trigger_date": "",
            "last_action": "Monitoring live",
            "last_action_ts": None,
            "dishwasher_armed": False,
            "dishwasher_running": False,
            "dishwasher_finish_since_ts": None,
        }
    return tapo_automation_runtime_by_ip[ip]


def get_tapo_automation_rules(ip=TAPO_DEFAULT_IP):
    rules = dict(DEFAULT_TAPO_AUTO_RULES)
    if ip in TAPO_DEVICE_AUTO_RULES:
        rules.update(TAPO_DEVICE_AUTO_RULES[ip])
    # Load persisted config from SQLite if available
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM app_config WHERE key = ?", (f"tapo_auto_{ip}",))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            saved = json.loads(row[0])
            rules.update(saved)
    except Exception:
        pass
    return rules


def save_tapo_automation_rules(ip, new_rules):
    ip = str(ip or TAPO_DEFAULT_IP).strip()
    merged = get_tapo_automation_rules(ip)
    merged.update(new_rules)
    merged.pop("ip", None)
    if ip in TAPO_DEVICE_AUTO_RULES:
        TAPO_DEVICE_AUTO_RULES[ip].update(merged)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
        (f"tapo_auto_{ip}", json.dumps(merged)),
    )
    conn.commit()
    conn.close()
    return merged


def evaluate_tapo_automation():
    with lock:
        if latest_tapo.get("status") != "online" or not latest_tapo.get("plugs"):
            return
        plugs_snapshot = [dict(p) for p in latest_tapo["plugs"]]
        bat_soc = latest_data.get("battery", {}).get("soc_pct", 100)
        bat_min = latest_data.get("battery", {}).get("min_soc_pct", 10)
        pv_w = latest_data.get("power", {}).get("pv_total_w", 0)
        load_w = latest_data.get("power", {}).get("load_w", 0)
        is_off_peak = latest_octopus.get("is_off_peak", False)
        hue_rooms = list(latest_hue.get("rooms", []))

    user, pw = get_tapo_credentials()
    if not user or not pw:
        return

    now_ts = int(time.time())
    now_dt = datetime.now()
    toggled_any = False

    for plug in plugs_snapshot:
        ip = plug.get("ip", TAPO_DEFAULT_IP)
        rules = get_tapo_automation_rules(ip)
        rt = get_tapo_runtime(ip)

        if not rules.get("enabled", True):
            rt["idle_since_ts"] = None
            rt["idle_elapsed_sec"] = 0
            continue

        is_on = bool(plug.get("is_on", False))
        watts = float(plug.get("watts", 0.0))
        idle_thresh = float(rules.get("idle_threshold_w", 15.0))
        idle_target_sec = max(60, int(float(rules.get("idle_minutes", 15)) * 60))

        # ==========================================================
        # SPECIAL: Dishwasher Smart Auto-Arm & Off-Peak / Solar Auto-Start
        # ==========================================================
        if rules.get("dishwasher_mode_enabled", False):
            # Phase 1: Smart Auto-Arm (User starts cycle outside cheap hours)
            # When plug is ON and not off-peak, if watts jump > arm_thresh (e.g. 25W), user pressed start!
            arm_thresh = float(rules.get("dishwasher_arm_watts", 25.0))
            if is_on and not is_off_peak and not rt.get("dishwasher_running", False):
                if watts >= arm_thresh:
                    try:
                        asyncio.run(_async_toggle_tapo(ip, False, user, pw))
                        rt["dishwasher_armed"] = True
                        rt["dishwasher_running"] = False
                        rt["dishwasher_finish_since_ts"] = None
                        rt["last_action"] = f"Cycle Armed! Paused ({watts:.0f}W). Auto-starts at 00:30 or on solar"
                        rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                        toggled_any = True
                        continue
                    except Exception as e:
                        print(f"[Dishwasher Arm Error {ip}] {e}")

            # Phase 2: Auto-Resume (Plug is OFF and Armed)
            if not is_on and rt.get("dishwasher_armed", False):
                trigger_reason = None
                solar_min_w = float(rules.get("dishwasher_solar_surplus_w", 1200.0))
                solar_surplus = max(0, pv_w - load_w)

                if rules.get("dishwasher_offpeak_trigger", True) and is_off_peak:
                    trigger_reason = "Octopus Go off-peak window active (9.4p/kWh)"
                elif rules.get("dishwasher_solar_trigger", True) and (pv_w >= solar_min_w or solar_surplus >= 800) and bat_soc >= 50:
                    trigger_reason = f"Free Solar surplus (+{solar_surplus}W surplus, PV {pv_w}W, Battery {bat_soc}%)"

                if trigger_reason:
                    try:
                        asyncio.run(_async_toggle_tapo(ip, True, user, pw))
                        rt["dishwasher_armed"] = False
                        rt["dishwasher_running"] = False
                        rt["dishwasher_finish_since_ts"] = None
                        rt["last_action"] = f"Auto-Resume: {trigger_reason}"
                        rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                        toggled_any = True
                        continue
                    except Exception as e:
                        print(f"[Dishwasher Resume Error {ip}] {e}")

            # Once resumed, plug stays ON indefinitely (standby is essentially 0W)
            continue

        # 1. Evaluate Auto-OFF rules when plug is ON
        if is_on:
            if watts <= idle_thresh:
                if rt["idle_since_ts"] is None:
                    rt["idle_since_ts"] = now_ts
                rt["idle_elapsed_sec"] = now_ts - rt["idle_since_ts"]
            else:
                rt["idle_since_ts"] = None
                rt["idle_elapsed_sec"] = 0

            # Rule A: Idle / Standby Vampire Cutoff
            if rules.get("idle_cutoff_enabled", True) and rt["idle_elapsed_sec"] >= idle_target_sec:
                try:
                    asyncio.run(_async_toggle_tapo(ip, False, user, pw))
                    rt["idle_since_ts"] = None
                    rt["idle_elapsed_sec"] = 0
                    rt["last_action"] = (
                        f"Auto-OFF: Idle ({watts:.1f}W ≤ {idle_thresh:.0f}W for {int(rules.get('idle_minutes', 15))}m)"
                    )
                    rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                    toggled_any = True
                    continue
                except Exception as e:
                    print(f"[Tapo Auto-Off Error {ip}] {e}")

            # Rule B: Battery Reserve Saver
            reserve_thresh = max(int(bat_min), int(rules.get("battery_reserve_soc_pct", 12)))
            if (
                rules.get("battery_reserve_cutoff_enabled", True)
                and bat_soc <= reserve_thresh
                and watts <= max(idle_thresh, 15.0)
                and rt["idle_elapsed_sec"] >= 60
            ):
                try:
                    asyncio.run(_async_toggle_tapo(ip, False, user, pw))
                    rt["idle_since_ts"] = None
                    rt["idle_elapsed_sec"] = 0
                    rt["last_action"] = f"Auto-OFF: Battery at {bat_soc}% reserve & idle ({watts:.1f}W)"
                    rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                    toggled_any = True
                    continue
                except Exception as e:
                    print(f"[Tapo Battery Cutoff Error {ip}] {e}")
        else:
            rt["idle_since_ts"] = None
            rt["idle_elapsed_sec"] = 0

        # 2. Evaluate Auto-ON rules when plug is OFF
        # Rule C: Hue Room Light Presence Sync
        target_room_id = str(rules.get("hue_room_id", "84"))
        room_obj = next((r for r in hue_rooms if str(r.get("id")) == target_room_id), None)
        room_any_on = bool(room_obj.get("any_on", False)) if room_obj else False
        prev_room_on = rt.get("last_hue_room_on", False)
        rt["last_hue_room_on"] = room_any_on

        if not is_on and rules.get("hue_room_sync_enabled", False) and room_any_on and not prev_room_on:
            try:
                asyncio.run(_async_toggle_tapo(ip, True, user, pw))
                rname = room_obj.get("name", "Hue Room") if room_obj else "Hue Room"
                rt["last_action"] = f"Auto-ON: {rname} lights switched on"
                rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                toggled_any = True
                continue
            except Exception as e:
                print(f"[Tapo Hue Sync Error {ip}] {e}")

        # Rule D: Scheduled Morning Wakeup (Weekday & Weekend Support)
        if not is_on:
            is_weekday = now_dt.weekday() < 5
            is_weekend = not is_weekday
            should_schedule_wake = False
            scheduled_wake_time = None

            if is_weekday and rules.get("workday_morning_on_enabled", False):
                should_schedule_wake = True
                scheduled_wake_time = str(rules.get("workday_on_time", "06:50")).strip()
            elif is_weekend and rules.get("weekend_morning_on_enabled", False):
                should_schedule_wake = True
                scheduled_wake_time = str(rules.get("weekend_on_time", "10:00")).strip()

            if should_schedule_wake and scheduled_wake_time:
                current_hm = now_dt.strftime("%H:%M")
                today_str = now_dt.strftime("%Y-%m-%d")
                if current_hm == scheduled_wake_time and rt.get("last_schedule_trigger_date") != today_str:
                    rt["last_schedule_trigger_date"] = today_str
                    try:
                        asyncio.run(_async_toggle_tapo(ip, True, user, pw))
                        day_type = "Weekday" if is_weekday else "Weekend"
                        rt["last_action"] = f"Auto-ON: {day_type} wakeup ({scheduled_wake_time})"
                        rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                        toggled_any = True
                        continue
                    except Exception as e:
                        print(f"[Tapo Schedule On Error {ip}] {e}")

        # Rule E: Media / TV Overnight Deep-Sleep Window (01:00 - 07:00)
        if rules.get("night_sleep_enabled", False):
            current_hm = now_dt.strftime("%H:%M")
            today_str = now_dt.strftime("%Y-%m-%d")
            sleep_hm = str(rules.get("night_sleep_off_time", "01:00")).strip()
            wake_hm = str(rules.get("night_sleep_wake_time", "07:00")).strip()
            max_active_w = float(rules.get("night_sleep_max_active_w", 25.0))

            # Turn OFF at sleep time if nobody is actively watching (draw <= max_active_w)
            if is_on and current_hm == sleep_hm and rt.get("last_sleep_trigger_date") != today_str:
                if watts <= max_active_w:
                    rt["last_sleep_trigger_date"] = today_str
                    try:
                        asyncio.run(_async_toggle_tapo(ip, False, user, pw))
                        rt["last_action"] = f"Overnight Deep-Sleep: Standby cut at {sleep_hm} ({watts:.1f}W)"
                        rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                        toggled_any = True
                        continue
                    except Exception as e:
                        print(f"[TV Sleep Error {ip}] {e}")
                else:
                    # Still actively in use (e.g. late night movie); don't annoy user!
                    rt["last_action"] = f"Sleep postponed: In active use ({watts:.1f}W > {max_active_w:.0f}W)"
                    rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")

            # Turn ON automatically every morning at wake time
            if not is_on and current_hm == wake_hm and rt.get("last_wake_trigger_date") != today_str:
                rt["last_wake_trigger_date"] = today_str
                try:
                    asyncio.run(_async_toggle_tapo(ip, True, user, pw))
                    rt["last_action"] = f"Morning Wakeup: Plug restored at {wake_hm}"
                    rt["last_action_ts"] = now_dt.strftime("%H:%M:%S")
                    toggled_any = True
                    continue
                except Exception as e:
                    print(f"[TV Wakeup Error {ip}] {e}")

    if toggled_any:
        poll_tapo_once()


async def _async_poll_single_tapo(ip, username, password):
    # Try high-performance Tapo client first (supports TPAP, KLAP v2, and newest Matter firmware)
    if HAS_TAPO:
        try:
            client = ApiClient(username, password)
            dev = await client.p110(ip)
            info = await dev.get_device_info()
            power = await dev.get_current_power()
            energy = await dev.get_energy_usage()
            alias = info.nickname or f"Tapo Plug ({ip})"
            model = info.model or "P110M(UK)"
            mac = getattr(info, "mac", "Unknown")
            is_on = bool(info.device_on)
            watts = float(power.current_power)
            today_kwh = float(energy.today_energy) / 1000.0
            month_kwh = float(energy.month_energy) / 1000.0
            return {
                "ip": ip,
                "alias": alias,
                "model": model,
                "mac": mac,
                "is_on": is_on,
                "watts": round(watts, 1),
                "today_kwh": round(today_kwh, 3),
                "month_kwh": round(month_kwh, 2),
                "authenticated": True,
            }
        except Exception as e:
            if not HAS_KASA:
                raise e

    # Fallback to python-kasa (legacy KLAP/AES)
    if HAS_KASA:
        creds = Credentials(username, password)
        dev = await Discover.discover_single(ip, credentials=creds, timeout=4)
        await dev.update()
        watts = 0.0
        today_kwh = 0.0
        month_kwh = 0.0
        if dev.has_emeter:
            em = dev.emeter_realtime
            if hasattr(em, "power") and em.power is not None:
                watts = float(em.power)
            elif isinstance(em, dict) and "power" in em:
                watts = float(em["power"])
            if dev.emeter_today is not None:
                today_kwh = float(dev.emeter_today)
            if dev.emeter_this_month is not None:
                month_kwh = float(dev.emeter_this_month)
        if watts == 0.0 and "Energy" in getattr(dev, "modules", {}):
            en_mod = dev.modules["Energy"]
            if getattr(en_mod, "current_consumption", None) is not None:
                watts = float(en_mod.current_consumption)
            if getattr(en_mod, "consumption_today", None) is not None:
                today_kwh = float(en_mod.consumption_today)
            if getattr(en_mod, "consumption_this_month", None) is not None:
                month_kwh = float(en_mod.consumption_this_month)

        alias = dev.alias or f"Tapo Plug ({ip})"
        model = dev.model or "P110M(UK)"
        mac = dev.mac or "Unknown"
        is_on = bool(dev.is_on)
        await dev.disconnect()
        return {
            "ip": ip,
            "alias": alias,
            "model": model,
            "mac": mac,
            "is_on": is_on,
            "watts": round(watts, 1),
            "today_kwh": round(today_kwh, 3),
            "month_kwh": round(month_kwh, 2),
            "authenticated": True,
        }
    raise RuntimeError("No Tapo or Kasa library available")


async def _async_poll_all_tapo(ips, username, password):
    results = []
    for ip in ips:
        try:
            info = await _async_poll_single_tapo(ip, username, password)
            results.append(info)
        except Exception as e:
            print(f"[Tapo Poll {ip} Error] {e}")
    return results


async def _async_toggle_tapo(ip, turn_on, username, password):
    if HAS_TAPO:
        try:
            client = ApiClient(username, password)
            dev = await client.p110(ip)
            if turn_on:
                await dev.on()
            else:
                await dev.off()
            return
        except Exception as e:
            if not HAS_KASA:
                raise e

    if HAS_KASA:
        creds = Credentials(username, password)
        dev = await Discover.discover_single(ip, credentials=creds, timeout=4)
        if turn_on:
            await dev.turn_on()
        else:
            await dev.turn_off()
        await dev.disconnect()


def poll_tapo_once():
    if not HAS_TAPO and not HAS_KASA:
        return
    ips = get_tapo_known_ips()
    user, pw = get_tapo_credentials()
    if not user or not pw:
        # Probe UDP 20002 to confirm plugs are online on LAN even before credentials are saved
        plugs_unauth = []
        for ip in ips:
            model = "P110M(UK)"
            mac = "Unknown"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1.2)
                s.sendto(bytes.fromhex("020000010000000000000000463cb5d3"), (ip, 20002))
                data, _ = s.recvfrom(2048)
                s.close()
                info = json.loads(data[16:].decode("utf-8", "ignore")).get("result", {})
                model = info.get("device_model", "P110M(UK)")
                mac = info.get("mac", "Unknown")
            except Exception:
                pass
            plugs_unauth.append(
                {
                    "ip": ip,
                    "alias": f"Tapo Plug ({ip})",
                    "model": model,
                    "mac": mac,
                    "is_on": False,
                    "watts": 0.0,
                    "today_kwh": 0.0,
                    "month_kwh": 0.0,
                    "authenticated": False,
                }
            )
        with lock:
            latest_tapo["status"] = "needs_auth"
            latest_tapo["configured"] = False
            latest_tapo["plugs"] = plugs_unauth
        return

    try:
        plug_list = asyncio.run(_async_poll_all_tapo(ips, user, pw))
        if not plug_list:
            raise RuntimeError("No Tapo plugs responded to local KLAP poll")
        total_w = round(sum(p["watts"] for p in plug_list), 1)
        with lock:
            latest_tapo["status"] = "online"
            latest_tapo["configured"] = True
            latest_tapo["error"] = None
            latest_tapo["total_watts"] = total_w
            latest_tapo["plugs"] = plug_list
    except Exception as e:
        print(f"[Tapo Poller Error] {e}")
        with lock:
            latest_tapo["status"] = "auth_error"
            latest_tapo["configured"] = True
            latest_tapo["error"] = str(e)


def get_shelly_known_ips():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS shelly_plugs (ip TEXT PRIMARY KEY, name TEXT, model TEXT)")
        c.execute("SELECT ip, name, model FROM shelly_plugs")
        rows = [{"ip": r[0], "name": r[1] or f"Shelly Plug ({r[0]})", "model": r[2] or "Plus Plug"} for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[Shelly DB Error] {e}")
        return []


def add_shelly_known_ip(ip, name="Shelly Plug", model="Plus Plug"):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS shelly_plugs (ip TEXT PRIMARY KEY, name TEXT, model TEXT)")
        c.execute("INSERT OR REPLACE INTO shelly_plugs VALUES (?, ?, ?)", (ip, name, model))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[Shelly DB Add Error] {e}")
        return False


def poll_shelly_once():
    known = get_shelly_known_ips()
    if not known:
        with lock:
            latest_shelly["plugs"] = []
            latest_shelly["total_watts"] = 0.0
        return

    plugs_out = []
    tot_w = 0.0

    for dev in known:
        ip = dev["ip"]
        alias = dev.get("name") or f"Shelly Plug ({ip})"
        model = dev.get("model", "Plus Plug")
        is_on = False
        watts = 0.0
        today_kwh = 0.0
        reachable = False

        # Try Gen2/Gen3 RPC
        try:
            url = f"http://{ip}/rpc/Switch.GetStatus?id=0"
            req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerDashboard/2.0"})
            with urllib.request.urlopen(req, timeout=1.8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                is_on = bool(data.get("output", False))
                watts = float(data.get("apower", 0.0))
                # aenergy.total is watt-hours
                aenergy = data.get("aenergy", {})
                today_kwh = round(float(aenergy.get("total", 0.0)) / 1000.0, 3)
                reachable = True
        except Exception:
            # Fallback to Gen1 /meter/0 and /relay/0
            try:
                url_m = f"http://{ip}/meter/0"
                req_m = urllib.request.Request(url_m, headers={"User-Agent": "SmartPowerDashboard/2.0"})
                with urllib.request.urlopen(req_m, timeout=1.8) as resp_m:
                    data_m = json.loads(resp_m.read().decode("utf-8"))
                    watts = float(data_m.get("power", 0.0))
                    today_kwh = round(float(data_m.get("total", 0.0)) / 60000.0, 3)

                url_r = f"http://{ip}/relay/0"
                req_r = urllib.request.Request(url_r, headers={"User-Agent": "SmartPowerDashboard/2.0"})
                with urllib.request.urlopen(req_r, timeout=1.8) as resp_r:
                    data_r = json.loads(resp_r.read().decode("utf-8"))
                    is_on = bool(data_r.get("ison", False))
                reachable = True
            except Exception:
                pass

        if reachable:
            tot_w += max(0.0, watts)
            plugs_out.append({
                "ip": ip,
                "alias": alias,
                "model": model,
                "is_on": is_on,
                "watts": round(max(0.0, watts), 1),
                "today_kwh": today_kwh,
                "reachable": True,
            })

    with lock:
        latest_shelly["plugs"] = plugs_out
        latest_shelly["total_watts"] = round(tot_w, 1)


def toggle_shelly_plug(ip, is_on):
    # Try Gen2/Gen3 RPC first
    try:
        on_str = "true" if is_on else "false"
        url = f"http://{ip}/rpc/Switch.Set?id=0&on={on_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return True
    except Exception:
        pass

    # Try Gen1
    try:
        turn_param = "on" if is_on else "off"
        url = f"http://{ip}/relay/0?turn={turn_param}"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerDashboard/2.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return True
    except Exception as e:
        print(f"[Shelly Toggle Error {ip}] {e}")
        return False


def save_telemetry_point(point):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            INSERT OR REPLACE INTO telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                point["ts"],
                point["pv_power"],
                point["pv1_power"],
                point["pv2_power"],
                point["load_power"],
                point["grid_power"],
                point["battery_power"],
                point["battery_soc"],
                point["inverter_power"],
                point["pv1_voltage"],
                point["pv2_voltage"],
                point["grid_voltage"],
                point["battery_voltage"],
                point["battery_temp"],
                point["inverter_temp"],
            ),
        )
        if point["ts"] % 3600 < POLL_INTERVAL:
            c.execute("DELETE FROM telemetry WHERE ts < ?", (point["ts"] - 14 * 86400,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB Error] {e}")


def to_int16(val):
    return struct.unpack(">h", struct.pack(">H", val))[0]


def to_uint32(high, low):
    return (high << 16) | low


def read_regs(sock, fc, addr, count, unit=INVERTER_UNIT, tx_id=1):
    req = struct.pack(">HHHBBHH", tx_id, 0, 6, unit, fc, addr, count)
    sock.sendall(req)
    hdr = b""
    while len(hdr) < 9:
        chunk = sock.recv(9 - len(hdr))
        if not chunk:
            raise ConnectionError("Socket closed while reading Modbus header")
        hdr += chunk
    if hdr[7] & 0x80:
        err_code = hdr[8]
        raise ValueError(f"Modbus exception code {err_code} at addr {addr}")
    byte_count = hdr[8]
    data = b""
    while len(data) < byte_count:
        chunk = sock.recv(byte_count - len(data))
        if not chunk:
            raise ConnectionError("Socket closed while reading Modbus payload")
        data += chunk
    return struct.unpack(f">{count}H", data)


def poll_inverter_once(read_static=False):
    sock = socket.create_connection((INVERTER_HOST, INVERTER_PORT), timeout=4.5)
    try:
        r31000 = read_regs(sock, 4, 31000, 20, tx_id=1)
        time.sleep(0.3)

        r31020 = read_regs(sock, 4, 31020, 15, tx_id=2)
        time.sleep(0.3)

        r32000 = read_regs(sock, 4, 32000, 24, tx_id=3)
        time.sleep(0.3)

        r41000 = read_regs(sock, 3, 41000, 15, tx_id=4)

        if read_static:
            try:
                time.sleep(0.35)
                r10000 = read_regs(sock, 3, 10000, 10, tx_id=10)
                raw_sn = b"".join(struct.pack(">H", r) for r in r10000[4:10])
                sn_str = raw_sn.decode("ascii", "ignore").strip("\x00 ")
                if sn_str:
                    static_device_info["serial"] = sn_str
                static_device_info["master_ver"] = f"{r10000[1] >> 8}.{r10000[1] & 0xFF}"
                static_device_info["slave_ver"] = f"{r10000[2] >> 8}.{r10000[2] & 0xFF}"
                static_device_info["manager_ver"] = f"{r10000[3] >> 8}.{r10000[3] & 0xFF}"
            except Exception as e:
                print(f"[Static Info Warning] {e}")
    finally:
        sock.close()

    now_ts = int(time.time())
    now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    pv1_voltage = round(r31000[0] * 0.1, 1)
    pv1_current = round(r31000[1] * 0.1, 1)
    pv1_power = to_int16(r31000[2])

    pv2_voltage = round(r31000[3] * 0.1, 1)
    pv2_current = round(r31000[4] * 0.1, 1)
    pv2_power = to_int16(r31000[5])

    pv_power = pv1_power + pv2_power

    grid_voltage = round(r31000[6] * 0.1, 1)
    inverter_current = round(r31000[7] * 0.1, 1)
    inverter_power = to_int16(r31000[8])
    grid_freq = round(r31000[9] * 0.01, 2)

    eps_voltage = round(r31000[10] * 0.1, 1)
    eps_current = round(r31000[11] * 0.1, 1)
    eps_power = to_int16(r31000[12])
    eps_freq = round(r31000[13] * 0.01, 2)

    meter_raw = to_int16(r31000[14])
    grid_import_w = max(0, -meter_raw)
    grid_export_w = max(0, meter_raw)
    grid_net_w = -meter_raw

    load_power = to_int16(r31000[16])

    ambient_temp = round(to_int16(r31000[17]) * 0.1, 1)
    inverter_temp = round(to_int16(r31000[18]) * 0.1, 1)
    boost_temp = round(to_int16(r31000[19]) * 0.1, 1)

    battery_voltage = round(r31020[0] * 0.1, 1)
    battery_current = round(to_int16(r31020[1]) * 0.1, 1)
    battery_power_raw = to_int16(r31020[2])
    battery_charge_w = max(0, -battery_power_raw)
    battery_discharge_w = max(0, battery_power_raw)
    battery_temp = round(to_int16(r31020[3]) * 0.1, 1)
    battery_soc = r31020[4]

    bms_max_charge_a = round(r31020[5] * 0.1, 1)
    bms_max_discharge_a = round(r31020[6] * 0.1, 1)
    inverter_state_code = r31020[7]
    bms_connected = r31020[9]

    pv_energy_total = round(to_uint32(r32000[0], r32000[1]) * 0.1, 1)
    pv_energy_today = round(r32000[2] * 0.1, 1)

    bat_charge_total = round(to_uint32(r32000[3], r32000[4]) * 0.1, 1)
    bat_charge_today = round(r32000[5] * 0.1, 1)

    bat_discharge_total = round(to_uint32(r32000[6], r32000[7]) * 0.1, 1)
    bat_discharge_today = round(r32000[8] * 0.1, 1)

    grid_export_total = round(to_uint32(r32000[9], r32000[10]) * 0.1, 1)
    grid_export_today = round(r32000[11] * 0.1, 1)

    grid_import_total = round(to_uint32(r32000[12], r32000[13]) * 0.1, 1)
    grid_import_today = round(r32000[14] * 0.1, 1)

    load_energy_total = round(to_uint32(r32000[15], r32000[16]) * 0.1, 1)
    load_energy_today = round(r32000[17] * 0.1, 1)

    solar_yield_total = round(to_uint32(r32000[21], r32000[22]) * 0.1, 1)
    solar_yield_today = round(r32000[23] * 0.1, 1)

    work_mode_code = r41000[0]
    p1_enable = bool(r41000[1])
    p1_start_val = r41000[2]
    p1_end_val = r41000[3]
    p2_enable = bool(r41000[4])
    p2_start_val = r41000[5]
    p2_end_val = r41000[6]
    max_set_charge_a = round(r41000[7] * 0.1, 1)
    max_set_discharge_a = round(r41000[8] * 0.1, 1)
    min_soc = r41000[9]
    max_soc = r41000[10]
    min_soc_on_grid = r41000[11]
    export_limit_w = r41000[12]

    p1_start_str = f"{p1_start_val // 256:02d}:{p1_start_val % 256:02d}"
    p1_end_str = f"{p1_end_val // 256:02d}:{p1_end_val % 256:02d}"
    p2_start_str = f"{p2_start_val // 256:02d}:{p2_start_val % 256:02d}"
    p2_end_str = f"{p2_end_val // 256:02d}:{p2_end_val % 256:02d}"

    self_sufficiency_pct = 100.0
    if load_power > 0:
        grid_share = min(load_power, grid_import_w)
        self_sufficiency_pct = round(max(0.0, min(100.0, ((load_power - grid_share) / load_power) * 100.0)), 1)

    today_self_sufficiency_pct = 0.0
    if load_energy_today > 0:
        today_self_sufficiency_pct = round(
            max(0.0, min(100.0, ((load_energy_today - min(load_energy_today, grid_import_today)) / load_energy_today) * 100.0)),
            1,
        )

    # Compute exact today solar generation from telemetry since midnight
    now_dt = datetime.now()
    midnight_ts = datetime(now_dt.year, now_dt.month, now_dt.day).timestamp()
    computed_today_solar_kwh = 0.0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ts, pv_power FROM telemetry WHERE ts >= ? ORDER BY ts ASC", (midnight_ts,))
        pv_rows = c.fetchall()
        conn.close()
        for i in range(1, len(pv_rows)):
            dt_h = (pv_rows[i][0] - pv_rows[i - 1][0]) / 3600.0
            if 0 < dt_h <= 0.05:
                computed_today_solar_kwh += ((pv_rows[i][1] or 0) * dt_h) / 1000.0
    except Exception:
        pass

    # Use telemetry integration if available for sub-0.1 kWh precision; fallback to inverter registers
    actual_today_solar = round(computed_today_solar_kwh, 2) if computed_today_solar_kwh > 0 else (solar_yield_today if solar_yield_today > 0 else pv_energy_today)

    if battery_power_raw < -10:
        battery_Direction = "Charging"
    elif battery_power_raw > 10:
        battery_Direction = "Discharging"
    else:
        battery_Direction = "Idle"

    payload = {
        "status": "online",
        "last_updated": now_iso,
        "timestamp": now_ts,
        "poll_interval_sec": POLL_INTERVAL,
        "error": None,
        "device": dict(static_device_info),
        "system_state": {
            "inverter_state_code": inverter_state_code,
            "inverter_state": STATE_MAP.get(inverter_state_code, f"State ({inverter_state_code})"),
            "work_mode_code": work_mode_code,
            "work_mode": WORK_MODE_MAP.get(work_mode_code, f"Mode ({work_mode_code})"),
            "bms_connected": bool(bms_connected),
        },
        "power": {
            "pv_total_w": pv_power,
            "pv1_w": pv1_power,
            "pv2_w": pv2_power,
            "load_w": load_power,
            "inverter_w": inverter_power,
            "eps_w": eps_power,
            "grid_net_w": grid_net_w,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
            "battery_raw_w": battery_power_raw,
            "battery_charge_w": battery_charge_w,
            "battery_discharge_w": battery_discharge_w,
            "battery_direction": battery_Direction,
            "self_sufficiency_pct": self_sufficiency_pct,
        },
        "pv": {
            "pv1_voltage_v": pv1_voltage,
            "pv1_current_a": pv1_current,
            "pv1_power_w": pv1_power,
            "pv2_voltage_v": pv2_voltage,
            "pv2_current_a": pv2_current,
            "pv2_power_w": pv2_power,
            "total_power_w": pv_power,
        },
        "battery": {
            "soc_pct": battery_soc,
            "voltage_v": battery_voltage,
            "current_a": battery_current,
            "power_w": battery_power_raw,
            "charge_w": battery_charge_w,
            "discharge_w": battery_discharge_w,
            "direction": battery_Direction,
            "temp_c": battery_temp,
            "bms_max_charge_a": bms_max_charge_a,
            "bms_max_discharge_a": bms_max_discharge_a,
            "min_soc_pct": min_soc,
            "min_soc_on_grid_pct": min_soc_on_grid,
            "max_soc_pct": max_soc,
        },
        "grid": {
            "voltage_v": grid_voltage,
            "current_a": inverter_current,
            "freq_hz": grid_freq,
            "net_power_w": grid_net_w,
            "import_w": grid_import_w,
            "export_w": grid_export_w,
            "export_limit_w": export_limit_w,
            "eps_voltage_v": eps_voltage,
            "eps_current_a": eps_current,
            "eps_power_w": eps_power,
            "eps_freq_hz": eps_freq,
        },
        "temperatures": {
            "inverter_c": inverter_temp,
            "boost_c": boost_temp,
            "battery_c": battery_temp,
            "ambient_c": ambient_temp,
        },
        "energy": {
            "today": {
                "solar_pv_kwh": actual_today_solar,
                "inverter_out_kwh": pv_energy_today,
                "battery_charge_kwh": bat_charge_today,
                "battery_discharge_kwh": bat_discharge_today,
                "grid_import_kwh": grid_import_today,
                "grid_export_kwh": grid_export_today,
                "load_kwh": load_energy_today,
                "self_sufficiency_pct": today_self_sufficiency_pct,
            },
            "total": {
                "solar_pv_kwh": solar_yield_total if solar_yield_total > 0 else pv_energy_total,
                "inverter_out_kwh": pv_energy_total,
                "battery_charge_kwh": bat_charge_total,
                "battery_discharge_kwh": bat_discharge_total,
                "grid_import_kwh": grid_import_total,
                "grid_export_kwh": grid_export_total,
                "load_kwh": load_energy_total,
            },
        },
        "settings": {
            "work_mode_code": work_mode_code,
            "work_mode": WORK_MODE_MAP.get(work_mode_code, f"Mode ({work_mode_code})"),
            "min_soc_pct": min_soc,
            "min_soc_on_grid_pct": min_soc_on_grid,
            "max_soc_pct": max_soc,
            "max_charge_current_a": max_set_charge_a,
            "max_discharge_current_a": max_set_discharge_a,
            "export_limit_w": export_limit_w,
            "grid_charge_schedule": {
                "period1": {
                    "enabled": p1_enable,
                    "start": p1_start_str,
                    "end": p1_end_str,
                },
                "period2": {
                    "enabled": p2_enable,
                    "start": p2_start_str,
                    "end": p2_end_str,
                },
                "max_soc_target_pct": max_soc,
                "current_limit_a": max_set_charge_a,
                "aligned_with_octopus": (p1_enable and p1_start_str == "00:30" and p1_end_str == "05:30"),
                "adaptive": dict(latest_adaptive),
            },
        },
    }

    hist_point = {
        "ts": now_ts,
        "iso": now_iso,
        "pv_power": pv_power,
        "pv1_power": pv1_power,
        "pv2_power": pv2_power,
        "load_power": load_power,
        "grid_power": grid_net_w,
        "battery_power": battery_power_raw,
        "battery_soc": battery_soc,
        "inverter_power": inverter_power,
        "pv1_voltage": pv1_voltage,
        "pv2_voltage": pv2_voltage,
        "grid_voltage": grid_voltage,
        "battery_voltage": battery_voltage,
        "battery_temp": battery_temp,
        "inverter_temp": inverter_temp,
    }

    with lock:
        latest_data.clear()
        latest_data.update(payload)
        recent_history.append(hist_point)

    save_telemetry_point(hist_point)


def poller_loop():
    cycle = 0
    while True:
        start = time.time()
        try:
            poll_inverter_once(read_static=(cycle % 360 == 0))
            cycle += 1
        except Exception as e:
            print(f"[Poller Error] {e}")
            with lock:
                latest_data["status"] = "degraded" if latest_data.get("last_updated") else "error"
                latest_data["error"] = str(e)

        try:
            poll_hue_once()
        except Exception as e:
            print(f"[Hue Poller Error] {e}")
            with lock:
                latest_hue["status"] = "error"
                latest_hue["error"] = str(e)

        try:
            if cycle % 30 == 1:
                scan_tapo_subnet()
            if cycle % 180 == 1:
                poll_octopus_rates()
                sync_foxess_octopus_schedule()
            if cycle % 360 == 1:
                poll_weather_forecast()
            # Daily aggregation and 14-day pruning maintenance (runs every ~10 mins / 60 cycles)
            if cycle % 60 == 1:
                rollup_daily_summary()
            if cycle % 8640 == 1:
                prune_old_telemetry(retention_days=14)
            poll_tapo_once()
            poll_shelly_once()
            evaluate_tapo_automation()
            # Enforce automatic battery cutoff at target SoC during 00:30-05:30 off-peak
            enforce_inverter_night_charge_cutoff()
        except Exception as e:
            print(f"[Maintenance Tasks Error] {e}")

        elapsed = time.time() - start
        sleep_for = max(1.0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_for)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/setup", "/setup.html", "/wizard", "/wizard.html"):
            self.path = "/wizard.html"
            return super().do_GET()

        if parsed.path == "/api/setup/status":
            self.send_json({
                "configured": app_config.get("configured", False),
                "config": app_config,
                "dno_regions": DNO_REGIONS,
                "tariffs": TARIFF_TEMPLATES,
                "batteries": BATTERY_PRESETS,
            })
            return

        if parsed.path == "/api/setup/discover":
            results = scan_network_for_devices()
            self.send_json(results)
            return

        if parsed.path == "/api/live":
            ai_data = generate_energy_insights()
            with lock:
                data_copy = dict(latest_data)
                octo_copy = dict(latest_octopus)
                weather_copy = dict(latest_weather)
            data_copy["octopus"] = octo_copy
            data_copy["weather"] = weather_copy
            data_copy["financials"] = calculate_daily_financials()
            data_copy["ai_insight"] = ai_data
            self.send_json(data_copy)
            return

        if parsed.path == "/api/devices":
            with lock:
                hue_copy = dict(latest_hue)
                tapo_copy = dict(latest_tapo)
                shelly_copy = dict(latest_shelly)
                plugs_copy = [dict(p) for p in tapo_copy.get("plugs", [])]
                shelly_plugs = [dict(p) for p in shelly_copy.get("plugs", [])]
                load_w = latest_data.get("power", {}).get("load_w", 0)
                pv_w = latest_data.get("power", {}).get("pv_total_w", 0)
            for p in plugs_copy:
                pip = p.get("ip", TAPO_DEFAULT_IP)
                p["automation"] = {
                    "rules": get_tapo_automation_rules(pip),
                    "runtime": dict(get_tapo_runtime(pip)),
                }
            tapo_copy["plugs"] = plugs_copy
            appliances = get_custom_appliances()
            appliances_w = round(sum(a["active_watts"] for a in appliances), 1)
            hue_w = hue_copy.get("total_watts", 0.0)
            tapo_w = tapo_copy.get("total_watts", 0.0)
            shelly_w = shelly_copy.get("total_watts", 0.0)
            tracked_w = round(hue_w + tapo_w + shelly_w + appliances_w, 1)
            untracked_w = round(max(0.0, load_w - tracked_w), 1)

            self.send_json(
                {
                    "house_load_w": load_w,
                    "solar_pv_w": pv_w,
                    "tracked_w": tracked_w,
                    "untracked_w": untracked_w,
                    "hue_w": hue_w,
                    "tapo_w": tapo_w,
                    "shelly_w": shelly_w,
                    "appliances_w": appliances_w,
                    "hue": hue_copy,
                    "tapo": tapo_copy,
                    "shelly": shelly_copy,
                    "appliances": appliances,
                }
            )
            return

        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            hours = float(qs.get("hours", ["6"])[0])
            # Query directly from SQLite so any requested window (1h, 6h, 24h, 48h) is fully accurate
            points = get_telemetry_history(hours=hours)
            self.send_json({"points": points})
            return

        if parsed.path == "/api/history/daily":
            qs = parse_qs(parsed.query)
            days = int(qs.get("days", ["30"])[0])
            history = get_daily_history(days=days)
            self.send_json({"daily": history})
            return

        if parsed.path == "/api/ai/insights":
            res = generate_energy_insights()
            self.send_json(res)
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw_body)

        if parsed.path == "/api/setup/save":
            global app_config, INVERTER_HOST, INVERTER_PORT, INVERTER_UNIT, BATTERY_CAPACITY_KWH
            if not isinstance(body, dict):
                self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
                return
            body["configured"] = True
            app_config.update(body)
            save_config(app_config)

            # Update live runtime parameters immediately
            if "inverter" in body:
                INVERTER_HOST = body["inverter"].get("host", INVERTER_HOST)
                INVERTER_PORT = int(body["inverter"].get("port", INVERTER_PORT))
                INVERTER_UNIT = int(body["inverter"].get("unit_id", INVERTER_UNIT))
            if "battery" in body:
                BATTERY_CAPACITY_KWH = float(body["battery"].get("gross_capacity_kwh", BATTERY_CAPACITY_KWH))

            # Kick off async rates sync with new region/tariffs
            threading.Thread(target=poll_octopus_rates, daemon=True).start()

            self.send_json({"ok": True, "config": app_config})
            return

        if parsed.path == "/api/ai/generate":
            res = generate_energy_insights()
            self.send_json({"ok": True, "insight": res})
            return

        if parsed.path == "/api/hue/light":
            lid = str(body.get("id"))
            state = {}
            if "on" in body:
                state["on"] = bool(body["on"])
            if "bri" in body:
                state["bri"] = max(1, min(254, int(body["bri"])))
                state["on"] = True
            set_hue_light_state(lid, state)
            time.sleep(0.15)
            poll_hue_once()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/hue/room":
            gid = str(body.get("id"))
            action = {"on": bool(body.get("on", False))}
            set_hue_group_action(gid, action)
            time.sleep(0.2)
            poll_hue_once()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/appliances/toggle":
            app_id = int(body.get("id"))
            is_on = 1 if body.get("is_on") else 0
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE custom_appliances SET is_on = ? WHERE id = ?", (is_on, app_id))
            conn.commit()
            conn.close()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/appliances/add":
            name = str(body.get("name", "Device")).strip()
            room = str(body.get("room", "General")).strip()
            watts = float(body.get("watts", 10.0))
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO custom_appliances (name, room, watts, is_on, icon) VALUES (?, ?, ?, 1, 'plug')",
                (name, room, watts),
            )
            conn.commit()
            conn.close()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/appliances/delete":
            app_id = int(body.get("id"))
            conn = sqlite3.connect(DB_PATH)
            conn.execute("DELETE FROM custom_appliances WHERE id = ?", (app_id,))
            conn.commit()
            conn.close()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/inverter/work_mode":
            mode = int(body.get("mode", 0))
            ok = set_foxess_work_mode(mode)
            time.sleep(0.3)
            poll_inverter_once(read_static=False)
            self.send_json({"ok": ok, "mode": mode, "label": WORK_MODE_MAP.get(mode, "Unknown")})
            return

        if parsed.path == "/api/inverter/schedule":
            enabled = bool(body.get("enabled", True))
            start_str = str(body.get("start", "00:30"))
            end_str = str(body.get("end", "05:30"))
            p2_enabled = bool(body.get("p2_enabled", False))
            p2_start_str = str(body.get("p2_start", "00:00"))
            p2_end_str = str(body.get("p2_end", "00:00"))
            try:
                sh, sm = [int(x) for x in start_str.split(":")]
                eh, em = [int(x) for x in end_str.split(":")]
            except Exception:
                sh, sm, eh, em = 0, 30, 5, 30
            try:
                p2_sh, p2_sm = [int(x) for x in p2_start_str.split(":")]
                p2_eh, p2_em = [int(x) for x in p2_end_str.split(":")]
            except Exception:
                p2_sh, p2_sm, p2_eh, p2_em = 0, 0, 0, 0
            ok = set_foxess_force_charge_schedule(enabled, sh, sm, eh, em, p2_enabled, p2_sh, p2_sm, p2_eh, p2_em)
            time.sleep(0.3)
            poll_inverter_once(read_static=False)
            self.send_json({
                "ok": ok,
                "enabled": enabled,
                "window": f"{sh:02d}:{sm:02d} - {eh:02d}:{em:02d}",
                "p2_enabled": p2_enabled,
                "p2_window": f"{p2_sh:02d}:{p2_sm:02d} - {p2_eh:02d}:{p2_em:02d}"
            })
            return

        if parsed.path == "/api/inverter/battery_limits":
            min_soc = int(body.get("min_soc", 10))
            max_soc = int(body.get("max_soc", 100))
            max_chg_a = float(body.get("max_charge_a", 40.0))
            max_dis_a = float(body.get("max_discharge_a", 40.0))
            ok = set_foxess_battery_reserves(min_soc, max_soc, min_soc, max_chg_a, max_dis_a)
            time.sleep(0.3)
            poll_inverter_once(read_static=False)
            self.send_json({"ok": ok, "min_soc": min_soc, "max_soc": max_soc, "max_charge_a": max_chg_a, "max_discharge_a": max_dis_a})
            return

        if parsed.path == "/api/inverter/adaptive_headroom":
            global ADAPTIVE_HEADROOM_ENABLED, ADAPTIVE_BUFFER_PCT, ADAPTIVE_TOPUP_ENABLED, ADAPTIVE_TOPUP_DROP_PCT
            headroom_en = bool(body.get("headroom_enabled", True))
            buffer_pct = int(body.get("buffer_pct", 10))
            topup_en = bool(body.get("topup_enabled", True))
            topup_drop = int(body.get("topup_drop_pct", 3))

            ADAPTIVE_HEADROOM_ENABLED = headroom_en
            ADAPTIVE_BUFFER_PCT = max(0, min(30, buffer_pct))
            ADAPTIVE_TOPUP_ENABLED = topup_en
            ADAPTIVE_TOPUP_DROP_PCT = topup_drop

            # Persist to config.json
            app_config.setdefault("battery", {})["adaptive_headroom_enabled"] = headroom_en
            app_config["battery"]["adaptive_buffer_pct"] = ADAPTIVE_BUFFER_PCT
            app_config["battery"]["adaptive_topup_enabled"] = topup_en
            app_config["battery"]["adaptive_topup_drop_pct"] = topup_drop
            save_config(app_config)

            # Re-evaluate adaptive values immediately
            sync_foxess_octopus_schedule()
            self.send_json({
                "ok": True,
                "headroom_enabled": ADAPTIVE_HEADROOM_ENABLED,
                "buffer_pct": ADAPTIVE_BUFFER_PCT,
                "topup_enabled": ADAPTIVE_TOPUP_ENABLED,
                "topup_drop_pct": ADAPTIVE_TOPUP_DROP_PCT
            })
            return

        if parsed.path == "/api/shelly/toggle":
            ip = str(body.get("ip", "")).strip()
            is_on = bool(body.get("is_on", False))
            if not ip:
                self.send_json({"ok": False, "error": "IP required"}, status=400)
                return
            ok = toggle_shelly_plug(ip, is_on)
            time.sleep(0.15)
            poll_shelly_once()
            self.send_json({"ok": ok})
            return

        if parsed.path == "/api/shelly/add_plug":
            ip = str(body.get("ip", "")).strip()
            name = str(body.get("name", "Shelly Plug")).strip()
            model = str(body.get("model", "Plus Plug")).strip()
            if not ip:
                self.send_json({"ok": False, "error": "IP required"}, status=400)
                return
            add_shelly_known_ip(ip, name, model)
            poll_shelly_once()
            self.send_json({"ok": True})
            return

        if parsed.path == "/api/tapo/auth":
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", "")).strip()
            if not username or not password:
                self.send_json({"ok": False, "error": "Email and password required"}, status=400)
                return
            save_tapo_credentials(username, password)
            poll_tapo_once()
            with lock:
                tp = dict(latest_tapo)
            self.send_json({"ok": tp["status"] == "online", "tapo": tp})
            return

        if parsed.path == "/api/tapo/toggle":
            ip = str(body.get("ip", TAPO_DEFAULT_IP))
            is_on = bool(body.get("is_on", False))
            user, pw = get_tapo_credentials()
            if user and pw:
                try:
                    asyncio.run(_async_toggle_tapo(ip, is_on, user, pw))
                    with lock:
                        rt = get_tapo_runtime(ip)
                        rt["idle_since_ts"] = None
                        rt["idle_elapsed_sec"] = 0
                        rt["last_action"] = f"Manual {'ON' if is_on else 'OFF'} via dashboard"
                        rt["last_action_ts"] = datetime.now().strftime("%H:%M:%S")
                    time.sleep(0.2)
                    poll_tapo_once()
                    self.send_json({"ok": True})
                    return
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, status=500)
                    return
            self.send_json({"ok": False, "error": "Tapo credentials not configured"}, status=400)
            return

        if parsed.path == "/api/tapo/automation":
            ip = str(body.get("ip", TAPO_DEFAULT_IP)).strip()
            updated = save_tapo_automation_rules(ip, body)
            self.send_json({"ok": True, "ip": ip, "rules": updated})
            return

        if parsed.path == "/api/tapo/add_plug":
            ip = str(body.get("ip", "")).strip()
            if not ip:
                self.send_json({"ok": False, "error": "IP address required"}, status=400)
                return
            ips = add_tapo_known_ip(ip)
            poll_tapo_once()
            self.send_json({"ok": True, "ips": ips})
            return

        self.send_json({"error": "Not found"}, status=404)


if __name__ == "__main__":
    print(f"Starting FoxESS + Hue Dashboard poller -> Inverter {INVERTER_HOST}:{INVERTER_PORT}, Hue {HUE_HOST} (every {POLL_INTERVAL}s)")
    init_db()
    try:
        poll_octopus_rates()
        sync_foxess_octopus_schedule()
        poll_weather_forecast()
        rollup_daily_summary()
    except Exception as e:
        print(f"[Init Sync Error] {e}")
    t = threading.Thread(target=poller_loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), DashboardHandler)
    print(f"Dashboard HTTP server listening on 0.0.0.0:{HTTP_PORT}")
    server.serve_forever()
