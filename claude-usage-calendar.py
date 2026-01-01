#!/usr/bin/env python3
"""
Claude Code Token Usage Calendar Generator

Parses Claude Code JSONL session files and generates an interactive HTML calendar
visualization of token usage with multiple views.

Usage:
    ./claude-usage-calendar.py
    ./claude-usage-calendar.py --utc
    ./claude-usage-calendar.py --tz-offset -8
    ./claude-usage-calendar.py --no-open
    ./claude-usage-calendar.py --output /tmp/x.html
"""

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def find_jsonl_files(search_path="~/"):
    """Find all JSONL files with UUID or agent- pattern names."""
    result = subprocess.run(
        ["find", os.path.expanduser(search_path), "-name", "*.jsonl", "-type", "f"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    files = []
    uuid_pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
    )
    agent_pattern = re.compile(r"agent-[0-9a-f]+\.jsonl$")

    for line in result.stdout.strip().split("\n"):
        if line:
            basename = os.path.basename(line)
            if uuid_pattern.match(basename) or agent_pattern.match(basename):
                files.append(line)

    return files


def parse_jsonl_files(files, tz):
    """Parse JSONL files and extract usage data, taking MAX values per message ID."""
    message_data = {}

    for filepath in files:
        try:
            with open(filepath, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())

                        if entry.get("type") == "assistant" and "message" in entry:
                            msg = entry["message"]
                            usage = msg.get("usage", {})

                            if not usage:
                                continue

                            msg_id = msg.get("id", "")
                            if not msg_id:
                                continue

                            timestamp = entry.get("timestamp", "")
                            if not timestamp:
                                continue

                            try:
                                dt = datetime.fromisoformat(
                                    timestamp.replace("Z", "+00:00")
                                )
                                dt_local = dt.astimezone(tz)
                                date_key = dt_local.strftime("%Y-%m-%d")
                            except Exception:
                                continue

                            input_t = usage.get("input_tokens", 0)
                            output_t = usage.get("output_tokens", 0)
                            cache_r = usage.get("cache_read_input_tokens", 0)
                            cache_c = usage.get("cache_creation_input_tokens", 0)

                            if msg_id not in message_data:
                                message_data[msg_id] = {
                                    "date": date_key,
                                    "input": input_t,
                                    "output": output_t,
                                    "cache_read": cache_r,
                                    "cache_create": cache_c,
                                }
                            else:
                                message_data[msg_id]["input"] = max(
                                    message_data[msg_id]["input"], input_t
                                )
                                message_data[msg_id]["output"] = max(
                                    message_data[msg_id]["output"], output_t
                                )
                                message_data[msg_id]["cache_read"] = max(
                                    message_data[msg_id]["cache_read"], cache_r
                                )
                                message_data[msg_id]["cache_create"] = max(
                                    message_data[msg_id]["cache_create"], cache_c
                                )

                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    daily_usage = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    )

    for msg_id, data in message_data.items():
        date_key = data["date"]
        daily_usage[date_key]["input_tokens"] += data["input"]
        daily_usage[date_key]["output_tokens"] += data["output"]
        daily_usage[date_key]["cache_read_input_tokens"] += data["cache_read"]
        daily_usage[date_key]["cache_creation_input_tokens"] += data["cache_create"]

    return dict(daily_usage), len(message_data)


