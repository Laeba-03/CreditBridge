"""
CreditBridge backend.

- Postgres (DATABASE_URL) or SQLite is the source of truth: users, deals, audit log, Claude link state.
- Each user has their own workspace, their own deals, and their own MCP link (/mcp/<token>).
- The officer UI (index.html) talks to /api/*; sessions are an HMAC-signed cookie.
- Claude connects over MCP (Streamable HTTP) at POST /mcp/<token> and pushes deals with
  the `upload_deal` tool. No AI runs inside this app.
"""
import os, json, uuid, sqlite3, hashlib, hmac, secrets, time, re
from datetime import datetime, timezone
from typing import List, Optional, Any, Dict

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "creditbridge.db"))

# Accounts created from the environment on every start. DEMO_* is the reviewer login;
# OWNER_* is yours. Passwords in the environment are always the truth (changing one resets it).
DEMO = {
    "email": os.environ.get("DEMO_EMAIL", "reviewer@creditbridge.demo"),
    "password": os.environ.get("DEMO_PASSWORD", "review-creditbridge"),
    "name": os.environ.get("DEMO_NAME", "Reviewer"),
    "workspace": os.environ.get("DEMO_WORKSPACE", "Acme Capital"),
}
OWNER = {
    "email": os.environ.get("OWNER_EMAIL", ""),
    "password": os.environ.get("OWNER_PASSWORD", ""),
    "name": os.environ.get("OWNER_NAME", "Founder"),
    "workspace": os.environ.get("OWNER_WORKSPACE", "CreditBridge"),
}
SESSION_DAYS = 30
# Self-service signup is off by default (reviewers use the demo login). Set ALLOW_SIGNUP=1 to enable it.
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "").lower() in ("1", "true", "yes")
# One-click guest workspaces (private, throwaway). On by default; set ALLOW_GUEST=0 to disable.
ALLOW_GUEST = os.environ.get("ALLOW_GUEST", "1").lower() in ("1", "true", "yes")
GUEST_DOMAIN = "guest.creditbridge"
GUEST_DAYS = 14   # guest workspaces older than this are removed at startup

# ---------------------------------------------------------------- storage
# Set DATABASE_URL (postgres://...) to use a hosted Postgres (Neon, Supabase, Render).
# Without it, falls back to a local SQLite file (fine for dev, wiped on Render deploys).
DATABASE_URL = os.environ.get("DATABASE_URL")
PG = bool(DATABASE_URL)
if PG:
    import psycopg
    from psycopg.rows import dict_row
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

import threading
_pool: list = []          # idle Postgres connections, reused across requests
_pool_lock = threading.Lock()

def _pg_connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

class DB:
    """Tiny wrapper so the rest of the app uses one SQL dialect ('?' placeholders).
    Postgres connections are pooled: opening a new TLS connection to a hosted DB costs
    100-300 ms, which was most of each page load."""
    def __init__(self):
        if PG:
            with _pool_lock:
                self.c = _pool.pop() if _pool else None
            if self.c is None or self.c.closed:
                self.c = _pg_connect()
        else:
            self.c = sqlite3.connect(DB_PATH); self.c.row_factory = sqlite3.Row
    def execute(self, sql, params=()):
        if PG: sql = sql.replace("?", "%s")
        try:
            return self.c.execute(sql, params)
        except psycopg.OperationalError if PG else sqlite3.OperationalError:
            # Neon drops idle connections when its compute suspends; reconnect once.
            if PG:
                try: self.c.close()
                except Exception: pass
                self.c = _pg_connect()
                return self.c.execute(sql, params)
            raise
    def executescript(self, sql):
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            self.c.execute(stmt)
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        try:
            if et is None: self.c.commit()
            else: self.c.rollback()
        except Exception:
            try: self.c.close()
            except Exception: pass
            return
        if PG and not self.c.closed:
            with _pool_lock:
                if len(_pool) < 5: _pool.append(self.c); return
        self.c.close()

def db():
    return DB()

def init_db():
    if not PG:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    audit_id = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with db() as c:
        c.executescript(f"""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            pw TEXT NOT NULL,
            name TEXT NOT NULL,
            workspace TEXT NOT NULL,
            mcp_token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deals (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id {audit_id},
            at TEXT NOT NULL,
            who TEXT NOT NULL,
            what TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)
        """)
    # Migrations for databases created before accounts existed.
    for table in ("deals", "audit"):
        try:
            with db() as c:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {'IF NOT EXISTS ' if PG else ''}owner TEXT")
        except Exception:
            pass  # SQLite: column already exists
