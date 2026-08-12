"""
Atlas v0.10.0-alpha patch layer.

Purpose:
- Preserve the existing app.py as the stable base.
- Add cross-device server-side Chats.
- Improve technical walkthrough behavior.
- Clean up failed chat turns so broken user-only history is not retained.
- Convert ugly Local/LM Studio transport errors into short user-facing messages.

Deployment:
    uvicorn atlas_v010:app --host 0.0.0.0 --port $PORT

Rollback:
    Switch the Railway start command back to:
    uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import app as base


PATCH_VERSION = "0.10.0-alpha"

# Keep the existing application and database. This file layers changes on top so
# rollback is one Railway start-command change instead of a code archaeology dig.
app = base.app
base.APP_VERSION = PATCH_VERSION
app.version = PATCH_VERSION


# ---------------------------------------------------------------------------
# 1. Technical Guidance Mode
# ---------------------------------------------------------------------------

TECHNICAL_GUIDANCE = """
ATLAS TECHNICAL GUIDANCE MODE:
When the user is actively setting up, debugging, configuring, deploying, or testing something:
- Always have a plan internally, but expose only the next useful action unless the user asks for the whole plan.
- Give exactly one concrete next step at a time.
- Put only exact runnable commands/code in fenced code blocks. Keep explanation outside the code block.
- Briefly explain what the action does and, when useful, what result to expect.
- Wait for the user's actual result before advancing.
- Use known environment and project context. Do not make the user repeat information Atlas already has.
- Do not repeat completed steps.
- If state is uncertain, verify it instead of guessing.
- Catch likely missteps early and explain them plainly.
- Prefer a recommended path over dumping a menu of options.
- Optimize for low cognitive load while still teaching the user what is happening.
""".strip()

_original_call_model = base.call_model


async def patched_call_model(
    provider_choice: str,
    deep: bool,
    use_web: bool,
    messages: list[dict],
    instructions: str,
    allow_openai: bool,
):
    enriched = f"{TECHNICAL_GUIDANCE}\n\n{instructions}"
    return await _original_call_model(
        provider_choice,
        deep,
        use_web,
        messages,
        enriched,
        allow_openai,
    )


base.call_model = patched_call_model


# ---------------------------------------------------------------------------
# 2. Cleaner Local errors
# ---------------------------------------------------------------------------

_original_call_local = base.call_local


def _friendly_local_error(exc: Exception) -> RuntimeError:
    text = str(exc)
    lower = text.lower()

    if isinstance(exc, httpx.TimeoutException) or "timed out" in lower or "timeout" in lower:
        return RuntimeError(
            "Local brain timed out. Make sure the Atlas PC is awake, LM Studio is running, "
            "and the local model is loaded."
        )

    if isinstance(exc, httpx.ConnectError) or "connect" in lower and "error" in lower:
        return RuntimeError(
            "Local brain is unavailable. Make sure the Atlas PC is awake, LM Studio is running, "
            "and Tailscale is connected."
        )

    if "local model api error 400" in lower or "jinja" in lower or "conversation roles must alternate" in lower:
        return RuntimeError(
            "Local brain could not process this chat history. Atlas cleaned the failed turn; "
            "try the message again."
        )

    if "local model api error" in lower:
        return RuntimeError(
            "Local brain returned an error. Check that LM Studio is running and the configured model is loaded."
        )

    return RuntimeError(f"Local brain error: {text[:300]}")


async def patched_call_local(model: str, messages: list[dict], instructions: str):
    try:
        return await _original_call_local(model, messages, instructions)
    except Exception as exc:
        raise _friendly_local_error(exc) from exc


base.call_local = patched_call_local


# ---------------------------------------------------------------------------
# 3. Failed-request history cleanup
# ---------------------------------------------------------------------------

_original_record_usage = base.record_usage


def patched_record_usage(
    workspace_id: str,
    conversation_id: str | None,
    user_message_id: int | None,
    assistant_message_id: int | None,
    provider: str | None,
    model: str | None,
    usage: dict | None,
    deep: bool,
    web: bool,
    semantic_memory: bool,
    memories: list[dict],
    skills: list[tuple[dict, str]],
    latency_ms: int,
    error: str | None = None,
):
    # Keep diagnostics first. We still want a record that the request failed.
    _original_record_usage(
        workspace_id,
        conversation_id,
        user_message_id,
        assistant_message_id,
        provider,
        model,
        usage,
        deep,
        web,
        semantic_memory,
        memories,
        skills,
        latency_ms,
        error,
    )

    # A failed model call currently leaves the inserted user message behind.
    # That creates user -> user history on retry, which stricter local templates
    # reject. Remove only the failed user turn, and remove an empty conversation.
    if error and user_message_id and conversation_id:
        try:
            with base.db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM messages m
                        USING conversations c
                        WHERE m.id=%s
                          AND m.role='user'
                          AND m.conversation_id=c.id
                          AND c.id=%s
                          AND c.workspace_id=%s
                        """,
                        (user_message_id, conversation_id, workspace_id),
                    )
                    cur.execute(
                        """
                        DELETE FROM conversations c
                        WHERE c.id=%s
                          AND c.workspace_id=%s
                          AND NOT EXISTS (
                              SELECT 1 FROM messages m WHERE m.conversation_id=c.id
                          )
                        """,
                        (conversation_id, workspace_id),
                    )
                conn.commit()
        except Exception:
            # Cleanup must never hide the original provider error.
            pass


