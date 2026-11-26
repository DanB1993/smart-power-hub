# Changelog

All notable changes to the **Smart Power OS** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.3] - 2026-11-26

### Changed
- **Read-Only Hue Lighting Telemetry**: Removed interactive Hue light/room toggle switches and brightness dimmer sliders from the Devices & Lighting view in favor of read-only status pills while preserving live lighting power monitoring.
- **Removed Baseline & Tracked Appliances**: Removed the manual Baseline & Tracked Appliances card from the Devices & Lighting view.
- **Merged Overview Optimization Sections**: Combined the Smart Appliance Planner (formerly under *Intelligent Energy Optimization*) directly into the *Energy Optimization & Savings* card.

---

## [1.3.2] - 2026-11-26

### Added
- **FoxESS Cloud Open API Force Discharge**: Added `set_foxess_cloud_force_discharge()` using `/op/v0/device/scheduler/enable` (with rolling 35-minute windows and 20-minute refresh cadence) plus `set_foxess_remote_power()` (`44000–44002`) for FoxESS H1-G2 inverter compatibility.

### Fixed
- **Daytime Solar Guard Watchdog**: Fixed `enforce_daytime_solar_battery_charging()` dictionary key mismatches (`power.pv_total_w`, `power.load_w`, `grid.export_w`, `battery.charge_w`, `settings.work_mode_code`) and added an online-status guard so daytime solar-to-battery self-healing works as intended.
- **Agile Auto-Export Bridging Lookup**: Fixed `enforce_agile_auto_export()` calling a nonexistent `calculate_bridging_analytics()` helper by reading `bridging_analytics` from `calculate_daily_financials()` outside `lock`.

---

## [1.3.1] - 2026-11-15

### Added
- **Devices Page Network Scan**: Added `🔍 Scan for Devices` and `🔄 Scan Plugs` buttons in the Devices & Lighting view backed by `/api/devices/scan` to discover and auto-register new Tapo smart plugs on demand.
- **BMS Charge Throttle Alert**: Added an amber `BMS Throttled` status pill and explanatory banner on the Battery Storage KPI card whenever the battery BMS clamps charge current (e.g. cold temperature or top-end SoC taper) and spills surplus solar to the grid.
- **Daytime Solar-to-Battery Watchdog**: Added `enforce_daytime_solar_battery_charging()` to automatically detect daytime solar export anomalies while battery SoC is below 98% and restore Self-Use mode / clear lingering force-charge schedules.

### Fixed
- **Daytime Solar Export Lock**: Fixed `enforce_inverter_night_charge_cutoff()` re-enabling the Period 1 force-charge schedule during daytime hours, which caused FoxESS firmware to hold the battery in standby and export surplus solar.
- **Tapo Subnet Discovery Timeout**: Paced UDP port 20002 probes across the active local subnet in `scan_tapo_subnet()` to prevent Linux kernel socket `sendto` timeouts during device discovery.

---

## [1.1.2] - 2026-11-07

### Added
- **Adaptive Safety Buffer (+%)**: Configurable percentage padding added on top of the calculated adaptive target (e.g. baseline 60% + 10% buffer = 70% target) to guarantee pre-solar morning load coverage without depleting stored reserves.
- **Dynamic In-UI Buffer Control**: Live input field in the Settings view to fine-tune the safety margin (+0% to +30%) with instant recalculation.

---

## [1.1.1] - 2026-11-07

### Added
- **Adaptive Solar Headroom Toggle**: Dedicated setting in the dashboard to enable or disable dynamic night-charge limiting, allowing users to choose between solar-headroom targeting vs full 100% night charging.
- **Automated Off-Peak Top-Up**: Actively monitors battery SoC throughout cheap tariff windows (e.g. Octopus Go 00:30–05:30) and automatically resumes grid charging if overnight household demand drops the battery below the adaptive target by a configurable threshold (default: $\ge 3\%$).
- **Persistent Headroom Configuration**: Settings save directly to `/data/config.json` via `/api/inverter/adaptive_headroom` to persist across container restarts.

### Fixed
- **Premature Night Charge Cutoff**: Resolved issue where battery stopped charging early in the off-peak window without topping back up when household baseline loads depleted stored capacity.

---

## [1.1.0] - 2026-11-06

### Added
- **Dedicated Settings View**: Interactive inverter configuration dashboard for controlling hardware parameters directly via Modbus RTU holding registers (FC06 / FC16).
- **Inverter Work Mode Selector**: Live dropdown to switch operating modes between Self-Use, Feed-in Priority, Backup, Force Charge, and Force Discharge (Holding Register 41000).
- **Dual-Period Battery Grid Charge Schedule**:
  - Configurable Period 1 and Period 2 force-charge time windows (Holding Registers 41001–41006).
  - Independent schedule enable/disable toggles for Slot 1 and Slot 2.
  - One-click **"⚡ Align with Tariff"** button to automatically sync schedule times with active supplier off-peak windows (e.g. Octopus Go 00:30–05:30).
- **Battery Safety & Current Limits**: Live controls to update Min SoC reserve (10%–30%) and maximum BMS charge/discharge current limits (10A–50A / ~3.7 kW) via Holding Registers 41007–41011.
- **Dedicated Sidebar Link**: Moved Settings into its own permanent **Configuration** section in the navigation bar.

---

## [1.0.0] - 2026-11-06

### Added
- **Zero-Cloud Modbus RTU-over-TCP**: Connects directly to FoxESS hybrid inverters with sub-second live readings and power flow visualization.
- **Auto-Discovery & Setup Wizard**: 4-step onboarding flow at `/setup` that scans local subnets for Modbus inverters (port 502), Philips Hue bridges, and Shelly smart plugs.
- **Multi-Supplier UK Tariff Engine**: Full support for Octopus Energy (Go, Agile, Intelligent, Cosy, Tracker), British Gas, E.ON Next, OVO, EDF, Scottish Power, and Ofgem Price Cap.
- **48-Slot Agile Rates Curve**: Wholesale export rate tracking, peak ranking, and export arbitrage dispatch recommendations.
- **Smart Battery Bridging**: Machine-learned baseload consumption forecasting predicting battery endurance to the 00:30 off-peak rate.
- **Smart Plugs & Lighting**: Native integration for TP-Link Tapo (KLAP v2), Shelly (local HTTP RPC), and Philips Hue.
