# StockVolumeAlerts

Production-oriented Python application for monitoring stocks and ETFs for unusual trading volume. It can send email and SMS alerts, run unattended during market hours, backtest alert rules, optimize thresholds, and generate CSV/HTML reports plus charts.

## Installation

Install Python 3.12 or newer.

```powershell
python --version
```

## Virtual Environment Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## Requirements Installation

```powershell
pip install -r requirements.txt
```

## Configuration

Copy the example configuration and edit it:

```powershell
Copy-Item config.json.example config.json
```

All runtime configuration lives in `config.json`. Add or remove symbols by editing the `symbols` list. No code changes are required.

Supported alert directions:

- `up`
- `down`
- `both`

Supported price references:

- `previous_close`
- `20_day_moving_average`
- `50_day_moving_average`

Supported SMS carriers:

- `verizon`
- `att`
- `tmobile`

## Gmail App Password Setup

For Gmail SMTP:

1. Enable 2-Step Verification on the Google account.
2. Create an app password from Google Account security settings.
3. Put the app password in `config.json` under `smtp.password`.
4. Use `smtp.gmail.com` and port `587`.

## Running Live Monitoring

```powershell
python volume_alert.py
```

The app scans every configured interval while the configured market window is open. It stops at `market_end` and sends a daily summary email if alerts occurred.

## Running Once

```powershell
python volume_alert.py --once
```

Runs one scan and exits.

## Backtesting

```powershell
python volume_alert.py --backtest
python volume_alert.py --backtest --ticker VTI
python volume_alert.py --backtest --start-date 2021-01-01 --end-date 2026-01-01
```

Backtesting uses the same alert engine as live monitoring.

Generated files:

- `reports/backtest_summary.csv`
- `reports/backtest_details.csv`
- `reports/backtest_report.html`
- `reports/cooldown_analysis.csv`
- `reports/charts/`

## Optimization Mode

```powershell
python volume_alert.py --optimize
python volume_alert.py --optimize --ticker VTI
```

Optimization tests volume thresholds of `50`, `100`, `150`, and `200`. If a symbol has the price filter enabled, it also tests price thresholds of `1`, `2`, `3`, `5`, and `10`.

Generated files:

- `reports/optimization_report.csv`
- `reports/optimization_report.html`
- `reports/optimization_alerts.csv`
- `reports/optimization_alerts.html`

The optimization summary section shows one row per tested threshold. It includes `VolumeThresholdPercent`, `FirstAlertDate`, `LastAlertDate`, and `AlertDateCount`.

The optimization detail section shows one row per alert date, sorted by date ascending. The same details are also written to the dedicated `optimization_alerts` files. It includes `ActualVolumePercentChange`, `RVOL`, `Volume`, `AverageVolume`, `CandleColor`, `CandleDirection`, and `CandleChangePercent`. `VolumeThresholdPercent` is the threshold being tested. `ActualVolumePercentChange` is what really happened on that date.

In HTML reports, `VolumeThresholdPercent` values of `100`, `150`, and `200` are shaded with progressively stronger red backgrounds.

Backtest details include `Open`, `Price`, `CandleColor`, `CandleDirection`, and `CandleChangePercent`. A green candle means the close was above the open. A red candle means the close was below the open. In reports, `DirectionTriggered` follows the candle direction: green is `UP`, red is `DOWN`, and neutral is `UNCHANGED`.

## Windows Task Scheduler Setup

1. Open Task Scheduler.
2. Create a new basic task.
3. Set the trigger to weekdays before market open.
4. Set the action to start a program.
5. Program/script:

```text
C:\Path\To\StockVolumeAlerts\.venv\Scripts\python.exe
```

6. Arguments:

```text
volume_alert.py
```

7. Start in:

```text
C:\Path\To\StockVolumeAlerts
```

## Log Locations

Logs are written to:

```text
logs/volume_alert.log
```

Rotating log files are used automatically.

## Troubleshooting

- If `config.json` is missing, copy `config.json.example`.
- If Gmail fails, confirm the username and app password.
- If text messages fail, verify the phone number and carrier.
- If yfinance returns empty data, verify the ticker and try again later.
- If alerts do not fire, check `minimum_volume`, `volume_percent`, direction, duplicate suppression, and cooldown settings.
- If market holiday handling is enabled, the app uses US holidays from the `holidays` package. This approximates market holidays but does not model early closes.

## Notes

Live data depends on yfinance availability and may be delayed or incomplete. Backtests use daily historical data, so cooldown analysis is evaluated at daily granularity.
