#!/usr/bin/env python3
"""
Shopify Metrics Email Report
────────────────────────────
Queries Shopify via ShopifyQL (GraphQL Admin API), builds a static summary email
body with inline cards, and attaches a full interactive Chart.js HTML report.
Sends both via AWS SES. Credentials loaded from .env.

Usage:
    python shopify_report.py                  # send email
    python shopify_report.py --preview        # save HTML locally, no email
    python shopify_report.py --auth           # run OAuth flow to get + save access token
"""

import argparse
import base64
import json
import math
import os
import sys
import hashlib
import secrets
import webbrowser
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import boto3
import requests
from dotenv import load_dotenv, set_key

# ─── Load .env from same directory as this script ─────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(_ENV_FILE)

# ============================================================
#  CONFIGURATION  (all values come from .env)
# ============================================================
SHOPIFY_STORE     = os.environ["SHOPIFY_STORE"]
SHOPIFY_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOPIFY_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
SHOPIFY_TOKEN     = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VER   = "2026-01"

AWS_REGION  = os.environ.get("AWS_REGION", "us-east-1")
FROM_EMAIL  = os.environ["FROM_EMAIL"]
TO_EMAILS   = [e.strip() for e in os.environ.get("TO_EMAILS", "").split(",") if e.strip()]

REPORT_DAYS   = int(os.environ.get("REPORT_DAYS", "30"))
REPORT_TITLE  = "Payless Mobile · Shopify Metrics Report"
STORE_NAME    = "Payless Mobile"
CURRENCY      = "USD"
# ============================================================


GQL_ENDPOINT = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VER}/graphql.json"
HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

# Persistent session with retry + backoff — Shopify drops stale keep-alive
# connections between sequential requests; retrying transparently fixes it.
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry as _Retry

_retry = _Retry(
    total=4,
    backoff_factor=0.6,          # waits: 0s, 0.6s, 1.2s, 2.4s
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
    raise_on_status=False,
)
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))


# ─────────────────────────────────────────────────────────────
#  OAuth 2.0 — get access token from client ID + secret
# ─────────────────────────────────────────────────────────────

REDIRECT_URI = "http://localhost:3456/callback"


