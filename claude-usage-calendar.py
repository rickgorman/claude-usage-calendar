#!/usr/bin/env python3
"""
Claude Code Token Usage Calendar Generator

Parses Claude Code JSONL session files and generates an HTML calendar
visualization of token usage.

Usage:
    ./claude-usage-calendar.py                    # December 2025, Arizona time
    ./claude-usage-calendar.py --month 11         # November 2025
    ./claude-usage-calendar.py --year 2024        # December 2024
    ./claude-usage-calendar.py --utc              # Use UTC instead of Arizona time
    ./claude-usage-calendar.py --tz-offset -8     # Custom timezone (PST = -8)
    ./claude-usage-calendar.py --no-open          # Don't open in browser
    ./claude-usage-calendar.py --output /tmp/x.html  # Custom output path
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

    return daily_usage, len(message_data)


def format_tokens(n):
    """Format token count as K/M/B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    elif n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def generate_html_calendar(daily_usage, year, month, tz_label):
    """Generate HTML calendar for the specified month."""
    cal = calendar.Calendar(firstweekday=6)
    month_days = list(cal.itermonthdays(year, month))
    month_name = calendar.month_name[month]

    max_total = 0
    for date_key, usage in daily_usage.items():
        if date_key.startswith(f"{year}-{month:02d}"):
            total = sum(usage.values())
            max_total = max(max_total, total)

    if max_total == 0:
        max_total = 1

    weeks = []
    current_week = []
    weekly_totals = []

    monthly_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    for day in month_days:
        current_week.append(day)
        if len(current_week) == 7:
            week_total = 0
            for d in current_week:
                if d != 0:
                    date_key = f"{year}-{month:02d}-{d:02d}"
                    usage = daily_usage.get(date_key, {})
                    week_total += sum(usage.values())
                    for key in monthly_totals:
                        monthly_totals[key] += usage.get(key, 0)
            weeks.append(current_week[:])
            weekly_totals.append(week_total)
            current_week = []

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Code Token Usage - {month_name} {year}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f0c29 0%, #1a1a3e 50%, #24243e 100%);
            min-height: 100vh;
            padding: 40px 20px;
            color: #e0e0e0;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        h1 {{
            text-align: center;
            font-size: 2.5rem;
            font-weight: 700;
            margin-bottom: 30px;
            background: linear-gradient(90deg, #00d4ff, #00ff88, #00d4ff);
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            animation: gradient 3s linear infinite;
        }}

        @keyframes gradient {{
            0% {{ background-position: 0% center; }}
            100% {{ background-position: 200% center; }}
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
            border-radius: 12px;
            padding: 12px;
            min-height: 100px;
            transition: all 0.3s ease;
            border: 1px solid rgba(255, 255, 255, 0.05);
            position: relative;
            overflow: hidden;
        }}

        .day-cell:hover {{
            transform: translateY(-3px);
            box-shadow: 0 10px 30px rgba(0, 212, 255, 0.2);
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

        .day-number {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 8px;
        }}

        .day-total {{
            font-size: 1.3rem;
            font-weight: 700;
            color: #00d4ff;
            margin-bottom: 6px;
        }}

        .day-breakdown {{
            font-size: 0.7rem;
            color: #888;
            line-height: 1.4;
        }}

        .day-breakdown span {{
            display: block;
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

        .monthly-summary {{
            margin-top: 30px;
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.1) 0%, rgba(0, 255, 136, 0.1) 100%);
            border-radius: 16px;
            padding: 25px;
            border: 1px solid rgba(0, 212, 255, 0.2);
        }}

        .monthly-summary h2 {{
            font-size: 1.2rem;
            font-weight: 600;
            color: #fff;
            margin-bottom: 20px;
            text-align: center;
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
    </style>
</head>
<body>
    <div class="container">
        <h1>Claude Code Token Usage - {month_name} {year} ({tz_label})</h1>

        <div class="calendar">
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
"""

    for week_idx, week in enumerate(weeks):
        html += '            <div class="week-row">\n'

        for day in week:
            if day == 0:
                html += '                <div class="day-cell empty"></div>\n'
            else:
                date_key = f"{year}-{month:02d}-{day:02d}"
                usage = daily_usage.get(
                    date_key,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                )

                total = sum(usage.values())
                intensity = min(5, int((total / max_total) * 5)) if total > 0 else 0
                intensity_class = (
                    f"intensity-{intensity}" if intensity > 0 else "intensity-low"
                )

                input_t = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                cache_r = usage.get("cache_read_input_tokens", 0)
                cache_c = usage.get("cache_creation_input_tokens", 0)

                html += f"""                <div class="day-cell {intensity_class}">
                    <div class="day-number">{day}</div>
                    <div class="day-total">{format_tokens(total)}</div>
                    <div class="day-breakdown">
                        <span class="in-label">In: {format_tokens(input_t)}</span>
                        <span class="out-label">Out: {format_tokens(output_t)}</span>
                        <span class="cache-r-label">Cache R: {format_tokens(cache_r)}</span>
                        <span class="cache-c-label">Cache C: {format_tokens(cache_c)}</span>
                    </div>
                </div>
"""

        html += f"""                <div class="week-total">
                    <div class="week-total-label">Week Total</div>
                    <div class="week-total-value">{format_tokens(weekly_totals[week_idx])}</div>
                </div>
            </div>
"""

    grand_total = sum(monthly_totals.values())

    html += f"""
            <div class="monthly-summary">
                <h2>Monthly Summary</h2>
                <div class="summary-grid">
                    <div class="summary-item">
                        <div class="summary-label">Input Tokens</div>
                        <div class="summary-value input">{format_tokens(monthly_totals['input_tokens'])}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Output Tokens</div>
                        <div class="summary-value output">{format_tokens(monthly_totals['output_tokens'])}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Read</div>
                        <div class="summary-value cache-read">{format_tokens(monthly_totals['cache_read_input_tokens'])}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Cache Create</div>
                        <div class="summary-value cache-create">{format_tokens(monthly_totals['cache_creation_input_tokens'])}</div>
                    </div>
                    <div class="summary-item">
                        <div class="summary-label">Grand Total</div>
                        <div class="summary-value total">{format_tokens(grand_total)}</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

    return html


def main():
    parser = argparse.ArgumentParser(
        description="Generate an HTML calendar of Claude Code token usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                          # December 2025, Arizona time
    %(prog)s --month 11 --year 2025   # November 2025
    %(prog)s --utc                    # Use UTC timezone
    %(prog)s --tz-offset -8           # Use PST (UTC-8)
    %(prog)s --no-open                # Generate but don't open browser
    %(prog)s -o ~/report.html         # Custom output path
    %(prog)s -q                       # Quiet mode (no console output)

How it works:
    1. Scans ~/ for Claude Code session files (*.jsonl with UUID/agent patterns)
    2. Parses token usage from each message (input, output, cache read, cache create)
    3. Deduplicates by message ID, taking MAX values (handles streaming updates)
    4. Aggregates by date in the specified timezone
    5. Generates a styled HTML calendar with daily/weekly/monthly breakdowns
    6. Opens the result in your default browser (unless --no-open)

Token types:
    - Input:        Tokens sent to the model (your prompts + context)
    - Output:       Tokens generated by the model (responses)
    - Cache Read:   Tokens read from prompt cache (saves cost)
    - Cache Create: Tokens written to prompt cache

Notes:
    - Arizona time (UTC-7) is the default since Arizona doesn't observe DST
    - Color intensity on calendar cells reflects relative daily usage
    - Hover over day cells for visual highlighting
        """,
    )
    parser.add_argument(
        "--month", "-m", type=int, default=12, help="Month (1-12), default: 12"
    )
    parser.add_argument("--year", "-y", type=int, default=2025, help="Year, default: 2025")
    parser.add_argument(
        "--utc", action="store_true", help="Use UTC instead of Arizona time"
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
        # Default: Arizona time (UTC-7, no DST)
        tz = timezone(timedelta(hours=-7))
        tz_label = "Arizona"

    if not args.quiet:
        print(f"Finding JSONL files in {args.search_path}...")

    files = find_jsonl_files(args.search_path)

    if not args.quiet:
        print(f"Found {len(files)} files matching UUID/agent pattern")
        print("Parsing usage data...")

    daily_usage, msg_count = parse_jsonl_files(files, tz)

    if not args.quiet:
        print(f"Found {msg_count} unique messages across {len(daily_usage)} days")
        print("Generating HTML calendar...")

    html = generate_html_calendar(daily_usage, args.year, args.month, tz_label)

    with open(args.output, "w") as f:
        f.write(html)

    if not args.quiet:
        print(f"Saved to {args.output}")

        # Print summary for the requested month
        print(f"\n--- Daily Usage Summary ({calendar.month_name[args.month]} {args.year}) ---")
        for date in sorted(daily_usage.keys()):
            if date.startswith(f"{args.year}-{args.month:02d}"):
                usage = daily_usage[date]
                total = sum(usage.values())
                print(
                    f"{date}: Total={format_tokens(total):>8}  "
                    f"In={format_tokens(usage['input_tokens']):>8}  "
                    f"Out={format_tokens(usage['output_tokens']):>8}  "
                    f"CacheR={format_tokens(usage['cache_read_input_tokens']):>8}  "
                    f"CacheC={format_tokens(usage['cache_creation_input_tokens']):>8}"
                )

    if not args.no_open:
        subprocess.run(["open", args.output])


if __name__ == "__main__":
    main()
