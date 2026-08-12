import os
import re
import json
import math
import hashlib
import secrets
import time
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.9.1-alpha"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ATLAS_MODEL = os.getenv("ATLAS_MODEL", "gpt-5-mini")
ATLAS_DEEP_MODEL = os.getenv("ATLAS_DEEP_MODEL", ATLAS_MODEL)

ATLAS_DEFAULT_PROVIDER = os.getenv("ATLAS_DEFAULT_PROVIDER", "openai").strip().lower()
ATLAS_LOCAL_BASE_URL = os.getenv("ATLAS_LOCAL_BASE_URL", "").strip()
ATLAS_LOCAL_API_KEY = os.getenv("ATLAS_LOCAL_API_KEY", "").strip()
ATLAS_LOCAL_MODEL = os.getenv("ATLAS_LOCAL_MODEL", "local-model").strip()
ATLAS_LOCAL_DEEP_MODEL = os.getenv("ATLAS_LOCAL_DEEP_MODEL", ATLAS_LOCAL_MODEL).strip()

ATLAS_ACCESS_KEY = os.getenv("ATLAS_ACCESS_KEY", "")
ATLAS_OWNER_NAME = os.getenv("ATLAS_OWNER_NAME", "Owner").strip() or "Owner"
# Optional legacy bootstrap for one tester. New testers can be created in the Admin page.
ATLAS_FRIEND_ACCESS_KEY = os.getenv("ATLAS_FRIEND_ACCESS_KEY", "").strip()
ATLAS_FRIEND_NAME = os.getenv("ATLAS_FRIEND_NAME", "Friend").strip() or "Friend"
ATLAS_FRIEND_BUDGET_USD = float(os.getenv("ATLAS_FRIEND_BUDGET_USD", "10"))

DATABASE_URL = os.getenv("DATABASE_URL", "")
ATLAS_ALLOW_WEB = os.getenv("ATLAS_ALLOW_WEB", "true").lower() == "true"

ATLAS_OPENAI_INPUT_USD_PER_M = float(os.getenv("ATLAS_OPENAI_INPUT_USD_PER_M", "0.25"))
ATLAS_OPENAI_OUTPUT_USD_PER_M = float(os.getenv("ATLAS_OPENAI_OUTPUT_USD_PER_M", "2.00"))
ATLAS_WEB_SEARCH_USD_PER_CALL = float(os.getenv("ATLAS_WEB_SEARCH_USD_PER_CALL", "0.01"))

ATLAS_EMBED_BASE_URL = os.getenv("ATLAS_EMBED_BASE_URL", OPENAI_BASE_URL).strip()
ATLAS_EMBED_API_KEY = os.getenv("ATLAS_EMBED_API_KEY", OPENAI_API_KEY).strip()
ATLAS_EMBED_MODEL = os.getenv("ATLAS_EMBED_MODEL", "text-embedding-3-small").strip()
ATLAS_SEMANTIC_MEMORY = os.getenv("ATLAS_SEMANTIC_MEMORY", "true").lower() == "true"
ATLAS_EMBED_CACHE_KEY = (
    hashlib.sha256(ATLAS_EMBED_BASE_URL.encode("utf-8")).hexdigest()[:12]
    + ":"
    + ATLAS_EMBED_MODEL
)

FIELD_TYPES = {"text", "note", "rule", "checklist", "steps"}