init_db()

def kv_get(k, default):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def kv_set(k, v):
    with db() as c:
        c.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def stamp():
    """Human stamp used by the UI, e.g. '6 Nov 14:22'."""
    d = datetime.now()
    return f"{d.day} {d.strftime('%b %H:%M')}"

# ---------------------------------------------------------------- users & sessions
_SECRET = None
def _secret():
    global _SECRET
    if _SECRET: return _SECRET
    sk = os.environ.get("SECRET_KEY")
    if not sk:
        sk = kv_get("secret", None)
        if not sk:
            sk = secrets.token_hex(32); kv_set("secret", sk)
    _SECRET = sk.encode()
    return _SECRET

def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${h}"

def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(cand, h)

def make_session(uid: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    msg = f"{uid}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"

def read_session(tok: Optional[str]) -> Optional[str]:
    if not tok:
        return None
    try:
        uid, exp, sig = tok.rsplit(".", 2)
    except ValueError:
        return None
    msg = f"{uid}.{exp}"
    if not hmac.compare_digest(hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    if int(exp) < time.time():
        return None
    return uid

def user_by(col: str, val: str):
    with db() as c:
        r = c.execute(f"SELECT * FROM users WHERE {col}=?", (val,)).fetchone()
    return dict(r) if r else None

def create_user(email, password, name, workspace):
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "enter a valid email address")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if user_by("email", email):
        raise HTTPException(409, "an account with that email already exists")
    u = {"id": "u-" + uuid.uuid4().hex[:10], "email": email, "pw": hash_pw(password),
         "name": name.strip() or email.split("@")[0], "workspace": workspace.strip() or "My workspace",
         "mcp_token": secrets.token_urlsafe(24), "created_at": utcnow()}
    with db() as c:
        c.execute("INSERT INTO users (id,email,pw,name,workspace,mcp_token,created_at) VALUES (?,?,?,?,?,?,?)",
                  (u["id"], u["email"], u["pw"], u["name"], u["workspace"], u["mcp_token"], u["created_at"]))
    return u

def is_guest(u):
    return u["email"].endswith("@" + GUEST_DOMAIN)

def public_user(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "workspace": u["workspace"], "guest": is_guest(u)}

def purge_old_guests():
    cutoff = datetime.now(timezone.utc).timestamp() - GUEST_DAYS * 86400
    with db() as c:
        rows = c.execute("SELECT id, created_at FROM users WHERE email LIKE ?", ("%@" + GUEST_DOMAIN,)).fetchall()
        for r in rows:
            try:
                if datetime.fromisoformat(r["created_at"]).timestamp() < cutoff:
                    c.execute("DELETE FROM deals WHERE owner=?", (r["id"],))
                    c.execute("DELETE FROM audit WHERE owner=?", (r["id"],))
                    c.execute("DELETE FROM kv WHERE k=?", ("claude:" + r["id"],))
                    c.execute("DELETE FROM users WHERE id=?", (r["id"],))
            except Exception:
                pass

def workspace_of(u):
    return {"name": u["workspace"], "officer": u["name"], "officerEmail": u["email"]}

def current_user(request: Request):
    uid = read_session(request.cookies.get("cb_session"))
    u = user_by("id", uid) if uid else None
    if not u:
        raise HTTPException(401, "not signed in")
    return u

def set_session_cookie(resp: Response, request: Request, uid: str):
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie("cb_session", make_session(uid), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=secure, path="/")

def seed_account(a):
    """Create an account from the environment, or reset its password to the environment's value."""
    if not a["email"] or not a["password"]:
        return None
    u = user_by("email", a["email"].lower())
    if not u:
        return create_user(a["email"], a["password"], a["name"], a["workspace"])
    if not check_pw(a["password"], u["pw"]):
        with db() as c:
            c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(a["password"]), u["id"]))
    return u

def seed_demo_user():
    """Create the reviewer and owner accounts, and adopt any deals created before accounts existed."""
    seed_account(OWNER)
    u = seed_account(DEMO)
    with db() as c:
        c.execute("UPDATE deals SET owner=? WHERE owner IS NULL", (u["id"],))
        c.execute("UPDATE audit SET owner=? WHERE owner IS NULL", (u["id"],))
    legacy = kv_get("claude", None)
    if legacy and legacy.get("linked") and not kv_get("claude:" + u["id"], None):
        kv_set("claude:" + u["id"], legacy)
seed_demo_user()
purge_old_guests()

