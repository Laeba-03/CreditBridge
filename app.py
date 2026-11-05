import os
import sys
import json
import uuid
import sqlite3
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(__file__), "creditbridge.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY,
        business_name TEXT NOT NULL,
        applicant_email TEXT,
        requested_amount REAL NOT NULL,
        recommended_limit REAL NOT NULL,
        approved_limit REAL NOT NULL,
        risk_tier TEXT NOT NULL,
        risk_score INTEGER DEFAULT 75,
        interest_rate REAL DEFAULT 12.0,
        dscr REAL DEFAULT 1.35,
        monthly_revenue REAL DEFAULT 50000.0,
        rationale TEXT NOT NULL,
        key_risks TEXT,
        conditions TEXT,
        status TEXT DEFAULT 'PENDING_REVIEW',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

app = FastAPI(title="CreditBridge", description="Zero-AI Underwriting Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CreditDecisionCreate(BaseModel):
    business_name: str
    applicant_email: Optional[str] = "applicant@example.com"
    requested_amount: float
    recommended_limit: float
    approved_limit: Optional[float] = None
    risk_tier: str = "B+"
    risk_score: Optional[int] = 78
    interest_rate: Optional[float] = 11.5
    dscr: Optional[float] = 1.42
    monthly_revenue: Optional[float] = 65000.0
    rationale: str
    key_risks: Optional[List[str]] = []
    conditions: Optional[List[str]] = []

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/")
def home():
    if os.path.exists("index.html"):
        with open("index.html") as f:
            return HTMLResponse(f.read())
    return {"status": "CreditBridge Backend Live", "dashboard": "/dashboard"}

@app.post("/api/decisions")
def create_decision(data: CreditDecisionCreate, request: Request):
    decision_id = f"cb_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow().isoformat()
    approved = data.approved_limit if data.approved_limit is not None else data.recommended_limit
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        decision_id, data.business_name, data.applicant_email, data.requested_amount,
        data.recommended_limit, approved, data.risk_tier, data.risk_score,
        data.interest_rate, data.dscr, data.monthly_revenue, data.rationale,
        json.dumps(data.key_risks or []), json.dumps(data.conditions or []),
        "PENDING_REVIEW", now, now
    ))
    conn.commit()
    conn.close()
    base_url = str(request.base_url).rstrip("/")
    return {
        "success": True,
        "id": decision_id,
        "offer_url": f"{base_url}/offer/{decision_id}"
    }

@app.get("/api/decisions")
def list_decisions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM decisions ORDER BY created_at DESC").fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results

# MCP SSE Remote Endpoint
@app.get("/sse")
async def mcp_sse(request: Request):
    from fastapi.responses import StreamingResponse
    async def event_generator():
        yield f"event: endpoint\ndata: /messages?session_id=default\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/messages")
async def mcp_messages(request: Request):
    body = await request.json()
    msg_id = body.get("id")
    method = body.get("method")
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "creditbridge", "version": "1.0.0"}
            }
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "create_credit_decision",
                        "description": "Publishes an underwritten commercial credit memo into CreditBridge via Claude. Zero AI inside app.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "business_name": {"type": "string"},
                                "requested_amount": {"type": "number"},
                                "recommended_limit": {"type": "number"},
                                "risk_tier": {"type": "string"},
                                "rationale": {"type": "string"}
                            },
                            "required": ["business_name", "requested_amount", "recommended_limit", "risk_tier", "rationale"]
                        }
                    }
                ]
            }
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
