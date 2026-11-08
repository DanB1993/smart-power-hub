# ⚡ Smart Power OS (v1.0.0)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Raspberry%20Pi-orange)](#)

A high-performance, **zero-cloud, local-first smart energy management operating system** for solar PV, home storage batteries, and dynamic electricity tariffs.

Engineered for FoxESS inverters, Octopus Energy (Go, Agile, Intelligent, Tracker), British Gas, E.ON Next, OVO, and local hardware energy meters (Shelly, TP-Link Tapo, Philips Hue).

---

## 🌟 Key Features

* **⚡ Zero-Cloud Modbus RTU-over-TCP**: Connects directly to your hybrid inverter over local Wi-Fi or Ethernet. Sub-second live readings without vendor cloud downtime or rate limits.
* **🔎 1-Click Network Auto-Discovery & Setup Wizard**: Interactive onboarding flow at `/setup`. Scans your local subnet to auto-detect Modbus inverters (port 502), Philips Hue bridges, and Shelly smart plugs.
* **📈 48-Slot Dynamic Wholesale Curve & Planner**: Full 24-hour visualization of half-hourly export rates color-coded by value, showing current window and arbitrage dispatch recommendations (e.g. discharging stored 9.4p night energy into 37p+ wholesale peaks).
* **🔋 Battery Health & 00:30 Bridging Analysis**: Machine-learned baseload consumption forecasting that calculates whether stored battery kWh will bridge demand until your cheap overnight rate, complete with confidence scoring and surplus/deficit metrics.
* **🇬🇧 Multi-Supplier UK Tariff Engine**: Pre-configured support for:
  * **Octopus Energy**: Octopus Go, Intelligent Octopus Go, Agile Outgoing & Import, Cosy (heat pump dual-dip), and Tracker.
  * **British Gas**: Electric Driver (7.9p night charging) and Standard.
  * **E.ON Next**: Next Drive (6.9p 7-hour charging window) and Next Pledge.
  * **OVO Energy**: Charge Anytime (7.0p) and Economy 7.
  * **EDF Energy**: GoElectric Overnight (8.0p).
  * **Scottish Power**: EV Saver (7.45p).
  * **Ofgem Price Cap**: Baseline comparisons with custom dual/flat rate modes.
* **🔌 Hardware Smart Plugs & Energy Monitoring**:
  * **Shelly**: Native local HTTP RPC/REST support (Plus Plug S, Plug UK/EU, Shelly Pro) with zero cloud requirements.
  * **TP-Link Tapo**: Encrypted local KLAP v2 support (P110, P110M, P115) with automated idle-cutoff rules.
  * **Philips Hue**: Local Zigbee bridge synchronization and power modeling.
* **📱 Progressive Web App (PWA)**: Installable directly to iOS, Android, and desktop with offline asset caching and responsive dark UI.

---

## 🚀 Quick Start with Docker (Recommended)

### 1. Clone the Repository
```bash
git clone https://github.com/DanB1993/smart-power-hub.git
cd smart-power-hub
```

### 2. Launch Container
```bash
docker compose up -d
```

### 3. Complete Setup
Open your browser to:
```
http://<server-ip>:8050/setup
```
The **Setup Wizard** will scan your network, find your inverter, configure your battery capacity, and set your energy tariff in under 60 seconds.

---

## 🛠️ Configuration

Configuration is stored persistently in `/data/config.json` (or `/tmp/config.json` in standalone mode).

You can configure the system through the **Web Setup Wizard** (`/setup`) or by setting environment variables in `docker-compose.yml`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `INVERTER_HOST` | `192.168.1.100` | Local IP address of your inverter datalogger |
| `INVERTER_PORT` | `502` | Modbus RTU-over-TCP port |
| `INVERTER_UNIT` | `247` | Modbus Unit Slave ID (FoxESS default is `247`) |
| `POLL_INTERVAL` | `10` | Frequency in seconds to sample inverter telemetry |
| `HTTP_PORT` | `8050` | Web dashboard listening port |
| `DB_PATH` | `/data/solar_history.db` | Path to persistent SQLite telemetry history database |
| `HUE_HOST` | `192.168.1.150` | (Optional) Philips Hue bridge IP address |
| `HUE_API_KEY` | *(empty)* | (Optional) Philips Hue whitelist bridge username |
| `LATITUDE` | `51.5074` | Latitude for local solar yield irradiance forecast |
| `LONGITUDE` | `-0.1278` | Longitude for local solar yield irradiance forecast |

