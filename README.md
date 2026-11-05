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
- **Add a persistent disk** (e.g. mount `/var/data`) and set `DB_PATH=/var/data/creditbridge.db`, otherwise SQLite is wiped on every deploy.
- Optional env: `WORKSPACE_NAME`, `OFFICER_NAME`, `OFFICER_EMAIL`, `MCP_TOKEN`.

## Connect Claude
1. In the app, click **Connect Claude** and copy the URL (`https://<your-host>/mcp`, or `/mcp?token=…` if `MCP_TOKEN` is set).
2. claude.ai → Settings → Connectors → Add custom connector → paste. (Claude Desktop / Claude Code: use the JSON shown in the modal.)
3. The app marks Claude as linked the moment the MCP `initialize` handshake arrives.
4. Ask Claude to underwrite a borrower's statements and call `upload_deal`. It appears under **Deals** as a draft.

MCP tools: `upload_deal`, `list_deals`, `get_deal`.

## API
- `GET /api/state` — workspace, Claude link, deals, audit log (officer UI)
- `PUT /api/deals/{id}` — save officer edits / publish
- `POST /api/audit` — append audit entry
- `POST /api/claude/revoke`
- `GET /api/offer/{id}` — single published deal for the borrower page (counts views)
- `POST /api/offer/{id}/sign` — `{name, title, draw}`; records signer IP and locks the deal