# ---------------------------------------------------------------- deals & audit (per owner)
def add_audit(owner: str, what: str, who: str):
    with db() as c:
        c.execute("INSERT INTO audit (at, who, what, owner) VALUES (?,?,?,?)", (stamp(), who, what, owner))

def all_deals(owner: str):
    with db() as c:
        rows = c.execute("SELECT data FROM deals WHERE owner=? ORDER BY created_at DESC", (owner,)).fetchall()
    return [json.loads(r["data"]) for r in rows]

def get_deal(deal_id: str, owner: Optional[str] = None):
    with db() as c:
        if owner is None:
            r = c.execute("SELECT data, owner FROM deals WHERE id=?", (deal_id,)).fetchone()
        else:
            r = c.execute("SELECT data, owner FROM deals WHERE id=? AND owner=?", (deal_id, owner)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"]); d["_owner"] = r["owner"]
    return d

def put_deal(deal, owner: Optional[str] = None, new=False):
    now = utcnow()
    deal = {k: v for k, v in deal.items() if k != "_owner"}
    with db() as c:
        if new:
            c.execute("INSERT INTO deals (id, status, data, created_at, updated_at, owner) VALUES (?,?,?,?,?,?)",
                      (deal["id"], deal["status"], json.dumps(deal), now, now, owner))
        else:
            c.execute("UPDATE deals SET status=?, data=?, updated_at=? WHERE id=?",
                      (deal["status"], json.dumps(deal), now, deal["id"]))

def audit_rows(owner: str):
    with db() as c:
        rows = c.execute("SELECT at, who, what FROM audit WHERE owner=? ORDER BY id DESC LIMIT 500", (owner,)).fetchall()
    return [dict(r) for r in rows]

def claude_state(owner: str):
    return kv_get("claude:" + owner, {"linked": False, "linkedAt": None, "client": None, "lastSeen": None})

def strip_owner(d):
    return {k: v for k, v in d.items() if k != "_owner"}

# ---------------------------------------------------------------- app
app = FastAPI(title="CreditBridge", description="Zero-AI underwriting workflow")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_HTML = None
@app.get("/", response_class=HTMLResponse)
def home():
    global _HTML
    if _HTML is None or os.environ.get("DEV"):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            _HTML = f.read().replace("__CB_CONFIG__", json.dumps({"signup": ALLOW_SIGNUP, "guest": ALLOW_GUEST}))
    return HTMLResponse(_HTML, headers={"Cache-Control": "no-cache"})

@app.get("/health")
def health():
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM deals").fetchone()["n"]
    return {"ok": True, "deals": n}

# ---- auth --------------------------------------------------------------
class SignupIn(BaseModel):
    email: str
    password: str
    name: str = ""
    workspace: str = ""

class LoginIn(BaseModel):
    email: str
    password: str

@app.post("/api/auth/signup")
def api_signup(body: SignupIn, request: Request):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "signup is invite-only; ask for a login")
    u = create_user(body.email, body.password, body.name, body.workspace)
    add_audit(u["id"], f"Created workspace {u['workspace']}", u["name"])
    resp = JSONResponse({"user": public_user(u)})
    set_session_cookie(resp, request, u["id"])
    return resp

@app.post("/api/auth/guest")
def api_guest(request: Request):
    """One click: a private throwaway workspace with its own deals and MCP link."""
    if not ALLOW_GUEST:
        raise HTTPException(403, "guest access is off")
    tag = secrets.token_hex(4)
    u = create_user(f"guest-{tag}@{GUEST_DOMAIN}", secrets.token_urlsafe(16), "Guest reviewer", f"Guest workspace {tag[:4].upper()}")
    add_audit(u["id"], "Started a guest workspace", u["name"])
    resp = JSONResponse({"user": public_user(u)})
    set_session_cookie(resp, request, u["id"])
    return resp

@app.post("/api/auth/login")
def api_login(body: LoginIn, request: Request):
    u = user_by("email", body.email.strip().lower())
    if not u or not check_pw(body.password, u["pw"]):
        raise HTTPException(401, "wrong email or password")
    resp = JSONResponse({"user": public_user(u)})
    set_session_cookie(resp, request, u["id"])
    return resp

@app.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cb_session", path="/")
    return resp

@app.get("/api/me")
def api_me(u=Depends(current_user)):
    return {"user": public_user(u)}

# ---- officer API (always scoped to the signed-in user) ------------------
def mcp_url(request: Request, u):
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/mcp/{u['mcp_token']}"