---

## 🔌 Supported Hardware & Protocols

### Inverters & Energy Storage
* **FoxESS**: H1, H3, AC1, KH, EP5, Mira HV25, ECS2900 series via direct Modbus TCP (port 502).
* **Generic Modbus**: Holding register adapter layer extensible for GivEnergy, Solis, and Sunsynk/Deye.

### Smart Plugs & Meters
* **Shelly**: All Gen 1, Gen 2, and Gen 3 models (`Plus Plug S`, `Shelly Plug UK`, `Shelly Pro 1PM`) via instant local HTTP REST.
* **TP-Link Tapo**: `P110`, `P110M` (UK Matter), and `P115` via local KLAP v2 encrypted handshake.
* **TP-Link Kasa**: `KP115`, `KP125`, `HS110` via local Kasa protocol.
* **Philips Hue**: Zigbee smart plugs and lighting groups via Hue Bridge local REST API.

---

## 📊 Dashboard Views

* **Overview**: Instant hero KPIs for PV Solar, Battery SoC %, Home Load, and Grid direction, alongside tariff shadow earnings and AI energy insights.
* **Flow & Telemetry**: SVG power flow animation with high-resolution historical telemetry charting (1H, 6H, 24H).
* **Agile & Tariffs**: 48-slot half-hourly export curve, peak dispatch planner, and 14-day financial savings ledger.
* **Solar Forecast**: 48-hour local solar yield predictions calibrated from Open-Meteo radiation models.
* **Devices & Lights**: Real-time load breakdown comparing Hue lights, Tapo plugs, Shelly meters, and custom appliances.
* **Schedule & Battery**: Battery cell temperatures, cycle counter, round-trip efficiency, and 00:30 off-peak bridging calculations.

---

## 💻 Local Development (Without Docker)

### Prerequisites
* Python 3.10+
* SQLite 3

### Installation
```bash
# Clone the repository
git clone https://github.com/DanB1993/smart-power-hub.git
cd smart-power-hub

# Install dependencies
pip install tapo python-kasa cryptography

# Run standalone server
python3 app.py
```
Visit `http://localhost:8050` in your browser.

---

## 🔒 Privacy & Local-First Philosophy

* **No External Cloud Accounts Required**: Your solar telemetry, battery charge levels, and household load data never leave your local network.
* **Direct Modbus Communication**: Queries holding and input registers directly from your inverter hardware.
* **Open Database**: Telemetry is stored locally in standard SQLite with automated 14-day rolling pruning.

---

## 📜 Changelog

### v1.0.0 (6 Nov 2026) — Initial Open-Source Release
* Complete zero-cloud local FoxESS Modbus RTU-over-TCP integration.
* Interactive 4-step Setup Wizard with network auto-discovery.
* Multi-supplier energy tariffs for Octopus Energy, British Gas, E.ON Next, OVO, EDF, Scottish Power, and Ofgem.
* 48-slot Agile rates curve with color-coded value tiers and peak dispatch planner.
* ML evening demand prediction and 00:30 off-peak bridging confidence meter.
* Native local Shelly, TP-Link Tapo, and Philips Hue device integrations.

---

## ⚠️ Disclaimer & Limitation of Liability

**USE AT YOUR OWN RISK**: Smart Power OS interacts directly with electrical hardware, hybrid solar inverters, and battery management systems (BMS) via Modbus RTU-over-TCP and local network protocols. 

This software is provided on an **"AS IS" AND "AS AVAILABLE" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND**, either express or implied. Under no circumstances shall the author(s), maintainer(s), or contributor(s) be liable for any direct, indirect, incidental, special, exemplary, or consequential damages (including, but not limited to, damage to inverters, battery storage packs, solar installations, home wiring, appliances, loss of electricity, grid utility non-compliance, or financial losses) arising out of the use, configuration, or failure of this software. 

This project is an independent community development and is not affiliated with, endorsed by, or certified by FoxESS Co., Ltd., Octopus Energy, or any other manufacturer or utility. See [`SECURITY.md`](SECURITY.md) for full safety disclosures.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