def run_oauth_flow() -> str:
    """
    OAuth flow for Shopify app.

    FIRST-TIME SETUP — register the redirect URI in Shopify Partners:
      1. Go to https://partners.shopify.com → Apps → your app → Configuration
      2. Under "URLs", add to Allowed redirect URLs:
             http://localhost:3456/callback
      3. Save, then re-run --auth

    How it works:
      - Starts a local server on port 3456
      - Opens the Shopify OAuth consent URL in your browser
      - Shopify redirects back to localhost with an auth code
      - We exchange that code for a permanent offline access token
      - Token is saved to .env automatically
    """
    SCOPES = "read_analytics,read_orders,read_products,read_customers,read_inventory"
    state  = secrets.token_urlsafe(16)

    auth_url = (
        f"https://{SHOPIFY_STORE}/admin/oauth/authorize?"
        + urlencode({
            "client_id":    SHOPIFY_CLIENT_ID,
            "scope":        SCOPES,
            "redirect_uri": REDIRECT_URI,
            "state":        state,
            # omitting grant_options → permanent offline token (no expiry)
        })
    )

    print("\n" + "─" * 60)
    print("SHOPIFY OAUTH SETUP")
    print("─" * 60)
    print(f"\n⚠️  Before this works, register the redirect URI once:")
    print(f"   1. https://partners.shopify.com → Apps → your app → Configuration")
    print(f"   2. Add to 'Allowed redirect URLs':")
    print(f"      {REDIRECT_URI}")
    print(f"   3. Save and re-run:  python shopify_report.py --auth")
    print(f"\n   (Already done? The browser will open now…)")
    print("─" * 60 + "\n")

    captured = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            captured["code"]  = qs.get("code",  [None])[0]
            captured["state"] = qs.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2 style='font-family:sans-serif;padding:40px'>"
                b"&#10003; Auth complete &mdash; you can close this tab.</h2>"
            )

        def log_message(self, *_):
            pass

    server = HTTPServer(("localhost", 3456), CallbackHandler)
    server.timeout = 120  # 2-minute window to complete browser auth

    print(f"🌐 Opening browser …")
    webbrowser.open(auth_url)
    print(f"   Waiting for Shopify to redirect back (2-min timeout) …\n")

    server.handle_request()

    if not captured.get("code"):
        raise ValueError(
            "No auth code received.\n"
            "Make sure http://localhost:3456/callback is registered in Shopify Partners."
        )
    if captured.get("state") != state:
        raise ValueError("OAuth state mismatch — possible CSRF. Aborting.")

    # Exchange code for permanent offline access token
    resp = requests.post(
        f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
        json={
            "client_id":     SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "code":          captured["code"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]

    set_key(str(_ENV_FILE), "SHOPIFY_ACCESS_TOKEN", token)
    print("✅ Access token saved to .env")
    return token


def ensure_token() -> str:
    """Return a valid access token, running OAuth if needed."""
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
    if not token:
        print("ℹ️  No SHOPIFY_ACCESS_TOKEN in .env — starting OAuth flow …")
        token = run_oauth_flow()
        # Update the in-process header
        HEADERS["X-Shopify-Access-Token"] = token
    return token


# ─────────────────────────────────────────────────────────────
#  ShopifyQL helper
# ─────────────────────────────────────────────────────────────

_DEBUG = False  # set by --debug flag
import time as _time
_LAST_REQUEST = 0.0   # timestamp of last API call


def shopifyql(query: str) -> dict:
    """Execute a ShopifyQL query. Returns {columns, rows}.

    Correct Shopify schema (verified via introspection):
      shopifyqlQuery {
        parseErrors          ← String scalar (not an object)
        tableData {
          columns { name dataType }
          rows               ← JSON scalar (array of arrays)
        }
      }
    """
    gql = """
    {
      shopifyqlQuery(query: %s) {
        parseErrors
        tableData {
          columns { name dataType }
          rows
        }
      }
    }
    """ % json.dumps(query)

    global _LAST_REQUEST
    gap = _time.time() - _LAST_REQUEST
    if gap < 0.4:
        _time.sleep(0.4 - gap)   # min 400 ms between Shopify API calls

    r = _SESSION.post(GQL_ENDPOINT, headers=HEADERS, json={"query": gql}, timeout=30)
    _LAST_REQUEST = _time.time()
    r.raise_for_status()
    body = r.json()

    if _DEBUG:
        print(f"\n[DEBUG] Query: {query[:80]}…")
        print(f"[DEBUG] Response: {json.dumps(body)[:600]}")

    if "errors" in body:
        raise ValueError(f"GraphQL error: {body['errors']}")

    node = body.get("data", {}).get("shopifyqlQuery", {})
    parse_errors = node.get("parseErrors")
    if parse_errors:
        raise ValueError(f"ShopifyQL parse error: {parse_errors}")

    td = node.get("tableData") or {}
    columns = [c["name"] for c in (td.get("columns") or [])]
    rows = td.get("rows") or []  # JSON scalar → already parsed by requests

    # Normalize: Shopify returns rows as list-of-dicts; convert to list-of-lists
    # so downstream code can index by integer position via the column index map.
    if rows and isinstance(rows[0], dict):
        rows = [[row.get(col) for col in columns] for row in rows]

    if _DEBUG and rows:
        print(f"[DEBUG] Columns : {columns}")
        print(f"[DEBUG] First row: {rows[0] if rows else 'empty'}")

    return {"columns": columns, "rows": rows}


def to_num(val) -> float:
    """Parse a value to float. Handles formatted strings like '$1,234.56', '4.2%', etc."""
    if val is None or val == "":
        return 0.0
    try:
        # Strip currency symbols, commas, percent signs, whitespace
        import re
        cleaned = re.sub(r"[^\d.\-]", "", str(val))
        return float(cleaned) if cleaned else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────
#  Metric queries
# ─────────────────────────────────────────────────────────────

def fetch_all_metrics(days: int) -> dict:
    since = f"-{days}d"
    data = {}

    print("  → Sales overview …")
    data["sales"] = shopifyql(
        f"FROM sales SHOW orders, gross_sales, net_sales, total_sales, "
        f"average_order_value TIMESERIES day SINCE {since} UNTIL today"
    )

    print("  → Sessions & conversion funnel …")
    data["sessions"] = shopifyql(
        f"FROM sessions SHOW sessions, sessions_with_cart_additions, "
        f"sessions_that_reached_checkout, sessions_that_completed_checkout, "
        f"conversion_rate TIMESERIES day SINCE {since} UNTIL today"
    )

    print("  → Top products …")
    data["products"] = shopifyql(
        "FROM sales SHOW gross_sales, net_sales, orders "
        "GROUP BY product_title ORDER BY gross_sales DESC LIMIT 10"
    )

    print("  → Device breakdown …")
    data["devices"] = shopifyql(
        f"FROM sessions SHOW sessions GROUP BY session_device_type "
        f"SINCE {since} UNTIL today"
    )

    print("  → Traffic sources (order referrer) …")
    data["traffic"] = shopifyql(
        f"FROM sales SHOW orders, total_sales "
        f"GROUP BY order_referrer_source SINCE {since} UNTIL today"
    )

    print("  → Attribution (session referrer source) …")
    data["attribution"] = shopifyql(
        f"FROM sessions SHOW sessions GROUP BY referrer_source "
        f"SINCE {since} UNTIL today"
    )

    print("  → Customer metrics …")
    data["customers"] = shopifyql(
        f"FROM sales SHOW returning_customers, customers, returning_customer_rate "
        f"TIMESERIES day SINCE {since} UNTIL today"
    )

    print("  → 90-day revenue trend …")
    data["trend90"] = shopifyql(
        "FROM sales SHOW orders, gross_sales, net_sales TIMESERIES week SINCE -90d UNTIL today"
    )

    return data


# ─────────────────────────────────────────────────────────────
#  Summary card calculations
# ─────────────────────────────────────────────────────────────

def calc_summary(data: dict) -> dict:
    s = data["sales"]
    cols = s["columns"]
    rows = s["rows"]

    idx = {c: i for i, c in enumerate(cols)}

    total_orders      = sum(to_num(r[idx["orders"]]) for r in rows)
    total_gross       = sum(to_num(r[idx["gross_sales"]]) for r in rows)
    total_net         = sum(to_num(r[idx["net_sales"]]) for r in rows)
    aov_vals          = [to_num(r[idx["average_order_value"]]) for r in rows
                         if to_num(r[idx["orders"]]) > 0]
    avg_aov           = sum(aov_vals) / len(aov_vals) if aov_vals else 0

    sc = data["sessions"]
    sidx = {c: i for i, c in enumerate(sc["columns"])}
    total_sessions    = sum(to_num(r[sidx["sessions"]]) for r in sc["rows"])
    total_completed   = sum(to_num(r[sidx["sessions_that_completed_checkout"]]) for r in sc["rows"])
    conv_rate         = (total_completed / total_sessions * 100) if total_sessions else 0

    prod_rows = data["products"]["rows"]
    top_product       = prod_rows[0][0] if prod_rows else "—"
    top_product_rev   = to_num(prod_rows[0][1]) if prod_rows else 0

    return {
        "total_gross":    total_gross,
        "total_net":      total_net,
        "total_orders":   int(total_orders),
        "avg_aov":        avg_aov,
        "total_sessions": int(total_sessions),
        "conv_rate":      conv_rate,
        "top_product":    top_product,
        "top_product_rev": top_product_rev,
    }


def fmt_money(val: float) -> str:
    return f"${val:,.2f}"

def fmt_pct(val: float) -> str:
    return f"{val:.2f}%"


# ─────────────────────────────────────────────────────────────
#  Interactive HTML report (Chart.js attachment)
# ─────────────────────────────────────────────────────────────

def build_interactive_html(data: dict, summary: dict, report_date: str) -> str:
    s      = data["sales"]
    sc     = data["sessions"]
    prods  = data["products"]
    devs   = data["devices"]
    trend  = data["trend90"]
    cust   = data["customers"]

    # ── sales timeseries ──
    s_idx = {c: i for i, c in enumerate(s["columns"])}
    sale_labels    = [r[s_idx["day"]] for r in s["rows"]]
    sale_gross     = [to_num(r[s_idx["gross_sales"]]) for r in s["rows"]]
    sale_net       = [to_num(r[s_idx["net_sales"]]) for r in s["rows"]]
    sale_orders    = [to_num(r[s_idx["orders"]]) for r in s["rows"]]

    # ── sessions funnel ──
    sc_idx = {c: i for i, c in enumerate(sc["columns"])}
    sess_labels  = [r[sc_idx["day"]] for r in sc["rows"]]
    sess_total   = [to_num(r[sc_idx["sessions"]]) for r in sc["rows"]]
    sess_cart    = [to_num(r[sc_idx["sessions_with_cart_additions"]]) for r in sc["rows"]]
    sess_check   = [to_num(r[sc_idx["sessions_that_reached_checkout"]]) for r in sc["rows"]]
    sess_done    = [to_num(r[sc_idx["sessions_that_completed_checkout"]]) for r in sc["rows"]]
    sess_conv    = [round(to_num(r[sc_idx["conversion_rate"]]) * 100, 2) for r in sc["rows"]]

    # ── products ──
    prod_labels  = [r[0] if r[0] else "(no title)" for r in prods["rows"]]
    prod_gross   = [to_num(r[1]) for r in prods["rows"]]
    prod_orders  = [to_num(r[2]) for r in prods["rows"]] if len(prods["columns"]) > 2 else []

    # ── devices ──
    dev_labels   = [r[0].title() for r in devs["rows"]]
    dev_vals     = [to_num(r[1]) for r in devs["rows"]]

    # ── attribution ──
    attr_rows    = data.get("attribution", {}).get("rows", [])
    attr_labels  = [(r[0].title() if r[0] else "Unknown") for r in attr_rows]
    attr_vals    = [to_num(r[1]) for r in attr_rows]

    # ── 90-day trend ──
    t_idx = {c: i for i, c in enumerate(trend["columns"])}
    trend_labels = [r[t_idx.get("week", t_idx.get("day", 0))] for r in trend["rows"]]
    trend_gross  = [to_num(r[t_idx["gross_sales"]]) for r in trend["rows"]]

    # ── customers ──
    cu_idx = {c: i for i, c in enumerate(cust["columns"])}
    cust_labels  = [r[cu_idx["day"]] for r in cust["rows"]]
    cust_return  = [to_num(r[cu_idx["returning_customers"]]) for r in cust["rows"]]
    cust_total   = [to_num(r[cu_idx["customers"]]) for r in cust["rows"]]

    def jlist(lst):
        return json.dumps(lst)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{REPORT_TITLE}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', system-ui, sans-serif; background: #f5f5f4; color: #111827; font-size: 14px; }}
  header {{ background: #111827; padding: 28px 40px 24px; color: white; }}
  header h1 {{ font-size: 1.25rem; font-weight: 600; letter-spacing: -.01em; }}
  header p  {{ color: rgba(255,255,255,.5); margin-top: 4px; font-size: .8rem; }}
  .container {{ max-width: 1280px; margin: 0 auto; padding: 28px 24px; }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 28px; }}
  .card {{ background: white; border-radius: 10px; padding: 16px 20px; border: 1px solid #e5e7eb; border-left: 3px solid; }}
  .card.c1 {{ border-left-color: #2a78d6; }}
  .card.c2 {{ border-left-color: #1baf7a; }}
  .card.c3 {{ border-left-color: #4a3aa7; }}
  .card.c4 {{ border-left-color: #eda100; }}
  .card.c5 {{ border-left-color: #eb6834; }}
  .card.c6 {{ border-left-color: #e87ba4; }}
  .card.c7 {{ border-left-color: #008300; }}
  .card label {{ font-size: .7rem; font-weight: 500; text-transform: uppercase; letter-spacing: .07em; color: #9ca3af; }}
  .card .val {{ font-size: 1.5rem; font-weight: 600; margin-top: 4px; color: #111827; letter-spacing: -.02em; }}
  .card .sub {{ font-size: .72rem; color: #d1d5db; margin-top: 2px; }}

  .charts-grid {{ display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(540px, 1fr)); }}
  .chart-card {{ background: white; border-radius: 10px; padding: 20px 24px; border: 1px solid #e5e7eb; }}
  .chart-card h3 {{ font-size: .85rem; font-weight: 600; color: #374151; margin-bottom: 4px; letter-spacing: -.01em; }}
  .chart-sub {{ font-size: .75rem; color: #9ca3af; margin-bottom: 14px; }}
  .chart-legend {{ display: flex; gap: 18px; margin-bottom: 14px; flex-wrap: wrap; }}
  .chart-legend span {{ display: flex; align-items: center; gap: 6px; font-size: .75rem; color: #6b7280; }}
  .chart-legend .sq {{ width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }}
  .chart-legend .ln {{ width: 22px; height: 2px; border-radius: 1px; flex-shrink: 0; }}
  .chart-legend .ln-dash {{ width: 22px; height: 0; border-top: 2px dashed; flex-shrink: 0; }}
  .chart-wrap {{ position: relative; height: 260px; }}

  footer {{ text-align: center; padding: 20px; color: #d1d5db; font-size: .75rem; border-top: 1px solid #e5e7eb; margin-top: 28px; }}
</style>
</head>
<body>
<header>
  <h1>{STORE_NAME} · Shopify Metrics</h1>
  <p>{report_date} &nbsp;·&nbsp; Last {REPORT_DAYS} days</p>
</header>

<div class="container">

  <div class="cards">
    <div class="card c1"><label>Gross revenue</label><div class="val">{fmt_money(summary["total_gross"])}</div><div class="sub">Last {REPORT_DAYS} days</div></div>
    <div class="card c2"><label>Net revenue</label><div class="val">{fmt_money(summary["total_net"])}</div><div class="sub">After returns</div></div>
    <div class="card c3"><label>Orders</label><div class="val">{summary["total_orders"]:,}</div><div class="sub">Completed</div></div>
    <div class="card c4"><label>Avg order value</label><div class="val">{fmt_money(summary["avg_aov"])}</div><div class="sub">Per order</div></div>
    <div class="card c5"><label>Sessions</label><div class="val">{summary["total_sessions"]:,}</div><div class="sub">Store visits</div></div>
    <div class="card c6"><label>Conversion rate</label><div class="val">{fmt_pct(summary["conv_rate"])}</div><div class="sub">Sessions → orders</div></div>
    <div class="card c7"><label>Top product</label><div class="val" style="font-size:1rem;line-height:1.35">{summary["top_product"]}</div><div class="sub">{fmt_money(summary["top_product_rev"])}</div></div>
  </div>

  <div class="charts-grid">

    <div class="chart-card" style="grid-column: 1 / -1;">
      <h3>Daily revenue</h3>
      <p class="chart-sub">Gross vs net sales over the last {REPORT_DAYS} days</p>
      <div class="chart-legend">
        <span><span class="ln" style="background:#2a78d6;"></span>Gross sales</span>
        <span><span class="ln-dash" style="border-color:#1baf7a;"></span>Net sales</span>
      </div>
      <div class="chart-wrap" style="height:300px"><canvas id="chartRevenue"></canvas></div>
    </div>

    <div class="chart-card">
      <h3>Orders per day</h3>
      <p class="chart-sub">Daily order count</p>
      <div class="chart-wrap"><canvas id="chartOrders"></canvas></div>
    </div>

    <div class="chart-card">
      <h3>Conversion rate</h3>
      <p class="chart-sub">% of sessions completing checkout</p>
      <div class="chart-wrap"><canvas id="chartConv"></canvas></div>
    </div>

    <div class="chart-card">
      <h3>Sessions funnel</h3>
      <p class="chart-sub">Sessions → cart → checkout → completed</p>
      <div class="chart-legend">
        <span><span class="sq" style="background:rgba(42,120,214,.75);"></span>Sessions</span>
        <span><span class="sq" style="background:rgba(237,161,0,.75);"></span>Cart adds</span>
        <span><span class="sq" style="background:rgba(235,104,52,.75);"></span>Checkout</span>
        <span><span class="sq" style="background:rgba(27,175,122,.9);"></span>Completed</span>
      </div>
      <div class="chart-wrap"><canvas id="chartSessions"></canvas></div>
    </div>

    <div class="chart-card">
      <h3>Top products</h3>
      <p class="chart-sub">Gross sales by product</p>
      <div class="chart-legend">
        <span><span class="sq" style="background:#2a78d6;"></span>Gross sales</span>
        <span><span class="sq" style="background:#eda100;"></span>Orders</span>
      </div>
      <div class="chart-wrap"><canvas id="chartProducts"></canvas></div>
    </div>

    <div class="chart-card">
      <h3>Sessions by device</h3>
      <p class="chart-sub">Traffic breakdown</p>
      <div class="chart-wrap"><canvas id="chartDevices"></canvas></div>
    </div>

    <div class="chart-card" style="grid-column: 1 / -1;">
      <h3>Traffic attribution</h3>
      <p class="chart-sub">Sessions by referrer source — last {REPORT_DAYS} days</p>
      <div class="chart-legend">
        <span><span class="sq" style="background:#eda100;"></span>Sessions</span>
      </div>
      <div class="chart-wrap" style="height:180px"><canvas id="chartAttrib"></canvas></div>
    </div>

    <div class="chart-card" style="grid-column: 1 / -1;">
      <h3>90-day revenue trend</h3>
      <p class="chart-sub">Weekly gross sales</p>
      <div class="chart-wrap" style="height:200px"><canvas id="chartTrend"></canvas></div>
    </div>

  </div>
</div>

<footer>{STORE_NAME} · {report_date} · ShopifyQL + AWS SES</footer>

<script>
const GRID   = '#e5e7eb';
const MUTED  = '#9ca3af';
const COVE   = ['#2a78d6','#eb6834','#1baf7a','#eda100','#e87ba4','#008300','#4a3aa7','#e34948'];

const BASE_OPTS = {{
  responsive: true,
  maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{
    legend: {{ display: false }},
    tooltip: {{ backgroundColor: '#111827', padding: 10, cornerRadius: 6,
                titleFont: {{ size: 12 }}, bodyFont: {{ size: 12 }} }}
  }},
  scales: {{
    x: {{ grid: {{ display: false }}, ticks: {{ color: MUTED, font: {{ size: 11 }}, maxRotation: 45, autoSkip: true, maxTicksLimit: 10 }}, border: {{ color: GRID }} }},
    y: {{ grid: {{ color: GRID, lineWidth: 1 }}, ticks: {{ color: MUTED, font: {{ size: 11 }} }}, border: {{ display: false }} }}
  }}
}};

function mk(id, type, labels, datasets, extra={{}}) {{
  const opts = JSON.parse(JSON.stringify(BASE_OPTS));
  Object.assign(opts, extra);
  if (extra.scales) Object.assign(opts.scales, extra.scales);
  if (extra.plugins) Object.assign(opts.plugins, extra.plugins);
  new Chart(document.getElementById(id), {{ type, data: {{ labels, datasets }}, options: opts }});
}}

// Daily revenue — styled to match the chat preview
const grossData = {jlist(sale_gross)};
const netData   = {jlist(sale_net)};
mk('chartRevenue', 'line', {jlist(sale_labels)}, [
  {{ label: 'Gross sales', data: grossData, borderColor: '#2a78d6', borderWidth: 2,
     backgroundColor: 'rgba(42,120,214,0.08)', fill: true, tension: 0.3,
     pointRadius: grossData.map(v => v > 0 ? 5 : 2),
     pointBackgroundColor: '#2a78d6', pointBorderColor: '#fff', pointBorderWidth: 2 }},
  {{ label: 'Net sales', data: netData, borderColor: '#1baf7a', borderWidth: 2,
     borderDash: [5,4], backgroundColor: 'rgba(27,175,122,0.04)', fill: false, tension: 0.3,
     pointRadius: netData.map(v => v !== 0 ? 5 : 2), pointStyle: 'rect',
     pointBackgroundColor: '#1baf7a', pointBorderColor: '#fff', pointBorderWidth: 2 }}
], {{ scales: {{ y: {{ ...BASE_OPTS.scales.y, ticks: {{ color: MUTED, font: {{ size:11 }}, callback: v => '$'+v }} }} }} }});

// Orders
mk('chartOrders', 'bar', {jlist(sale_labels)},
  [{{ label: 'Orders', data: {jlist(sale_orders)}, backgroundColor: '#4a3aa7', borderRadius: 4, borderSkipped: false }}],
  {{ scales: {{ y: {{ ...BASE_OPTS.scales.y, ticks: {{ color: MUTED, font: {{ size:11 }}, stepSize: 1 }} }} }} }});

// Conversion rate
const convData = {jlist(sess_conv)};
mk('chartConv', 'line', {jlist(sess_labels)},
  [{{ label: 'Conversion %', data: convData, borderColor: '#e87ba4', borderWidth: 2,
     backgroundColor: 'rgba(232,123,164,0.08)', fill: true, tension: 0.4,
     pointRadius: convData.map(v => v > 0 ? 5 : 2),
     pointBackgroundColor: '#e87ba4', pointBorderColor: '#fff', pointBorderWidth: 2 }}],
  {{ scales: {{ y: {{ ...BASE_OPTS.scales.y, ticks: {{ color: MUTED, font: {{ size:11 }}, callback: v => v+'%' }} }} }} }});

// Sessions funnel
mk('chartSessions', 'bar', {jlist(sess_labels)}, [
  {{ label: 'Sessions',   data: {jlist(sess_total)}, backgroundColor: 'rgba(42,120,214,.7)',  borderRadius: 3 }},
  {{ label: 'Cart adds',  data: {jlist(sess_cart)},  backgroundColor: 'rgba(237,161,0,.75)', borderRadius: 3 }},
  {{ label: 'Checkout',   data: {jlist(sess_check)}, backgroundColor: 'rgba(235,104,52,.75)',borderRadius: 3 }},
  {{ label: 'Completed',  data: {jlist(sess_done)},  backgroundColor: 'rgba(27,175,122,.9)', borderRadius: 3 }}
]);

// Top products (horizontal bar)
mk('chartProducts', 'bar', {jlist(prod_labels)}, [
  {{ label: 'Gross sales', data: {jlist(prod_gross)}, backgroundColor: '#2a78d6', borderRadius: 4 }},
  {{ label: 'Orders',      data: {jlist(prod_orders)},backgroundColor: '#eda100', borderRadius: 4 }}
], {{ indexAxis: 'y', scales: {{ x: {{ ...BASE_OPTS.scales.x, grid: {{ color: GRID }} }},
  y: {{ ...BASE_OPTS.scales.y, grid: {{ display: false }} }} }} }});

// Device doughnut
mk('chartDevices', 'doughnut', {jlist(dev_labels)},
  [{{ data: {jlist(dev_vals)}, backgroundColor: COVE, borderWidth: 3, borderColor: '#fff' }}],
  {{ plugins: {{ legend: {{ display: true, position: 'right',
     labels: {{ font: {{ size: 12 }}, color: '#374151', padding: 12, boxWidth: 12, boxHeight: 12 }} }} }} }});

// Attribution horizontal bar
mk('chartAttrib', 'bar', {jlist(attr_labels)},
  [{{ label: 'Sessions', data: {jlist(attr_vals)}, backgroundColor: '#eda100', borderRadius: 4 }}],
  {{ indexAxis: 'y',
     scales: {{
       x: {{ ...BASE_OPTS.scales.x, grid: {{ color: GRID }},
             ticks: {{ color: MUTED, font: {{ size: 11 }} }} }},
       y: {{ ...BASE_OPTS.scales.y, grid: {{ display: false }},
             ticks: {{ color: '#374151', font: {{ size: 12 }} }} }}
     }},
     plugins: {{ ...BASE_OPTS.plugins,
       tooltip: {{ ...BASE_OPTS.plugins.tooltip,
         callbacks: {{ label: ctx => ` Sessions: ${{ctx.parsed.x.toLocaleString()}}` }} }} }}
  }});

// 90-day trend
mk('chartTrend', 'line', {jlist(trend_labels)},
  [{{ label: 'Weekly gross', data: {jlist(trend_gross)}, borderColor: '#2a78d6', borderWidth: 2.5,
     backgroundColor: 'rgba(42,120,214,0.1)', fill: true, tension: 0.4,
     pointRadius: 4, pointBackgroundColor: '#2a78d6', pointBorderColor: '#fff', pointBorderWidth: 2 }}],
  {{ scales: {{ y: {{ ...BASE_OPTS.scales.y, ticks: {{ color: MUTED, font: {{ size:11 }}, callback: v => '$'+v }} }} }} }});
</script>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────────────────────
#  SVG sales chart helper (email-safe — no JS required)
# ─────────────────────────────────────────────────────────────

def build_sales_svg(labels, gross, net=None, width=560, height=210):
    """Generate an inline SVG line chart of daily gross vs net sales."""
    ml, mr, mt, mb = 52, 16, 16, 36
    pw = width - ml - mr
    ph = height - mt - mb

    n = len(labels)
    all_vals = [v for v in gross + (net if net is not None else []) if v > 0]
    raw_max = max(all_vals) if all_vals else 10
    nice = math.ceil(raw_max / 10) * 10 or 10

    def px(i):
        return ml + (i / max(n - 1, 1)) * pw

    def py(v):
        return mt + ph - (max(v, 0) / nice) * ph

    # Gridlines + Y-axis labels
    grid = ""
    for tick in [0, nice // 2, nice]:
        y = py(tick)
        grid += (f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb" stroke-width="1"/>')
        grid += (f'<text x="{ml-8}" y="{y+4:.1f}" text-anchor="end" '
                 f'fill="#9ca3af" font-size="11" font-family="system-ui,sans-serif">'
                 f'${int(tick)}</text>')

    # X-axis labels (every ~5 days)
    xlabels = ""
    step = max(1, n // 6)
    for i in range(0, n, step):
        if i < len(labels):
            lbl = labels[i][5:] if len(labels[i]) >= 7 else labels[i]
            xlabels += (f'<text x="{px(i):.1f}" y="{mt+ph+22}" text-anchor="middle" '
                        f'fill="#9ca3af" font-size="11" font-family="system-ui,sans-serif">{lbl}</text>')

    # Gross area fill path
    area_pts = " ".join(f"L {px(i):.1f},{py(v):.1f}" for i, v in enumerate(gross))
    area_path = (f"M {px(0):.1f},{mt+ph:.1f} {area_pts} "
                 f"L {px(n-1):.1f},{mt+ph:.1f} Z")

    # Polyline points
    gross_pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(gross))
    gross_dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" '
        f'fill="#2a78d6" stroke="white" stroke-width="2"/>'
        for i, v in enumerate(gross) if v > 0
    )

    net_line = net_dots = ""
    if net is not None:
        net_pts  = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(net))
        net_line = f'<polyline points="{net_pts}" fill="none" stroke="#1baf7a" stroke-width="2" stroke-dasharray="5,4"/>'
        net_dots = "".join(
            f'<rect x="{px(i)-3.5:.1f}" y="{py(v)-3.5:.1f}" width="7" height="7" '
            f'fill="#1baf7a" stroke="white" stroke-width="2"/>'
            for i, v in enumerate(net) if v != 0
        )

    # Legend
    if net is not None:
        legend = (
            f'<g transform="translate({ml},{mt-4})">'
            f'<rect x="0" y="-8" width="18" height="3" rx="1" fill="#2a78d6"/>'
            f'<circle cx="9" cy="-6.5" r="3.5" fill="#2a78d6" stroke="white" stroke-width="1.5"/>'
            f'<text x="24" y="-3" fill="#6b7280" font-size="11" font-family="system-ui,sans-serif">Gross</text>'
            f'<line x1="72" y1="-6.5" x2="90" y2="-6.5" stroke="#1baf7a" stroke-width="2" stroke-dasharray="4,3"/>'
            f'<rect x="77" y="-10" width="7" height="7" fill="#1baf7a" stroke="white" stroke-width="1.5"/>'
            f'<text x="96" y="-3" fill="#6b7280" font-size="11" font-family="system-ui,sans-serif">Net</text>'
            f'</g>'
        )
    else:
        legend = ""

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block;max-width:100%;">'
        f'<defs>'
        f'<linearGradient id="areaGrad{width}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="#2a78d6" stop-opacity="0.12"/>'
        f'<stop offset="100%" stop-color="#2a78d6" stop-opacity="0.01"/>'
        f'</linearGradient>'
        f'</defs>'
        f'{grid}'
        f'<path d="{area_path}" fill="url(#areaGrad{width})"/>'
        f'{net_line}'
        f'<polyline points="{gross_pts}" fill="none" stroke="#2a78d6" stroke-width="2.5"/>'
        f'{net_dots}{gross_dots}'
        f'{xlabels}'
        f'{legend}'
        f'</svg>'
    )


def build_hbar_svg(labels, values, color='#2a78d6', width=540, height=None, fmt_val=None):
    """Horizontal bar chart SVG. Height auto-sizes to bars."""
    n = len(labels)
    bar_h, gap = 20, 10
    ml, mr, mt, mb = 110, 50, 10, 10
    if height is None:
        height = mt + mb + n * (bar_h + gap) - gap
    pw = width - ml - mr
    max_val = max(values) if values else 1

    if fmt_val is None:
        fmt_val = lambda v: str(int(v))

    bars = ""
    for i, (lbl, v) in enumerate(zip(labels, values)):
        y   = mt + i * (bar_h + gap)
        bw  = (v / max_val) * pw
        pct = v / sum(values) * 100 if sum(values) else 0
        # Label
        short_lbl = (lbl[:14] + '…') if len(lbl) > 15 else lbl
        bars += (f'<text x="{ml-8}" y="{y+bar_h*.7:.1f}" text-anchor="end" '
                 f'fill="#374151" font-size="12" font-family="system-ui,sans-serif">{short_lbl}</text>')
        # Bar background
        bars += (f'<rect x="{ml}" y="{y}" width="{pw}" height="{bar_h}" '
                 f'fill="#f3f4f6" rx="3"/>')
        # Bar fill
        bars += (f'<rect x="{ml}" y="{y}" width="{max(bw,2):.1f}" height="{bar_h}" '
                 f'fill="{color}" rx="3"/>')
        # Value label
        bars += (f'<text x="{ml+bw+6:.1f}" y="{y+bar_h*.72:.1f}" '
                 f'fill="#6b7280" font-size="11" font-family="system-ui,sans-serif">'
                 f'{fmt_val(v)} ({pct:.0f}%)</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block;max-width:100%;">'
        f'{bars}'
        f'</svg>'
    )


def build_vbar_svg(labels, values, color='#4a3aa7', width=262, height=160, fmt_y=None, tick_fmt=None):
    """Vertical bar chart SVG."""
    ml, mr, mt, mb = 38, 8, 8, 28
    pw = width - ml - mr
    ph = height - mt - mb
    n  = len(labels)
    if n == 0:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"></svg>'

    max_val = max(max(values), 1)
    nice    = max(math.ceil(max_val / 4) * 4, 1)

    def px(i): return ml + (i + 0.5) * (pw / n)
    def py(v): return mt + ph * (1 - max(v, 0) / nice)

    bar_w = max(4, (pw / n) * 0.55)

    # Gridlines
    grid = ""
    for t in [0, nice // 2, nice]:
        y = py(t)
        grid += (f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}" '
                 f'stroke="#e5e7eb" stroke-width="1"/>')
        lbl = tick_fmt(t) if tick_fmt else str(int(t))
        grid += (f'<text x="{ml-5}" y="{y+4:.1f}" text-anchor="end" '
                 f'fill="#9ca3af" font-size="10" font-family="system-ui,sans-serif">{lbl}</text>')

    bars = ""
    for i, (lbl, v) in enumerate(zip(labels, values)):
        bh = ph * (max(v, 0) / nice)
        x  = px(i) - bar_w / 2
        y  = py(v)
        bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}" rx="2"/>'

    # X labels (skip to avoid overlap)
    step = max(1, n // 5)
    xlabels = ""
    for i in range(0, n, step):
        lbl = labels[i][5:] if len(labels[i]) >= 7 else labels[i]
        xlabels += (f'<text x="{px(i):.1f}" y="{mt+ph+18}" text-anchor="middle" '
                    f'fill="#9ca3af" font-size="10" font-family="system-ui,sans-serif">{lbl}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block;max-width:100%;">'
        f'{grid}{bars}{xlabels}'
        f'</svg>'
    )


# ─────────────────────────────────────────────────────────────
#  Email-safe chart helpers
#  SVG is stripped by most email clients. Use:
#    quickchart_img()  → <img> tag via QuickChart.io (time-series)
#    html_hbar()       → pure HTML/CSS table bars (categorical)
# ─────────────────────────────────────────────────────────────

def quickchart_img(cfg: dict, width=540, height=200, alt="Chart") -> str:
    """Return an <img> tag using QuickChart.io to render a Chart.js config.
    Works in all email clients — it's just a PNG image served over HTTPS."""
    import urllib.parse
    cfg_str = json.dumps(cfg, separators=(',', ':'))
    qs = urllib.parse.urlencode({
        'c': cfg_str, 'w': width, 'h': height,
        'bkg': 'white', 'devicePixelRatio': '2',
    })
    url = f'https://quickchart.io/chart?{qs}'
    return (f'<img src="{url}" width="{width}" height="{height}" alt="{alt}" '
            f'style="display:block;max-width:100%;border-radius:6px;">')


def html_hbar(labels, values, color='#2a78d6', fmt_val=None,
              label_color='#374151', track_color='#f3f4f6', val_color='#6b7280') -> str:
    """Pure HTML/CSS horizontal bar chart. Works in all email clients."""
    total  = sum(values) or 1
    max_v  = max(values) if values else 1
    fmt    = fmt_val or (lambda v: f'{int(v):,}')
    rows   = ""
    for lbl, v in zip(labels, values):
        bar_pct   = int(v / max_v * 100)
        total_pct = v / total * 100
        short     = (lbl[:16] + '…') if len(lbl) > 17 else lbl
        rows += (
            f'<tr>'
            f'<td style="width:110px;padding:5px 8px 5px 0;font-size:12px;'
            f'color:{label_color};white-space:nowrap;vertical-align:middle;">{short}</td>'
            f'<td style="padding:5px 0;vertical-align:middle;">'
            f'<table cellpadding="0" cellspacing="0" width="100%"><tr>'
            f'<td style="background:{track_color};border-radius:4px;height:18px;">'
            f'<table cellpadding="0" cellspacing="0" height="18" style="border-radius:4px;">'
            f'<tr><td style="width:{bar_pct}%;background:{color};border-radius:4px;height:18px;"></td>'
            f'<td></td></tr></table></td>'
            f'</tr></table></td>'
            f'<td style="padding:5px 0 5px 8px;font-size:11px;color:{val_color};white-space:nowrap;vertical-align:middle;">'
            f'{fmt(v)} ({total_pct:.0f}%)</td>'
            f'</tr>'
        )
    return f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>'


# ─────────────────────────────────────────────────────────────
#  Static email body (no JS — safe for all mail clients)
# ─────────────────────────────────────────────────────────────

def build_email_body(data: dict, summary: dict, report_date: str) -> str:
    # ── extract all data series ──────────────────────────────────
    s_idx       = {c: i for i, c in enumerate(data["sales"]["columns"])}
    sale_labels = [r[s_idx["day"]] for r in data["sales"]["rows"]]
    sale_gross  = [to_num(r[s_idx["gross_sales"]]) for r in data["sales"]["rows"]]
    sale_net    = [to_num(r[s_idx["net_sales"]]) for r in data["sales"]["rows"]]
    sale_orders = [to_num(r[s_idx["orders"]]) for r in data["sales"]["rows"]]

    sc_idx      = {c: i for i, c in enumerate(data["sessions"]["columns"])}
    sess_conv   = [round(to_num(r[sc_idx["conversion_rate"]]) * 100, 2) for r in data["sessions"]["rows"]]
    sess_labels = [r[sc_idx["day"]] for r in data["sessions"]["rows"]]
    sess_total  = [to_num(r[sc_idx["sessions"]]) for r in data["sessions"]["rows"]]
    sess_done   = [to_num(r[sc_idx["sessions_that_completed_checkout"]]) for r in data["sessions"]["rows"]]

    prod_rows   = data["products"]["rows"]
    prod_labels = [(r[0] or "(no title)") for r in prod_rows]
    prod_gross  = [to_num(r[1]) for r in prod_rows]

    dev_rows    = data["devices"]["rows"]
    dev_labels  = [r[0].title() for r in dev_rows]
    dev_vals    = [to_num(r[1]) for r in dev_rows]

    attr_rows   = data.get("attribution", {}).get("rows", [])
    attr_labels = [(r[0].title() or "Unknown") for r in attr_rows]
    attr_vals   = [to_num(r[1]) for r in attr_rows]

    # Thin date labels — keep only every Nth so the axis isn't crowded
    n = len(sale_labels)
    step = max(1, n // 7)
    thin_labels = [lbl[5:] if len(lbl) >= 10 else lbl  # strip year → MM-DD
                   for i, lbl in enumerate(sale_labels)]

    # ── QuickChart.io image charts (time-series) ─────────────────
    font_defaults = {"family": "sans-serif", "size": 10}

    # Revenue: gross (blue area) + net (green dashed line)
    img_revenue = quickchart_img({
        "type": "line",
        "data": {
            "labels": thin_labels,
            "datasets": [
                {
                    "label": "Gross",
                    "data": sale_gross,
                    "borderColor": "#2a78d6",
                    "backgroundColor": "rgba(42,120,214,0.15)",
                    "fill": True,
                    "tension": 0.35,
                    "pointRadius": 0,
                    "borderWidth": 2,
                },
                {
                    "label": "Net",
                    "data": sale_net,
                    "borderColor": "#1baf7a",
                    "backgroundColor": "transparent",
                    "fill": False,
                    "borderDash": [5, 4],
                    "tension": 0.35,
                    "pointRadius": 0,
                    "borderWidth": 2,
                },
            ],
        },
        "options": {
            "plugins": {
                "legend": {"display": True, "labels": font_defaults},
            },
            "scales": {
                "x": {"ticks": {"font": font_defaults, "maxTicksLimit": 8, "maxRotation": 0}},
                "y": {"ticks": {"font": font_defaults}},
            },
        },
    }, width=540, height=200, alt="Daily Revenue")

    # Orders per day: vertical bar
    img_orders = quickchart_img({
        "type": "bar",
        "data": {
            "labels": thin_labels,
            "datasets": [{
                "label": "Orders",
                "data": sale_orders,
                "backgroundColor": "#4a3aa7",
                "borderRadius": 3,
            }],
        },
        "options": {
            "plugins": {"legend": {"display": False}},
            "scales": {
                "x": {"ticks": {"font": font_defaults, "maxTicksLimit": 7, "maxRotation": 0}},
                "y": {"ticks": {"font": font_defaults}},
            },
        },
    }, width=260, height=160, alt="Orders per Day")

    # Conversion rate: line
    img_conv = quickchart_img({
        "type": "line",
        "data": {
            "labels": [lbl[5:] if len(lbl) >= 10 else lbl for lbl in sess_labels],
            "datasets": [{
                "label": "Conv %",
                "data": sess_conv,
                "borderColor": "#eb6834",
                "backgroundColor": "rgba(235,104,52,0.12)",
                "fill": True,
                "tension": 0.35,
                "pointRadius": 0,
                "borderWidth": 2,
            }],
        },
        "options": {
            "plugins": {"legend": {"display": False}},
            "scales": {
                "x": {"ticks": {"font": font_defaults, "maxTicksLimit": 7, "maxRotation": 0}},
                "y": {"ticks": {"font": font_defaults}},
            },
        },
    }, width=260, height=160, alt="Conversion Rate")

    # ── HTML/CSS bar charts (categorical — no external requests) ──
    funnel_labels = ['Sessions', 'Cart adds', 'Checkout', 'Completed']
    funnel_vals   = [
        sum(sess_total),
        sum(to_num(r[sc_idx["sessions_with_cart_additions"]]) for r in data["sessions"]["rows"]),
        sum(to_num(r[sc_idx["sessions_that_reached_checkout"]]) for r in data["sessions"]["rows"]),
        sum(sess_done),
    ]

    # Dark theme colors
    DK_PAGE   = "#0f172a"   # outermost background
    DK_CARD   = "#1e293b"   # chart card background
    DK_INSET  = "#0f172a"   # summary card background
    DK_BORDER = "#334155"   # borders
    DK_TEXT   = "#f1f5f9"   # primary text
    DK_MUTED  = "#94a3b8"   # secondary / label text
    DK_TRACK  = "#334155"   # bar chart empty track
    DK_CHART  = "#1e293b"   # QuickChart background

    bar_products    = html_hbar(prod_labels,   prod_gross,   color='#2a78d6', fmt_val=fmt_money,
                                label_color=DK_TEXT, track_color=DK_TRACK, val_color=DK_MUTED)
    bar_devices     = html_hbar(dev_labels,    dev_vals,     color='#1baf7a',
                                label_color=DK_TEXT, track_color=DK_TRACK, val_color=DK_MUTED)
    bar_funnel      = html_hbar(funnel_labels, funnel_vals,  color='#eb6834',
                                label_color=DK_TEXT, track_color=DK_TRACK, val_color=DK_MUTED)
    bar_attribution = html_hbar(attr_labels,   attr_vals,    color='#eda100',
                                label_color=DK_TEXT, track_color=DK_TRACK, val_color=DK_MUTED) if attr_rows else ""

    # Re-render QuickChart images with dark background + light axis labels
    import urllib.parse as _up
    def _qc_dark(cfg, width, height, alt):
        cfg["options"].setdefault("plugins", {}).setdefault("legend", {}).setdefault("labels", {})["color"] = DK_TEXT
        for ax in ("x", "y"):
            cfg["options"].setdefault("scales", {}).setdefault(ax, {}).setdefault("ticks", {})["color"] = DK_MUTED
            cfg["options"]["scales"][ax].setdefault("grid", {})["color"] = "rgba(148,163,184,0.12)"
        qs = _up.urlencode({'c': json.dumps(cfg, separators=(',',':')),
                            'w': width, 'h': height,
                            'bkg': DK_CHART, 'devicePixelRatio': '2'})
        return (f'<img src="https://quickchart.io/chart?{qs}" width="{width}" height="{height}" '
                f'alt="{alt}" style="display:block;max-width:100%;border-radius:6px;">')

    img_revenue = _qc_dark({
        "type": "line",
        "data": {
            "labels": thin_labels,
            "datasets": [
                {"label": "Gross", "data": sale_gross, "borderColor": "#2a78d6",
                 "backgroundColor": "rgba(42,120,214,0.25)", "fill": True,
                 "tension": 0.35, "pointRadius": 0, "borderWidth": 2},
                {"label": "Net",   "data": sale_net,   "borderColor": "#1baf7a",
                 "backgroundColor": "transparent", "fill": False,
                 "borderDash": [5,4], "tension": 0.35, "pointRadius": 0, "borderWidth": 2},
            ],
        },
        "options": {"plugins": {"legend": {"display": True}}, "scales": {
            "x": {"ticks": {"maxTicksLimit": 8, "maxRotation": 0}},
            "y": {"ticks": {}},
        }},
    }, 540, 200, "Daily Revenue")

    img_orders = _qc_dark({
        "type": "bar",
        "data": {"labels": thin_labels, "datasets": [
            {"label": "Orders", "data": sale_orders,
             "backgroundColor": "#4a3aa7", "borderRadius": 3},
        ]},
        "options": {"plugins": {"legend": {"display": False}}, "scales": {
            "x": {"ticks": {"maxTicksLimit": 7, "maxRotation": 0}}, "y": {"ticks": {}},
        }},
    }, 260, 160, "Orders per Day")

    img_conv = _qc_dark({
        "type": "line",
        "data": {"labels": [lbl[5:] if len(lbl) >= 10 else lbl for lbl in sess_labels],
                 "datasets": [{"label": "Conv %", "data": sess_conv,
                               "borderColor": "#eb6834", "backgroundColor": "rgba(235,104,52,0.2)",
                               "fill": True, "tension": 0.35, "pointRadius": 0, "borderWidth": 2}]},
        "options": {"plugins": {"legend": {"display": False}}, "scales": {
            "x": {"ticks": {"maxTicksLimit": 7, "maxRotation": 0}}, "y": {"ticks": {}},
        }},
    }, 260, 160, "Conversion Rate")

    # ── summary cards ────────────────────────────────────────────
    CS = (f"display:inline-block;min-width:130px;margin:5px;padding:14px 18px;"
          f"background:{DK_INSET};border-radius:8px;border-left:3px solid {{c}};vertical-align:top;")
    metrics = [
        ("#2a78d6", "Gross revenue",   fmt_money(summary["total_gross"])),
        ("#1baf7a", "Net revenue",     fmt_money(summary["total_net"])),
        ("#4a3aa7", "Orders",          f"{summary['total_orders']:,}"),
        ("#eda100", "Avg order value", fmt_money(summary["avg_aov"])),
        ("#eb6834", "Sessions",        f"{summary['total_sessions']:,}"),
        ("#e87ba4", "Conversion rate", fmt_pct(summary["conv_rate"])),
    ]
    cards_html = '<div style="margin:0 -5px;">'
    for clr, lbl, val in metrics:
        cards_html += (f'<div style="{CS.format(c=clr)}">'
                       f'<div style="font-size:10px;font-weight:600;text-transform:uppercase;'
                       f'letter-spacing:.07em;color:{DK_MUTED};">{lbl}</div>'
                       f'<div style="font-size:22px;font-weight:600;color:{DK_TEXT};margin-top:3px;'
                       f'letter-spacing:-.01em;">{val}</div>'
                       f'</div>')
    cards_html += "</div>"

    # ── chart card wrapper helper ─────────────────────────────────
    def cc(title, subtitle, content):
        return (f'<div style="background:{DK_CARD};border-radius:8px;border:1px solid {DK_BORDER};'
                f'padding:14px 12px 12px;">'
                f'<div style="font-size:12px;font-weight:600;color:{DK_TEXT};margin-bottom:2px;">{title}</div>'
                f'<div style="font-size:11px;color:{DK_MUTED};margin-bottom:10px;">{subtitle}</div>'
                f'{content}</div>')

    # 2-col table helper
    def two_col(left, right):
        return (f'<table width="100%" cellpadding="0" cellspacing="0">'
                f'<tr><td width="49%" valign="top">{left}</td>'
                f'<td width="2%"></td>'
                f'<td width="49%" valign="top">{right}</td></tr>'
                f'</table>')

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:{DK_PAGE};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}}
</style>
</head>
<body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:{DK_PAGE};padding:24px 12px;">
<tr><td>
<table width="600" cellpadding="0" cellspacing="0" align="center" style="max-width:600px;width:100%;">

  <!-- Header -->
  <tr><td style="background:#0f172a;border-radius:10px 10px 0 0;padding:24px 28px;border-bottom:1px solid {DK_BORDER};">
    <div style="color:{DK_TEXT};font-size:18px;font-weight:600;letter-spacing:-.01em;">{STORE_NAME} · Shopify Metrics</div>
    <div style="color:{DK_MUTED};margin-top:4px;font-size:12px;">{report_date} &nbsp;·&nbsp; Last {REPORT_DAYS} days</div>
  </td></tr>

  <tr><td style="background:{DK_PAGE};padding:24px 20px;">

    <!-- Summary cards -->
    {cards_html}

    <!-- Revenue chart -->
    <div style="margin-top:24px;"></div>
    {cc("Daily revenue", "Gross (blue area) vs net (green dashed) &mdash; last " + str(REPORT_DAYS) + " days", img_revenue)}

    <!-- Orders + Conversion rate -->
    <div style="margin-top:16px;"></div>
    {two_col(
        cc("Orders per day", "Daily completed orders", img_orders),
        cc("Conversion rate", "% of sessions completing checkout", img_conv)
    )}

    <!-- Top products -->
    <div style="margin-top:16px;"></div>
    {cc("Top products", "Gross sales by product", bar_products)}

    <!-- Devices + Sessions funnel -->
    <div style="margin-top:16px;"></div>
    {two_col(
        cc("Traffic by device", "Sessions by device type", bar_devices),
        cc("Conversion funnel", "Sessions &rarr; cart &rarr; checkout &rarr; done", bar_funnel)
    )}

    <!-- Attribution -->
    {"<div style='margin-top:16px;'></div>" + cc("Traffic attribution", "Sessions by referrer source", bar_attribution) if bar_attribution else ""}

  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#0f172a;border-radius:0 0 10px 10px;padding:14px 20px;text-align:center;border-top:1px solid {DK_BORDER};">
    <p style="margin:0;font-size:11px;color:{DK_MUTED};">Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &nbsp;&bull;&nbsp; Shopify &nbsp;&bull;&nbsp; Last {REPORT_DAYS} days &nbsp;&bull;&nbsp; See attached <em>PayLess_Shopify_{datetime.now().strftime("%Y%m%d")}.html</em> for interactive version</p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""


# ─────────────────────────────────────────────────────────────
#  AWS SES sender
# ─────────────────────────────────────────────────────────────

def send_via_ses(body_html: str, attachment_html: str, report_date: str, recipients=None):
    if recipients is None:
        recipients = TO_EMAILS
    subject = f"{STORE_NAME} Shopify Report · {report_date}"

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"James Little <{FROM_EMAIL}>"
    msg["To"]      = ", ".join(recipients)

    # HTML body part
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("Please view this email in an HTML-capable client.", "plain"))
    alt.attach(MIMEText(body_html, "html"))
    msg.attach(alt)

    # Interactive HTML attachment
    filename = f"PayLess_Shopify_{datetime.now().strftime('%Y%m%d')}.html"
    attach = MIMEBase("text", "html")
    attach.set_payload(attachment_html.encode("utf-8"))
    encoders.encode_base64(attach)
    attach.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attach)

    ses = boto3.client("ses", region_name=AWS_REGION)
    resp = ses.send_raw_email(
        Source=FROM_EMAIL,
        Destinations=recipients,
        RawMessage={"Data": msg.as_bytes()},
    )
    return resp["MessageId"]


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shopify metrics email report")
    parser.add_argument("--preview", action="store_true",
                        help="Save HTML locally; skip sending email")
    parser.add_argument("--auth", action="store_true",
                        help="Run OAuth flow to obtain + save access token, then exit")
    parser.add_argument("--days", type=int, default=REPORT_DAYS,
                        help=f"Days to report on (default: {REPORT_DAYS})")
    parser.add_argument("--to",    nargs="+", help="Override recipient email(s)")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw API responses to diagnose zero-value issues")
    args = parser.parse_args()

    global _DEBUG
    _DEBUG = args.debug

    # OAuth-only mode
    if args.auth:
        token = run_oauth_flow()
        print(f"Token: {token[:8]}…  (full value saved to .env)")
        return

    days = args.days
    recipients = args.to if args.to else TO_EMAILS

    report_date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    out_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"\n🛍️  {STORE_NAME} · Shopify Report")
    print(f"   Store : {SHOPIFY_STORE}")
    print(f"   Period: last {days} days\n")

    # Ensure we have a valid token (runs OAuth if SHOPIFY_ACCESS_TOKEN is blank)
    ensure_token()

    print("📡 Fetching metrics from Shopify …")
    data = fetch_all_metrics(days)

    print("\n📊 Calculating summary …")
    summary = calc_summary(data)

    print(f"   Gross Revenue  : {fmt_money(summary['total_gross'])}")
    print(f"   Net Revenue    : {fmt_money(summary['total_net'])}")
    print(f"   Orders         : {summary['total_orders']}")
    print(f"   Avg Order Value: {fmt_money(summary['avg_aov'])}")
    print(f"   Sessions       : {summary['total_sessions']:,}")
    print(f"   Conversion Rate: {fmt_pct(summary['conv_rate'])}")

    print("\n🎨 Building HTML reports …")
    body_html       = build_email_body(data, summary, report_date)
    interactive_html = build_interactive_html(data, summary, report_date)

    # Always save the interactive HTML locally
    html_path = os.path.join(out_dir, f"shopify_report_{datetime.now().strftime('%Y%m%d')}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(interactive_html)
    print(f"   Saved: {html_path}")

    if args.preview:
        print("\n✅ Preview mode — email not sent.")
        print(f"   Open {html_path} in your browser.")
        return

    print(f"\n📧 Sending via AWS SES ({AWS_REGION}) …")
    print(f"   From : {FROM_EMAIL}")
    print(f"   To   : {', '.join(recipients)}")

    try:
        msg_id = send_via_ses(body_html, interactive_html, report_date, recipients)
        print(f"\n✅ Sent! MessageId: {msg_id}")
    except Exception as e:
        print(f"\n❌ SES error: {e}")
        print("   Check that FROM_EMAIL is SES-verified and your AWS credentials are configured.")
        sys.exit(1)


if __name__ == "__main__":
    main()
