# Security Policy & Safety Guidelines

## Supported Versions

We release patches and security fixes for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

---

## ⚠️ Important Disclaimer & Limitation of Liability

**PLEASE READ THIS SECTION CAREFULLY BEFORE USING THIS SOFTWARE.**

1. **USE AT YOUR OWN RISK**: Smart Power OS interacts directly with high-voltage electrical hardware, solar hybrid inverters, battery management systems (BMS), and mains AC circuits via local protocols including Modbus RTU-over-TCP, HTTP, and KLAP. This software is provided on an **"AS IS" AND "AS AVAILABLE" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND**, either express or implied, including, without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE.
2. **NO LIABILITY FOR DAMAGE**: Under no circumstances shall the author(s), maintainer(s), copyright holder(s), or contributor(s) be liable for any direct, indirect, incidental, special, exemplary, punitive, or consequential damages (including, but not limited to, damage to inverters, batteries, solar panels, electrical wiring, home appliances, electrical fires, loss of electricity, grid utility fines, or financial losses associated with energy tariffs or export dispatch) arising in any way out of the use, misconfiguration, or failure of this software, even if advised of the possibility of such damage.
3. **NOT AN OFFICIAL PRODUCT**: Smart Power OS is an independent, community-driven open-source project. It is **not endorsed, certified, or sponsored** by FoxESS Co., Ltd., Octopus Energy, TP-Link, Signify/Philips Hue, Shelly (Allterco), or any other manufacturer or utility mentioned herein.
4. **INSTALLATION RESPONSIBILITY**: You are solely responsible for ensuring that any inverter register writes, battery charge/discharge parameters, and schedule configurations comply with your local electrical codes, manufacturer warranties, DNO connection agreements (e.g. G98 / G99 in the UK), and equipment specifications.

---

## Reporting a Vulnerability

If you discover a security vulnerability in Smart Power OS, **please do not open a public GitHub issue.**

Instead, please report it privately:

1. **GitHub Private Vulnerability Reporting**: Use the **Security** tab -> **Report a vulnerability** on the repository.
2. **Coordinated Disclosure**: You may also reach out via GitHub private messages to the repository owner.

Please include:
* A description of the issue and potential impact.
* Steps or a proof-of-concept to reproduce the vulnerability.
* Any affected components (e.g., Modbus poller, Tapo KLAP handshake, web API endpoints).

You will receive an acknowledgment within 48 hours, followed by updates on patch progress and coordinated disclosure.

---

## Security Architecture & Best Practices

Smart Power OS is built with a **local-first, zero-cloud architecture**:

### 1. Inverter Modbus Safety
* The application communicates with hybrid inverters over local Modbus RTU-over-TCP (port 502).
* Register writes are strictly bounded by hardware safety limits (e.g., maximum charge current is clamped to manufacturer specifications, minimum protected SoC reserve cannot be set below safe operating thresholds).

### 2. Network Isolation
* **Do not expose Smart Power OS (port 8050) directly to the public internet** without a secure reverse proxy enforcing authentication (e.g., Cloudflare Access, Tailscale, WireGuard, or Nginx with SSL and HTTP Basic Auth).
* Keep your inverter and smart devices on a trusted local LAN or isolated IoT VLAN.

### 3. Credential Storage
* API keys (e.g. Octopus API tokens) and local device credentials (TP-Link Tapo passwords) are stored locally on your machine in `/data/config.json` and `/data/solar_history.db`.
* Never commit `/data/` volumes or `.env` files containing live credentials to public repositories.
