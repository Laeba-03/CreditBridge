"""
CreditBridge backend.

- SQLite is the source of truth (deals, audit log, workspace, Claude link state).
- The officer UI (index.html) talks to /api/*.
- Claude connects over MCP (Streamable HTTP) at POST /mcp and pushes deals with
  the `upload_deal` tool. No AI runs inside this app.
"""
import os, json, uuid, sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
# On Render, mount a persistent disk (e.g. /var/data) and set DB_PATH=/var/data/creditbridge.db,
# otherwise the DB is wiped on every deploy.
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "creditbridge.db"))
WORKSPACE = {
    "name": os.environ.get("WORKSPACE_NAME", "Acme Capital"),
    "slug": os.environ.get("WORKSPACE_SLUG", "acme-capital"),
    "officer": os.environ.get("OFFICER_NAME", "Credit Officer"),
    "officerEmail": os.environ.get("OFFICER_EMAIL", "officer@example.com"),
}
# Optional shared secret for the MCP endpoint: set MCP_TOKEN and connect with
# https://<host>/mcp?token=<MCP_TOKEN>. Leave unset to allow any client.
MCP_TOKEN = os.environ.get("MCP_TOKEN")

# ---------------------------------------------------------------- storage
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS deals (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            who TEXT NOT NULL,
            what TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        """)
init_db()

def kv_get(k, default):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def kv_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def stamp():
    """Human stamp used by the UI, e.g. '6 Nov 14:22'."""
    d = datetime.now()
    return f"{d.day} {d.strftime('%b %H:%M')}"

def add_audit(what, who=None):
    with db() as c:
        c.execute("INSERT INTO audit (at, who, what) VALUES (?,?,?)",
                  (stamp(), who or WORKSPACE["officer"], what))

def all_deals():
    with db() as c:
        rows = c.execute("SELECT data FROM deals ORDER BY created_at DESC").fetchall()
    return [json.loads(r["data"]) for r in rows]

def get_deal(deal_id):
    with db() as c:
        r = c.execute("SELECT data FROM deals WHERE id=?", (deal_id,)).fetchone()
    return json.loads(r["data"]) if r else None

def put_deal(deal, new=False):
    now = utcnow()
    with db() as c:
        if new:
            c.execute("INSERT INTO deals VALUES (?,?,?,?,?)",
                      (deal["id"], deal["status"], json.dumps(deal), now, now))
        else:
            c.execute("UPDATE deals SET status=?, data=?, updated_at=? WHERE id=?",
                      (deal["status"], json.dumps(deal), now, deal["id"]))

def audit_rows():
    with db() as c:
        rows = c.execute("SELECT at, who, what FROM audit ORDER BY id DESC LIMIT 500").fetchall()
    return [dict(r) for r in rows]

def claude_state():
    return kv_get("claude", {"linked": False, "linkedAt": None, "client": None, "lastSeen": None})

# ---------------------------------------------------------------- app
app = FastAPI(title="CreditBridge", description="Zero-AI underwriting workflow")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()

@app.get("/health")
def health():
    return {"ok": True, "deals": len(all_deals())}

# ---- officer API --------------------------------------------------------
@app.get("/api/state")
def api_state():
    return {"workspace": WORKSPACE, "claude": claude_state(),
            "deals": all_deals(), "audit": audit_rows()}

@app.put("/api/deals/{deal_id}")
async def api_put_deal(deal_id: str, request: Request):
    body = await request.json()
    if not get_deal(deal_id):
        raise HTTPException(404, "deal not found")
    if body.get("id") != deal_id:
        raise HTTPException(400, "id mismatch")
    put_deal(body)
    return {"ok": True}

@app.delete("/api/deals/{deal_id}")
def api_delete_deal(deal_id: str):
    d = get_deal(deal_id)
    if not d:
        raise HTTPException(404, "deal not found")
    with db() as c:
        c.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    add_audit(f"Deleted deal {d['borrower']}")
    return {"ok": True}

class AuditIn(BaseModel):
    what: str
    who: Optional[str] = None

@app.post("/api/audit")
def api_audit(a: AuditIn):
    add_audit(a.what, a.who)
    return {"ok": True}

@app.post("/api/claude/revoke")
def api_revoke():
    kv_set("claude", {"linked": False, "linkedAt": None, "client": None, "lastSeen": None})
    add_audit("Revoked Claude connection")
    return {"ok": True}

# ---- borrower offer API (only exposes one deal, never the whole book) ----
@app.get("/api/offer/{deal_id}")
def api_offer(deal_id: str):
    d = get_deal(deal_id)
    if not d or not d["offer"].get("published"):
        return {"workspace": WORKSPACE, "deal": None}
    if not d["offer"].get("signed"):
        d["offer"]["views"] = (d["offer"].get("views") or 0) + 1
        d["offer"]["lastView"] = stamp()
        put_deal(d)
    return {"workspace": WORKSPACE, "deal": d}

class SignIn(BaseModel):
    name: str
    title: str = "Director"
    draw: float

@app.post("/api/offer/{deal_id}/sign")
def api_sign(deal_id: str, s: SignIn, request: Request):
    d = get_deal(deal_id)
    if not d or not d["offer"].get("published"):
        raise HTTPException(404, "offer not available")
    if d["offer"].get("signed"):
        raise HTTPException(409, "already signed")
    if d["offer"].get("expires") and d["offer"]["expires"] < datetime.now().timestamp() * 1000:
        raise HTTPException(410, "offer expired")
    limit = d["fields"]["limit"]["value"]
    if not (0 < s.draw <= limit):
        raise HTTPException(400, "draw must be between 0 and the approved limit")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    d["offer"].update({"signed": stamp(), "signer": f"{s.name.strip()}, {s.title.strip() or 'Director'}",
                       "ip": ip, "draw": s.draw})
    d["status"] = "signed"
    put_deal(d)
    add_audit(f"Signed {d['borrower']} facility · drew ₹{int(s.draw):,} · IP {ip}", s.name.strip())
    return {"ok": True, "deal": d}

# ---------------------------------------------------------------- MCP
class Field_(BaseModel):
    value: float
    rationale: str = ""
    src: str = ""

class CovenantIn(BaseModel):
    text: str
    borrower: str = ""          # plain-language version shown to the borrower
    breach: str = "draw freeze"
    on: bool = True

class EvidenceIn(BaseModel):
    t: str
    src: str = ""

class UploadDeal(BaseModel):
    borrower: str
    city: str = ""
    statements: int = Field(6, description="Number of monthly bank statements read")
    requested: float = Field(..., description="Amount requested by the borrower, INR")
    monthly_revenue: float
    revenue_qoq: str = ""
    revenue_series: List[float] = []
    dscr: float
    volatility_pct: float = 0
    volatility_series: List[float] = []
    tier: str = Field(..., description="A, B, C or D")
    confidence: str = "Medium"
    limit: Field_
    apr: Field_
    term_months: Field_
    draw_months: Field_
    covenants: List[CovenantIn] = []
    evidence: List[EvidenceIn] = []
    flagged: List[str] = []
    tampered: bool = False

def _field(f: Field_):
    return {"claude": f.value, "value": f.value, "prov": "claude", "by": None, "at": None,
            "note": "", "rationale": f.rationale, "src": f.src}

def tool_upload_deal(args: Dict[str, Any]):
    u = UploadDeal(**args)
    did = "cb-" + uuid.uuid4().hex[:6]
    deal = {
        "id": did, "borrower": u.borrower, "city": u.city, "stmts": u.statements,
        "ingested": stamp(), "status": "draft",
        "glance": {"revenue": u.monthly_revenue, "revQoQ": u.revenue_qoq,
                   "spark": u.revenue_series or [u.monthly_revenue / 1e5] * 6,
                   "dscr": u.dscr, "vol": u.volatility_pct,
                   "volSpark": u.volatility_series or [1] * 6,
                   "tier": u.tier.upper()[:1], "confidence": u.confidence,
                   "flags": len(u.flagged)},
        "requested": u.requested,
        "fields": {"limit": _field(u.limit), "apr": _field(u.apr),
                   "term": _field(u.term_months), "draw": _field(u.draw_months)},
        "covenants": [{"id": f"c{i+1}", "on": c.on, "prov": "claude", "text": c.text,
                       "borrower": c.borrower or c.text, "breach": c.breach}
                      for i, c in enumerate(u.covenants)],
        "evidence": [e.model_dump() if hasattr(e, "model_dump") else e.dict() for e in u.evidence],
        "flagged": u.flagged,
        "offer": {"published": False, "publishedAt": None, "views": 0, "lastView": None,
                  "expires": None, "signed": None, "signer": None, "draw": None},
        "tampered": u.tampered,
    }
    put_deal(deal, new=True)
    add_audit(f"Claude uploaded deal {u.borrower} (requested ₹{int(u.requested):,}, proposed limit ₹{int(u.limit.value):,})", "Claude")
    return {"id": did, "status": "draft", "review_url": "/#/deals/" + did,
            "message": "Deal created. A credit officer must confirm every value before it can be published."}

def tool_list_deals(_):
    return [{"id": d["id"], "borrower": d["borrower"], "status": d["status"],
             "requested": d["requested"], "proposed_limit": d["fields"]["limit"]["value"],
             "tier": d["glance"]["tier"]} for d in all_deals()]

def tool_get_deal(args):
    d = get_deal(args["id"])
    if not d:
        raise ValueError("deal not found")
    return d

TOOLS = [
    {"name": "upload_deal",
     "description": "Push an underwritten SME credit memo into CreditBridge as a draft deal for human review. "
                    "Call this after you have read the borrower's bank statements and decided limit, APR, term, "
                    "covenants and evidence. All amounts in INR.",
     "inputSchema": UploadDeal.model_json_schema() if hasattr(UploadDeal, "model_json_schema") else UploadDeal.schema()},
    {"name": "list_deals", "description": "List deals in the workspace with their status.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_deal", "description": "Get the full record of one deal, including officer edits and offer status.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]
TOOL_FNS = {"upload_deal": tool_upload_deal, "list_deals": tool_list_deals, "get_deal": tool_get_deal}

def _touch_claude(client=None):
    st = claude_state()
    st["lastSeen"] = stamp()
    if client:
        st.update({"linked": True, "linkedAt": st.get("linkedAt") or stamp(), "client": client})
    kv_set("claude", st)

def _rpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _rpc_error(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}

def handle_rpc(msg: Dict[str, Any]):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        client = (params.get("clientInfo") or {}).get("name") or "Claude"
        was_linked = claude_state()["linked"]
        _touch_claude(client)
        if not was_linked:
            add_audit(f"Linked Claude ({client})")
        return _rpc_result(mid, {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "creditbridge", "version": "2.0.0"}})
    if method == "ping":
        _touch_claude(); return _rpc_result(mid, {})
    if method == "tools/list":
        _touch_claude(); return _rpc_result(mid, {"tools": TOOLS})
    if method == "tools/call":
        _touch_claude()
        name, args = params.get("name"), params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if not fn:
            return _rpc_error(mid, -32602, f"unknown tool {name}")
        try:
            out = fn(args)
            return _rpc_result(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}]})
        except Exception as e:
            return _rpc_result(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    if method and method.startswith("notifications/"):
        return None
    return _rpc_error(mid, -32601, f"method not found: {method}")

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    if MCP_TOKEN and request.query_params.get("token") != MCP_TOKEN \
       and request.headers.get("authorization") != f"Bearer {MCP_TOKEN}":
        raise HTTPException(401, "bad token")
    body = await request.json()
    msgs = body if isinstance(body, list) else [body]
    out = [r for r in (handle_rpc(m) for m in msgs) if r is not None]
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])

@app.get("/mcp")
def mcp_get():
    # Streamable HTTP only; no server-initiated SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})

@app.delete("/mcp")
def mcp_delete():
    return Response(status_code=204)
