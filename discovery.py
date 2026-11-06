import concurrent.futures
import json
import os
import re
import socket
import struct
import urllib.request

CONFIG_FILE_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

DEFAULT_CONFIG = {
    "configured": False,
    "system_name": "Smart Power Station",
    "inverter": {
        "brand": "foxess",
        "model": "H1-3.7-E-G2-WL",
        "host": "192.168.1.100",
        "port": 502,
        "unit_id": 247,
        "rated_power_w": 3700,
    },
    "battery": {
        "gross_capacity_kwh": 9.84,
        "usable_capacity_kwh": 8.86,
        "min_reserve_soc_pct": 10,
        "max_charge_current_a": 40.0,
        "max_discharge_current_a": 40.0,
    },
    "tariff": {
        "provider": "octopus",
        "region": "H",
        "region_name": "Southern England",
        "product_code": "GO-FIX-12M-26-10-14",
        "import_tariff_code": "E-1R-GO-FIX-12M-26-10-14-H",
        "off_peak_rate_p_kwh": 9.40,
        "peak_rate_p_kwh": 36.56,
        "off_peak_start": "00:30",
        "off_peak_end": "05:30",
        "export_mode": "agile",
        "fixed_export_rate_p_kwh": 15.0,
        "agile_product_code": "AGILE-OUTGOING-19-05-13",
        "agile_tariff_code": "E-1R-AGILE-OUTGOING-19-05-13-H",
        "api_key": "",
        "account_number": "",
    },
    "location": {
        "latitude": 51.5074,
        "longitude": -0.1278,
        "city": "Reading",
    },
    "devices": {
        "hue": {
            "enabled": True,
            "host": "192.168.1.150",
            "api_key": "",
        },
        "tapo": {
            "enabled": True,
            "known_ips": ["192.168.1.200"],
        },
    },
}

DNO_REGIONS = [
    {"code": "A", "name": "Eastern England (UK Power Networks)"},
    {"code": "B", "name": "East Midlands (National Grid)"},
    {"code": "C", "name": "London (UK Power Networks)"},
    {"code": "D", "name": "Merseyside and Northern Wales (SP Energy Networks)"},
    {"code": "E", "name": "West Midlands (National Grid)"},
    {"code": "F", "name": "North Eastern England (Northern Powergrid)"},
    {"code": "G", "name": "North Western England (Electricity North West)"},
    {"code": "H", "name": "Southern England (Scottish and Southern Electricity)"},
    {"code": "J", "name": "South Eastern England (UK Power Networks)"},
    {"code": "K", "name": "South Wales (National Grid)"},
    {"code": "L", "name": "South Western England (National Grid)"},
    {"code": "M", "name": "Yorkshire (Northern Powergrid)"},
    {"code": "N", "name": "Southern Scotland (SP Energy Networks)"},
    {"code": "P", "name": "Northern Scotland (Scottish and Southern Electricity)"},
]

TARIFF_TEMPLATES = [
    {
        "id": "octopus_go",
        "name": "Octopus Go (Night Off-Peak)",
        "product_code": "GO-FIX-12M-26-10-14",
        "import_template": "E-1R-GO-FIX-12M-26-10-14-{region}",
        "off_peak_rate": 9.40,
        "peak_rate": 36.56,
        "off_peak_start": "00:30",
        "off_peak_end": "05:30",
        "default_export": "agile",
        "description": "5 hours cheap night rate (9.4p) for battery and EV charging.",
    },
    {
        "id": "octopus_intelligent_go",
        "name": "Intelligent Octopus Go",
        "product_code": "INTELLIGENT-OCTOPUS-GO-10-23",
        "import_template": "E-1R-INTELLIGENT-OCTOPUS-GO-10-23-{region}",
        "off_peak_rate": 7.00,
        "peak_rate": 28.50,
        "off_peak_start": "23:30",
        "off_peak_end": "05:30",
        "default_export": "fixed",
        "description": "6 hours ultra-low night rate (7.0p) + dynamic smart charging slots.",
    },
    {
        "id": "octopus_agile_import",
        "name": "Octopus Agile (Dynamic Half-Hourly)",
        "product_code": "AGILE-24-04-03",
        "import_template": "E-1R-AGILE-24-04-03-{region}",
        "off_peak_rate": 15.0,
        "peak_rate": 35.0,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "agile",
        "description": "Wholesale half-hourly import price tracking with plunges & negative pricing.",
    },
    {
        "id": "octopus_tracker",
        "name": "Octopus Tracker (Daily Market Rate)",
        "product_code": "SILVER-24-04-03",
        "import_template": "E-1R-SILVER-24-04-03-{region}",
        "off_peak_rate": 22.50,
        "peak_rate": 22.50,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Daily wholesale spot rate index (single daily flat rate).",
    },
    {
        "id": "custom_fixed",
        "name": "Standard / Custom Flat Rate",
        "product_code": "CUSTOM",
        "import_template": "CUSTOM",
        "off_peak_rate": 26.00,
        "peak_rate": 26.00,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Standard flat day rate (e.g. Ofgem Price Cap baseline ~26p/kWh).",
    },
]

