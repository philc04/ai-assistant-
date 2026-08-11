import os
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.4.1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ATLAS_MODEL = os.getenv("ATLAS_MODEL", "gpt-5.6-terra")
ATLAS_DEEP_MODEL = os.getenv("ATLAS_DEEP_MODEL", "gpt-5.6-sol")
ATLAS_ACCESS_KEY = os.getenv("ATLAS_ACCESS_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ATLAS_ALLOW_WEB = os.getenv("ATLAS_ALLOW_WEB", "true").lower() == "true"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL,
  provider TEXT, model TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS memories (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL, content TEXT NOT NULL, project TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS feedback (
  id BIGSERIAL PRIMARY KEY,
  message_id BIGINT REFERENCES messages(id) ON DELETE CASCADE,
  rating INTEGER, correction TEXT, notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS training_candidates (
  id BIGSERIAL PRIMARY KEY,
  specialist TEXT NOT NULL DEFAULT 'general',
  source_type TEXT NOT NULL,
  input_text TEXT NOT NULL,
  target_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn

def init_db():
    if DATABASE_URL:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()

def require_key(x_atlas_key: str | None = Header(default=None)):
    if not ATLAS_ACCESS_KEY:
        raise HTTPException(503, "ATLAS_ACCESS_KEY is not configured.")
    if x_atlas_key != ATLAS_ACCESS_KEY:
        raise HTTPException(401, "Invalid Atlas access key.")

def history_for(cid: str, limit: int = 20):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY id DESC LIMIT %s", (cid, limit))
            rows = list(cur.fetchall())
    return list(reversed(rows))

def relevant_memories(message: str, limit: int = 6):
    terms = [t.strip(".,!?;:()[]{}").lower() for t in message.split()]
    terms = [t for t in terms if len(t) >= 4][:8]
    if not terms:
        return []
    clauses = " OR ".join(["LOWER(content) LIKE %s"] * len(terms))
    params = [f"%{t}%" for t in terms] + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM memories WHERE ({clauses}) ORDER BY importance DESC, updated_at DESC LIMIT %s", params)
            return list(cur.fetchall())

def extract_text(data: dict) -> str:
    chunks = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()

async def call_openai(model: str, messages: list[dict], instructions: str, use_web: bool):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    payload = {
        "model": model,
        "input": messages,
        "instructions": instructions,
        "store": False,
        "reasoning": {"effort": "medium"},
        "text": {"verbosity": "medium"},
    }
    if use_web:
        if not ATLAS_ALLOW_WEB:
            raise RuntimeError("Web search is disabled.")
        payload["tools"] = [{"type": "web_search"}]
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{OPENAI_BASE_URL.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"OpenAI API error {response.status_code}: {detail}")
    data = response.json()
    text = extract_text(data)
    if not text:
        raise RuntimeError("OpenAI returned no text output.")
    return text

class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    use_web: bool = False
    deep: bool = False

class MemoryRequest(BaseModel):
    content: str = Field(min_length=1)
    kind: str = "knowledge"
    importance: float = Field(default=0.8, ge=0, le=1)

class FeedbackRequest(BaseModel):
    message_id: int
    rating: int | None = Field(default=None, ge=-1, le=1)
    correction: str | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Atlas", version=APP_VERSION, lifespan=lifespan)

@app.get("/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "database": bool(DATABASE_URL),
        "openai": bool(OPENAI_API_KEY),
        "access_key": bool(ATLAS_ACCESS_KEY),
    }

@app.post("/api/chat", dependencies=[Depends(require_key)])
async def chat(req: ChatRequest):
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL is not configured.")
    cid = req.conversation_id or str(uuid4())
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO conversations(id, title) VALUES (%s, %s) ON CONFLICT(id) DO NOTHING", (cid, req.message[:80]))
        conn.commit()
    history = history_for(cid)
    memories = relevant_memories(req.message)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO messages(conversation_id, role, content) VALUES (%s, 'user', %s)", (cid, req.message))
        conn.commit()
    memory_block = "\n".join(f"- [{m['kind']}] {m['content']}" for m in memories) or "(none)"
    instructions = (
        "You are the current reasoning engine used by Atlas Core. "
        "Atlas Core owns identity, memory, permissions, tools, and learning records. "
        "Use provided memories only when relevant. Never invent a memory. "
        "Do not claim an external action completed unless Atlas has a tool result proving it.\n\n"
        f"Relevant Atlas memory:\n{memory_block}"
    )
    model = ATLAS_DEEP_MODEL if req.deep else ATLAS_MODEL
    try:
        answer = await call_openai(model, history + [{"role": "user", "content": req.message}], instructions, req.use_web)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO messages(conversation_id, role, content, provider, model) VALUES (%s, 'assistant', %s, 'openai', %s) RETURNING id", (cid, answer, model))
            message_id = cur.fetchone()["id"]
            cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s", (cid,))
        conn.commit()
    return {"conversation_id": cid, "answer": answer, "model": model, "assistant_message_id": message_id}

@app.post("/api/memory", dependencies=[Depends(require_key)])
def memory(req: MemoryRequest):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO memories(kind, content, importance, confidence, source) VALUES (%s, %s, %s, 1.0, 'user') RETURNING id", (req.kind, req.content, req.importance))
            memory_id = cur.fetchone()["id"]
        conn.commit()
    return {"saved": True, "id": memory_id}

@app.post("/api/feedback", dependencies=[Depends(require_key)])
def feedback(req: FeedbackRequest):
    candidate = False
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT conversation_id FROM messages WHERE id=%s", (req.message_id,))
            msg = cur.fetchone()
            if not msg:
                raise HTTPException(404, "Message not found.")
            cur.execute("INSERT INTO feedback(message_id, rating, correction) VALUES (%s, %s, %s)", (req.message_id, req.rating, req.correction))
            if req.correction and req.correction.strip():
                cur.execute("SELECT content FROM messages WHERE conversation_id=%s AND role='user' AND id < %s ORDER BY id DESC LIMIT 1", (msg["conversation_id"], req.message_id))
                prior = cur.fetchone()
                if prior:
                    cur.execute("INSERT INTO training_candidates(source_type, input_text, target_text) VALUES ('user_correction', %s, %s)", (prior["content"], req.correction.strip()))
                    candidate = True
        conn.commit()
    return {"saved": True, "training_candidate_created": candidate}

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#111"><title>Atlas</title><style>body{margin:0;background:#0d0d0f;color:#eee;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.w{max-width:760px;margin:auto;padding:18px;min-height:100vh}.h{display:flex;justify-content:space-between;align-items:center}.brand{font-size:26px;font-weight:800}.muted{color:#888;font-size:12px}.card{background:#17171a;border:1px solid #2b2b31;border-radius:16px;padding:14px;margin:12px 0}.m{padding:12px;border-radius:14px;margin:10px 0;white-space:pre-wrap;line-height:1.45}.u{background:#303038;margin-left:12%}.a{background:#19191d;border:1px solid #2c2c31;margin-right:7%}textarea,input{width:100%;box-sizing:border-box;background:#101012;color:#fff;border:1px solid #38383e;border-radius:12px;padding:12px;font-size:16px}textarea{min-height:58px}button{border:0;border-radius:11px;padding:11px 13px;font-weight:700}.row{display:flex;gap:8px}.row button{flex:1}.opts{display:flex;gap:16px;padding:10px 0;color:#bbb;font-size:14px}.opts input{width:auto}.small{font-size:12px;color:#999}.fb{display:flex;gap:6px;margin-top:8px}.fb button{padding:7px 9px;background:#2b2b30;color:#ddd}#login{display:none}</style></head><body><div class=w><div class=h><div><div class=brand>Atlas</div><div class=muted>OpenAI now. Atlas Core stays ours.</div></div><button onclick="toggleLogin()">Access</button></div><div id=login class=card><input id=k type=password placeholder="Private Atlas access key"><button style="margin-top:8px" onclick="saveKey()">Save on this phone</button></div><div id=c></div><div class=card><textarea id=p placeholder="Ask Atlas anything..."></textarea><div class=opts><label><input id=web type=checkbox> Web search</label><label><input id=deep type=checkbox> Deep model</label></div><div class=row><button onclick="send()">Send</button><button onclick="teach()">Teach Atlas</button></div><div id=s class=small style="margin-top:8px"></div></div></div><script>let cid=localStorage.getItem('atlas_cid')||null,key=localStorage.getItem('atlas_key')||'';k.value=key;if(!key)login.style.display='block';function toggleLogin(){login.style.display=login.style.display==='block'?'none':'block'}function saveKey(){key=k.value.trim();localStorage.setItem('atlas_key',key);login.style.display='none'}function add(t,who,id){let d=document.createElement('div');d.className='m '+(who==='u'?'u':'a');d.append(document.createTextNode(t));if(who==='a'&&id){let f=document.createElement('div');f.className='fb';let good=document.createElement('button');good.textContent='Useful';good.onclick=()=>rate(id,1,null);let fix=document.createElement('button');fix.textContent='Needs fix';fix.onclick=()=>correct(id,d);f.append(good,fix);d.appendChild(f)}c.appendChild(d);window.scrollTo(0,document.body.scrollHeight)}async function send(){let m=p.value.trim();if(!m)return;if(!key){toggleLogin();return}add(m,'u');p.value='';s.textContent='Atlas is working...';try{let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Key':key},body:JSON.stringify({message:m,conversation_id:cid,use_web:web.checked,deep:deep.checked})});let j=await r.json();if(!r.ok)throw new Error(j.detail||'Failed');cid=j.conversation_id;localStorage.setItem('atlas_cid',cid);add(j.answer,'a',j.assistant_message_id);s.textContent='Model: '+j.model}catch(e){add('Error: '+e.message,'a');s.textContent=''}}async function teach(){if(!key){toggleLogin();return}let x=prompt('What should Atlas remember?');if(!x)return;let r=await fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Key':key},body:JSON.stringify({content:x})});alert(r.ok?'Saved to Atlas memory.':'Could not save it.')}async function rate(id,rating,correction){await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Key':key},body:JSON.stringify({message_id:id,rating,correction})})}function correct(id,parent){let x=prompt('What should Atlas have answered or done instead?');if(x)rate(id,-1,x)}p.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}})</script></body></html>''')
