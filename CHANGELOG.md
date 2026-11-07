# Changelog

All notable changes to the **Smart Power OS** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