BATTERY_PRESETS = [
    {
        "id": "foxess_mira_984",
        "name": "FoxESS Mira HV25 (9.84 kWh gross · 8.86 kWh usable)",
        "gross_kwh": 9.84,
        "usable_kwh": 8.86,
        "min_soc": 10,
        "max_a": 40.0,
    },
    {
        "id": "foxess_ecs_576",
        "name": "FoxESS ECS2900 2-Stack (5.76 kWh gross · 5.18 kWh usable)",
        "gross_kwh": 5.76,
        "usable_kwh": 5.18,
        "min_soc": 10,
        "max_a": 40.0,
    },
    {
        "id": "foxess_ep5_50",
        "name": "FoxESS EP5 / All-In-One (5.0 kWh gross · 4.5 kWh usable)",
        "gross_kwh": 5.0,
        "usable_kwh": 4.5,
        "min_soc": 10,
        "max_a": 35.0,
    },
    {
        "id": "givenergy_95",
        "name": "GivEnergy Gen 2 (9.5 kWh gross · 8.5 kWh usable)",
        "gross_kwh": 9.5,
        "usable_kwh": 8.5,
        "min_soc": 10,
        "max_a": 32.0,
    },
    {
        "id": "custom_pack",
        "name": "Custom / Other Battery System",
        "gross_kwh": 10.0,
        "usable_kwh": 9.0,
        "min_soc": 10,
        "max_a": 40.0,
    },
]


def load_config():
    """Load configuration from JSON or initialize with defaults."""
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg
        except Exception as e:
            print(f"[Config Load Error] {e}")

    # Fallback to defaults or environment variable overrides
    cfg = dict(DEFAULT_CONFIG)
    # If environment variables were explicitly injected (legacy docker run), mark as configured
    if os.environ.get("INVERTER_HOST"):
        cfg["inverter"]["host"] = os.environ.get("INVERTER_HOST")
        cfg["configured"] = True
    return cfg


def save_config(cfg_data):
    """Save configuration to JSON file."""
    os.makedirs(os.path.dirname(os.path.abspath(CONFIG_FILE_PATH)), exist_ok=True)
    with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=2)
    return True


def get_local_subnet():
    """Detect local IP and calculate common /24 prefix."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "192.168.86.1"
    finally:
        s.close()

    parts = local_ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}"
    return "192.168.1"


def probe_port(ip, port, timeout=0.35):
    """Attempt a rapid TCP connect to an IP and port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


def test_modbus_foxess(ip, port=502, unit_id=247, timeout=1.2):
    """Perform a live FoxESS Modbus RTU-over-TCP handshake probe."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        # Read Holding Register 31000 (1 register: Inverter State)
        # TransID(2) + Proto(2) + Len(2) + Unit(1) + Func(1) + Reg(2) + Count(2)
        req = struct.pack(">HHHBBHH", 1, 0, 6, unit_id, 3, 31000, 1)
        s.sendall(req)
        resp = s.recv(64)
        s.close()
        if len(resp) >= 9 and resp[7] == 3:
            return True
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return False


def test_hue_bridge(ip, timeout=1.0):
    """Test if IP is a Philips Hue Bridge via UPnP description."""
    try:
        url = f"http://{ip}/description.xml"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerSetup/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            if "Philips hue bridge" in text or "IpBridge" in text:
                return True
    except Exception:
        pass
    return False


def scan_network_for_devices(custom_subnets=None):
    """Scan local network subnets for FoxESS inverters (Port 502), Hue Bridges (Port 80), and Tapo plugs."""
    detected = {
        "inverters": [],
        "hue_bridges": [],
        "tapo_plugs": [],
    }

    subnets = custom_subnets or []
    detected_sub = get_local_subnet()
    if detected_sub not in subnets:
        subnets.append(detected_sub)
    # Common secondary subnet (e.g. 192.168.0.x or 192.168.86.x)
    if "192.168.0" not in subnets:
        subnets.append("192.168.0")
    if "192.168.1" not in subnets:
        subnets.append("192.168.1")

    # High-probability IP scans
    target_ips = []
    # Check specific host addresses across subnets (e.g. .57, .103, .182, .231, and common static 2-50)
    priority_hosts = [57, 103, 182, 231, 2, 10, 20, 50, 100, 101, 102, 104, 150, 200, 254]
    for sub in subnets:
        for host in priority_hosts:
            target_ips.append(f"{sub}.{host}")

    # Remove duplicates
    target_ips = list(dict.fromkeys(target_ips))

    # Probe port 502 (Modbus)
    def probe_modbus_job(ip):
        if probe_port(ip, 502, timeout=0.4):
            is_fox = test_modbus_foxess(ip, 502, 247)
            return ("inverter", ip, is_fox)
        return None

    # Probe port 80 (Hue / Tapo)
    def probe_http_job(ip):
        if probe_port(ip, 80, timeout=0.35):
            if test_hue_bridge(ip):
                return ("hue", ip, True)
            # Check Tapo KLAP port 80
            return ("http", ip, True)
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=35) as executor:
        modbus_futs = [executor.submit(probe_modbus_job, ip) for ip in target_ips]
        http_futs = [executor.submit(probe_http_job, ip) for ip in target_ips]

        for f in concurrent.futures.as_completed(modbus_futs):
            res = f.result()
            if res and res[0] == "inverter":
                _, ip, is_fox = res
                detected["inverters"].append({
                    "ip": ip,
                    "port": 502,
                    "brand": "foxess" if is_fox else "generic_modbus",
                    "label": f"FoxESS Inverter ({ip})" if is_fox else f"Modbus Inverter ({ip})",
                    "confirmed": is_fox,
                })

        for f in concurrent.futures.as_completed(http_futs):
            res = f.result()
            if res:
                kind, ip, ok = res
                if kind == "hue":
                    detected["hue_bridges"].append({
                        "ip": ip,
                        "label": f"Philips Hue Bridge ({ip})",
                    })

    return detected
