# CreditBridge

Human-in-the-loop SME credit workflow. Claude reads bank statements and proposes terms; a credit officer confirms every value before an offer goes live; the borrower signs on a public offer page. **No AI runs inside this app** — Claude pushes deals in over MCP.

## Run locally
```
pip install -r requirements.txt
uvicorn app:app --reload
```
Open http://127.0.0.1:8000.

## Deploy on Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- **Use Postgres**: set `DATABASE_URL` (Render Postgres, Neon or Supabase). Without it SQLite is wiped on every deploy, and the deal a reviewer just created disappears.
- **Keep it warm**: the free tier sleeps after 15 minutes, and Claude's MCP handshake can time out while the dyno wakes. Ping `GET /health` every 10 minutes from a free cron (cron-job.org, UptimeRobot), or open the app URL in a browser before connecting Claude.
- Optional env: `WORKSPACE_NAME`, `OFFICER_NAME`, `OFFICER_EMAIL`, `MCP_TOKEN`.

## Connect Claude
1. In the app, click **Connect Claude** and copy the URL (`https://<your-host>/mcp`, or `/mcp?token=…` if `MCP_TOKEN` is set).
2. claude.ai → Settings → Connectors → Add custom connector → paste. (Claude Desktop / Claude Code: use the JSON shown in the modal.)
3. The app marks Claude as linked the moment the MCP `initialize` handshake arrives.
4. Ask Claude to underwrite a borrower's statements and call `upload_deal`. It appears under **Deals** as a draft.

MCP tools: `upload_deal`, `list_deals`, `get_deal`.

### Try it without bank statements
The empty Deals page has a **Copy prompt** button with a six-month statement summary written inline, so the first `upload_deal` call works without any files. Paste it into Claude as-is:

> Underwrite this SME for a revolving credit line and push it to CreditBridge with upload_deal.
>
> Borrower: Northwind Logistics Pvt Ltd, Mumbai. Requested ₹80,00,000.
> Bank statement summary, last 6 months (HDFC current account):
> - Monthly credits: ₹42 L, ₹47 L, ₹51 L, ₹49 L, ₹58 L, ₹61 L (mostly NEFT from 4 FMCG clients; largest client ~38% of inflows)
> - Monthly debits: ₹39 L, ₹44 L, ₹46 L, ₹47 L, ₹52 L, ₹55 L (fuel, driver salaries, two vehicle EMIs of ₹1.9 L each)
> - Closing balance ranged ₹3.2 L to ₹11.4 L; one cheque return in month 2 (₹1.2 L, re-presented and cleared)
> - Existing debt service ≈ ₹3.8 L / month
>
> Decide limit, APR, term and draw window with a one-line rationale and a source reference for each, propose 2–3 protective covenants in plain language, list evidence lines with page refs, and flag anything that worries you.

Then in the app: open the deal, edit the limit (a reason is required for changes over 10%), confirm the rest, publish, and open the borrower link on a phone to sign.

## What the server enforces
- A deal cannot be published while any value still carries Claude's ✦ provenance, or while Claude's tamper flag is set.
- Claude's original values are immutable; officer edits are stored beside them, never over them.
- Signature fields (`signed`, `signer`, `ip`, `draw`) are written only by `/api/offer/{id}/sign`.
- Signed facilities are locked: `PUT` and `DELETE` return 409.
- Declining a deal requires a reason and is written to the audit log.

## Not in this version (v1 roadmap)
Officer login, per-officer Claude links, source-statement viewer beside the evidence, executed-agreement PDF, Aadhaar eSign / OTP on the borrower page, facility amendments, covenant monitoring.

## API
- `GET /api/state` — workspace, Claude link, deals, audit log (officer UI)
- `PUT /api/deals/{id}` — save officer edits / publish (rejected on signed deals or with unreviewed Claude values)
- `DELETE /api/deals/{id}?reason=…` — decline a draft or live deal (rejected on signed deals)
- `POST /api/audit` — append audit entry
- `POST /api/claude/revoke`
- `GET /api/offer/{id}` — single published deal for the borrower page (counts views)
- `POST /api/offer/{id}/sign` — `{name, title, draw}`; records signer IP and locks the deal