def format_tokens(n):
    """Format token count as K/M/B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    elif n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_usage_data(daily_usage, msg_count, tz_label):
    """Build the canonical JSON data structure from parsed usage data."""
    dates = sorted(daily_usage.keys())
    totals = {
        "input_tokens": sum(d["input_tokens"] for d in daily_usage.values()),
        "output_tokens": sum(d["output_tokens"] for d in daily_usage.values()),
        "cache_read_input_tokens": sum(
            d["cache_read_input_tokens"] for d in daily_usage.values()
        ),
        "cache_creation_input_tokens": sum(
            d["cache_creation_input_tokens"] for d in daily_usage.values()
        ),
    }
    totals["total_tokens"] = sum(totals.values())

    return {
        "timezone": tz_label,
        "date_range": {
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
        },
        "days_with_data": len(daily_usage),
        "unique_messages": msg_count,
        "totals": totals,
        "daily_usage": daily_usage,
    }


def generate_html(usage_data):
    """Generate interactive HTML with all views."""
    # Extract data from the canonical structure
    daily_usage = usage_data["daily_usage"]
    tz_label = usage_data["timezone"]
    date_range = usage_data["date_range"]

    # Convert daily_usage to JSON for embedding
    daily_data_json = json.dumps(daily_usage)

    # Find date range
    if date_range["start"]:
        min_date = date_range["start"]
        max_date = date_range["end"]
        min_year = int(min_date[:4])
        max_year = int(max_date[:4])
    else:
        now = datetime.now()
        min_year = max_year = now.year
        min_date = max_date = now.strftime("%Y-%m-%d")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Code Token Usage</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        html {{
            background: #0a0818;
        }}

        body {{
            font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #1a1a3e 50%, #24243e 100%);
            min-height: 100vh;
            padding: 20px;
            color: #e0e0e0;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        .header-bar {{
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            margin-bottom: 15px;
        }}

        .header-bar .tz-label {{
            text-align: right;
        }}

        h1 {{
            font-size: 1.6rem;
            font-weight: 700;
            background: linear-gradient(90deg, #00d4ff, #00ff88, #00d4ff);
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            animation: gradient 3s linear infinite;
            margin: 0;
        }}

        .tz-label {{
            color: #666;
            font-size: 0.8rem;
        }}

        @keyframes gradient {{
            0% {{ background-position: 0% center; }}
            100% {{ background-position: 200% center; }}
        }}

        /* Navigation Tabs */
        .nav-tabs {{
            display: flex;
            justify-content: center;
            gap: 8px;
        }}

        .nav-tab {{
            padding: 8px 18px;
            background: rgba(40, 40, 80, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 6px;
            color: #888;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 500;
            transition: all 0.3s ease;
        }}

        .nav-tab:hover {{
            background: rgba(60, 60, 100, 0.6);
            color: #fff;
        }}

        .nav-tab.active {{
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.3) 0%, rgba(0, 255, 136, 0.2) 100%);
            border-color: rgba(0, 212, 255, 0.5);
            color: #00d4ff;
        }}

        /* Sub Navigation */
        .sub-nav {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 15px;
            margin-bottom: 15px;
        }}

        .nav-arrow {{
            padding: 5px 12px;
            background: rgba(40, 40, 80, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 5px;
            color: #888;
            cursor: pointer;
            font-size: 1rem;
            transition: all 0.3s ease;
        }}

        .nav-arrow:hover:not(.disabled) {{
            background: rgba(60, 60, 100, 0.6);
            color: #00d4ff;
            border-color: rgba(0, 212, 255, 0.3);
        }}

        .nav-arrow.disabled {{
            opacity: 0.3;
            cursor: not-allowed;
        }}

        .nav-current {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            min-width: 160px;
            text-align: center;
        }}

        /* Content area */
        .view-content {{
            display: none;
        }}

        .view-content.active {{
            display: block;
        }}

        .calendar {{
            background: rgba(30, 30, 60, 0.6);
            border-radius: 20px;
            padding: 30px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
        }}

        .calendar-header {{
            display: grid;
            grid-template-columns: repeat(7, 1fr) 120px;
            gap: 10px;
            margin-bottom: 15px;
        }}

        .header-cell {{
            text-align: center;
            font-weight: 600;
            font-size: 0.9rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 10px;
        }}

        .week-row {{
            display: grid;
            grid-template-columns: repeat(7, 1fr) 120px;
            gap: 10px;
            margin-bottom: 10px;
        }}

        .day-cell {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 8px;
            padding: 6px 8px;
            min-height: 58px;
            transition: all 0.3s ease;
            border: 1px solid rgba(255, 255, 255, 0.05);
            position: relative;
            overflow: hidden;
        }}

        .day-cell:hover {{
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(0, 212, 255, 0.2);
            border-color: rgba(0, 212, 255, 0.3);
        }}

        .day-cell.empty {{
            background: rgba(20, 20, 40, 0.3);
            border: none;
        }}

        .day-cell.empty:hover {{
            transform: none;
            box-shadow: none;
        }}

        .day-cell.other-month {{
            opacity: 0.7;
        }}

        .day-cell.other-month .day-total,
        .day-cell.other-month .day-breakdown span {{
            color: #777 !important;
        }}

        .day-cell.other-month .day-number {{
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.1);
            color: #777;
        }}

        /* Keyboard shortcuts modal */
        .modal-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }}

        .modal-overlay.active {{
            display: flex;
        }}

        .modal {{
            background: linear-gradient(135deg, #1a1a3e 0%, #24243e 100%);
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 16px;
            padding: 30px;
            max-width: 400px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
        }}

        .modal h3 {{
            color: #00d4ff;
            font-size: 1.1rem;
            margin-bottom: 5px;
            text-align: center;
        }}

        .modal-hint {{
            text-align: center;
            color: #666;
            font-size: 0.8rem;
            margin-bottom: 15px;
        }}

        kbd {{
            background: rgba(0, 212, 255, 0.15);
            border: 1px solid rgba(0, 212, 255, 0.4);
            border-radius: 4px;
            padding: 3px 8px;
            font-family: monospace;
            font-size: 0.85rem;
            color: #00d4ff;
            display: inline-block;
            min-width: 24px;
            text-align: center;
        }}

        .modal-divider {{
            height: 1px;
            background: rgba(255, 255, 255, 0.1);
            margin: 15px 0;
        }}

        .shortcut-table {{
            width: 100%;
            border-collapse: collapse;
        }}

        .shortcut-table tr {{
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .shortcut-table tr:last-child {{
            border-bottom: none;
        }}

        .shortcut-table td {{
            padding: 8px 0;
            color: #aaa;
            font-size: 0.9rem;
        }}

        .shortcut-table td:first-child {{
            width: 90px;
            padding-right: 15px;
        }}

        .protip {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #1a1a3e;
            border: 1px solid rgba(0, 212, 255, 0.3);
            border-radius: 8px;
            padding: 8px 14px;
            font-size: 0.8rem;
            color: #888;
            z-index: 100;
            transition: opacity 0.3s ease;
        }}

        .protip kbd {{
            background: rgba(0, 212, 255, 0.2);
            border: 1px solid rgba(0, 212, 255, 0.4);
            border-radius: 4px;
            padding: 2px 6px;
            font-family: monospace;
            color: #00d4ff;
        }}

        .protip.hidden {{
            opacity: 0;
            pointer-events: none;
        }}

        .github-link {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            color: #666;
            text-decoration: none;
            font-size: 0.8rem;
            transition: color 0.2s ease;
            margin-top: 15px;
        }}

        .github-link:hover {{
            color: #00d4ff;
        }}

        .github-link svg {{
            width: 18px;
            height: 18px;
            fill: currentColor;
        }}

        .day-header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 3px;
        }}

        .day-total {{
            font-size: 1rem;
            font-weight: 700;
            color: #00d4ff;
        }}

        .day-number {{
            font-size: 0.75rem;
            font-weight: 600;
            color: #fff;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 4px;
            padding: 2px 6px;
            min-width: 22px;
            text-align: center;
        }}

        .day-breakdown {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1px 6px;
            font-size: 0.6rem;
            color: #888;
            line-height: 1.2;
        }}

        .in-label {{ color: #4ade80; }}
        .out-label {{ color: #f472b6; }}
        .cache-r-label {{ color: #fbbf24; }}
        .cache-c-label {{ color: #a78bfa; }}

        .week-total {{
            background: rgba(0, 212, 255, 0.1);
            border-radius: 12px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            border: 1px solid rgba(0, 212, 255, 0.2);
        }}

        .week-total-label {{
            font-size: 0.7rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 5px;
        }}

        .week-total-value {{
            font-size: 1.2rem;
            font-weight: 700;
            color: #00d4ff;
        }}

        .summary {{
            margin-top: 20px;
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.1) 0%, rgba(0, 255, 136, 0.1) 100%);
            border-radius: 12px;
            padding: 15px 20px;
            border: 1px solid rgba(0, 212, 255, 0.2);
        }}

        .summary h2 {{
            font-size: 1.2rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 20px;
            text-align: center;
        }}

        .summary-row {{
            display: flex;
            justify-content: flex-start;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .summary-title {{
            font-size: 0.85rem;
            font-weight: 600;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-right: 8px;
        }}

        .summary-inline {{
            display: flex;
            align-items: baseline;
            gap: 8px;
            background: rgba(0, 0, 0, 0.25);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            padding: 8px 14px;
        }}

        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 20px;
        }}

        .summary-item {{
            text-align: center;
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 12px;
        }}

        .summary-label {{
            font-size: 0.75rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}

        .summary-label-inline {{
            font-size: 0.8rem;
            color: #888;
        }}

        .summary-value {{
            font-size: 1.5rem;
            font-weight: 700;
        }}

        .summary-value.input {{ color: #4ade80; }}
        .summary-value.output {{ color: #f472b6; }}
        .summary-value.cache-read {{ color: #fbbf24; }}
        .summary-value.cache-create {{ color: #a78bfa; }}
        .summary-value.total {{
            background: linear-gradient(90deg, #00d4ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .intensity-low {{ background: rgba(40, 40, 80, 0.5); }}
        .intensity-1 {{ background: linear-gradient(135deg, rgba(0, 80, 100, 0.6) 0%, rgba(40, 40, 80, 0.5) 100%); }}
        .intensity-2 {{ background: linear-gradient(135deg, rgba(0, 120, 130, 0.6) 0%, rgba(40, 60, 90, 0.5) 100%); }}
        .intensity-3 {{ background: linear-gradient(135deg, rgba(0, 160, 160, 0.7) 0%, rgba(40, 80, 100, 0.5) 100%); }}
        .intensity-4 {{ background: linear-gradient(135deg, rgba(0, 200, 180, 0.7) 0%, rgba(40, 100, 110, 0.5) 100%); }}
        .intensity-5 {{ background: linear-gradient(135deg, rgba(0, 212, 255, 0.8) 0%, rgba(0, 180, 150, 0.6) 100%); }}

        /* Yearly View */
        .year-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 30px;
        }}

        .month-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 20px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            cursor: pointer;
            transition: all 0.3s ease;
        }}

        .month-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0, 212, 255, 0.25);
            border-color: rgba(0, 212, 255, 0.4);
        }}

        .month-card.no-data {{
            opacity: 0.4;
            cursor: default;
        }}

        .month-card.no-data:hover {{
            transform: none;
            box-shadow: none;
            border-color: rgba(255, 255, 255, 0.05);
        }}

        .month-name {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 12px;
        }}

        .month-total {{
            font-size: 1.8rem;
            font-weight: 700;
            color: #00d4ff;
            margin-bottom: 10px;
        }}

        .month-breakdown {{
            font-size: 0.75rem;
            color: #888;
            line-height: 1.6;
        }}

        /* All Time View */
        .all-time-header {{
            text-align: center;
            margin-bottom: 25px;
        }}

        .all-time-date-range {{
            font-size: 1.2rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 5px;
        }}

        .all-time-days-count {{
            font-size: 0.9rem;
            color: #888;
        }}

        .all-time-stats {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }}

        .all-time-breakdown {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }}

        .stat-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 25px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .stat-label {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }}

        .stat-value {{
            font-size: 2rem;
            font-weight: 700;
        }}

        .stat-value.highlight {{
            background: linear-gradient(90deg, #00d4ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .token-breakdown {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
        }}

        .token-card {{
            background: rgba(40, 40, 80, 0.5);
            border-radius: 16px;
            padding: 25px;
            text-align: center;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .token-card-label {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
        }}

        .token-card-value {{
            font-size: 2.2rem;
            font-weight: 700;
        }}

        .token-card-pct {{
            font-size: 0.9rem;
            color: #666;
            margin-top: 8px;
        }}
    </style>
</head>
<body>
    <div class="protip" id="protip">protip: press <kbd>?</kbd> for keyboard shortcuts</div>
    <div class="container">
        <div class="header-bar">
            <h1>Claude Code Token Usage</h1>
            <div class="nav-tabs">
                <div class="nav-tab active" data-view="monthly">📆 Monthly</div>
                <div class="nav-tab" data-view="yearly">📅 Yearly</div>
                <div class="nav-tab" data-view="alltime">📊 All Time</div>
            </div>
            <div class="tz-label">{tz_label}</div>
        </div>

        <div class="sub-nav" id="sub-nav" style="display: none;">
            <div class="nav-arrow" id="nav-prev">◀</div>
            <div class="nav-current" id="nav-current"></div>
            <div class="nav-arrow" id="nav-next">▶</div>
        </div>

        <div class="view-content" id="view-alltime">
            <div class="calendar">
                <div class="all-time-header" id="all-time-header"></div>
                <div class="all-time-stats" id="all-time-stats"></div>
                <div class="summary" id="all-time-summary"></div>
            </div>
        </div>

        <div class="view-content" id="view-yearly">
            <div class="calendar">
                <div class="year-grid" id="year-grid"></div>
                <div class="summary" id="year-summary"></div>
            </div>
        </div>

        <div class="view-content active" id="view-monthly">
            <div class="calendar" id="monthly-calendar"></div>
        </div>
    </div>

    <div class="modal-overlay" id="help-modal">
        <div class="modal">
            <h3>Keyboard Shortcuts</h3>
            <div class="modal-hint">press esc to close</div>
            <div class="modal-divider"></div>
            <table class="shortcut-table">
                <tr><td><kbd>1</kbd></td><td>Monthly view</td></tr>
                <tr><td><kbd>2</kbd></td><td>Yearly view</td></tr>
                <tr><td><kbd>3</kbd></td><td>All Time view</td></tr>
                <tr><td><kbd>h</kbd> / <kbd>k</kbd></td><td>Previous (month/year)</td></tr>
                <tr><td><kbd>j</kbd> / <kbd>l</kbd></td><td>Next (month/year)</td></tr>
                <tr><td><kbd>c</kbd></td><td>Current month</td></tr>
                <tr><td><kbd>?</kbd></td><td>Show this help</td></tr>
            </table>
            <div class="modal-divider"></div>
            <a href="https://github.com/rickgorman/claude-usage-calendar" target="_blank" class="github-link">
                <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
                rickgorman/claude-usage-calendar
            </a>
        </div>
    </div>

    <script>
        const dailyData = {daily_data_json};
        const minYear = {min_year};
        const maxYear = {max_year};
        const monthNames = ['January', 'February', 'March', 'April', 'May', 'June',
                           'July', 'August', 'September', 'October', 'November', 'December'];

        let currentView = 'alltime';
        let currentYear = maxYear;
        let currentMonth = new Date().getMonth() + 1;

        // Find latest month with data
        const dates = Object.keys(dailyData).sort();
        if (dates.length > 0) {{
            const latestDate = dates[dates.length - 1];
            currentYear = parseInt(latestDate.substring(0, 4));
            currentMonth = parseInt(latestDate.substring(5, 7));
        }}

        function formatTokens(n) {{
            if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
            if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
            if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
            return n.toString();
        }}

        function getTotal(usage) {{
            return (usage.input_tokens || 0) + (usage.output_tokens || 0) +
                   (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
        }}

        function aggregateByMonth(year, month) {{
            const prefix = `${{year}}-${{String(month).padStart(2, '0')}}`;
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const [date, usage] of Object.entries(dailyData)) {{
                if (date.startsWith(prefix)) {{
                    result.input_tokens += usage.input_tokens || 0;
                    result.output_tokens += usage.output_tokens || 0;
                    result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                    result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
                }}
            }}
            return result;
        }}

        function aggregateByYear(year) {{
            const prefix = `${{year}}-`;
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const [date, usage] of Object.entries(dailyData)) {{
                if (date.startsWith(prefix)) {{
                    result.input_tokens += usage.input_tokens || 0;
                    result.output_tokens += usage.output_tokens || 0;
                    result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                    result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
                }}
            }}
            return result;
        }}

        function aggregateAllTime() {{
            const result = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
            for (const usage of Object.values(dailyData)) {{
                result.input_tokens += usage.input_tokens || 0;
                result.output_tokens += usage.output_tokens || 0;
                result.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                result.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;
            }}
            return result;
        }}

        function renderAllTime() {{
            const totals = aggregateAllTime();
            const grandTotal = getTotal(totals);
            const dates = Object.keys(dailyData).sort();
            const numDays = dates.length;

            let peakDay = '';
            let peakAmount = 0;
            for (const [date, usage] of Object.entries(dailyData)) {{
                const total = getTotal(usage);
                if (total > peakAmount) {{
                    peakAmount = total;
                    peakDay = date;
                }}
            }}

            const avgDaily = numDays > 0 ? Math.round(grandTotal / numDays) : 0;
            const dateRange = dates.length > 0 ? `${{dates[0]}} to ${{dates[dates.length-1]}}` : 'No data';

            // Header with date range
            document.getElementById('all-time-header').innerHTML = `
                <div class="all-time-date-range">${{dateRange}}</div>
                <div class="all-time-days-count">${{numDays}} days with data</div>
            `;

            // Row 1: Total | Avg Daily | Peak Day
            document.getElementById('all-time-stats').innerHTML = `
                <div class="stat-card">
                    <div class="stat-label">Total Tokens</div>
                    <div class="stat-value highlight">${{formatTokens(grandTotal)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Average Daily</div>
                    <div class="stat-value" style="color: #00d4ff;">${{formatTokens(avgDaily)}}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Peak Day</div>
                    <div class="stat-value" style="color: #00d4ff;">${{formatTokens(peakAmount)}}</div>
                    <div style="color: #666; font-size: 0.85rem; margin-top: 8px;">${{peakDay}}</div>
                </div>
            `;

            // Bottom row: 5-column summary grid (matching yearly/monthly format)
            document.getElementById('all-time-summary').innerHTML = `
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Input Tokens</div>
                        <div class="summary-value input">${{formatTokens(totals.input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Output Tokens</div>
                        <div class="summary-value output">${{formatTokens(totals.output_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Read</div>
                        <div class="summary-value cache-read">${{formatTokens(totals.cache_read_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Create</div>
                        <div class="summary-value cache-create">${{formatTokens(totals.cache_creation_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Grand Total</div>
                        <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                    </div>
                </div>
            `;
        }}

        function renderYearly() {{
            document.getElementById('nav-current').textContent = currentYear;
            document.getElementById('nav-prev').classList.toggle('disabled', currentYear <= minYear);
            document.getElementById('nav-next').classList.toggle('disabled', currentYear >= maxYear);

            let html = '';
            let yearTotal = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};

            for (let m = 1; m <= 12; m++) {{
                const monthData = aggregateByMonth(currentYear, m);
                const total = getTotal(monthData);
                const hasData = total > 0;

                yearTotal.input_tokens += monthData.input_tokens;
                yearTotal.output_tokens += monthData.output_tokens;
                yearTotal.cache_read_input_tokens += monthData.cache_read_input_tokens;
                yearTotal.cache_creation_input_tokens += monthData.cache_creation_input_tokens;

                html += `
                    <div class="month-card ${{hasData ? '' : 'no-data'}}" data-month="${{m}}" data-year="${{currentYear}}">
                        <div class="month-name">${{monthNames[m-1]}}</div>
                        <div class="month-total">${{hasData ? formatTokens(total) : '—'}}</div>
                        <div class="month-breakdown">
                            <span class="in-label">In: ${{formatTokens(monthData.input_tokens)}}</span>
                            <span class="out-label">Out: ${{formatTokens(monthData.output_tokens)}}</span>
                            <span class="cache-r-label">Cache R: ${{formatTokens(monthData.cache_read_input_tokens)}}</span>
                            <span class="cache-c-label">Cache C: ${{formatTokens(monthData.cache_creation_input_tokens)}}</span>
                        </div>
                    </div>
                `;
            }}

            document.getElementById('year-grid').innerHTML = html;

            const grandTotal = getTotal(yearTotal);
            document.getElementById('year-summary').innerHTML = `
                <h2>${{currentYear}} Yearly Summary</h2>
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Input Tokens</div>
                        <div class="summary-value input">${{formatTokens(yearTotal.input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Output Tokens</div>
                        <div class="summary-value output">${{formatTokens(yearTotal.output_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Read</div>
                        <div class="summary-value cache-read">${{formatTokens(yearTotal.cache_read_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Create</div>
                        <div class="summary-value cache-create">${{formatTokens(yearTotal.cache_creation_input_tokens)}}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Grand Total</div>
                        <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                    </div>
                </div>
            `;

            // Add click handlers for month cards
            document.querySelectorAll('.month-card:not(.no-data)').forEach(card => {{
                card.addEventListener('click', () => {{
                    currentMonth = parseInt(card.dataset.month);
                    currentYear = parseInt(card.dataset.year);
                    document.querySelector('.nav-tab[data-view="monthly"]').click();
                }});
            }});
        }}

        function getDaysInMonth(year, month) {{
            return new Date(year, month, 0).getDate();
        }}

        function getFirstDayOfMonth(year, month) {{
            // Returns 0=Sun, 1=Mon, etc.
            return new Date(year, month - 1, 1).getDay();
        }}

        function renderMonthly() {{
            document.getElementById('nav-current').textContent = `${{monthNames[currentMonth-1]}} ${{currentYear}}`;

            // Determine if we can go prev/next
            const canPrev = currentYear > minYear || (currentYear === minYear && currentMonth > 1);
            const canNext = currentYear < maxYear || (currentYear === maxYear && currentMonth < 12);
            document.getElementById('nav-prev').classList.toggle('disabled', !canPrev);
            document.getElementById('nav-next').classList.toggle('disabled', !canNext);

            const daysInMonth = getDaysInMonth(currentYear, currentMonth);
            const firstDay = getFirstDayOfMonth(currentYear, currentMonth);

            // Previous month info
            const prevMonth = currentMonth === 1 ? 12 : currentMonth - 1;
            const prevYear = currentMonth === 1 ? currentYear - 1 : currentYear;
            const daysInPrevMonth = getDaysInMonth(prevYear, prevMonth);

            // Next month info
            const nextMonth = currentMonth === 12 ? 1 : currentMonth + 1;
            const nextYear = currentMonth === 12 ? currentYear + 1 : currentYear;

            // Find max for intensity scaling
            let maxTotal = 0;
            for (let d = 1; d <= daysInMonth; d++) {{
                const dateKey = `${{currentYear}}-${{String(currentMonth).padStart(2,'0')}}-${{String(d).padStart(2,'0')}}`;
                const usage = dailyData[dateKey] || {{}};
                const total = getTotal(usage);
                if (total > maxTotal) maxTotal = total;
            }}
            if (maxTotal === 0) maxTotal = 1;

            let html = `
                <div class="calendar-header">
                    <div class="header-cell">Sun</div>
                    <div class="header-cell">Mon</div>
                    <div class="header-cell">Tue</div>
                    <div class="header-cell">Wed</div>
                    <div class="header-cell">Thu</div>
                    <div class="header-cell">Fri</div>
                    <div class="header-cell">Sat</div>
                    <div class="header-cell">Weekly</div>
                </div>
            `;

            let day = 1;
            let nextMonthDay = 1;
            let monthlyTotals = {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};

            // Calculate number of weeks needed
            const totalCells = firstDay + daysInMonth;
            const numWeeks = Math.ceil(totalCells / 7);

            // Generate weeks
            for (let week = 0; week < numWeeks; week++) {{
                html += '<div class="week-row">';
                let weekTotal = 0;

                for (let dow = 0; dow < 7; dow++) {{
                    const cellIndex = week * 7 + dow;

                    if (cellIndex < firstDay) {{
                        // Previous month days
                        const prevDay = daysInPrevMonth - firstDay + 1 + cellIndex;
                        const dateKey = `${{prevYear}}-${{String(prevMonth).padStart(2,'0')}}-${{String(prevDay).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const total = getTotal(usage);
                        weekTotal += total;

                        html += `
                            <div class="day-cell other-month intensity-low">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{prevDay}}</span>
                                </div>
                                <div class="day-breakdown">
                                    <span class="in-label">In: ${{formatTokens(usage.input_tokens || 0)}}</span>
                                    <span class="out-label">Out: ${{formatTokens(usage.output_tokens || 0)}}</span>
                                    <span class="cache-r-label">CR: ${{formatTokens(usage.cache_read_input_tokens || 0)}}</span>
                                    <span class="cache-c-label">CC: ${{formatTokens(usage.cache_creation_input_tokens || 0)}}</span>
                                </div>
                            </div>
                        `;
                    }} else if (day <= daysInMonth) {{
                        // Current month days
                        const dateKey = `${{currentYear}}-${{String(currentMonth).padStart(2,'0')}}-${{String(day).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const total = getTotal(usage);
                        weekTotal += total;

                        monthlyTotals.input_tokens += usage.input_tokens || 0;
                        monthlyTotals.output_tokens += usage.output_tokens || 0;
                        monthlyTotals.cache_read_input_tokens += usage.cache_read_input_tokens || 0;
                        monthlyTotals.cache_creation_input_tokens += usage.cache_creation_input_tokens || 0;

                        const intensity = total > 0 ? Math.min(5, Math.ceil((total / maxTotal) * 5)) : 0;
                        const intensityClass = intensity > 0 ? `intensity-${{intensity}}` : 'intensity-low';

                        html += `
                            <div class="day-cell ${{intensityClass}}">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{day}}</span>
                                </div>
                                <div class="day-breakdown">
                                    <span class="in-label">In: ${{formatTokens(usage.input_tokens || 0)}}</span>
                                    <span class="out-label">Out: ${{formatTokens(usage.output_tokens || 0)}}</span>
                                    <span class="cache-r-label">CR: ${{formatTokens(usage.cache_read_input_tokens || 0)}}</span>
                                    <span class="cache-c-label">CC: ${{formatTokens(usage.cache_creation_input_tokens || 0)}}</span>
                                </div>
                            </div>
                        `;
                        day++;
                    }} else {{
                        // Next month days
                        const dateKey = `${{nextYear}}-${{String(nextMonth).padStart(2,'0')}}-${{String(nextMonthDay).padStart(2,'0')}}`;
                        const usage = dailyData[dateKey] || {{ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }};
                        const total = getTotal(usage);
                        weekTotal += total;

                        html += `
                            <div class="day-cell other-month intensity-low">
                                <div class="day-header">
                                    <span class="day-total">${{formatTokens(total)}}</span>
                                    <span class="day-number">${{nextMonthDay}}</span>
                                </div>
                                <div class="day-breakdown">
                                    <span class="in-label">In: ${{formatTokens(usage.input_tokens || 0)}}</span>
                                    <span class="out-label">Out: ${{formatTokens(usage.output_tokens || 0)}}</span>
                                    <span class="cache-r-label">CR: ${{formatTokens(usage.cache_read_input_tokens || 0)}}</span>
                                    <span class="cache-c-label">CC: ${{formatTokens(usage.cache_creation_input_tokens || 0)}}</span>
                                </div>
                            </div>
                        `;
                        nextMonthDay++;
                    }}
                }}

                html += `
                    <div class="week-total">
                        <div class="week-total-label">Week Total</div>
                        <div class="week-total-value">${{formatTokens(weekTotal)}}</div>
                    </div>
                </div>`;
            }}

            const grandTotal = getTotal(monthlyTotals);
            html += `
                <div class="summary">
                    <h2>${{monthNames[currentMonth-1]}} ${{currentYear}} Summary</h2>
                    <div class="summary-grid">
                        <div class="summary-item">
                            <div class="summary-label">Input Tokens</div>
                            <div class="summary-value input">${{formatTokens(monthlyTotals.input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Output Tokens</div>
                            <div class="summary-value output">${{formatTokens(monthlyTotals.output_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Read</div>
                            <div class="summary-value cache-read">${{formatTokens(monthlyTotals.cache_read_input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Cache Create</div>
                            <div class="summary-value cache-create">${{formatTokens(monthlyTotals.cache_creation_input_tokens)}}</div>
                        </div>
                        <div class="summary-item">
                            <div class="summary-label">Grand Total</div>
                            <div class="summary-value total">${{formatTokens(grandTotal)}}</div>
                        </div>
                    </div>
                </div>
            `;

            document.getElementById('monthly-calendar').innerHTML = html;
        }}

        function switchView(view) {{
            currentView = view;

            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelector(`.nav-tab[data-view="${{view}}"]`).classList.add('active');

            document.querySelectorAll('.view-content').forEach(v => v.classList.remove('active'));
            document.getElementById(`view-${{view}}`).classList.add('active');

            const subNav = document.getElementById('sub-nav');
            if (view === 'alltime') {{
                subNav.style.display = 'none';
                renderAllTime();
            }} else if (view === 'yearly') {{
                subNav.style.display = 'flex';
                renderYearly();
            }} else if (view === 'monthly') {{
                subNav.style.display = 'flex';
                renderMonthly();
            }}
        }}

        // Event listeners
        document.querySelectorAll('.nav-tab').forEach(tab => {{
            tab.addEventListener('click', () => switchView(tab.dataset.view));
        }});

        document.getElementById('nav-prev').addEventListener('click', () => {{
            if (currentView === 'yearly' && currentYear > minYear) {{
                currentYear--;
                renderYearly();
            }} else if (currentView === 'monthly') {{
                currentMonth--;
                if (currentMonth < 1) {{
                    currentMonth = 12;
                    currentYear--;
                }}
                renderMonthly();
            }}
        }});

        document.getElementById('nav-next').addEventListener('click', () => {{
            if (currentView === 'yearly' && currentYear < maxYear) {{
                currentYear++;
                renderYearly();
            }} else if (currentView === 'monthly') {{
                currentMonth++;
                if (currentMonth > 12) {{
                    currentMonth = 1;
                    currentYear++;
                }}
                renderMonthly();
            }}
        }});

        // Initial render
        switchView('monthly');

        // Keyboard shortcuts
        const helpModal = document.getElementById('help-modal');
        const protip = document.getElementById('protip');

        function showHelp() {{
            helpModal.classList.add('active');
            protip.classList.add('hidden');
        }}

        function hideHelp() {{
            helpModal.classList.remove('active');
        }}

        function navigatePrev() {{
            document.getElementById('nav-prev').click();
        }}

        function navigateNext() {{
            document.getElementById('nav-next').click();
        }}

        function goToCurrentMonth() {{
            // Reset to the latest month with data
            const dates = Object.keys(dailyData).sort();
            if (dates.length > 0) {{
                const latestDate = dates[dates.length - 1];
                currentYear = parseInt(latestDate.substring(0, 4));
                currentMonth = parseInt(latestDate.substring(5, 7));
            }}
            switchView('monthly');
        }}

        document.addEventListener('keydown', (e) => {{
            // Ignore if typing in an input
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            const key = e.key;

            if (key === 'Escape') {{
                hideHelp();
                return;
            }}

            if (helpModal.classList.contains('active')) return;

            switch (key) {{
                case '?':
                    showHelp();
                    break;
                case '1':
                    switchView('monthly');
                    break;
                case '2':
                    switchView('yearly');
                    break;
                case '3':
                    switchView('alltime');
                    break;
                case 'h':
                case 'k':
                    navigatePrev();
                    break;
                case 'j':
                case 'l':
                    navigateNext();
                    break;
                case 'c':
                    goToCurrentMonth();
                    break;
            }}
        }});

        // Close modal when clicking overlay
        helpModal.addEventListener('click', (e) => {{
            if (e.target === helpModal) hideHelp();
        }});
    </script>
</body>
</html>
"""
    return html