ATLAS_PROJECT_BRIEF = """
Atlas is a personal AI system being built to become the user's primary long-term assistant.
Core principle: Atlas owns the system; models are replaceable plugins.
Atlas Core owns persistent context, memory, About Me, Skills/playbooks, behavior rules,
corrections/training examples, model routing, permissions, tools, actions, and usage policy.

Current product structure:
- Chat: conversations, Web/Deep controls, provider routing, feedback/corrections.
- Me: user-created profile sections and fields.
- Skills: user-created playbooks plus learned corrections.
- Settings: behavior rules, provider status, and personal usage.
- Admin: isolated tester accounts, budgets, usage analytics, and diagnostics.

Current technical foundation:
- Python/FastAPI backend.
- PostgreSQL persistence on Railway.
- GitHub repository: philc04/ai-assistant-.
- Semantic retrieval for memories/profile/skills with a keyword fallback.
- OpenAI is a supported provider, not Atlas's identity.
- An OpenAI-compatible local provider can be connected and used without silently falling back
  to OpenAI when Local is explicitly selected.
- Each account has an isolated workspace. One user's memories/profile/skills/conversations are
  never used to answer another user's requests.

Long-term direction:
- Make Atlas exceptionally good at knowing each user and their real workflows.
- Use multiple replaceable specialist/local/cloud models.
- Fine-tune an Atlas-specific open model only after enough vetted real corrections and examples exist.
- Keep changing personal facts in Atlas memory/database rather than baking them into model weights.
- Add tools and approved actions gradually, with clear permission boundaries.

Security rule: never reveal, repeat, or infer API keys, access keys, database credentials,
private provider URLs, or other secrets from configuration.
""".strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memories (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  project TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feedback (
  id BIGSERIAL PRIMARY KEY,
  message_id BIGINT REFERENCES messages(id) ON DELETE CASCADE,
  rating INTEGER,
  correction TEXT,
  notes TEXT,
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

CREATE TABLE IF NOT EXISTS profile_sections (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS profile_fields (
  id BIGSERIAL PRIMARY KEY,
  section_id BIGINT NOT NULL REFERENCES profile_sections(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  field_type TEXT NOT NULL DEFAULT 'text',
  value TEXT NOT NULL DEFAULT '',
  include_in_chat BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS skills (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS skill_fields (
  id BIGSERIAL PRIMARY KEY,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  field_type TEXT NOT NULL DEFAULT 'note',
  value TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS skill_lessons (
  id BIGSERIAL PRIMARY KEY,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  source_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
  correction TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS behavior_fields (
  id BIGSERIAL PRIMARY KEY,
  label TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS message_skill_context (
  message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'auto',
  PRIMARY KEY (message_id, skill_id)
);

CREATE TABLE IF NOT EXISTS semantic_embeddings (
  source_type TEXT NOT NULL,
  source_id BIGINT NOT NULL,
  content_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  embedding JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (source_type, source_id)
);

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE training_candidates ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE profile_sections ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE skills ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE behavior_fields ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE semantic_embeddings ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'owner';

CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);
CREATE INDEX IF NOT EXISTS idx_profile_sections_workspace ON profile_sections(workspace_id);
CREATE INDEX IF NOT EXISTS idx_skills_workspace ON skills(workspace_id);
CREATE INDEX IF NOT EXISTS idx_behavior_fields_workspace ON behavior_fields(workspace_id);
CREATE INDEX IF NOT EXISTS idx_training_candidates_workspace ON training_candidates(workspace_id);
CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_workspace_model ON semantic_embeddings(workspace_id, model);

CREATE TABLE IF NOT EXISTS atlas_users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  access_key_hash TEXT NOT NULL UNIQUE,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  monthly_budget_usd NUMERIC(10,2) NOT NULL DEFAULT 10.00,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS model_usage (
  id BIGSERIAL PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  conversation_id TEXT,
  user_message_id BIGINT,
  assistant_message_id BIGINT,
  provider TEXT,
  model TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  web_search_calls INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
  deep BOOLEAN NOT NULL DEFAULT FALSE,
  web BOOLEAN NOT NULL DEFAULT FALSE,
  semantic_memory BOOLEAN NOT NULL DEFAULT FALSE,
  memory_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  skill_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  latency_ms INTEGER,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_usage_workspace_created
  ON model_usage(workspace_id, created_at DESC);
"""


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def hash_access_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def init_db():
    if not DATABASE_URL:
        return
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            if ATLAS_ACCESS_KEY:
                cur.execute(
                    """
                    INSERT INTO atlas_users(id, name, access_key_hash, is_admin, monthly_budget_usd, enabled)
                    VALUES ('owner', %s, %s, TRUE, 0, TRUE)
                    ON CONFLICT(id) DO UPDATE SET
                      name=EXCLUDED.name,
                      access_key_hash=EXCLUDED.access_key_hash,
                      is_admin=TRUE,
                      enabled=TRUE,
                      updated_at=NOW()
                    """,
                    (ATLAS_OWNER_NAME, hash_access_key(ATLAS_ACCESS_KEY)),
                )
            if ATLAS_FRIEND_ACCESS_KEY:
                cur.execute(
                    """
                    INSERT INTO atlas_users(id, name, access_key_hash, is_admin, monthly_budget_usd, enabled)
                    VALUES ('friend', %s, %s, FALSE, %s, TRUE)
                    ON CONFLICT(id) DO UPDATE SET
                      name=EXCLUDED.name,
                      access_key_hash=EXCLUDED.access_key_hash,
                      monthly_budget_usd=EXCLUDED.monthly_budget_usd,
                      enabled=TRUE,
                      updated_at=NOW()
                    """,
                    (
                        ATLAS_FRIEND_NAME,
                        hash_access_key(ATLAS_FRIEND_ACCESS_KEY),
                        ATLAS_FRIEND_BUDGET_USD,
                    ),
                )
        conn.commit()


def require_user(x_atlas_key: str | None = Header(default=None)):
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL is not configured.")
    if not x_atlas_key:
        raise HTTPException(401, "Atlas access key required.")
    digest = hash_access_key(x_atlas_key.strip())
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, is_admin, monthly_budget_usd, enabled
                FROM atlas_users
                WHERE access_key_hash=%s AND enabled=TRUE
                """,
                (digest,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(401, "Invalid Atlas access key.")
    return dict(row)


def require_admin(user: dict = Depends(require_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin access required.")
    return user


def clean_field_type(value: str) -> str:
    value = (value or "text").strip().lower()
    return value if value in FIELD_TYPES else "text"


def next_sort_order(cur, table: str, where_sql: str = "", params: tuple = ()) -> int:
    allowed = {"profile_sections", "profile_fields", "skill_fields", "behavior_fields"}
    if table not in allowed:
        raise ValueError("Invalid sortable table.")
    query = f"SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM {table}"
    if where_sql:
        query += f" WHERE {where_sql}"
    cur.execute(query, params)
    return int(cur.fetchone()["n"])


def month_spend(workspace_id: str) -> float:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0) AS spend
                FROM model_usage
                WHERE workspace_id=%s
                  AND created_at >= date_trunc('month', NOW())
                """,
                (workspace_id,),
            )
            return float(cur.fetchone()["spend"] or 0)


def budget_status(user: dict) -> dict:
    budget = float(user.get("monthly_budget_usd") or 0)
    spent = month_spend(user["id"])
    remaining = None if budget <= 0 else max(0.0, budget - spent)
    return {"budget_usd": budget, "spent_usd": spent, "remaining_usd": remaining}


def openai_allowed_for(user: dict) -> bool:
    budget = float(user.get("monthly_budget_usd") or 0)
    if budget <= 0:
        return True
    return month_spend(user["id"]) < budget


def history_for(cid: str, workspace_id: str, limit: int = 24):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.role, m.content
                FROM messages m
                JOIN conversations c ON c.id=m.conversation_id
                WHERE m.conversation_id=%s AND c.workspace_id=%s
                ORDER BY m.id DESC
                LIMIT %s
                """,
                (cid, workspace_id, limit),
            )
            rows = list(cur.fetchall())
    return list(reversed(rows))


def tokenize(text: str) -> list[str]:
    return [
        t
        for t in re.findall(r"[a-z0-9][a-z0-9_'-]{2,}", (text or "").lower())
        if t not in {
            "the", "and", "that", "this", "with", "from", "have", "what", "when",
            "where", "which", "your", "about", "would", "could", "should", "into",
            "just", "like", "want", "need", "make", "help", "tell", "give"
        }
    ][:20]


def keyword_memories(message: str, workspace_id: str, limit: int = 6):
    terms = tokenize(message)[:8]
    if not terms:
        return []
    clauses = " OR ".join(["LOWER(content) LIKE %s"] * len(terms))
    params = [workspace_id] + [f"%{t}%" for t in terms] + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM memories
                WHERE workspace_id=%s AND ({clauses})
                ORDER BY importance DESC, updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return list(cur.fetchall())


def profile_data(workspace_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM profile_sections WHERE workspace_id=%s ORDER BY sort_order, id",
                (workspace_id,),
            )
            sections = list(cur.fetchall())
            if sections:
                ids = [s["id"] for s in sections]
                cur.execute(
                    "SELECT * FROM profile_fields WHERE section_id = ANY(%s) ORDER BY section_id, sort_order, id",
                    (ids,),
                )
                fields = list(cur.fetchall())
            else:
                fields = []

    by_section: dict[int, list[dict]] = {}
    for field in fields:
        by_section.setdefault(field["section_id"], []).append(dict(field))

    result = []
    for section in sections:
        item = dict(section)
        item["fields"] = by_section.get(section["id"], [])
        result.append(item)
    return result


def profile_context(workspace_id: str, max_chars: int = 12000) -> str:
    lines = []
    for section in profile_data(workspace_id):
        included = [
            f for f in section["fields"]
            if f["include_in_chat"] and (f["value"] or "").strip()
        ]
        if not included:
            continue
        lines.append(f"[{section['name']}]")
        for field in included:
            lines.append(f"- {field['label']}: {field['value'].strip()}")
    text = "\n".join(lines).strip()
    return text[:max_chars] if text else "(none)"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lexical_overlap(query: str, text: str) -> float:
    q = set(tokenize(query))
    if not q:
        return 0.0
    t = set(tokenize(text))
    if not t:
        return 0.0
    return len(q & t) / max(1, len(q))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not ATLAS_EMBED_BASE_URL:
        raise RuntimeError("ATLAS_EMBED_BASE_URL is not configured.")
    if not texts:
        return []

    payload = {"model": ATLAS_EMBED_MODEL, "input": texts, "encoding_format": "float"}
    headers = {"Content-Type": "application/json"}
    if ATLAS_EMBED_API_KEY:
        headers["Authorization"] = f"Bearer {ATLAS_EMBED_API_KEY}"

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{ATLAS_EMBED_BASE_URL.rstrip('/')}/embeddings",
            headers=headers,
            json=payload,
        )

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"Embedding API error {response.status_code}: {detail}")

    data = response.json().get("data", [])
    data.sort(key=lambda x: x.get("index", 0))
    vectors = [row.get("embedding", []) for row in data]
    if len(vectors) != len(texts):
        raise RuntimeError("Embedding API returned an unexpected number of vectors.")
    return vectors


def behavior_data(workspace_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM behavior_fields WHERE workspace_id=%s ORDER BY sort_order, id",
                (workspace_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def behavior_context(workspace_id: str, max_chars: int = 8000) -> str:
    rows = behavior_data(workspace_id)
    lines = [
        f"- {row['label']}: {row['value'].strip()}"
        for row in rows
        if (row["value"] or "").strip()
    ]
    text = "\n".join(lines).strip()
    return text[:max_chars] if text else "(none)"


def skills_data(workspace_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM skills WHERE workspace_id=%s ORDER BY updated_at DESC, id DESC",
                (workspace_id,),
            )
            skills = [dict(row) for row in cur.fetchall()]
            if skills:
                ids = [s["id"] for s in skills]
                cur.execute(
                    "SELECT * FROM skill_fields WHERE skill_id = ANY(%s) ORDER BY skill_id, sort_order, id",
                    (ids,),
                )
                fields = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    "SELECT * FROM skill_lessons WHERE skill_id = ANY(%s) ORDER BY skill_id, created_at DESC, id DESC",
                    (ids,),
                )
                lessons = [dict(row) for row in cur.fetchall()]
            else:
                fields, lessons = [], []

    fields_by: dict[int, list[dict]] = {}
    lessons_by: dict[int, list[dict]] = {}
    for row in fields:
        fields_by.setdefault(row["skill_id"], []).append(row)
    for row in lessons:
        lessons_by.setdefault(row["skill_id"], []).append(row)

    for skill in skills:
        skill["fields"] = fields_by.get(skill["id"], [])
        skill["lessons"] = lessons_by.get(skill["id"], [])[:20]
    return skills


def skill_to_context(skill: dict, max_chars: int = 8000) -> str:
    lines = [
        f"SKILL: {skill['name']}",
        f"Purpose: {(skill['description'] or '').strip() or '(not specified)'}",
    ]
    for field in skill.get("fields", []):
        value = (field.get("value") or "").strip()
        if value:
            lines.append(f"{field['label']} [{field['field_type']}]: {value}")
    lessons = skill.get("lessons", [])
    if lessons:
        lines.append("Learned corrections:")
        for lesson in lessons[:12]:
            lines.append(f"- {lesson['correction']}")
    return "\n".join(lines)[:max_chars]


def find_relevant_skills(message: str, workspace_id: str, active_skill_id: int | None = None, limit: int = 2):
    all_skills = [s for s in skills_data(workspace_id) if s["enabled"]]
    selected = []
    seen = set()

    if active_skill_id:
        active = next((s for s in all_skills if s["id"] == active_skill_id), None)
        if active:
            selected.append((active, "active"))
            seen.add(active["id"])

    terms = tokenize(message)
    scored = []
    for skill in all_skills:
        if skill["id"] in seen:
            continue
        title = (skill["name"] or "").lower()
        description = (skill["description"] or "").lower()
        field_labels = " ".join((f["label"] or "") for f in skill.get("fields", [])).lower()
        field_values = " ".join((f["value"] or "") for f in skill.get("fields", [])).lower()
        score = 0
        for term in terms:
            if term in title:
                score += 6
            if term in description:
                score += 3
            if term in field_labels:
                score += 2
            if term in field_values:
                score += 1
        if score > 0:
            scored.append((score, skill))

    scored.sort(key=lambda x: (-x[0], x[1]["name"].lower()))
    for _, skill in scored[: max(0, limit - len(selected))]:
        selected.append((skill, "auto"))
        seen.add(skill["id"])
    return selected


def semantic_sources(workspace_id: str) -> dict[tuple[str, int], dict]:
    sources: dict[tuple[str, int], dict] = {}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, kind, content, importance, source, updated_at
                FROM memories
                WHERE workspace_id=%s
                ORDER BY id
                """,
                (workspace_id,),
            )
            for row in cur.fetchall():
                text = f"Memory [{row['kind']}]: {row['content']}"
                sources[("memory", row["id"])] = {"text": text[:10000], "row": dict(row)}

            cur.execute(
                """
                SELECT pf.id, pf.label, pf.field_type, pf.value, pf.include_in_chat,
                       ps.name AS section_name
                FROM profile_fields pf
                JOIN profile_sections ps ON ps.id=pf.section_id
                WHERE ps.workspace_id=%s
                  AND pf.include_in_chat=TRUE
                  AND LENGTH(TRIM(pf.value)) > 0
                ORDER BY ps.sort_order, ps.id, pf.sort_order, pf.id
                """,
                (workspace_id,),
            )
            for row in cur.fetchall():
                text = f"About Me / {row['section_name']} / {row['label']}: {row['value']}"
                sources[("profile", row["id"])] = {"text": text[:10000], "row": dict(row)}

    for skill in [s for s in skills_data(workspace_id) if s["enabled"]]:
        text = skill_to_context(skill, max_chars=10000)
        sources[("skill", skill["id"])] = {"text": text, "row": skill}
    return sources


async def ensure_semantic_index(workspace_id: str, sources: dict[tuple[str, int], dict]):
    if not ATLAS_SEMANTIC_MEMORY:
        return
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_type, source_id, content_hash, model
                FROM semantic_embeddings
                WHERE workspace_id=%s
                """,
                (workspace_id,),
            )
            existing = {
                (r["source_type"], r["source_id"]): (r["content_hash"], r["model"])
                for r in cur.fetchall()
            }

    stale = []
    for key, item in sources.items():
        h = content_hash(item["text"])
        cached = existing.get(key)
        if not cached or cached[0] != h or cached[1] != ATLAS_EMBED_CACHE_KEY:
            stale.append((key, item["text"], h))

    deleted = [key for key in existing if key not in sources]
    if deleted:
        with db() as conn:
            with conn.cursor() as cur:
                for source_type, source_id in deleted:
                    cur.execute(
                        """
                        DELETE FROM semantic_embeddings
                        WHERE workspace_id=%s AND source_type=%s AND source_id=%s
                        """,
                        (workspace_id, source_type, source_id),
                    )
            conn.commit()

    for i in range(0, len(stale), 48):
        batch = stale[i:i + 48]
        vectors = await embed_texts([x[1] for x in batch])
        with db() as conn:
            with conn.cursor() as cur:
                for ((source_type, source_id), _text, h), vector in zip(batch, vectors):
                    cur.execute(
                        """
                        INSERT INTO semantic_embeddings(
                          workspace_id, source_type, source_id, content_hash, model, embedding, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
                        ON CONFLICT(source_type, source_id) DO UPDATE SET
                          workspace_id=EXCLUDED.workspace_id,
                          content_hash=EXCLUDED.content_hash,
                          model=EXCLUDED.model,
                          embedding=EXCLUDED.embedding,
                          updated_at=NOW()
                        """,
                        (
                            workspace_id, source_type, source_id, h,
                            ATLAS_EMBED_CACHE_KEY, json.dumps(vector),
                        ),
                    )
            conn.commit()


async def semantic_context(message: str, workspace_id: str, active_skill_id: int | None = None):
    sources = semantic_sources(workspace_id)
    await ensure_semantic_index(workspace_id, sources)
    query_vector = (await embed_texts([message]))[0]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_type, source_id, embedding
                FROM semantic_embeddings
                WHERE workspace_id=%s AND model=%s
                """,
                (workspace_id, ATLAS_EMBED_CACHE_KEY),
            )
            vectors = list(cur.fetchall())

    ranked: dict[str, list[tuple[float, dict]]] = {"memory": [], "profile": [], "skill": []}
    for row in vectors:
        key = (row["source_type"], row["source_id"])
        item = sources.get(key)
        if not item:
            continue
        score = cosine_similarity(query_vector, row["embedding"])
        score += min(0.10, lexical_overlap(message, item["text"]) * 0.10)
        if row["source_type"] == "memory":
            raw = item["row"]
            score += min(0.06, float(raw.get("importance") or 0) * 0.06)
            if raw.get("kind") == "correction" or raw.get("source") == "user_correction":
                score += 0.08
        ranked.setdefault(row["source_type"], []).append((score, item))

    for bucket in ranked.values():
        bucket.sort(key=lambda x: x[0], reverse=True)

    lower = message.lower()
    broad_profile = any(x in lower for x in ["about me", "who am i", "know about me", "my profile"])
    broad_memory = any(x in lower for x in ["what do you remember", "what do you know", "remember about me"])
    profile_limit = 20 if broad_profile else 8
    memory_limit = 10 if broad_memory else 6

    selected_profile = [item for score, item in ranked["profile"][:profile_limit] if broad_profile or score >= 0.20]
    selected_memories = [item["row"] for score, item in ranked["memory"][:memory_limit] if broad_memory or score >= 0.20]

    all_skills = [s for s in skills_data(workspace_id) if s["enabled"]]
    chosen_skills = []
    seen = set()
    if active_skill_id:
        active = next((s for s in all_skills if s["id"] == active_skill_id), None)
        if active:
            chosen_skills.append((active, "active"))
            seen.add(active["id"])

    for score, item in ranked["skill"]:
        skill = item["row"]
        if skill["id"] in seen or score < 0.22:
            continue
        chosen_skills.append((skill, "semantic"))
        seen.add(skill["id"])
        if len(chosen_skills) >= 2:
            break

    profile_lines = []
    grouped = {}
    for item in selected_profile:
        row = item["row"]
        grouped.setdefault(row["section_name"], []).append(row)
    for section, rows in grouped.items():
        profile_lines.append(f"[{section}]")
        for row in rows:
            profile_lines.append(f"- {row['label']}: {row['value']}")

    return {
        "profile_block": "\n".join(profile_lines).strip() or "(none)",
        "memories": selected_memories,
        "skills": chosen_skills,
    }


def extract_text(data: dict) -> str:
    chunks = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def extract_chat_completion_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    chunks.append(value)
        return "\n".join(chunks).strip()
    return ""


def normalize_provider(value: str | None) -> str:
    value = (value or "auto").strip().lower()
    return value if value in {"auto", "openai", "local"} else "auto"


def openai_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_BASE_URL)


def local_configured() -> bool:
    return bool(ATLAS_LOCAL_BASE_URL and ATLAS_LOCAL_MODEL)


def embedding_backend_name() -> str:
    return "openai" if ATLAS_EMBED_BASE_URL.rstrip("/") == OPENAI_BASE_URL.rstrip("/") else "custom"


def atlas_self_context(requested_provider: str = "auto", deep: bool = False, use_web: bool = False) -> str:
    provider = normalize_provider(requested_provider)
    local_status = f"connected ({ATLAS_LOCAL_MODEL})" if local_configured() else "not connected"
    openai_status = f"connected ({ATLAS_MODEL})" if openai_configured() else "not connected"
    return (
        f"{ATLAS_PROJECT_BRIEF}\n\n"
        "CURRENT RUNTIME STATUS (authoritative for this request):\n"
        f"- Atlas version: {APP_VERSION}\n"
        f"- Database configured: {'yes' if DATABASE_URL else 'no'}\n"
        f"- OpenAI provider: {openai_status}\n"
        f"- Local provider: {local_status}\n"
        f"- Default provider: {ATLAS_DEFAULT_PROVIDER}\n"
        f"- Requested provider preference for this turn: {provider}\n"
        f"- Deep mode requested: {'yes' if deep else 'no'}\n"
        f"- Web requested: {'yes' if use_web else 'no'}\n"
        f"- Web capability enabled by server: {'yes' if ATLAS_ALLOW_WEB else 'no'}\n"
        f"- Semantic memory: {'on' if ATLAS_SEMANTIC_MEMORY else 'off'}\n"
        f"- Embeddings backend/model: {embedding_backend_name()} / {ATLAS_EMBED_MODEL}\n\n"
        "When asked about Atlas itself, its project, version, capabilities, providers, local brain, "
        "memory system, or architecture, answer from this block rather than guessing."
    )


def normalize_openai_usage(data: dict) -> dict:
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    web_calls = sum(
        1 for item in (data.get("output") or [])
        if item.get("type") in {"web_search_call", "web_search"}
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "web_search_calls": web_calls,
    }


def normalize_local_usage(data: dict) -> dict:
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "web_search_calls": 0,
    }


def estimate_cost(provider: str, usage: dict) -> float:
    if provider != "openai":
        return 0.0
    return round(
        (usage.get("input_tokens", 0) / 1_000_000) * ATLAS_OPENAI_INPUT_USD_PER_M
        + (usage.get("output_tokens", 0) / 1_000_000) * ATLAS_OPENAI_OUTPUT_USD_PER_M
        + usage.get("web_search_calls", 0) * ATLAS_WEB_SEARCH_USD_PER_CALL,
        6,
    )


async def call_openai(model: str, messages: list[dict], instructions: str, use_web: bool):
    if not openai_configured():
        raise RuntimeError("OpenAI provider is not configured.")
    payload = {"model": model, "input": messages, "instructions": instructions, "store": False}
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
    answer = extract_text(data)
    if not answer:
        raise RuntimeError("OpenAI returned no text output.")
    return answer, normalize_openai_usage(data)


async def call_local(model: str, messages: list[dict], instructions: str):
    if not local_configured():
        raise RuntimeError("Local provider is not configured. Set ATLAS_LOCAL_BASE_URL and ATLAS_LOCAL_MODEL.")
    headers = {"Content-Type": "application/json"}
    if ATLAS_LOCAL_API_KEY:
        headers["Authorization"] = f"Bearer {ATLAS_LOCAL_API_KEY}"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": instructions}] + messages,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{ATLAS_LOCAL_BASE_URL.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"Local model API error {response.status_code}: {detail}")
    data = response.json()
    answer = extract_chat_completion_text(data)
    if not answer:
        raise RuntimeError("Local provider returned no text output.")
    return answer, normalize_local_usage(data)


async def call_model(
    provider_choice: str,
    deep: bool,
    use_web: bool,
    messages: list[dict],
    instructions: str,
    allow_openai: bool,
):
    requested = normalize_provider(provider_choice)

    if requested == "local":
        if use_web:
            raise RuntimeError("Web search requires the OpenAI provider. Turn Web off or choose Auto/OpenAI.")
        model = ATLAS_LOCAL_DEEP_MODEL if deep else ATLAS_LOCAL_MODEL
        answer, usage = await call_local(model, messages, instructions)
        return answer, "local", model, usage

    if requested == "openai":
        if not allow_openai:
            raise RuntimeError("This account has reached its monthly OpenAI budget.")
        model = ATLAS_DEEP_MODEL if deep else ATLAS_MODEL
        answer, usage = await call_openai(model, messages, instructions, use_web)
        return answer, "openai", model, usage

    default = ATLAS_DEFAULT_PROVIDER if ATLAS_DEFAULT_PROVIDER in {"local", "openai"} else "openai"
    order = [default, "local" if default == "openai" else "openai"]
    if use_web:
        order = ["openai"]

    errors = []
    for provider in order:
        try:
            if provider == "local":
                if not local_configured():
                    continue
                model = ATLAS_LOCAL_DEEP_MODEL if deep else ATLAS_LOCAL_MODEL
                answer, usage = await call_local(model, messages, instructions)
                return answer, "local", model, usage
            if provider == "openai":
                if not allow_openai:
                    errors.append("openai: monthly budget reached")
                    continue
                if not openai_configured():
                    continue
                model = ATLAS_DEEP_MODEL if deep else ATLAS_MODEL
                answer, usage = await call_openai(model, messages, instructions, use_web)
                return answer, "openai", model, usage
        except Exception as exc:
            errors.append(f"{provider}: {exc}")

    if errors:
        raise RuntimeError("No model provider succeeded. " + " | ".join(errors))
    raise RuntimeError("No model provider is configured.")


def record_usage(
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
    usage = usage or {}
    memory_refs = [
        {"id": int(m["id"]), "kind": m.get("kind", "memory")}
        for m in memories if m.get("id") is not None
    ]
    skill_refs = [
        {"id": int(skill["id"]), "name": skill["name"], "source": source}
        for skill, source in skills
    ]
    cost = estimate_cost(provider or "", usage)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_usage(
                  workspace_id, conversation_id, user_message_id, assistant_message_id,
                  provider, model, input_tokens, output_tokens, total_tokens,
                  web_search_calls, estimated_cost_usd, deep, web, semantic_memory,
                  memory_refs, skill_refs, latency_ms, error
                )
                VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s
                )
                """,
                (
                    workspace_id, conversation_id, user_message_id, assistant_message_id,
                    provider, model,
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    int(usage.get("total_tokens", 0)),
                    int(usage.get("web_search_calls", 0)),
                    cost, deep, web, semantic_memory,
                    json.dumps(memory_refs), json.dumps(skill_refs), latency_ms, error,
                ),
            )
        conn.commit()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=30000)
    conversation_id: str | None = None
    use_web: bool = False
    deep: bool = False
    active_skill_id: int | None = None
    provider: str = "auto"


class MemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    kind: str = Field(default="knowledge", max_length=80)
    importance: float = Field(default=0.8, ge=0, le=1)


class FeedbackRequest(BaseModel):
    message_id: int
    rating: int | None = Field(default=None, ge=-1, le=1)
    correction: str | None = Field(default=None, max_length=30000)


class SectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SectionUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProfileFieldCreate(BaseModel):
    section_id: int
    label: str = Field(min_length=1, max_length=160)
    field_type: str = "text"
    value: str = Field(default="", max_length=50000)
    include_in_chat: bool = True


class ProfileFieldUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    field_type: str = "text"
    value: str = Field(default="", max_length=50000)
    include_in_chat: bool = True


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=3000)


class SkillUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=3000)
    enabled: bool = True


class SkillFieldCreate(BaseModel):
    skill_id: int
    label: str = Field(min_length=1, max_length=160)
    field_type: str = "note"
    value: str = Field(default="", max_length=50000)


class SkillFieldUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    field_type: str = "note"
    value: str = Field(default="", max_length=50000)


class BehaviorFieldCreate(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    value: str = Field(default="", max_length=30000)


class BehaviorFieldUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    value: str = Field(default="", max_length=30000)


class TesterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    monthly_budget_usd: float = Field(default=10.0, ge=0, le=10000)


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
        "openai": openai_configured(),
        "local": local_configured(),
        "default_provider": ATLAS_DEFAULT_PROVIDER,
        "owner_access_key": bool(ATLAS_ACCESS_KEY),
        "semantic_memory": ATLAS_SEMANTIC_MEMORY,
        "embedding_model": ATLAS_EMBED_MODEL,
    }


@app.get("/api/session")
def session_info(user: dict = Depends(require_user)):
    status = budget_status(user)
    return {
        "workspace_id": user["id"],
        "name": user["name"],
        "is_admin": bool(user["is_admin"]),
        **status,
    }


@app.get("/api/system")
def system_info(user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM training_candidates WHERE workspace_id=%s",
                (user["id"],),
            )
            training_count = int(cur.fetchone()["n"])
    return {
        "version": APP_VERSION,
        "default_provider": ATLAS_DEFAULT_PROVIDER,
        "openai_configured": openai_configured(),
        "local_configured": local_configured(),
        "openai_model": ATLAS_MODEL,
        "local_model": ATLAS_LOCAL_MODEL if local_configured() else None,
        "semantic_memory": ATLAS_SEMANTIC_MEMORY,
        "embedding_model": ATLAS_EMBED_MODEL,
        "embedding_backend": embedding_backend_name(),
        "self_context": True,
        "training_candidates": training_count,
        "workspace_id": user["id"],
        "workspace_name": user["name"],
        "is_admin": bool(user["is_admin"]),
        **budget_status(user),
    }


@app.get("/api/usage")
def own_usage(user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE error IS NULL) AS requests,
                  COALESCE(SUM(input_tokens),0) AS input_tokens,
                  COALESCE(SUM(output_tokens),0) AS output_tokens,
                  COALESCE(SUM(total_tokens),0) AS total_tokens,
                  COALESCE(SUM(web_search_calls),0) AS web_search_calls,
                  COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd
                FROM model_usage
                WHERE workspace_id=%s
                  AND created_at >= date_trunc('month', NOW())
                """,
                (user["id"],),
            )
            row = dict(cur.fetchone())
    row = {k: float(v) if k == "estimated_cost_usd" else int(v or 0) for k, v in row.items()}
    return {**row, **budget_status(user)}


@app.get("/api/profile")
def get_profile(user: dict = Depends(require_user)):
    return {"sections": profile_data(user["id"])}


@app.post("/api/profile/sections")
def create_profile_section(req: SectionCreate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            order = next_sort_order(cur, "profile_sections", "workspace_id=%s", (user["id"],))
            cur.execute(
                """
                INSERT INTO profile_sections(name, sort_order, workspace_id)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (req.name.strip(), order, user["id"]),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["fields"] = []
    return row


@app.patch("/api/profile/sections/{section_id}")
def update_profile_section(section_id: int, req: SectionUpdate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_sections
                SET name=%s, updated_at=NOW()
                WHERE id=%s AND workspace_id=%s
                RETURNING *
                """,
                (req.name.strip(), section_id, user["id"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Section not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/profile/sections/{section_id}")
def delete_profile_section(section_id: int, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM profile_sections WHERE id=%s AND workspace_id=%s RETURNING id",
                (section_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Section not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/profile/fields")
def create_profile_field(req: ProfileFieldCreate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM profile_sections WHERE id=%s AND workspace_id=%s",
                (req.section_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Section not found.")
            order = next_sort_order(cur, "profile_fields", "section_id=%s", (req.section_id,))
            cur.execute(
                """
                INSERT INTO profile_fields(section_id, label, field_type, value, include_in_chat, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    req.section_id, req.label.strip(), clean_field_type(req.field_type),
                    req.value.strip(), req.include_in_chat, order,
                ),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/profile/fields/{field_id}")
def update_profile_field(field_id: int, req: ProfileFieldUpdate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_fields pf
                SET label=%s, field_type=%s, value=%s, include_in_chat=%s, updated_at=NOW()
                WHERE pf.id=%s
                  AND EXISTS (
                    SELECT 1 FROM profile_sections ps
                    WHERE ps.id=pf.section_id AND ps.workspace_id=%s
                  )
                RETURNING pf.*
                """,
                (
                    req.label.strip(), clean_field_type(req.field_type), req.value.strip(),
                    req.include_in_chat, field_id, user["id"],
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/profile/fields/{field_id}")
def delete_profile_field(field_id: int, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM profile_fields pf
                WHERE pf.id=%s
                  AND EXISTS (
                    SELECT 1 FROM profile_sections ps
                    WHERE ps.id=pf.section_id AND ps.workspace_id=%s
                  )
                RETURNING pf.id
                """,
                (field_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Field not found.")
        conn.commit()
    return {"deleted": True}


@app.get("/api/skills")
def get_skills(user: dict = Depends(require_user)):
    return {"skills": skills_data(user["id"])}


@app.post("/api/skills")
def create_skill(req: SkillCreate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skills(name, description, workspace_id)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (req.name.strip(), req.description.strip(), user["id"]),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["fields"] = []
    row["lessons"] = []
    return row


@app.patch("/api/skills/{skill_id}")
def update_skill(skill_id: int, req: SkillUpdate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE skills
                SET name=%s, description=%s, enabled=%s, updated_at=NOW()
                WHERE id=%s AND workspace_id=%s
                RETURNING *
                """,
                (req.name.strip(), req.description.strip(), req.enabled, skill_id, user["id"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Skill not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: int, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM skills WHERE id=%s AND workspace_id=%s RETURNING id",
                (skill_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Skill not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/skills/fields")
def create_skill_field(req: SkillFieldCreate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM skills WHERE id=%s AND workspace_id=%s",
                (req.skill_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Skill not found.")
            order = next_sort_order(cur, "skill_fields", "skill_id=%s", (req.skill_id,))
            cur.execute(
                """
                INSERT INTO skill_fields(skill_id, label, field_type, value, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    req.skill_id, req.label.strip(), clean_field_type(req.field_type),
                    req.value.strip(), order,
                ),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/skills/fields/{field_id}")
def update_skill_field(field_id: int, req: SkillFieldUpdate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE skill_fields sf
                SET label=%s, field_type=%s, value=%s, updated_at=NOW()
                WHERE sf.id=%s
                  AND EXISTS (
                    SELECT 1 FROM skills s
                    WHERE s.id=sf.skill_id AND s.workspace_id=%s
                  )
                RETURNING sf.*
                """,
                (
                    req.label.strip(), clean_field_type(req.field_type), req.value.strip(),
                    field_id, user["id"],
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Skill field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/skills/fields/{field_id}")
def delete_skill_field(field_id: int, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM skill_fields sf
                WHERE sf.id=%s
                  AND EXISTS (
                    SELECT 1 FROM skills s
                    WHERE s.id=sf.skill_id AND s.workspace_id=%s
                  )
                RETURNING sf.id
                """,
                (field_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Skill field not found.")
        conn.commit()
    return {"deleted": True}


@app.get("/api/behavior")
def get_behavior(user: dict = Depends(require_user)):
    return {"fields": behavior_data(user["id"])}


@app.post("/api/behavior/fields")
def create_behavior_field(req: BehaviorFieldCreate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            order = next_sort_order(cur, "behavior_fields", "workspace_id=%s", (user["id"],))
            cur.execute(
                """
                INSERT INTO behavior_fields(label, value, sort_order, workspace_id)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (req.label.strip(), req.value.strip(), order, user["id"]),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/behavior/fields/{field_id}")
def update_behavior_field(field_id: int, req: BehaviorFieldUpdate, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE behavior_fields
                SET label=%s, value=%s, updated_at=NOW()
                WHERE id=%s AND workspace_id=%s
                RETURNING *
                """,
                (req.label.strip(), req.value.strip(), field_id, user["id"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Behavior field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/behavior/fields/{field_id}")
def delete_behavior_field(field_id: int, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM behavior_fields WHERE id=%s AND workspace_id=%s RETURNING id",
                (field_id, user["id"]),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Behavior field not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(require_user)):
    workspace_id = user["id"]
    cid = req.conversation_id or str(uuid4())

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM conversations WHERE id=%s", (cid,))
            existing = cur.fetchone()
            if existing and existing["workspace_id"] != workspace_id:
                raise HTTPException(404, "Conversation not found.")
            cur.execute(
                """
                INSERT INTO conversations(id, title, workspace_id)
                VALUES (%s, %s, %s)
                ON CONFLICT(id) DO NOTHING
                """,
                (cid, req.message[:80], workspace_id),
            )
        conn.commit()

    history = history_for(cid, workspace_id)
    allow_openai = openai_allowed_for(user)

    semantic_used = False
    semantic_error = None
    try:
        if not ATLAS_SEMANTIC_MEMORY:
            raise RuntimeError("Semantic memory is disabled.")
        if not allow_openai and embedding_backend_name() == "openai":
            raise RuntimeError("Cloud semantic embeddings skipped because this account reached its monthly budget.")
        semantic = await semantic_context(req.message, workspace_id, req.active_skill_id)
        profile_block = semantic["profile_block"]
        memories = semantic["memories"]
        chosen_skills = semantic["skills"]
        semantic_used = True
    except Exception as exc:
        semantic_error = str(exc)
        profile_block = profile_context(workspace_id)
        memories = keyword_memories(req.message, workspace_id)
        chosen_skills = find_relevant_skills(req.message, workspace_id, req.active_skill_id)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages(conversation_id, role, content)
                VALUES (%s, 'user', %s)
                RETURNING id
                """,
                (cid, req.message),
            )
            user_message_id = cur.fetchone()["id"]
        conn.commit()

    memory_block = "\n".join(f"- [{m['kind']}] {m['content']}" for m in memories) or "(none)"
    skill_block = "\n\n".join(skill_to_context(skill) for skill, _source in chosen_skills) or "(none)"

    instructions = (
        "You are the reasoning engine used by Atlas Core, a personal AI system. "
        "Use only this workspace's supplied profile, skills, memory, and behavior. "
        "Never invent a memory, preference, skill, or completed external action. "
        "User corrections are high-priority lessons. "
        "If a user-defined instruction conflicts with safety or verified facts, explain the conflict.\n\n"
        "DEFAULT RESPONSE DISCIPLINE:\n"
        "- Be concise by default. Normal answers should usually stay under about 180 words.\n"
        "- Answer the question first. Do not repeat the user's request back to them.\n"
        "- For live setup/troubleshooting, give only the next useful step unless the user asks for the full plan.\n"
        "- Expand only when the user asks for depth, detail, a long-form artifact, or the task genuinely requires it.\n\n"
        f"ATLAS SELF / PROJECT CONTEXT:\n{atlas_self_context(req.provider, req.deep, req.use_web)}\n\n"
        f"ATLAS BEHAVIOR RULES:\n{behavior_context(workspace_id)}\n\n"
        f"ABOUT THE USER:\n{profile_block}\n\n"
        f"RELEVANT SKILLS:\n{skill_block}\n\n"
        f"RELEVANT MEMORY:\n{memory_block}"
    )

    started = time.perf_counter()
    provider_used = None
    model = None
    usage = {}
    try:
        answer, provider_used, model, usage = await call_model(
            req.provider,
            req.deep,
            req.use_web,
            history + [{"role": "user", "content": req.message}],
            instructions,
            allow_openai=allow_openai,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        record_usage(
            workspace_id, cid, user_message_id, None, provider_used, model, usage,
            req.deep, req.use_web, semantic_used, memories, chosen_skills,
            latency_ms, error=str(exc)[:2000],
        )
        raise HTTPException(502, str(exc)) from exc

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages(conversation_id, role, content, provider, model)
                VALUES (%s, 'assistant', %s, %s, %s)
                RETURNING id
                """,
                (cid, answer, provider_used, model),
            )
            message_id = cur.fetchone()["id"]
            for skill, source in chosen_skills:
                cur.execute(
                    """
                    INSERT INTO message_skill_context(message_id, skill_id, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(message_id, skill_id) DO UPDATE SET source=EXCLUDED.source
                    """,
                    (message_id, skill["id"], source),
                )
            cur.execute("UPDATE conversations SET updated_at=NOW() WHERE id=%s AND workspace_id=%s", (cid, workspace_id))
        conn.commit()

    latency_ms = int((time.perf_counter() - started) * 1000)
    record_usage(
        workspace_id, cid, user_message_id, message_id, provider_used, model, usage,
        req.deep, req.use_web, semantic_used, memories, chosen_skills, latency_ms,
    )

    return {
        "conversation_id": cid,
        "answer": answer,
        "provider": provider_used,
        "model": model,
        "assistant_message_id": message_id,
        "user_message_id": user_message_id,
        "semantic_memory": semantic_used,
        "semantic_error": semantic_error if not semantic_used else None,
        "skills_used": [
            {"id": skill["id"], "name": skill["name"], "source": source}
            for skill, source in chosen_skills
        ],
        "usage": {
            **usage,
            "estimated_cost_usd": estimate_cost(provider_used, usage),
            **budget_status(user),
        },
    }


@app.post("/api/memory")
def memory(req: MemoryRequest, user: dict = Depends(require_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories(kind, content, importance, confidence, source, workspace_id)
                VALUES (%s, %s, %s, 1.0, 'user', %s)
                RETURNING id
                """,
                (req.kind.strip() or "knowledge", req.content.strip(), req.importance, user["id"]),
            )
            memory_id = cur.fetchone()["id"]
        conn.commit()
    return {"saved": True, "id": memory_id}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: dict = Depends(require_user)):
    candidate = False
    correction_memory_saved = False
    skill_lesson_saved = False
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.conversation_id
                FROM messages m
                JOIN conversations c ON c.id=m.conversation_id
                WHERE m.id=%s AND c.workspace_id=%s
                """,
                (req.message_id, user["id"]),
            )
            msg = cur.fetchone()
            if not msg:
                raise HTTPException(404, "Message not found.")

            cur.execute(
                "INSERT INTO feedback(message_id, rating, correction) VALUES (%s, %s, %s)",
                (req.message_id, req.rating, req.correction),
            )

            correction = (req.correction or "").strip()
            if correction:
                cur.execute(
                    """
                    SELECT content
                    FROM messages
                    WHERE conversation_id=%s AND role='user' AND id < %s
                    ORDER BY id DESC LIMIT 1
                    """,
                    (msg["conversation_id"], req.message_id),
                )
                prior = cur.fetchone()
                if prior:
                    cur.execute(
                        """
                        INSERT INTO training_candidates(workspace_id, source_type, input_text, target_text)
                        VALUES (%s, 'user_correction', %s, %s)
                        """,
                        (user["id"], prior["content"], correction),
                    )
                    candidate = True
                    memory_text = f"User correction for request '{prior['content']}': {correction}"
                    cur.execute(
                        """
                        INSERT INTO memories(kind, content, importance, confidence, source, workspace_id)
                        VALUES ('correction', %s, 1.0, 1.0, 'user_correction', %s)
                        """,
                        (memory_text, user["id"]),
                    )
                    correction_memory_saved = True

                cur.execute(
                    """
                    SELECT msc.skill_id
                    FROM message_skill_context msc
                    JOIN skills s ON s.id=msc.skill_id
                    WHERE msc.message_id=%s AND msc.source='active' AND s.workspace_id=%s
                    LIMIT 1
                    """,
                    (req.message_id, user["id"]),
                )
                active = cur.fetchone()
                if active:
                    cur.execute(
                        """
                        INSERT INTO skill_lessons(skill_id, source_message_id, correction)
                        VALUES (%s, %s, %s)
                        """,
                        (active["skill_id"], req.message_id, correction),
                    )
                    skill_lesson_saved = True
        conn.commit()

    return {
        "saved": True,
        "training_candidate_created": candidate,
        "correction_memory_saved": correction_memory_saved,
        "skill_lesson_saved": skill_lesson_saved,
    }


@app.post("/api/admin/users")
def create_tester(req: TesterCreate, admin: dict = Depends(require_admin)):
    workspace_id = "user_" + uuid4().hex[:12]
    access_key = "atlas_" + secrets.token_urlsafe(24)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO atlas_users(id, name, access_key_hash, is_admin, monthly_budget_usd, enabled)
                VALUES (%s, %s, %s, FALSE, %s, TRUE)
                RETURNING id, name, is_admin, monthly_budget_usd, enabled, created_at
                """,
                (workspace_id, req.name.strip(), hash_access_key(access_key), req.monthly_budget_usd),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["monthly_budget_usd"] = float(row["monthly_budget_usd"])
    return {"user": row, "access_key": access_key}


@app.get("/api/admin/usage")
def admin_usage(
    days: int = Query(default=30, ge=1, le=365),
    admin: dict = Depends(require_admin),
):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  u.id, u.name, u.is_admin, u.enabled, u.monthly_budget_usd, u.created_at,
                  COALESCE(SUM(mu.estimated_cost_usd) FILTER (
                    WHERE mu.created_at >= date_trunc('month', NOW())
                  ), 0) AS month_spend,
                  COUNT(mu.id) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day') AND mu.error IS NULL
                  ) AS requests,
                  COALESCE(SUM(mu.input_tokens) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day')
                  ), 0) AS input_tokens,
                  COALESCE(SUM(mu.output_tokens) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day')
                  ), 0) AS output_tokens,
                  COALESCE(SUM(mu.estimated_cost_usd) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day')
                  ), 0) AS period_cost,
                  COUNT(mu.id) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day') AND mu.provider='local'
                  ) AS local_requests,
                  COUNT(mu.id) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day') AND mu.provider='openai'
                  ) AS openai_requests,
                  COUNT(mu.id) FILTER (
                    WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day') AND mu.error IS NOT NULL
                  ) AS errors
                FROM atlas_users u
                LEFT JOIN model_usage mu ON mu.workspace_id=u.id
                GROUP BY u.id
                ORDER BY u.is_admin DESC, u.created_at ASC
                """,
                (days, days, days, days, days, days, days),
            )
            users = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT
                  mu.id, mu.workspace_id, u.name AS user_name, mu.provider, mu.model,
                  mu.input_tokens, mu.output_tokens, mu.total_tokens, mu.web_search_calls,
                  mu.estimated_cost_usd, mu.deep, mu.web, mu.semantic_memory,
                  mu.memory_refs, mu.skill_refs, mu.latency_ms, mu.error, mu.created_at
                FROM model_usage mu
                LEFT JOIN atlas_users u ON u.id=mu.workspace_id
                WHERE mu.created_at >= NOW() - (%s * INTERVAL '1 day')
                ORDER BY mu.id DESC
                LIMIT 100
                """,
                (days,),
            )
            recent = [dict(r) for r in cur.fetchall()]

    normalized_users = []
    for row in users:
        budget = float(row["monthly_budget_usd"] or 0)
        spend = float(row["month_spend"] or 0)
        normalized_users.append({
            **row,
            "monthly_budget_usd": budget,
            "month_spend": spend,
            "remaining_usd": None if budget <= 0 else max(0.0, budget - spend),
            "requests": int(row["requests"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "period_cost": float(row["period_cost"] or 0),
            "local_requests": int(row["local_requests"] or 0),
            "openai_requests": int(row["openai_requests"] or 0),
            "errors": int(row["errors"] or 0),
        })
    normalized_recent = []
    for row in recent:
        row["estimated_cost_usd"] = float(row["estimated_cost_usd"] or 0)
        normalized_recent.append(row)

    return {"days": days, "users": normalized_users, "recent": normalized_recent}


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(r'''<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b0f">
<title>Atlas</title>
<style>
:root{--bg:#0b0b0f;--panel:#15151a;--panel2:#1b1b21;--line:#32323c;--purple:#c5a3ff;--purple2:#9774d2;--text:#eee8ff;--muted:#9d91b7;--button:#24242b;--danger:#d26f8a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea,select{font:inherit}button{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--button);color:var(--purple);font-weight:700}button.primary{background:var(--purple);color:#160f20;border-color:var(--purple)}button.ghost{background:transparent}button.danger{color:#f0a2b5}button:disabled{opacity:.55}input,textarea,select{width:100%;background:#101014;color:var(--text);border:1px solid #3a3a45;border-radius:12px;padding:11px}textarea{min-height:90px;resize:vertical}.wrap{max-width:760px;margin:auto;min-height:100vh;padding:16px 16px 92px}.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px}.brand{font-size:26px;font-weight:850;color:var(--purple)}.sub{font-size:12px;color:var(--muted);margin-top:2px}.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px;margin:10px 0}.cardTitle{font-size:17px;font-weight:800;color:var(--purple);margin-bottom:4px}.small{font-size:12px;color:var(--muted)}.row{display:flex;gap:8px;align-items:center}.row.wraprow{flex-wrap:wrap}.spread{display:flex;justify-content:space-between;align-items:center;gap:10px}.stack{display:flex;flex-direction:column;gap:8px}.page{display:none}.page.active{display:block}.chat{min-height:46vh;padding-bottom:8px}.msg{padding:12px;border-radius:16px;margin:10px 0;white-space:pre-wrap;line-height:1.45}.msg.user{background:#2a2a32;margin-left:12%;color:#e7d9ff}.msg.atlas{background:#17171c;border:1px solid #2c2c34;margin-right:7%;color:var(--purple)}.feedback{display:flex;gap:6px;margin-top:9px}.feedback button{font-size:12px;padding:7px 9px}.thinking{opacity:.82;font-style:italic}.dots span{animation:blink 1.2s infinite;opacity:.2}.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}.composer{position:sticky;bottom:78px;background:rgba(11,11,15,.97);padding-top:8px}.composer textarea{min-height:58px}.skillChip{display:none;margin:0 0 7px;padding:8px 10px;background:#21182d;border:1px solid #44305f;border-radius:12px;color:var(--purple);font-size:13px}.field{padding:11px 0;border-top:1px solid #292932}.field:first-of-type{border-top:0}.fieldLabel{font-weight:750;color:#d9c5ff}.fieldValue{font-size:14px;color:#c9bddf;margin-top:4px;white-space:pre-wrap}.typeTag{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#826da8}.sectionHeader{display:flex;justify-content:space-between;align-items:center;gap:8px}.skillCard{cursor:pointer}.lesson{padding:8px 0;border-top:1px solid #292932;font-size:13px;color:#cdbce8}.empty{padding:28px 14px;text-align:center;color:var(--muted);border:1px dashed #353540;border-radius:16px}.nav{position:fixed;left:0;right:0;bottom:0;background:rgba(12,12,16,.98);border-top:1px solid #282832;padding:8px max(10px,env(safe-area-inset-right)) calc(8px + env(safe-area-inset-bottom)) max(10px,env(safe-area-inset-left));display:flex;justify-content:center;z-index:20}.navin{width:min(760px,100%);display:grid;grid-template-columns:repeat(5,1fr);gap:7px}.nav button{padding:10px 5px;background:#17171c;color:#9286a8}.nav button.active{color:var(--purple);background:#251b32}.overlay{display:none;position:fixed;inset:0;background:rgba(5,5,8,.96);z-index:50;overflow:auto}.sheet{max-width:620px;margin:auto;padding:18px 16px calc(30px + env(safe-area-inset-bottom))}.sheetTop{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:16px}.sheetTitle{font-size:22px;font-weight:850;color:var(--purple)}.formField{margin:12px 0}.formField label{display:block;font-size:13px;color:#c7b0e8;margin-bottom:6px;font-weight:700}.toggleRow{display:flex;align-items:center;gap:8px;color:#c7b0e8;font-size:13px}.toggleRow input{width:auto}#accessCard{display:none}.status{font-size:12px;color:var(--muted);margin-top:7px;min-height:16px}.metricGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:10px 0}.metric{background:#101014;border:1px solid #292932;border-radius:14px;padding:12px}.metricValue{font-size:20px;font-weight:800;color:var(--purple)}.auditRow{padding:10px 0;border-top:1px solid #292932}.secretBox{word-break:break-all;background:#101014;border:1px solid #3a3a45;border-radius:12px;padding:12px;color:#f1e8ff;margin:10px 0}.adminOnly{display:none}@media(max-width:430px){.wrap{padding-left:12px;padding-right:12px}button{padding:9px 10px}.brand{font-size:24px}.nav button{font-size:11px}.metricGrid{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><div class="brand">Atlas</div><div id="sessionLabel" class="sub">Private workspace</div></div>
    <button class="ghost" onclick="toggleAccess()">Access</button>
  </div>

  <div id="accessCard" class="card">
    <div class="cardTitle">Private Access</div>
    <div class="small">Each person gets a different key and a completely separate Atlas workspace.</div>
    <input id="accessKey" type="password" placeholder="Atlas access key" style="margin-top:10px">
    <button onclick="saveAccess()" style="margin-top:8px">Save / switch account</button>
    <div id="accessStatus" class="status"></div>
  </div>

  <section id="page-chat" class="page active">
    <div id="chatMessages" class="chat"></div>
    <div class="composer">
      <div id="activeSkillChip" class="skillChip"></div>
      <div class="card">
        <textarea id="promptBox" placeholder="Ask Atlas anything..."></textarea>
        <div class="row wraprow" style="margin:9px 0">
          <label class="toggleRow"><input id="webToggle" type="checkbox"> Web</label>
          <label class="toggleRow"><input id="deepToggle" type="checkbox"> Deep</label>
        </div>
        <div class="row"><button onclick="teachAtlas()">Teach</button><button class="primary" style="flex:1" onclick="sendMessage()">Send</button></div>
        <div id="chatStatus" class="status"></div>
      </div>
    </div>
  </section>

  <section id="page-me" class="page">
    <div class="spread"><div><div class="cardTitle">About Me</div><div class="small">Private to this account.</div></div><button onclick="openSectionEditor()">+ Section</button></div>
    <div id="profileList"></div>
  </section>

  <section id="page-skills" class="page">
    <div id="skillsHome"><div class="spread"><div><div class="cardTitle">Skills</div><div class="small">Private playbooks for this account.</div></div><button onclick="openSkillEditor()">+ Skill</button></div><div id="skillsList"></div></div>
    <div id="skillDetail" style="display:none"></div>
  </section>

  <section id="page-settings" class="page">
    <div class="card"><div class="cardTitle">Brain</div><div class="small">Atlas owns the routing. Pick a provider, or let Auto choose.</div><select id="brainSelect" onchange="saveBrainChoice()" style="margin-top:10px"><option value="auto">Auto</option><option value="openai">OpenAI</option><option id="localBrainOption" value="local">Local</option></select><div id="brainInfo" class="small" style="margin-top:8px">Loading provider status...</div></div>
    <div class="card"><div class="cardTitle">Usage this month</div><div id="usageInfo" class="small">Loading...</div></div>
    <div class="spread" style="margin-top:18px"><div><div class="cardTitle">Behavior</div><div class="small">Response rules for this account.</div></div><button onclick="openBehaviorEditor()">+ Rule</button></div>
    <div id="behaviorList"></div>
    <div class="card" style="margin-top:18px"><div class="cardTitle">System</div><div id="systemInfo" class="small">Atlas v0.9.0-alpha</div></div>
  </section>

  <section id="page-admin" class="page">
    <div class="spread"><div><div class="cardTitle">Admin</div><div class="small">Usage and diagnostics. Conversation text is not shown here.</div></div><button onclick="createTester()">+ Tester</button></div>
    <div id="adminSummary"></div>
    <div id="adminUsers"></div>
    <div id="adminEvents"></div>
  </section>
</div>

<nav class="nav"><div class="navin"><button data-page="chat" class="active" onclick="showPage('chat')">Chat</button><button data-page="me" onclick="showPage('me')">Me</button><button data-page="skills" onclick="showPage('skills')">Skills</button><button data-page="settings" onclick="showPage('settings')">Settings</button><button id="adminNav" data-page="admin" class="adminOnly" onclick="showPage('admin')">Admin</button></div></nav>

<div id="editorOverlay" class="overlay"><div class="sheet"><div class="sheetTop"><div id="editorTitle" class="sheetTitle">Edit</div><button onclick="closeEditor()">Close</button></div><div id="editorBody"></div><div id="editorStatus" class="status"></div></div></div>

<script>
let key=localStorage.getItem("atlas_key")||"";
let cid=localStorage.getItem("atlas_cid")||null;
let activeSkill=JSON.parse(localStorage.getItem("atlas_active_skill")||"null");
let brainChoice=localStorage.getItem("atlas_brain")||"auto";
let currentSkill=null,profileCache=[],skillsCache=[],behaviorCache=[],session=null;
accessKey.value=key;brainSelect.value=brainChoice;if(!key)accessCard.style.display="block";updateSkillChip();

function authHeaders(json=true){let h={"X-Atlas-Key":key};if(json)h["Content-Type"]="application/json";return h}
async function api(url,options={}){options.headers=options.headers||authHeaders(options.method&&options.method!=="GET");let r=await fetch(url,options);let data={};try{data=await r.json()}catch(_e){}if(!r.ok)throw new Error(data.detail||"Request failed");return data}
function toggleAccess(){accessCard.style.display=accessCard.style.display==="block"?"none":"block"}
async function saveAccess(){key=accessKey.value.trim();localStorage.setItem("atlas_key",key);cid=null;activeSkill=null;localStorage.removeItem("atlas_cid");localStorage.removeItem("atlas_active_skill");updateSkillChip();try{await loadSession();accessCard.style.display="none";chatMessages.innerHTML="";accessStatus.textContent="";chatStatus.textContent="Account switched."}catch(e){accessStatus.textContent=e.message;accessCard.style.display="block"}}
async function loadSession(){if(!key){accessCard.style.display="block";return}session=await api("/api/session",{headers:authHeaders(false)});sessionLabel.textContent=session.name+(session.is_admin?" • Admin":"")+" • "+session.workspace_id;document.querySelectorAll(".adminOnly").forEach(x=>x.style.display=session.is_admin?"block":"none");return session}
function saveBrainChoice(){brainChoice=brainSelect.value;localStorage.setItem("atlas_brain",brainChoice);brainInfo.textContent="Brain preference saved on this device."}
async function loadSystem(){if(!key){toggleAccess();return}try{let data=await api("/api/system",{headers:authHeaders(false)});localBrainOption.disabled=!data.local_configured;if(brainChoice==="local"&&!data.local_configured){brainChoice="auto";brainSelect.value="auto";localStorage.setItem("atlas_brain","auto")}let providers=[];if(data.openai_configured)providers.push("OpenAI ready");if(data.local_configured)providers.push("Local ready: "+data.local_model);else providers.push("Local not connected yet");brainInfo.textContent=providers.join(" • ");systemInfo.textContent="Atlas v"+data.version+" • Workspace: "+data.workspace_name+" • Semantic memory: "+(data.semantic_memory?"on":"off")+" • Embeddings: "+data.embedding_backend+" / "+data.embedding_model+" • Training examples: "+data.training_candidates;let u=await api("/api/usage",{headers:authHeaders(false)});let budget=u.budget_usd<=0?"Unlimited":("$"+u.budget_usd.toFixed(2));let rem=u.remaining_usd===null?"Unlimited":("$"+u.remaining_usd.toFixed(2));usageInfo.textContent="$"+u.estimated_cost_usd.toFixed(4)+" estimated • "+u.requests+" requests • "+u.total_tokens.toLocaleString()+" tokens • Budget "+budget+" • Remaining "+rem}catch(e){brainInfo.textContent="System status error: "+e.message}}
function showPage(name){if(name==="admin"&&(!session||!session.is_admin))return;document.querySelectorAll(".page").forEach(x=>x.classList.remove("active"));document.getElementById("page-"+name).classList.add("active");document.querySelectorAll(".nav button").forEach(x=>x.classList.toggle("active",x.dataset.page===name));if(name==="me")loadProfile();if(name==="skills")loadSkills();if(name==="settings"){loadBehavior();loadSystem()}if(name==="admin")loadAdmin()}

function addMessage(text,who,id){let d=document.createElement("div");d.className="msg "+(who==="user"?"user":"atlas");d.append(document.createTextNode(text));if(who==="atlas"&&id){let f=document.createElement("div");f.className="feedback";let good=document.createElement("button");good.textContent="Useful";good.onclick=async()=>{if(await rate(id,1,null)){good.textContent="Saved";good.disabled=true}};let fix=document.createElement("button");fix.textContent="Needs fix";fix.onclick=()=>correct(id,fix);f.append(good,fix);d.appendChild(f)}chatMessages.appendChild(d);window.scrollTo(0,document.body.scrollHeight);return d}
function thinkingBubble(){let d=document.createElement("div");d.className="msg atlas thinking";d.innerHTML='Atlas is thinking<span class="dots"><span>.</span><span>.</span><span>.</span></span>';chatMessages.appendChild(d);window.scrollTo(0,document.body.scrollHeight);return d}
async function sendMessage(){let message=promptBox.value.trim();if(!message)return;if(!key){toggleAccess();return}addMessage(message,"user");promptBox.value="";chatStatus.textContent="";let thinking=thinkingBubble();try{let data=await api("/api/chat",{method:"POST",headers:authHeaders(true),body:JSON.stringify({message,conversation_id:cid,use_web:webToggle.checked,deep:deepToggle.checked,active_skill_id:activeSkill?activeSkill.id:null,provider:brainChoice})});thinking.remove();cid=data.conversation_id;localStorage.setItem("atlas_cid",cid);addMessage(data.answer,"atlas",data.assistant_message_id);let used=(data.skills_used||[]).map(x=>x.name);let cost=data.usage?" • $"+Number(data.usage.estimated_cost_usd||0).toFixed(4):"";chatStatus.textContent="Brain: "+data.provider+" / "+data.model+(data.semantic_memory?" • Semantic memory":" • Memory fallback")+(used.length?" • Skills: "+used.join(", "):"")+cost}catch(e){thinking.remove();addMessage("Error: "+e.message,"atlas")}}
async function teachAtlas(){if(!key){toggleAccess();return}let text=prompt("What should Atlas remember?");if(!text)return;try{await api("/api/memory",{method:"POST",headers:authHeaders(true),body:JSON.stringify({content:text})});chatStatus.textContent="Saved to this Atlas workspace."}catch(e){chatStatus.textContent="Memory error: "+e.message}}
async function rate(id,rating,correction){try{let data=await api("/api/feedback",{method:"POST",headers:authHeaders(true),body:JSON.stringify({message_id:id,rating,correction})});chatStatus.textContent=correction?(data.skill_lesson_saved?"Correction saved to memory and the active skill.":"Correction saved to Atlas memory."):"Feedback saved.";return true}catch(e){chatStatus.textContent="Feedback error: "+e.message;return false}}
async function correct(id,button){let text=prompt("What should Atlas have answered or done instead?");if(!text)return;if(await rate(id,-1,text)){button.textContent="Correction saved";button.disabled=true}}

function updateSkillChip(){if(activeSkill){activeSkillChip.style.display="flex";activeSkillChip.innerHTML="";let name=document.createElement("span");name.textContent="Using skill: "+activeSkill.name;name.style.flex="1";let clear=document.createElement("button");clear.textContent="Clear";clear.style.padding="4px 8px";clear.onclick=clearActiveSkill;activeSkillChip.append(name,clear)}else{activeSkillChip.style.display="none";activeSkillChip.innerHTML=""}}
function setActiveSkill(skill){activeSkill={id:skill.id,name:skill.name};localStorage.setItem("atlas_active_skill",JSON.stringify(activeSkill));updateSkillChip();showPage("chat");chatStatus.textContent="Atlas will use "+skill.name+" for this chat."}
function clearActiveSkill(){activeSkill=null;localStorage.removeItem("atlas_active_skill");updateSkillChip();chatStatus.textContent="Active skill cleared."}
function openEditor(title,html){editorTitle.textContent=title;editorBody.innerHTML=html;editorStatus.textContent="";editorOverlay.style.display="block"}
function closeEditor(){editorOverlay.style.display="none";editorBody.innerHTML="";editorStatus.textContent=""}
function formValue(id){return document.getElementById(id).value.trim()}
function fieldTypeOptions(selected){return["text","note","rule","checklist","steps"].map(x=>`<option value="${x}" ${x===selected?"selected":""}>${x}</option>`).join("")}

async function loadProfile(){if(!key){toggleAccess();return}try{let data=await api("/api/profile",{headers:authHeaders(false)});profileCache=data.sections||[];renderProfile()}catch(e){profileList.innerHTML=`<div class="empty">Could not load About Me: ${escapeHtml(e.message)}</div>`}}
function renderProfile(){profileList.innerHTML="";if(!profileCache.length){profileList.innerHTML='<div class="empty">No sections yet. This account starts clean and private.</div>';return}for(const section of profileCache){let card=document.createElement("div");card.className="card";let head=document.createElement("div");head.className="sectionHeader";let left=document.createElement("div");left.innerHTML=`<div class="cardTitle">${escapeHtml(section.name)}</div><div class="small">${section.fields.length} field${section.fields.length===1?"":"s"}</div>`;let actions=document.createElement("div");actions.className="row";let add=document.createElement("button");add.textContent="+ Field";add.onclick=()=>openProfileFieldEditor(section.id);let edit=document.createElement("button");edit.textContent="•••";edit.onclick=()=>openSectionEditor(section);actions.append(add,edit);head.append(left,actions);card.appendChild(head);for(const field of section.fields){let row=document.createElement("div");row.className="field";row.onclick=()=>openProfileFieldEditor(section.id,field);row.innerHTML=`<div class="spread"><div class="fieldLabel">${escapeHtml(field.label)}</div><div class="typeTag">${escapeHtml(field.field_type)}</div></div><div class="fieldValue">${escapeHtml(field.value||"(empty)")}</div>`;card.appendChild(row)}profileList.appendChild(card)}}
function openSectionEditor(section=null){let editing=!!section;openEditor(editing?"Edit Section":"New Section",`<div class="formField"><label>Section name</label><input id="edSectionName" value="${editing?escapeAttr(section.name):""}" placeholder="e.g. Work, Gear, Preferences"></div><div class="row">${editing?'<button class="danger" onclick="deleteSection('+section.id+')">Delete</button>':""}<button class="primary" style="flex:1" onclick="saveSection(${editing?section.id:"null"})">Save</button></div>`)}
async function saveSection(id){let name=formValue("edSectionName");if(!name){editorStatus.textContent="Give the section a name.";return}try{await api(id?"/api/profile/sections/"+id:"/api/profile/sections",{method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify({name})});closeEditor();await loadProfile()}catch(e){editorStatus.textContent=e.message}}
async function deleteSection(id){if(!confirm("Delete this section and every field inside it?"))return;try{await api("/api/profile/sections/"+id,{method:"DELETE",headers:authHeaders(false)});closeEditor();await loadProfile()}catch(e){editorStatus.textContent=e.message}}
function openProfileFieldEditor(sectionId,field=null){let editing=!!field;openEditor(editing?"Edit Field":"New Field",`<div class="formField"><label>Field name</label><input id="edFieldLabel" value="${editing?escapeAttr(field.label):""}" placeholder="Anything you want Atlas to know"></div><div class="formField"><label>Type</label><select id="edFieldType">${fieldTypeOptions(editing?field.field_type:"text")}</select></div><div class="formField"><label>Value</label><textarea id="edFieldValue" placeholder="Teach Atlas the actual information">${editing?escapeHtml(field.value):""}</textarea></div><div class="formField"><label class="toggleRow"><input id="edInclude" type="checkbox" ${!editing||field.include_in_chat?"checked":""}> Use this information in chat</label></div><div class="row">${editing?'<button class="danger" onclick="deleteProfileField('+field.id+')">Delete</button>':""}<button class="primary" style="flex:1" onclick="saveProfileField(${sectionId},${editing?field.id:"null"})">Save</button></div>`)}
async function saveProfileField(sectionId,id){let payload={section_id:sectionId,label:formValue("edFieldLabel"),field_type:document.getElementById("edFieldType").value,value:formValue("edFieldValue"),include_in_chat:document.getElementById("edInclude").checked};if(!payload.label){editorStatus.textContent="Give the field a name.";return}if(id)delete payload.section_id;try{await api(id?"/api/profile/fields/"+id:"/api/profile/fields",{method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)});closeEditor();await loadProfile()}catch(e){editorStatus.textContent=e.message}}
async function deleteProfileField(id){if(!confirm("Delete this field?"))return;try{await api("/api/profile/fields/"+id,{method:"DELETE",headers:authHeaders(false)});closeEditor();await loadProfile()}catch(e){editorStatus.textContent=e.message}}

async function loadSkills(){if(!key){toggleAccess();return}try{let data=await api("/api/skills",{headers:authHeaders(false)});skillsCache=data.skills||[];if(currentSkill){currentSkill=skillsCache.find(x=>x.id===currentSkill.id)||null;if(currentSkill)renderSkillDetail(currentSkill);else showSkillsHome()}else renderSkillsList()}catch(e){skillsList.innerHTML=`<div class="empty">Could not load skills: ${escapeHtml(e.message)}</div>`}}
function renderSkillsList(){skillsHome.style.display="block";skillDetail.style.display="none";skillsList.innerHTML="";if(!skillsCache.length){skillsList.innerHTML='<div class="empty">No skills yet. This account gets its own playbooks.</div>';return}for(const skill of skillsCache){let card=document.createElement("div");card.className="card skillCard";card.onclick=()=>openSkill(skill.id);card.innerHTML=`<div class="spread"><div><div class="cardTitle">${escapeHtml(skill.name)}</div><div class="small">${escapeHtml(skill.description||"No description yet")}</div></div><div class="typeTag">${skill.enabled?"ON":"OFF"}</div></div><div class="small" style="margin-top:8px">${skill.fields.length} fields • ${skill.lessons.length} learned correction${skill.lessons.length===1?"":"s"}</div>`;skillsList.appendChild(card)}}
function openSkill(id){currentSkill=skillsCache.find(x=>x.id===id);if(currentSkill)renderSkillDetail(currentSkill)}function showSkillsHome(){currentSkill=null;skillsHome.style.display="block";skillDetail.style.display="none";renderSkillsList()}
function renderSkillDetail(skill){skillsHome.style.display="none";skillDetail.style.display="block";skillDetail.innerHTML="";let top=document.createElement("div");top.className="stack";top.innerHTML=`<div class="row"><button onclick="showSkillsHome()">Back</button><button onclick="openSkillEditorById(${skill.id})">Edit</button><button class="primary" style="flex:1" onclick="useSkillById(${skill.id})">Use in Chat</button></div><div><div class="cardTitle" style="font-size:22px">${escapeHtml(skill.name)}</div><div class="small">${escapeHtml(skill.description||"No description yet")}</div></div>`;skillDetail.appendChild(top);let fields=document.createElement("div");fields.className="card";let fh=document.createElement("div");fh.className="spread";fh.innerHTML='<div><div class="cardTitle">Playbook</div><div class="small">Knowledge Atlas needs for this skill.</div></div>';let add=document.createElement("button");add.textContent="+ Field";add.onclick=()=>openSkillFieldEditor(skill.id);fh.appendChild(add);fields.appendChild(fh);if(!skill.fields.length){let e=document.createElement("div");e.className="empty";e.style.marginTop="12px";e.textContent="No fields yet.";fields.appendChild(e)}else{for(const field of skill.fields){let row=document.createElement("div");row.className="field";row.onclick=()=>openSkillFieldEditor(skill.id,field);row.innerHTML=`<div class="spread"><div class="fieldLabel">${escapeHtml(field.label)}</div><div class="typeTag">${escapeHtml(field.field_type)}</div></div><div class="fieldValue">${escapeHtml(field.value||"(empty)")}</div>`;fields.appendChild(row)}}skillDetail.appendChild(fields);if(skill.lessons.length){let lessons=document.createElement("div");lessons.className="card";lessons.innerHTML='<div class="cardTitle">Learned Corrections</div><div class="small">Corrections made while this skill was explicitly active.</div>';for(const lesson of skill.lessons){let d=document.createElement("div");d.className="lesson";d.textContent=lesson.correction;lessons.appendChild(d)}skillDetail.appendChild(lessons)}}
function openSkillEditor(skill=null){let editing=!!skill;openEditor(editing?"Edit Skill":"New Skill",`<div class="formField"><label>Skill name</label><input id="edSkillName" value="${editing?escapeAttr(skill.name):""}" placeholder="e.g. Church Mix Workflow"></div><div class="formField"><label>What is this skill for?</label><textarea id="edSkillDesc" placeholder="Keep this focused.">${editing?escapeHtml(skill.description):""}</textarea></div>${editing?'<div class="formField"><label class="toggleRow"><input id="edSkillEnabled" type="checkbox" '+(skill.enabled?"checked":"")+'> Allow Atlas to use this skill</label></div>':""}<div class="row">${editing?'<button class="danger" onclick="deleteSkill('+skill.id+')">Delete</button>':""}<button class="primary" style="flex:1" onclick="saveSkill(${editing?skill.id:"null"})">Save</button></div>`)}
function openSkillEditorById(id){let skill=skillsCache.find(x=>x.id===id);if(skill)openSkillEditor(skill)}
async function saveSkill(id){let payload={name:formValue("edSkillName"),description:formValue("edSkillDesc")};if(id)payload.enabled=document.getElementById("edSkillEnabled").checked;if(!payload.name){editorStatus.textContent="Give the skill a name.";return}try{let saved=await api(id?"/api/skills/"+id:"/api/skills",{method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)});closeEditor();currentSkill=id?{id}:null;await loadSkills();if(!id)openSkill(saved.id)}catch(e){editorStatus.textContent=e.message}}
async function deleteSkill(id){if(!confirm("Delete this skill, its fields, and learned corrections?"))return;try{await api("/api/skills/"+id,{method:"DELETE",headers:authHeaders(false)});if(activeSkill&&activeSkill.id===id)clearActiveSkill();closeEditor();showSkillsHome();await loadSkills()}catch(e){editorStatus.textContent=e.message}}
function openSkillFieldEditor(skillId,field=null){let editing=!!field;openEditor(editing?"Edit Skill Field":"New Skill Field",`<div class="formField"><label>Field name</label><input id="edSkillFieldLabel" value="${editing?escapeAttr(field.label):""}" placeholder="e.g. Steps, Rules, Export settings"></div><div class="formField"><label>Type</label><select id="edSkillFieldType">${fieldTypeOptions(editing?field.field_type:"note")}</select></div><div class="formField"><label>Teach Atlas</label><textarea id="edSkillFieldValue">${editing?escapeHtml(field.value):""}</textarea></div><div class="row">${editing?'<button class="danger" onclick="deleteSkillField('+field.id+')">Delete</button>':""}<button class="primary" style="flex:1" onclick="saveSkillField(${skillId},${editing?field.id:"null"})">Save</button></div>`)}
async function saveSkillField(skillId,id){let payload={skill_id:skillId,label:formValue("edSkillFieldLabel"),field_type:document.getElementById("edSkillFieldType").value,value:formValue("edSkillFieldValue")};if(!payload.label){editorStatus.textContent="Give the field a name.";return}if(id)delete payload.skill_id;try{await api(id?"/api/skills/fields/"+id:"/api/skills/fields",{method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)});closeEditor();currentSkill={id:skillId};await loadSkills()}catch(e){editorStatus.textContent=e.message}}
async function deleteSkillField(id){if(!confirm("Delete this field?"))return;try{await api("/api/skills/fields/"+id,{method:"DELETE",headers:authHeaders(false)});closeEditor();await loadSkills()}catch(e){editorStatus.textContent=e.message}}
function useSkillById(id){let skill=skillsCache.find(x=>x.id===id);if(skill)setActiveSkill(skill)}

async function loadBehavior(){if(!key){toggleAccess();return}try{let data=await api("/api/behavior",{headers:authHeaders(false)});behaviorCache=data.fields||[];renderBehavior()}catch(e){behaviorList.innerHTML=`<div class="empty">Could not load behavior: ${escapeHtml(e.message)}</div>`}}
function renderBehavior(){behaviorList.innerHTML="";if(!behaviorCache.length){behaviorList.innerHTML='<div class="empty">No custom behavior rules yet. Atlas still uses its concise default behavior.</div>';return}for(const field of behaviorCache){let card=document.createElement("div");card.className="card";card.onclick=()=>openBehaviorEditor(field);card.innerHTML=`<div class="fieldLabel">${escapeHtml(field.label)}</div><div class="fieldValue">${escapeHtml(field.value||"(empty)")}</div>`;behaviorList.appendChild(card)}}
function openBehaviorEditor(field=null){let editing=!!field;openEditor(editing?"Edit Behavior Rule":"New Behavior Rule",`<div class="formField"><label>Rule name</label><input id="edBehaviorLabel" value="${editing?escapeAttr(field.label):""}" placeholder="e.g. Response style"></div><div class="formField"><label>Instruction</label><textarea id="edBehaviorValue">${editing?escapeHtml(field.value):""}</textarea></div><div class="row">${editing?'<button class="danger" onclick="deleteBehavior('+field.id+')">Delete</button>':""}<button class="primary" style="flex:1" onclick="saveBehavior(${editing?field.id:"null"})">Save</button></div>`)}
async function saveBehavior(id){let payload={label:formValue("edBehaviorLabel"),value:formValue("edBehaviorValue")};if(!payload.label){editorStatus.textContent="Give the rule a name.";return}try{await api(id?"/api/behavior/fields/"+id:"/api/behavior/fields",{method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)});closeEditor();await loadBehavior()}catch(e){editorStatus.textContent=e.message}}
async function deleteBehavior(id){if(!confirm("Delete this behavior rule?"))return;try{await api("/api/behavior/fields/"+id,{method:"DELETE",headers:authHeaders(false)});closeEditor();await loadBehavior()}catch(e){editorStatus.textContent=e.message}}

async function createTester(){let name=prompt("Tester name");if(!name)return;let raw=prompt("Monthly OpenAI budget in USD","10");if(raw===null)return;let budget=Number(raw);if(!Number.isFinite(budget)||budget<0){alert("Enter a valid budget.");return}try{let data=await api("/api/admin/users",{method:"POST",headers:authHeaders(true),body:JSON.stringify({name,monthly_budget_usd:budget})});openEditor("Tester created",`<div class="small">Give this key to ${escapeHtml(data.user.name)}. Atlas stores only a hash, so copy it now.</div><div class="secretBox">${escapeHtml(data.access_key)}</div><div class="small">Budget: $${Number(data.user.monthly_budget_usd).toFixed(2)}/month</div>`);await loadAdmin()}catch(e){alert(e.message)}}
async function loadAdmin(){if(!session||!session.is_admin)return;try{let data=await api("/api/admin/usage?days=30",{headers:authHeaders(false)});let totalCost=data.users.reduce((a,u)=>a+Number(u.period_cost||0),0);let totalReq=data.users.reduce((a,u)=>a+Number(u.requests||0),0);adminSummary.innerHTML=`<div class="metricGrid"><div class="metric"><div class="small">30-day requests</div><div class="metricValue">${totalReq.toLocaleString()}</div></div><div class="metric"><div class="small">30-day estimated cost</div><div class="metricValue">$${totalCost.toFixed(3)}</div></div></div>`;adminUsers.innerHTML='<div class="card"><div class="cardTitle">Accounts</div></div>';let uc=adminUsers.firstChild;for(const u of data.users){let budget=Number(u.monthly_budget_usd)<=0?"Unlimited":"$"+Number(u.monthly_budget_usd).toFixed(2);let rem=u.remaining_usd===null?"Unlimited":"$"+Number(u.remaining_usd).toFixed(2);let d=document.createElement("div");d.className="auditRow";d.innerHTML=`<div class="spread"><div><div class="fieldLabel">${escapeHtml(u.name)}${u.is_admin?" • Admin":""}</div><div class="small">${escapeHtml(u.id)}</div></div><div class="typeTag">${u.enabled?"ACTIVE":"OFF"}</div></div><div class="small" style="margin-top:6px">Month: $${Number(u.month_spend).toFixed(4)} / ${budget} • Remaining ${rem} • 30d requests ${u.requests} • OpenAI ${u.openai_requests} • Local ${u.local_requests} • Errors ${u.errors}</div>`;uc.appendChild(d)}adminEvents.innerHTML='<div class="card"><div class="cardTitle">Recent diagnostics</div><div class="small">No prompt or response text is exposed here.</div></div>';let ec=adminEvents.firstChild;for(const e of data.recent.slice(0,40)){let skills=(e.skill_refs||[]).map(x=>x.name).join(", ")||"none";let memories=(e.memory_refs||[]).map(x=>x.kind+" #"+x.id).join(", ")||"none";let d=document.createElement("div");d.className="auditRow";d.innerHTML=`<div class="spread"><div class="fieldLabel">${escapeHtml(e.user_name||e.workspace_id)}</div><div class="typeTag">${escapeHtml(e.provider||"error")}</div></div><div class="small">${escapeHtml(e.model||"")} • ${e.total_tokens||0} tokens • $${Number(e.estimated_cost_usd||0).toFixed(4)} • ${e.latency_ms||0} ms${e.web?" • Web":""}${e.deep?" • Deep":""}</div><div class="small">Memory: ${escapeHtml(memories)} • Skills: ${escapeHtml(skills)}${e.error?" • Error: "+escapeHtml(e.error):""}</div>`;ec.appendChild(d)}}catch(e){adminSummary.innerHTML=`<div class="empty">Admin data error: ${escapeHtml(e.message)}</div>`}}

function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}function escapeAttr(value){return escapeHtml(value)}
promptBox.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage()}});
(async()=>{if(key){try{await loadSession();await loadSystem()}catch(e){accessCard.style.display="block";accessStatus.textContent=e.message}}})();
</script>
</body>
</html>''')
