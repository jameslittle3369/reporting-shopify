# Shopify Metrics Email Report

Queries all key Shopify metrics via the ShopifyQL / Admin GraphQL API, generates a dark-themed HTML email with inline charts, and sends it via AWS SES with a full interactive Chart.js report attached.

---

## What it sends

**Email body** (renders correctly in Outlook, Gmail, iPhone Mail, etc.)
- Summary cards — gross revenue, net revenue, orders, avg order value, sessions, conversion rate, bounce rate
- Daily revenue chart (gross vs net) via QuickChart.io PNG
- Orders per day and conversion rate charts
- Top 10 products by gross sales
- Traffic by device, conversion funnel, and referrer attribution — all as pure HTML bars

**Attached HTML** (`PayLess_Shopify_YYYYMMDD.html`)
- Full interactive Chart.js dashboard — 8 charts with hover tooltips, legends, and trend lines
- Opens in any browser, no internet required after download

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Configure `.env`

```env
# Shopify
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_CLIENT_ID=your_client_id
SHOPIFY_CLIENT_SECRET=your_client_secret
SHOPIFY_ACCESS_TOKEN=          # leave blank; populated automatically by --auth

# AWS SES
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=             # leave blank to use ~/.aws/credentials default profile
AWS_SECRET_ACCESS_KEY=

# Email
FROM_EMAIL=you@example.com
TO_EMAILS=you@example.com,other@example.com

# Report window
REPORT_DAYS=30
```

`SHOPIFY_STORE` must be the `.myshopify.com` subdomain. Find it by logging into Shopify Admin — the URL will be `https://admin.shopify.com/store/your-handle`, and your store is `your-handle.myshopify.com`.

### 3. Register the OAuth redirect URI

In [Shopify Partners](https://partners.shopify.com) → Your App → Configuration → Allowed redirect URLs, add:

```
http://localhost:3456/callback
```

### 4. Authenticate

```bash
python shopify_report.py --auth
```

This opens a browser, completes the OAuth flow, and saves the access token to `.env` automatically. Only needed once (the token is permanent/offline).

### 5. Verify AWS SES

Ensure `FROM_EMAIL` is verified in SES and your AWS credentials have `ses:SendRawEmail` permission. With the default profile in `~/.aws/credentials`, no keys are needed in `.env`.

---

## Usage

```bash
# Send the report
python shopify_report.py

# Save HTML locally and skip sending (good for testing)
python shopify_report.py --preview

# Override report window
python shopify_report.py --days 7

# Override recipients for this run
python shopify_report.py --to ops@example.com ceo@example.com

# Print raw API responses to diagnose data issues
python shopify_report.py --debug

# Re-run OAuth (e.g. if token is revoked)
python shopify_report.py --auth
```

---

## Metrics pulled from Shopify

| Query | Fields |
|---|---|
| Sales (daily) | orders, gross_sales, net_sales, total_sales, average_order_value |
| Sessions (daily) | sessions, online_store_visitors, cart additions, checkout reached, completed, conversion_rate, bounce_rate |
| Top products | gross_sales, net_sales, orders — top 10 by gross |
| Devices | sessions by device type |
| Traffic source | orders, total_sales by referrer source |
| Attribution | sessions by referrer source |
| Customers (daily) | returning_customers, customers, returning_customer_rate |
| 90-day trend (weekly) | orders, gross_sales, net_sales |

---

## Files

```
shopify_report.py   Main script
requirements.txt    Python dependencies
.env                Credentials and settings (not committed)
README.md           This file
```

---

## Troubleshooting

**`ConnectionResetError: 10054`** — Shopify's edge dropped the TLS connection. The script uses a retry session (4 attempts, exponential backoff) and a 400 ms inter-request delay to handle this automatically. If it persists, run `--auth` to refresh the token.

**Charts missing in email** — The email body uses QuickChart.io for time-series charts (PNG images) and pure HTML/CSS bars for categorical data. No SVG or JavaScript is used in the email, so all clients are supported. Make sure outbound HTTPS to `quickchart.io` is allowed.

**Zero values in all metrics** — Run with `--debug` to see the raw API response. Usually caused by a wrong store URL (use the `.myshopify.com` domain, not a custom domain) or a token with insufficient scopes.

**SES send failure** — Confirm `FROM_EMAIL` is SES-verified. In sandbox mode, recipient addresses must also be verified. Check `AWS_REGION` matches where your SES identities are configured.