def main():
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML calendar of Claude Code token usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s
    %(prog)s --utc
    %(prog)s --tz-offset -8
    %(prog)s --no-open
    %(prog)s -o ~/report.html
    %(prog)s -q

How it works:
    1. Scans ~/ for Claude Code session files (*.jsonl with UUID/agent patterns)
    2. Parses token usage from each message (input, output, cache read, cache create)
    3. Deduplicates by message ID, taking MAX values (handles streaming updates)
    4. Aggregates by date in your local timezone
    5. Generates an interactive HTML with three views:
       - All Time: Overall statistics and token breakdown
       - Yearly: Month-by-month overview (click to drill down)
       - Monthly: Daily calendar view with weekly totals
    6. Opens the result in your default browser (unless --no-open)

Token types:
    - Input:        Tokens sent to the model (your prompts + context)
    - Output:       Tokens generated by the model (responses)
    - Cache Read:   Tokens read from prompt cache (saves cost)
    - Cache Create: Tokens written to prompt cache

Notes:
    - Uses your system's local timezone by default
    - Color intensity on calendar cells reflects relative daily usage
    - Click month cards in yearly view to jump to that month
        """,
    )
    parser.add_argument(
        "--utc", action="store_true", help="Use UTC instead of local time"
    )
    parser.add_argument(
        "--tz-offset",
        type=int,
        default=None,
        help="Custom timezone offset from UTC (e.g., -8 for PST)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="/tmp/claude_usage_calendar.html",
        help="Output HTML file path",
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Don't open the HTML file in browser"
    )
    parser.add_argument(
        "--search-path",
        type=str,
        default="~/",
        help="Path to search for JSONL files (default: ~/)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser.add_argument(
        "--json", action="store_true", help="Output JSON data instead of HTML calendar"
    )

    args = parser.parse_args()

    # Determine timezone
    if args.utc:
        tz = timezone.utc
        tz_label = "UTC"
    elif args.tz_offset is not None:
        tz = timezone(timedelta(hours=args.tz_offset))
        if args.tz_offset >= 0:
            tz_label = f"UTC+{args.tz_offset}"
        else:
            tz_label = f"UTC{args.tz_offset}"
    else:
        # Default: system local timezone
        tz = datetime.now().astimezone().tzinfo
        tz_label = datetime.now().astimezone().strftime("%Z")

    if not args.quiet:
        print(f"Finding JSONL files in {args.search_path}...")

    files = find_jsonl_files(args.search_path)

    if not args.quiet:
        print(f"Found {len(files)} files matching UUID/agent pattern")
        print("Parsing usage data...")

    daily_usage, msg_count = parse_jsonl_files(files, tz)

    if not args.quiet:
        print(f"Found {msg_count} unique messages across {len(daily_usage)} days")

    # Build the canonical data structure
    usage_data = build_usage_data(daily_usage, msg_count, tz_label)

    # JSON output mode
    if args.json:
        print(json.dumps(usage_data, indent=2))
        return

    if not args.quiet:
        print("Generating interactive HTML...")

    html = generate_html(usage_data)

    with open(args.output, "w") as f:
        f.write(html)

    if not args.quiet:
        print(f"Saved to {args.output}")

    if not args.no_open:
        subprocess.run(["open", args.output])


if __name__ == "__main__":
    main()