def workspace_state(owner: str):
    """Deals, audit and Claude state in ONE round-trip (matters when the DB is remote)."""
    if PG:
        sql = """SELECT
          (SELECT COALESCE(json_agg(data::json ORDER BY created_at DESC), '[]'::json) FROM deals WHERE owner=?) AS deals,
          (SELECT COALESCE(json_agg(json_build_object('at',at,'who',who,'what',what) ORDER BY id DESC), '[]'::json)
             FROM (SELECT * FROM audit WHERE owner=? ORDER BY id DESC LIMIT 500) a) AS audit,
          (SELECT v FROM kv WHERE k=?) AS claude"""
    else:
        sql = """SELECT
          (SELECT COALESCE(json_group_array(json(data)), '[]') FROM (SELECT data FROM deals WHERE owner=? ORDER BY created_at DESC)) AS deals,
          (SELECT COALESCE(json_group_array(json_object('at',at,'who',who,'what',what)), '[]')
             FROM (SELECT * FROM audit WHERE owner=? ORDER BY id DESC LIMIT 500)) AS audit,
          (SELECT v FROM kv WHERE k=?) AS claude"""
    with db() as c:
        r = c.execute(sql, (owner, owner, "claude:" + owner)).fetchone()
    deals = r["deals"] if isinstance(r["deals"], list) else json.loads(r["deals"] or "[]")
    audit = r["audit"] if isinstance(r["audit"], list) else json.loads(r["audit"] or "[]")
    claude = json.loads(r["claude"]) if r["claude"] else {"linked": False, "linkedAt": None, "client": None, "lastSeen": None}
    return deals, audit, claude

@app.get("/api/state")
def api_state(request: Request, u=Depends(current_user)):
    deals, audit, claude = workspace_state(u["id"])
    return {"user": public_user(u), "workspace": workspace_of(u),
            "claude": {**claude, "mcpUrl": mcp_url(request, u)},
            "deals": deals, "audit": audit}

@app.put("/api/deals/{deal_id}")
async def api_put_deal(deal_id: str, request: Request, u=Depends(current_user)):
    body = await request.json()
    current = get_deal(deal_id, u["id"])
    if not current:
        raise HTTPException(404, "deal not found")
    if body.get("id") != deal_id:
        raise HTTPException(400, "id mismatch")
    if current["status"] == "signed":
        raise HTTPException(409, "deal is signed and locked")
    if body.get("status") not in ("draft", "live"):
        raise HTTPException(400, "status must be draft or live")
    # Signature data is only ever written by /api/offer/{id}/sign.
    body.setdefault("offer", {})
    for k in ("signed", "signer", "ip", "draw"):
        body["offer"][k] = current["offer"].get(k)
    # Original Claude values are immutable.
    for k, f in current["fields"].items():
        if k in body.get("fields", {}):
            body["fields"][k]["claude"] = f["claude"]
    # A field can only be published once a human has confirmed or edited it.
    if body["status"] == "live":
        pending = [k for k, f in body["fields"].items() if f.get("prov") == "claude"]
        pending += [c["text"] for c in body.get("covenants", []) if c.get("prov") == "claude"]
        if pending or body.get("tampered"):
            raise HTTPException(409, "cannot publish: unreviewed Claude values or tampered flag")
    put_deal(body)
    return {"ok": True}

@app.delete("/api/deals/{deal_id}")
def api_delete_deal(deal_id: str, reason: str = "", u=Depends(current_user)):
    d = get_deal(deal_id, u["id"])
    if not d:
        raise HTTPException(404, "deal not found")
    if d["status"] == "signed":
        raise HTTPException(409, "signed facilities cannot be deleted")
    with db() as c:
        c.execute("DELETE FROM deals WHERE id=? AND owner=?", (deal_id, u["id"]))
    add_audit(u["id"], f"Declined {d['borrower']} (Claude proposed ₹{int(d['fields']['limit']['claude']):,})"
              + (f" · “{reason.strip()}”" if reason.strip() else ""), u["name"])
    return {"ok": True}

class AuditIn(BaseModel):
    what: str
    who: Optional[str] = None

@app.post("/api/audit")
def api_audit(a: AuditIn, u=Depends(current_user)):
    add_audit(u["id"], a.what, a.who or u["name"])
    return {"ok": True}

