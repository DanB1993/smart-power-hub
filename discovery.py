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
    # Octopus Energy
    {
        "id": "octopus_go",
        "supplier": "Octopus Energy",
        "name": "Octopus Go (9.4p Night · 00:30-05:30)",
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
        "supplier": "Octopus Energy",
        "name": "Intelligent Octopus Go (7.0p Night · 23:30-05:30)",
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
        "id": "octopus_cosy",
        "supplier": "Octopus Energy",
        "name": "Octopus Cosy (Heat Pump / Battery 3-Window)",
        "product_code": "COSY-22-12-08",
        "import_template": "E-1R-COSY-22-12-08-{region}",
        "off_peak_rate": 13.50,
        "peak_rate": 34.00,
        "off_peak_start": "04:00",
        "off_peak_end": "07:00",
        "default_export": "agile",
        "description": "Dual off-peak slots (04:00-07:00 & 13:00-16:00 @ ~13.5p) + peak 16:00-19:00.",
    },
    {
        "id": "octopus_agile_import",
        "supplier": "Octopus Energy",
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
        "supplier": "Octopus Energy",
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

    # British Gas
    {
        "id": "british_gas_electric_driver",
        "supplier": "British Gas",
        "name": "British Gas Electric Driver (7.9p Night · 00:00-05:00)",
        "product_code": "BG-ELECTRIC-DRIVER",
        "import_template": "BG-ED-{region}",
        "off_peak_rate": 7.90,
        "peak_rate": 30.20,
        "off_peak_start": "00:00",
        "off_peak_end": "05:00",
        "default_export": "fixed",
        "description": "5 hours cheap night electricity (7.9p) for EV & home storage batteries.",
    },
    {
        "id": "british_gas_fixed",
        "supplier": "British Gas",
        "name": "British Gas Standard / Fixed Rate",
        "product_code": "BG-FIXED-STANDARD",
        "import_template": "BG-FIX-{region}",
        "off_peak_rate": 25.50,
        "peak_rate": 25.50,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Standard variable / fixed single tariff (~25.5p/kWh).",
    },

    # E.ON Next
    {
        "id": "eon_next_drive",
        "supplier": "E.ON Next",
        "name": "E.ON Next Drive (6.9p Night · 00:00-07:00)",
        "product_code": "EON-NEXT-DRIVE",
        "import_template": "EON-ND-{region}",
        "off_peak_rate": 6.90,
        "peak_rate": 31.40,
        "off_peak_start": "00:00",
        "off_peak_end": "07:00",
        "default_export": "fixed",
        "description": "7 hours generous night charging (6.9p/kWh) between midnight and 07:00.",
    },
    {
        "id": "eon_next_pledge",
        "supplier": "E.ON Next",
        "name": "E.ON Next Pledge (Tracker vs Cap)",
        "product_code": "EON-NEXT-PLEDGE",
        "import_template": "EON-NP-{region}",
        "off_peak_rate": 24.50,
        "peak_rate": 24.50,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Guaranteed tracking below the Ofgem price cap baseline.",
    },

    # OVO Energy
    {
        "id": "ovo_charge_anytime",
        "supplier": "OVO Energy",
        "name": "OVO Charge Anytime (7.0p Add-on / Drive)",
        "product_code": "OVO-CHARGE-ANYTIME",
        "import_template": "OVO-CA-{region}",
        "off_peak_rate": 7.00,
        "peak_rate": 27.80,
        "off_peak_start": "00:00",
        "off_peak_end": "05:00",
        "default_export": "fixed",
        "description": "7.0p flat charging window / smart EV add-on credit.",
    },
    {
        "id": "ovo_economy_7",
        "supplier": "OVO Energy",
        "name": "OVO Economy 7 Dual Rate",
        "product_code": "OVO-ECONOMY-7",
        "import_template": "OVO-E7-{region}",
        "off_peak_rate": 14.80,
        "peak_rate": 33.50,
        "off_peak_start": "00:30",
        "off_peak_end": "07:30",
        "default_export": "fixed",
        "description": "Traditional 7-hour overnight heating & battery charging window.",
    },

    # EDF Energy
    {
        "id": "edf_go_electric",
        "supplier": "EDF Energy",
        "name": "EDF GoElectric Overnight (8.0p Night · 00:00-05:00)",
        "product_code": "EDF-GO-ELECTRIC-OVERNIGHT",
        "import_template": "EDF-GEO-{region}",
        "off_peak_rate": 8.00,
        "peak_rate": 31.00,
        "off_peak_start": "00:00",
        "off_peak_end": "05:00",
        "default_export": "fixed",
        "description": "5 hours cheap overnight energy (8.0p/kWh) backed by zero-carbon nuclear.",
    },

    # Scottish Power
    {
        "id": "scottish_power_ev",
        "supplier": "Scottish Power",
        "name": "Scottish Power EV Saver (7.45p Night · 00:00-05:00)",
        "product_code": "SP-EV-SAVER",
        "import_template": "SP-EVS-{region}",
        "off_peak_rate": 7.45,
        "peak_rate": 29.80,
        "off_peak_start": "00:00",
        "off_peak_end": "05:00",
        "default_export": "fixed",
        "description": "5 hours low overnight rate (7.45p) for EVs and storage batteries.",
    },

    # Standard / Custom
    {
        "id": "ofgem_price_cap",
        "supplier": "Ofgem Price Cap",
        "name": "Ofgem UK Price Cap Standard (~24.5p / kWh)",
        "product_code": "OFGEM-PRICE-CAP",
        "import_template": "CAP-{region}",
        "off_peak_rate": 24.50,
        "peak_rate": 24.50,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Official UK regulatory price cap standard flat unit rate.",
    },
    {
        "id": "custom_dual",
        "supplier": "Custom / Other",
        "name": "Custom Dual Rate (Day / Night)",
        "product_code": "CUSTOM-DUAL",
        "import_template": "CUSTOM-DUAL",
        "off_peak_rate": 10.00,
        "peak_rate": 30.00,
        "off_peak_start": "00:30",
        "off_peak_end": "05:30",
        "default_export": "fixed",
        "description": "Enter any custom supplier day and night rate with exact hours.",
    },
    {
        "id": "custom_fixed",
        "supplier": "Custom / Other",
        "name": "Custom Flat Rate (Single Rate)",
        "product_code": "CUSTOM-FLAT",
        "import_template": "CUSTOM-FLAT",
        "off_peak_rate": 26.00,
        "peak_rate": 26.00,
        "off_peak_start": "00:00",
        "off_peak_end": "00:00",
        "default_export": "fixed",
        "description": "Single all-day electricity rate without off-peak differential.",
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


def test_shelly_plug(ip, timeout=0.8):
    """Test if an IP is a local Shelly smart plug (Gen1 or Gen2/Gen3 Plus/Pro)."""
    # Try Gen2/Gen3 RPC status endpoint first (/rpc/Shelly.GetDeviceInfo)
    try:
        url = f"http://{ip}/rpc/Shelly.GetDeviceInfo"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerDiscovery/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            app_id = data.get("app", "")
            model = data.get("model", "")
            name = data.get("name") or data.get("id") or "Shelly Device"
            if "PlusPlug" in model or "Plug" in model or "Plug" in app_id or "shelly" in data.get("id", "").lower():
                return {"type": "gen2", "model": model, "name": name}
    except Exception:
        pass

    # Try Gen1 status endpoint (/shelly)
    try:
        url = f"http://{ip}/shelly"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartPowerDiscovery/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            model = data.get("type", "")
            if "SHPLG" in model or "shelly" in model.lower():
                return {"type": "gen1", "model": model, "name": f"Shelly Plug ({ip})"}
    except Exception:
        pass

    return None


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
    """Scan local network subnets for FoxESS inverters (Port 502), Hue Bridges (Port 80), Shelly plugs, and Tapo plugs."""
    detected = {
        "inverters": [],
        "hue_bridges": [],
        "shelly_plugs": [],
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

    # Probe port 80 (Hue / Shelly / Tapo)
    def probe_http_job(ip):
        if probe_port(ip, 80, timeout=0.35):
            if test_hue_bridge(ip):
                return ("hue", ip, True)
            shelly_info = test_shelly_plug(ip)
            if shelly_info:
                return ("shelly", ip, shelly_info)
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
                kind, ip, data = res
                if kind == "hue":
                    detected["hue_bridges"].append({
                        "ip": ip,
                        "label": f"Philips Hue Bridge ({ip})",
                    })
                elif kind == "shelly":
                    detected["shelly_plugs"].append({
                        "ip": ip,
                        "label": f"{data.get('name', 'Shelly Plug')} ({ip})",
                        "model": data.get("model", "Plus Plug"),
                        "gen": data.get("type", "gen2"),
                    })

    return detected