base.record_usage = patched_record_usage


# ---------------------------------------------------------------------------
# 4. Cross-device Chats API
# ---------------------------------------------------------------------------

class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.get("/api/conversations")
def list_conversations(user: dict = Depends(base.require_user)):
    with base.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at,
                    COUNT(m.id) AS message_count,
                    (
                        SELECT m2.content
                        FROM messages m2
                        WHERE m2.conversation_id=c.id
                        ORDER BY m2.id DESC
                        LIMIT 1
                    ) AS preview
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id=c.id
                WHERE c.workspace_id=%s
                GROUP BY c.id
                HAVING COUNT(m.id) > 0
                ORDER BY c.updated_at DESC, c.created_at DESC
                LIMIT 100
                """,
                (user["id"],),
            )
            rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        row["message_count"] = int(row["message_count"] or 0)
        row["preview"] = (row.get("preview") or "")[:160]
    return {"conversations": rows}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: dict = Depends(base.require_user)):
    with base.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM conversations
                WHERE id=%s AND workspace_id=%s
                """,
                (conversation_id, user["id"]),
            )
            conversation = cur.fetchone()
            if not conversation:
                raise HTTPException(404, "Chat not found.")

            cur.execute(
                """
                SELECT id, role, content, provider, model, created_at
                FROM messages
                WHERE conversation_id=%s
                ORDER BY id ASC
                """,
                (conversation_id,),
            )
            messages = [dict(row) for row in cur.fetchall()]

    return {"conversation": dict(conversation), "messages": messages}


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    req: ConversationRename,
    user: dict = Depends(base.require_user),
):
    title = req.title.strip()
    with base.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE conversations
                SET title=%s, updated_at=NOW()
                WHERE id=%s AND workspace_id=%s
                RETURNING id, title, created_at, updated_at
                """,
                (title, conversation_id, user["id"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Chat not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: dict = Depends(base.require_user)):
    with base.db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM conversations
                WHERE id=%s AND workspace_id=%s
                RETURNING id
                """,
                (conversation_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Chat not found.")
        conn.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 5. Inject Chats UI into the existing Atlas interface
# ---------------------------------------------------------------------------

_original_home = base.home

CHAT_CSS = r"""
.chatTopBar{
    display:grid;
    grid-template-columns:auto minmax(0,1fr) auto;
    gap:8px;
    align-items:center;
    margin-bottom:8px
}
.chatTopTitle{
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
    text-align:center;
    color:var(--muted);
    font-size:13px
}
.chatListRow{
    padding:11px 0;
    border-top:1px solid #292932
}
.chatListRow:first-child{border-top:0}
.chatListTitle{
    font-weight:750;
    color:#d9c5ff;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap
}
.chatListPreview{
    font-size:12px;
    color:var(--muted);
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
    margin-top:3px
}
"""

CHAT_BAR = r"""
<div class="chatTopBar">
  <button onclick="atlasOpenChatList()">Chats</button>
  <div id="currentChatTitle" class="chatTopTitle">New chat</div>
  <button onclick="atlasNewChat()">+ New</button>
</div>
"""

CHAT_SCRIPT = r"""
<script>
const atlasBaseSaveAccess = saveAccess;
const atlasBaseSendMessage = sendMessage;

function atlasSetChatTitle(title){
    const el=document.getElementById("currentChatTitle");
    if(el)el.textContent=(title||"New chat");
}

async function atlasFetchChats(){
    if(!key)return [];
    const data=await api("/api/conversations",{headers:authHeaders(false)});
    return data.conversations||[];
}

async function atlasOpenConversation(id, closeSheet=true){
    if(!key||!id)return;
    const data=await api("/api/conversations/"+encodeURIComponent(id),{headers:authHeaders(false)});
    cid=data.conversation.id;
    localStorage.setItem("atlas_cid",cid);
    chatMessages.innerHTML="";
    for(const m of (data.messages||[])){
        addMessage(
            m.content,
            m.role==="user"?"user":"atlas",
            m.role==="assistant"?m.id:null
        );
    }
    atlasSetChatTitle(data.conversation.title||"Chat");
    if(closeSheet)closeEditor();
    showPage("chat");
    window.scrollTo(0,document.body.scrollHeight);
}

async function atlasSyncConversation(){
    if(!key)return;
    try{
        const chats=await atlasFetchChats();
        if(!chats.length){
            cid=null;
            localStorage.removeItem("atlas_cid");
            chatMessages.innerHTML="";
            atlasSetChatTitle("New chat");
            return;
        }
        const target=chats.find(x=>x.id===cid)||chats[0];
        await atlasOpenConversation(target.id,false);
    }catch(_e){}
}

function atlasNewChat(){
    cid=null;
    localStorage.removeItem("atlas_cid");
    chatMessages.innerHTML="";
    chatStatus.textContent="";
    atlasSetChatTitle("New chat");
    showPage("chat");
    promptBox.focus();
}

async function atlasOpenChatList(){
    if(!key){toggleAccess();return}
    openEditor(
        "Chats",
        '<div class="row" style="margin-bottom:10px"><button class="primary" style="flex:1" onclick="atlasNewChat();closeEditor()">+ New chat</button></div><div id="atlasServerChatList"><div class="small">Loading chats...</div></div>'
    );
    const host=document.getElementById("atlasServerChatList");
    try{
        const chats=await atlasFetchChats();
        host.innerHTML="";
        if(!chats.length){
            host.innerHTML='<div class="empty">No chats yet.</div>';
            return;
        }
        for(const chat of chats){
            const row=document.createElement("div");
            row.className="chatListRow";

            const top=document.createElement("div");
            top.className="spread";

            const text=document.createElement("div");
            text.style.minWidth="0";
            text.style.flex="1";

            const title=document.createElement("div");
            title.className="chatListTitle";
            title.textContent=chat.title||"Untitled chat";

            const preview=document.createElement("div");
            preview.className="chatListPreview";
            preview.textContent=chat.preview||chat.message_count+" messages";

            text.append(title,preview);

            const actions=document.createElement("div");
            actions.className="row";

            const open=document.createElement("button");
            open.textContent="Open";
            open.onclick=()=>atlasOpenConversation(chat.id);

            const more=document.createElement("button");
            more.textContent="•••";
            more.onclick=()=>atlasChatActions(chat.id,chat.title||"Untitled chat");

            actions.append(open,more);
            top.append(text,actions);
            row.append(top);
            host.append(row);
        }
    }catch(e){
        host.innerHTML='<div class="empty">Could not load chats: '+escapeHtml(e.message)+'</div>';
    }
}

async function atlasChatActions(id,currentTitle){
    const choice=prompt('Type "rename" or "delete" for this chat.');
    if(!choice)return;
    const action=choice.trim().toLowerCase();

    if(action==="rename"){
        const title=prompt("New chat name",currentTitle||"");
        if(!title||!title.trim())return;
        try{
            await api("/api/conversations/"+encodeURIComponent(id),{
                method:"PATCH",
                headers:authHeaders(true),
                body:JSON.stringify({title:title.trim()})
            });
            if(cid===id)atlasSetChatTitle(title.trim());
            await atlasOpenChatList();
        }catch(e){alert(e.message)}
        return;
    }

    if(action==="delete"){
        if(!confirm("Delete this chat permanently?"))return;
        try{
            await api("/api/conversations/"+encodeURIComponent(id),{
                method:"DELETE",
                headers:authHeaders(false)
            });
            if(cid===id)atlasNewChat();
            await atlasOpenChatList();
        }catch(e){alert(e.message)}
    }
}

async function atlasRefreshCurrentTitle(){
    if(!cid)return;
    try{
        const data=await api("/api/conversations/"+encodeURIComponent(cid),{headers:authHeaders(false)});
        atlasSetChatTitle(data.conversation.title||"Chat");
    }catch(_e){}
}

saveAccess = async function(){
    await atlasBaseSaveAccess();
    if(key&&session)await atlasSyncConversation();
};

sendMessage = async function(){
    await atlasBaseSendMessage();
    if(cid)await atlasRefreshCurrentTitle();
};

(async()=>{
    if(!key)return;
    try{
        if(!session)await loadSession();
        await atlasSyncConversation();
    }catch(_e){}
})();
</script>
"""


def _patched_html() -> str:
    response = _original_home()
    html = response.body.decode("utf-8")

    if CHAT_CSS not in html:
        html = html.replace("</style>", CHAT_CSS + "\n</style>", 1)

    marker = '<section id="page-chat" class="page active">'
    if CHAT_BAR not in html:
        html = html.replace(marker, marker + "\n" + CHAT_BAR, 1)

    if CHAT_SCRIPT not in html:
        html = html.replace("</body>", CHAT_SCRIPT + "\n</body>", 1)

    # Small visible nudge that this is the new cross-device build.
    html = html.replace(
        '<div id="systemInfo" class="small">Atlas v0.9.0-alpha</div>',
        f'<div id="systemInfo" class="small">Atlas v{PATCH_VERSION}</div>',
        1,
    )
    return html


# Remove only the old GET / page. Everything else from the base application stays.
app.router.routes = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == "/"
        and "GET" in (getattr(route, "methods", set()) or set())
    )
]


@app.get("/", response_class=HTMLResponse)
def patched_home():
    return HTMLResponse(_patched_html())


# Make generated docs reflect the added routes if docs are used.
app.openapi_schema = None