@app.post("/api/claude/revoke")
def api_revoke(u=Depends(current_user)):
    """Disconnect Claude: reset the link state and rotate the MCP token so the old link stops working."""
    kv_set("claude:" + u["id"], {"linked": False, "linkedAt": None, "client": None, "lastSeen": None})
    with db() as c:
        c.execute("UPDATE users SET mcp_token=? WHERE id=?", (secrets.token_urlsafe(24), u["id"]))
    add_audit(u["id"], "Disconnected Claude and rotated the MCP link", u["name"])
    return {"ok": True}

# ---- borrower offer API (only exposes one deal, never the whole book) ----
@app.get("/api/offer/{deal_id}")
def api_offer(deal_id: str):
    d = get_deal(deal_id)
    if not d or not d["offer"].get("published"):
        return {"workspace": {"name": "CreditBridge", "officer": "", "officerEmail": ""}, "deal": None}
    owner = user_by("id", d["_owner"])
    if not d["offer"].get("signed"):
        d["offer"]["views"] = (d["offer"].get("views") or 0) + 1
        d["offer"]["lastView"] = stamp()
        put_deal(d)
    return {"workspace": workspace_of(owner) if owner else {"name": "CreditBridge", "officer": "", "officerEmail": ""},
            "deal": strip_owner(d)}

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
    add_audit(d["_owner"], f"Signed {d['borrower']} facility · drew ₹{int(s.draw):,} · IP {ip}", s.name.strip())
    return {"ok": True, "deal": strip_owner(d)}

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

def tool_upload_deal(args: Dict[str, Any], owner: str):
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
    put_deal(deal, owner=owner, new=True)
    add_audit(owner, f"Claude uploaded deal {u.borrower} (requested ₹{int(u.requested):,}, proposed limit ₹{int(u.limit.value):,})", "Claude")
    return {"id": did, "status": "draft", "review_url": "/#/deals/" + did,
            "message": "Deal created. A credit officer must confirm every value before it can be published."}

def tool_list_deals(_, owner: str):
    return [{"id": d["id"], "borrower": d["borrower"], "status": d["status"],
             "requested": d["requested"], "proposed_limit": d["fields"]["limit"]["value"],
             "tier": d["glance"]["tier"]} for d in all_deals(owner)]

def tool_get_deal(args, owner: str):
    d = get_deal(args["id"], owner)
    if not d:
        raise ValueError("deal not found")
    return strip_owner(d)

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

def _touch_claude(owner: str, client=None):
    st = claude_state(owner)
    st["lastSeen"] = stamp()
    if client:
        st.update({"linked": True, "linkedAt": st.get("linkedAt") or stamp(), "client": client})
    kv_set("claude:" + owner, st)

def _rpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _rpc_error(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}

def handle_rpc(msg: Dict[str, Any], u: Dict[str, Any]):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    owner = u["id"]
    if method == "initialize":
        client = (params.get("clientInfo") or {}).get("name") or "Claude"
        was_linked = claude_state(owner)["linked"]
        _touch_claude(owner, client)
        if not was_linked:
            add_audit(owner, f"Linked Claude ({client})", u["name"])
        return _rpc_result(mid, {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "creditbridge", "version": "3.0.0"}})
    if method == "ping":
        _touch_claude(owner); return _rpc_result(mid, {})
    if method == "tools/list":
        _touch_claude(owner); return _rpc_result(mid, {"tools": TOOLS})
    if method == "tools/call":
        _touch_claude(owner)
        name, args = params.get("name"), params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if not fn:
            return _rpc_error(mid, -32602, f"unknown tool {name}")
        try:
            out = fn(args, owner)
            return _rpc_result(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}]})
        except Exception as e:
            return _rpc_result(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    if method and method.startswith("notifications/"):
        return None
    return _rpc_error(mid, -32601, f"method not found: {method}")

async def _mcp(request: Request, token: Optional[str]):
    token = token or request.query_params.get("token") or \
        (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    u = user_by("mcp_token", token) if token else None
    if not u:
        raise HTTPException(401, "unknown MCP link; copy a fresh one from Connect Claude in the app")
    body = await request.json()
    msgs = body if isinstance(body, list) else [body]
    out = [r for r in (handle_rpc(m, u) for m in msgs) if r is not None]
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])

@app.post("/mcp/{token}")
async def mcp_endpoint_token(token: str, request: Request):
    return await _mcp(request, token)

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    return await _mcp(request, None)

@app.get("/mcp/{token}")
@app.get("/mcp")
def mcp_get():
    # Streamable HTTP only; no server-initiated SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})

@app.delete("/mcp/{token}")
@app.delete("/mcp")
def mcp_delete():
    return Response(status_code=204)
