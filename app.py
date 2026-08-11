import os
import re
import json
import math
import hashlib
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.8.0"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ATLAS_MODEL = os.getenv("ATLAS_MODEL", "gpt-5-mini")
ATLAS_DEEP_MODEL = os.getenv("ATLAS_DEEP_MODEL", ATLAS_MODEL)

# Atlas owns routing. OpenAI is one provider, not the identity of the system.
ATLAS_DEFAULT_PROVIDER = os.getenv("ATLAS_DEFAULT_PROVIDER", "openai").strip().lower()
ATLAS_LOCAL_BASE_URL = os.getenv("ATLAS_LOCAL_BASE_URL", "").strip()
ATLAS_LOCAL_API_KEY = os.getenv("ATLAS_LOCAL_API_KEY", "").strip()
ATLAS_LOCAL_MODEL = os.getenv("ATLAS_LOCAL_MODEL", "local-model").strip()
ATLAS_LOCAL_DEEP_MODEL = os.getenv("ATLAS_LOCAL_DEEP_MODEL", ATLAS_LOCAL_MODEL).strip()

ATLAS_ACCESS_KEY = os.getenv("ATLAS_ACCESS_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ATLAS_ALLOW_WEB = os.getenv("ATLAS_ALLOW_WEB", "true").lower() == "true"

# Embeddings are also replaceable. By default they use OpenAI, but Atlas can
# point this at any OpenAI-compatible /embeddings endpoint later.
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
CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_model
  ON semantic_embeddings(model);
"""


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def init_db():
    if not DATABASE_URL:
        return
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()


def require_key(x_atlas_key: str | None = Header(default=None)):
    if not ATLAS_ACCESS_KEY:
        raise HTTPException(503, "ATLAS_ACCESS_KEY is not configured.")
    if x_atlas_key != ATLAS_ACCESS_KEY:
        raise HTTPException(401, "Invalid Atlas access key.")


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


def history_for(cid: str, limit: int = 24):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id=%s
                ORDER BY id DESC
                LIMIT %s
                """,
                (cid, limit),
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


def keyword_memories(message: str, limit: int = 6):
    terms = tokenize(message)[:8]
    if not terms:
        return []
    clauses = " OR ".join(["LOWER(content) LIKE %s"] * len(terms))
    params = [f"%{t}%" for t in terms] + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM memories
                WHERE ({clauses})
                ORDER BY importance DESC, updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return list(cur.fetchall())


def profile_data():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM profile_sections ORDER BY sort_order, id")
            sections = list(cur.fetchall())
            cur.execute(
                "SELECT * FROM profile_fields ORDER BY section_id, sort_order, id"
            )
            fields = list(cur.fetchall())

    by_section: dict[int, list[dict]] = {}
    for field in fields:
        by_section.setdefault(field["section_id"], []).append(dict(field))

    result = []
    for section in sections:
        item = dict(section)
        item["fields"] = by_section.get(section["id"], [])
        result.append(item)
    return result


def profile_context(max_chars: int = 12000) -> str:
    """Fallback context when semantic retrieval is unavailable."""
    lines = []
    for section in profile_data():
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

    payload = {
        "model": ATLAS_EMBED_MODEL,
        "input": texts,
        "encoding_format": "float",
    }
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


def semantic_sources() -> dict[tuple[str, int], dict]:
    sources: dict[tuple[str, int], dict] = {}

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, content, importance, source, updated_at FROM memories ORDER BY id"
            )
            for row in cur.fetchall():
                text = f"Memory [{row['kind']}]: {row['content']}"
                sources[("memory", row["id"])] = {
                    "text": text[:10000],
                    "row": dict(row),
                }

            cur.execute(
                """
                SELECT pf.id, pf.label, pf.field_type, pf.value, pf.include_in_chat,
                       ps.name AS section_name
                FROM profile_fields pf
                JOIN profile_sections ps ON ps.id=pf.section_id
                WHERE pf.include_in_chat=TRUE AND LENGTH(TRIM(pf.value)) > 0
                ORDER BY ps.sort_order, ps.id, pf.sort_order, pf.id
                """
            )
            for row in cur.fetchall():
                text = f"About Me / {row['section_name']} / {row['label']}: {row['value']}"
                sources[("profile", row["id"])] = {
                    "text": text[:10000],
                    "row": dict(row),
                }

    for skill in [s for s in skills_data() if s["enabled"]]:
        text = skill_to_context(skill, max_chars=10000)
        sources[("skill", skill["id"])] = {"text": text, "row": skill}

    return sources


async def ensure_semantic_index(sources: dict[tuple[str, int], dict]):
    if not ATLAS_SEMANTIC_MEMORY:
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_type, source_id, content_hash, model FROM semantic_embeddings"
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

    # Remove embeddings for deleted sources so the cache cannot resurrect old data.
    deleted = [key for key in existing if key not in sources]
    if deleted:
        with db() as conn:
            with conn.cursor() as cur:
                for source_type, source_id in deleted:
                    cur.execute(
                        "DELETE FROM semantic_embeddings WHERE source_type=%s AND source_id=%s",
                        (source_type, source_id),
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
                          source_type, source_id, content_hash, model, embedding, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                        ON CONFLICT(source_type, source_id) DO UPDATE SET
                          content_hash=EXCLUDED.content_hash,
                          model=EXCLUDED.model,
                          embedding=EXCLUDED.embedding,
                          updated_at=NOW()
                        """,
                        (source_type, source_id, h, ATLAS_EMBED_CACHE_KEY, json.dumps(vector)),
                    )
            conn.commit()


async def semantic_context(message: str, active_skill_id: int | None = None):
    sources = semantic_sources()
    await ensure_semantic_index(sources)
    query_vector = (await embed_texts([message]))[0]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_type, source_id, embedding
                FROM semantic_embeddings
                WHERE model=%s
                """,
                (ATLAS_EMBED_CACHE_KEY,),
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

    all_skills = [s for s in skills_data() if s["enabled"]]
    chosen_skills = []
    seen = set()
    if active_skill_id:
        active = next((s for s in all_skills if s["id"] == active_skill_id), None)
        if active:
            chosen_skills.append((active, "active"))
            seen.add(active["id"])

    for score, item in ranked["skill"]:
        skill = item["row"]
        if skill["id"] in seen:
            continue
        if score < 0.22:
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

def behavior_data():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM behavior_fields ORDER BY sort_order, id")
            return [dict(row) for row in cur.fetchall()]


def behavior_context(max_chars: int = 8000) -> str:
    rows = behavior_data()
    lines = [
        f"- {row['label']}: {row['value'].strip()}"
        for row in rows
        if (row["value"] or "").strip()
    ]
    text = "\n".join(lines).strip()
    return text[:max_chars] if text else "(none)"


def skills_data():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM skills ORDER BY updated_at DESC, id DESC")
            skills = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT * FROM skill_fields ORDER BY skill_id, sort_order, id")
            fields = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT * FROM skill_lessons ORDER BY skill_id, created_at DESC, id DESC"
            )
            lessons = [dict(row) for row in cur.fetchall()]

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


def find_relevant_skills(message: str, active_skill_id: int | None = None, limit: int = 2):
    all_skills = [s for s in skills_data() if s["enabled"]]
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


async def call_openai(model: str, messages: list[dict], instructions: str, use_web: bool):
    if not openai_configured():
        raise RuntimeError("OpenAI provider is not configured.")

    # Keep this payload deliberately small so Atlas can swap OpenAI model IDs
    # without coupling the whole app to model-specific optional parameters.
    payload = {
        "model": model,
        "input": messages,
        "instructions": instructions,
        "store": False,
    }

    if use_web:
        if not ATLAS_ALLOW_WEB:
            raise RuntimeError("Web search is disabled.")
        payload["tools"] = [{"type": "web_search"}]

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{OPENAI_BASE_URL.rstrip('/')}/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"OpenAI API error {response.status_code}: {detail}")

    answer = extract_text(response.json())
    if not answer:
        raise RuntimeError("OpenAI returned no text output.")
    return answer


async def call_local(model: str, messages: list[dict], instructions: str):
    if not local_configured():
        raise RuntimeError(
            "Local provider is not configured. Set ATLAS_LOCAL_BASE_URL and ATLAS_LOCAL_MODEL."
        )

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

    answer = extract_chat_completion_text(response.json())
    if not answer:
        raise RuntimeError("Local provider returned no text output.")
    return answer


async def call_model(
    provider_choice: str,
    deep: bool,
    use_web: bool,
    messages: list[dict],
    instructions: str,
):
    requested = normalize_provider(provider_choice)

    if requested == "local":
        if use_web:
            # Never silently send an explicitly local/private request to a cloud provider.
            raise RuntimeError("Web search requires the OpenAI provider. Turn Web off or choose Auto/OpenAI.")
        model = ATLAS_LOCAL_DEEP_MODEL if deep else ATLAS_LOCAL_MODEL
        return await call_local(model, messages, instructions), "local", model

    if requested == "openai":
        model = ATLAS_DEEP_MODEL if deep else ATLAS_MODEL
        return await call_openai(model, messages, instructions, use_web), "openai", model

    # Auto mode can fall back because the user explicitly delegated routing to Atlas.
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
                answer = await call_local(model, messages, instructions)
                return answer, "local", model

            if provider == "openai":
                if not openai_configured():
                    continue
                model = ATLAS_DEEP_MODEL if deep else ATLAS_MODEL
                answer = await call_openai(model, messages, instructions, use_web)
                return answer, "openai", model
        except Exception as exc:
            errors.append(f"{provider}: {exc}")

    if errors:
        raise RuntimeError("No model provider succeeded. " + " | ".join(errors))
    raise RuntimeError("No model provider is configured.")


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
        "access_key": bool(ATLAS_ACCESS_KEY),
        "semantic_memory": ATLAS_SEMANTIC_MEMORY,
        "embedding_model": ATLAS_EMBED_MODEL,
    }


@app.get("/api/system", dependencies=[Depends(require_key)])
def system_info():
    training_count = 0
    if DATABASE_URL:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM training_candidates")
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
        "embedding_backend": (
            "openai"
            if ATLAS_EMBED_BASE_URL.rstrip("/") == OPENAI_BASE_URL.rstrip("/")
            else "custom"
        ),
        "training_candidates": training_count,
    }


@app.get("/api/profile", dependencies=[Depends(require_key)])
def get_profile():
    return {"sections": profile_data()}


@app.post("/api/profile/sections", dependencies=[Depends(require_key)])
def create_profile_section(req: SectionCreate):
    with db() as conn:
        with conn.cursor() as cur:
            order = next_sort_order(cur, "profile_sections")
            cur.execute(
                """
                INSERT INTO profile_sections(name, sort_order)
                VALUES (%s, %s)
                RETURNING *
                """,
                (req.name.strip(), order),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["fields"] = []
    return row


@app.patch("/api/profile/sections/{section_id}", dependencies=[Depends(require_key)])
def update_profile_section(section_id: int, req: SectionUpdate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_sections
                SET name=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (req.name.strip(), section_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Section not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/profile/sections/{section_id}", dependencies=[Depends(require_key)])
def delete_profile_section(section_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM profile_sections WHERE id=%s RETURNING id", (section_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Section not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/profile/fields", dependencies=[Depends(require_key)])
def create_profile_field(req: ProfileFieldCreate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM profile_sections WHERE id=%s", (req.section_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Section not found.")
            order = next_sort_order(cur, "profile_fields", "section_id=%s", (req.section_id,))
            cur.execute(
                """
                INSERT INTO profile_fields(
                  section_id, label, field_type, value, include_in_chat, sort_order
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    req.section_id,
                    req.label.strip(),
                    clean_field_type(req.field_type),
                    req.value.strip(),
                    req.include_in_chat,
                    order,
                ),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/profile/fields/{field_id}", dependencies=[Depends(require_key)])
def update_profile_field(field_id: int, req: ProfileFieldUpdate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_fields
                SET label=%s, field_type=%s, value=%s, include_in_chat=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (
                    req.label.strip(),
                    clean_field_type(req.field_type),
                    req.value.strip(),
                    req.include_in_chat,
                    field_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/profile/fields/{field_id}", dependencies=[Depends(require_key)])
def delete_profile_field(field_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM profile_fields WHERE id=%s RETURNING id", (field_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Field not found.")
        conn.commit()
    return {"deleted": True}


@app.get("/api/skills", dependencies=[Depends(require_key)])
def get_skills():
    return {"skills": skills_data()}


@app.post("/api/skills", dependencies=[Depends(require_key)])
def create_skill(req: SkillCreate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skills(name, description)
                VALUES (%s, %s)
                RETURNING *
                """,
                (req.name.strip(), req.description.strip()),
            )
            row = dict(cur.fetchone())
        conn.commit()
    row["fields"] = []
    row["lessons"] = []
    return row


@app.patch("/api/skills/{skill_id}", dependencies=[Depends(require_key)])
def update_skill(skill_id: int, req: SkillUpdate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE skills
                SET name=%s, description=%s, enabled=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (req.name.strip(), req.description.strip(), req.enabled, skill_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Skill not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/skills/{skill_id}", dependencies=[Depends(require_key)])
def delete_skill(skill_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM skills WHERE id=%s RETURNING id", (skill_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Skill not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/skills/fields", dependencies=[Depends(require_key)])
def create_skill_field(req: SkillFieldCreate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM skills WHERE id=%s", (req.skill_id,))
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
                    req.skill_id,
                    req.label.strip(),
                    clean_field_type(req.field_type),
                    req.value.strip(),
                    order,
                ),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/skills/fields/{field_id}", dependencies=[Depends(require_key)])
def update_skill_field(field_id: int, req: SkillFieldUpdate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE skill_fields
                SET label=%s, field_type=%s, value=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (
                    req.label.strip(),
                    clean_field_type(req.field_type),
                    req.value.strip(),
                    field_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Skill field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/skills/fields/{field_id}", dependencies=[Depends(require_key)])
def delete_skill_field(field_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM skill_fields WHERE id=%s RETURNING id", (field_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Skill field not found.")
        conn.commit()
    return {"deleted": True}


@app.get("/api/behavior", dependencies=[Depends(require_key)])
def get_behavior():
    return {"fields": behavior_data()}


@app.post("/api/behavior/fields", dependencies=[Depends(require_key)])
def create_behavior_field(req: BehaviorFieldCreate):
    with db() as conn:
        with conn.cursor() as cur:
            order = next_sort_order(cur, "behavior_fields")
            cur.execute(
                """
                INSERT INTO behavior_fields(label, value, sort_order)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (req.label.strip(), req.value.strip(), order),
            )
            row = dict(cur.fetchone())
        conn.commit()
    return row


@app.patch("/api/behavior/fields/{field_id}", dependencies=[Depends(require_key)])
def update_behavior_field(field_id: int, req: BehaviorFieldUpdate):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE behavior_fields
                SET label=%s, value=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (req.label.strip(), req.value.strip(), field_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Behavior field not found.")
        conn.commit()
    return dict(row)


@app.delete("/api/behavior/fields/{field_id}", dependencies=[Depends(require_key)])
def delete_behavior_field(field_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM behavior_fields WHERE id=%s RETURNING id", (field_id,))
            if not cur.fetchone():
                raise HTTPException(404, "Behavior field not found.")
        conn.commit()
    return {"deleted": True}


@app.post("/api/chat", dependencies=[Depends(require_key)])
async def chat(req: ChatRequest):
    if not DATABASE_URL:
        raise HTTPException(503, "DATABASE_URL is not configured.")

    cid = req.conversation_id or str(uuid4())

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations(id, title)
                VALUES (%s, %s)
                ON CONFLICT(id) DO NOTHING
                """,
                (cid, req.message[:80]),
            )
        conn.commit()

    history = history_for(cid)

    semantic_used = False
    semantic_error = None
    try:
        if not ATLAS_SEMANTIC_MEMORY:
            raise RuntimeError("Semantic memory is disabled.")
        semantic = await semantic_context(req.message, req.active_skill_id)
        profile_block = semantic["profile_block"]
        memories = semantic["memories"]
        chosen_skills = semantic["skills"]
        semantic_used = True
    except Exception as exc:
        semantic_error = str(exc)
        profile_block = profile_context()
        memories = keyword_memories(req.message)
        chosen_skills = find_relevant_skills(req.message, req.active_skill_id)

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

    memory_block = "\n".join(
        f"- [{m['kind']}] {m['content']}" for m in memories
    ) or "(none)"

    skill_block = "\n\n".join(
        skill_to_context(skill) for skill, _source in chosen_skills
    ) or "(none)"

    instructions = (
        "You are the reasoning engine used by Atlas Core, a personal AI system. "
        "Atlas should know this user deeply rather than pretending to be an expert at everything. "
        "Use About Me information as trusted user-provided context when relevant. "
        "Skill playbooks are user-defined operating procedures: follow them closely when relevant, "
        "but never force an unrelated skill into a response. "
        "User corrections are high-priority lessons. "
        "If a user-defined instruction conflicts with safety or verified facts, explain the conflict. "
        "Never invent a memory, preference, skill, or completed external action.\n\n"
        f"ATLAS BEHAVIOR RULES:\n{behavior_context()}\n\n"
        f"ABOUT THE USER:\n{profile_block}\n\n"
        f"RELEVANT SKILLS:\n{skill_block}\n\n"
        f"RELEVANT MEMORY:\n{memory_block}"
    )

    try:
        answer, provider_used, model = await call_model(
            req.provider,
            req.deep,
            req.use_web,
            history + [{"role": "user", "content": req.message}],
            instructions,
        )
    except Exception as exc:
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

            cur.execute(
                "UPDATE conversations SET updated_at=NOW() WHERE id=%s",
                (cid,),
            )
        conn.commit()

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
    }


@app.post("/api/memory", dependencies=[Depends(require_key)])
def memory(req: MemoryRequest):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories(kind, content, importance, confidence, source)
                VALUES (%s, %s, %s, 1.0, 'user')
                RETURNING id
                """,
                (req.kind.strip() or "knowledge", req.content.strip(), req.importance),
            )
            memory_id = cur.fetchone()["id"]
        conn.commit()
    return {"saved": True, "id": memory_id}


@app.post("/api/feedback", dependencies=[Depends(require_key)])
def feedback(req: FeedbackRequest):
    candidate = False
    correction_memory_saved = False
    skill_lesson_saved = False

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM messages WHERE id=%s",
                (req.message_id,),
            )
            msg = cur.fetchone()
            if not msg:
                raise HTTPException(404, "Message not found.")

            cur.execute(
                """
                INSERT INTO feedback(message_id, rating, correction)
                VALUES (%s, %s, %s)
                """,
                (req.message_id, req.rating, req.correction),
            )

            correction = (req.correction or "").strip()
            if correction:
                cur.execute(
                    """
                    SELECT content
                    FROM messages
                    WHERE conversation_id=%s
                      AND role='user'
                      AND id < %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (msg["conversation_id"], req.message_id),
                )
                prior = cur.fetchone()

                if prior:
                    cur.execute(
                        """
                        INSERT INTO training_candidates(
                          source_type, input_text, target_text
                        )
                        VALUES ('user_correction', %s, %s)
                        """,
                        (prior["content"], correction),
                    )
                    candidate = True

                    memory_text = (
                        f"User correction for request '{prior['content']}': {correction}"
                    )
                    cur.execute(
                        """
                        INSERT INTO memories(
                          kind, content, importance, confidence, source
                        )
                        VALUES ('correction', %s, 1.0, 1.0, 'user_correction')
                        """,
                        (memory_text,),
                    )
                    correction_memory_saved = True

                cur.execute(
                    """
                    SELECT skill_id
                    FROM message_skill_context
                    WHERE message_id=%s AND source='active'
                    LIMIT 1
                    """,
                    (req.message_id,),
                )
                active = cur.fetchone()
                if active:
                    cur.execute(
                        """
                        INSERT INTO skill_lessons(
                          skill_id, source_message_id, correction
                        )
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


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(r"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b0f">
<title>Atlas</title>
<style>
:root{
  --bg:#0b0b0f;--panel:#15151a;--panel2:#1b1b21;--line:#32323c;
  --purple:#c5a3ff;--purple2:#9774d2;--text:#eee8ff;--muted:#9d91b7;
  --button:#24242b;--danger:#d26f8a
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,textarea,select{font:inherit}
button{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--button);color:var(--purple);font-weight:700}
button.primary{background:var(--purple);color:#160f20;border-color:var(--purple)}
button.ghost{background:transparent}
button.danger{color:#f0a2b5}
button:disabled{opacity:.55}
input,textarea,select{width:100%;background:#101014;color:var(--text);border:1px solid #3a3a45;border-radius:12px;padding:11px}
textarea{min-height:90px;resize:vertical}
.wrap{max-width:760px;margin:auto;min-height:100vh;padding:16px 16px 92px}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px}
.brand{font-size:26px;font-weight:850;color:var(--purple)}
.sub{font-size:12px;color:var(--muted);margin-top:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px;margin:10px 0}
.cardTitle{font-size:17px;font-weight:800;color:var(--purple);margin-bottom:4px}
.small{font-size:12px;color:var(--muted)}
.row{display:flex;gap:8px;align-items:center}
.row.wraprow{flex-wrap:wrap}
.spread{display:flex;justify-content:space-between;align-items:center;gap:10px}
.stack{display:flex;flex-direction:column;gap:8px}
.page{display:none}.page.active{display:block}
.chat{min-height:46vh;padding-bottom:8px}
.msg{padding:12px;border-radius:16px;margin:10px 0;white-space:pre-wrap;line-height:1.45}
.msg.user{background:#2a2a32;margin-left:12%;color:#e7d9ff}
.msg.atlas{background:#17171c;border:1px solid #2c2c34;margin-right:7%;color:var(--purple)}
.feedback{display:flex;gap:6px;margin-top:9px}
.feedback button{font-size:12px;padding:7px 9px}
.thinking{opacity:.82;font-style:italic}
.dots span{animation:blink 1.2s infinite;opacity:.2}.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}
.composer{position:sticky;bottom:78px;background:rgba(11,11,15,.97);padding-top:8px}
.composer textarea{min-height:58px}
.skillChip{display:none;margin:0 0 7px;padding:8px 10px;background:#21182d;border:1px solid #44305f;border-radius:12px;color:var(--purple);font-size:13px}
.field{padding:11px 0;border-top:1px solid #292932}
.field:first-of-type{border-top:0}
.fieldLabel{font-weight:750;color:#d9c5ff}
.fieldValue{font-size:14px;color:#c9bddf;margin-top:4px;white-space:pre-wrap}
.typeTag{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#826da8}
.sectionHeader{display:flex;justify-content:space-between;align-items:center;gap:8px}
.skillCard{cursor:pointer}
.lesson{padding:8px 0;border-top:1px solid #292932;font-size:13px;color:#cdbce8}
.empty{padding:28px 14px;text-align:center;color:var(--muted);border:1px dashed #353540;border-radius:16px}
.nav{position:fixed;left:0;right:0;bottom:0;background:rgba(12,12,16,.98);border-top:1px solid #282832;padding:8px max(10px,env(safe-area-inset-right)) calc(8px + env(safe-area-inset-bottom)) max(10px,env(safe-area-inset-left));display:flex;justify-content:center;z-index:20}
.navin{width:min(760px,100%);display:grid;grid-template-columns:repeat(4,1fr);gap:7px}
.nav button{padding:10px 5px;background:#17171c;color:#9286a8}
.nav button.active{color:var(--purple);background:#251b32}
.overlay{display:none;position:fixed;inset:0;background:rgba(5,5,8,.96);z-index:50;overflow:auto}
.sheet{max-width:620px;margin:auto;padding:18px 16px calc(30px + env(safe-area-inset-bottom))}
.sheetTop{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:16px}
.sheetTitle{font-size:22px;font-weight:850;color:var(--purple)}
.formField{margin:12px 0}
.formField label{display:block;font-size:13px;color:#c7b0e8;margin-bottom:6px;font-weight:700}
.toggleRow{display:flex;align-items:center;gap:8px;color:#c7b0e8;font-size:13px}
.toggleRow input{width:auto}
#accessCard{display:none}
.status{font-size:12px;color:var(--muted);margin-top:7px;min-height:16px}
@media(max-width:430px){
  .wrap{padding-left:12px;padding-right:12px}
  button{padding:9px 10px}
  .brand{font-size:24px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <div class="brand">Atlas</div>
      <div class="sub">Know me well. Learn my work. Stay focused.</div>
    </div>
    <button class="ghost" onclick="toggleAccess()">Access</button>
  </div>

  <div id="accessCard" class="card">
    <div class="cardTitle">Private Access</div>
    <div class="small">Stored only in this browser on this device.</div>
    <input id="accessKey" type="password" placeholder="Atlas access key" style="margin-top:10px">
    <button onclick="saveAccess()" style="margin-top:8px">Save on this phone</button>
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
        <div class="row">
          <button onclick="teachAtlas()">Teach</button>
          <button class="primary" style="flex:1" onclick="sendMessage()">Send</button>
        </div>
        <div id="chatStatus" class="status"></div>
      </div>
    </div>
  </section>

  <section id="page-me" class="page">
    <div class="spread">
      <div>
        <div class="cardTitle">About Me</div>
        <div class="small">Only add things you actually want Atlas to know.</div>
      </div>
      <button onclick="openSectionEditor()">+ Section</button>
    </div>
    <div id="profileList"></div>
  </section>

  <section id="page-skills" class="page">
    <div id="skillsHome">
      <div class="spread">
        <div>
          <div class="cardTitle">Skills</div>
          <div class="small">Focused playbooks for things you actually do.</div>
        </div>
        <button onclick="openSkillEditor()">+ Skill</button>
      </div>
      <div id="skillsList"></div>
    </div>
    <div id="skillDetail" style="display:none"></div>
  </section>

  <section id="page-settings" class="page">
    <div class="card">
      <div class="cardTitle">Brain</div>
      <div class="small">Atlas owns the routing. Pick a provider, or let Auto choose.</div>
      <select id="brainSelect" onchange="saveBrainChoice()" style="margin-top:10px">
        <option value="auto">Auto</option>
        <option value="openai">OpenAI</option>
        <option id="localBrainOption" value="local">Local</option>
      </select>
      <div id="brainInfo" class="small" style="margin-top:8px">Loading provider status...</div>
    </div>

    <div class="spread" style="margin-top:18px">
      <div>
        <div class="cardTitle">Behavior</div>
        <div class="small">Create only the response rules you care about.</div>
      </div>
      <button onclick="openBehaviorEditor()">+ Rule</button>
    </div>
    <div id="behaviorList"></div>

    <div class="card" style="margin-top:18px">
      <div class="cardTitle">System</div>
      <div id="systemInfo" class="small">Atlas v0.8.0 • Semantic memory + replaceable model providers.</div>
    </div>
  </section>
</div>

<nav class="nav">
  <div class="navin">
    <button data-page="chat" class="active" onclick="showPage('chat')">Chat</button>
    <button data-page="me" onclick="showPage('me')">Me</button>
    <button data-page="skills" onclick="showPage('skills')">Skills</button>
    <button data-page="settings" onclick="showPage('settings')">Settings</button>
  </div>
</nav>

<div id="editorOverlay" class="overlay">
  <div class="sheet">
    <div class="sheetTop">
      <div id="editorTitle" class="sheetTitle">Edit</div>
      <button onclick="closeEditor()">Close</button>
    </div>
    <div id="editorBody"></div>
    <div id="editorStatus" class="status"></div>
  </div>
</div>

<script>
let key = localStorage.getItem("atlas_key") || "";
let cid = localStorage.getItem("atlas_cid") || null;
let activeSkill = JSON.parse(localStorage.getItem("atlas_active_skill") || "null");
let brainChoice = localStorage.getItem("atlas_brain") || "auto";
let currentSkill = null;
let profileCache = [];
let skillsCache = [];
let behaviorCache = [];

accessKey.value = key;
brainSelect.value = brainChoice;
if(!key) accessCard.style.display = "block";
updateSkillChip();

function authHeaders(json=true){
  let h = {"X-Atlas-Key":key};
  if(json) h["Content-Type"] = "application/json";
  return h;
}

async function api(url, options={}){
  options.headers = options.headers || authHeaders(options.method && options.method !== "GET");
  let r = await fetch(url, options);
  let data = {};
  try{ data = await r.json(); }catch(_e){}
  if(!r.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function toggleAccess(){
  accessCard.style.display = accessCard.style.display === "block" ? "none" : "block";
}

function saveAccess(){
  key = accessKey.value.trim();
  localStorage.setItem("atlas_key",key);
  accessCard.style.display = "none";
  chatStatus.textContent = "Access key saved on this phone.";
}

function saveBrainChoice(){
  brainChoice = brainSelect.value;
  localStorage.setItem("atlas_brain",brainChoice);
  brainInfo.textContent = "Brain preference saved on this device.";
}

async function loadSystem(){
  if(!key){ toggleAccess(); return; }
  try{
    let data = await api("/api/system",{headers:authHeaders(false)});
    localBrainOption.disabled = !data.local_configured;
    if(brainChoice==="local" && !data.local_configured){
      brainChoice = "auto";
      brainSelect.value = "auto";
      localStorage.setItem("atlas_brain","auto");
    }
    let providers = [];
    if(data.openai_configured) providers.push("OpenAI ready");
    if(data.local_configured) providers.push("Local ready: " + data.local_model);
    else providers.push("Local not connected yet");
    brainInfo.textContent = providers.join(" • ");
    systemInfo.textContent =
      "Atlas v" + data.version +
      " • Semantic memory: " + (data.semantic_memory ? "on" : "off") +
      " • Embeddings: " + data.embedding_backend + " / " + data.embedding_model +
      " • Training examples: " + data.training_candidates;
  }catch(e){
    brainInfo.textContent = "System status error: " + e.message;
  }
}

function showPage(name){
  document.querySelectorAll(".page").forEach(x=>x.classList.remove("active"));
  document.getElementById("page-"+name).classList.add("active");
  document.querySelectorAll(".nav button").forEach(x=>x.classList.toggle("active",x.dataset.page===name));
  if(name==="me") loadProfile();
  if(name==="skills") loadSkills();
  if(name==="settings"){
    loadBehavior();
    loadSystem();
  }
}

function addMessage(text, who, id){
  let d = document.createElement("div");
  d.className = "msg " + (who==="user" ? "user" : "atlas");
  d.append(document.createTextNode(text));
  if(who==="atlas" && id){
    let f = document.createElement("div");
    f.className = "feedback";
    let good = document.createElement("button");
    good.textContent = "Useful";
    good.onclick = async ()=>{
      if(await rate(id,1,null)){
        good.textContent = "Saved";
        good.disabled = true;
      }
    };
    let fix = document.createElement("button");
    fix.textContent = "Needs fix";
    fix.onclick = ()=>correct(id,fix);
    f.append(good,fix);
    d.appendChild(f);
  }
  chatMessages.appendChild(d);
  window.scrollTo(0,document.body.scrollHeight);
  return d;
}

function thinkingBubble(){
  let d = document.createElement("div");
  d.className = "msg atlas thinking";
  d.innerHTML = 'Atlas is thinking<span class="dots"><span>.</span><span>.</span><span>.</span></span>';
  chatMessages.appendChild(d);
  window.scrollTo(0,document.body.scrollHeight);
  return d;
}

async function sendMessage(){
  let message = promptBox.value.trim();
  if(!message) return;
  if(!key){ toggleAccess(); return; }

  addMessage(message,"user");
  promptBox.value = "";
  chatStatus.textContent = "";
  let thinking = thinkingBubble();

  try{
    let data = await api("/api/chat",{
      method:"POST",
      headers:authHeaders(true),
      body:JSON.stringify({
        message,
        conversation_id:cid,
        use_web:webToggle.checked,
        deep:deepToggle.checked,
        active_skill_id:activeSkill ? activeSkill.id : null,
        provider:brainChoice
      })
    });
    thinking.remove();
    cid = data.conversation_id;
    localStorage.setItem("atlas_cid",cid);
    addMessage(data.answer,"atlas",data.assistant_message_id);
    let used = (data.skills_used || []).map(x=>x.name);
    chatStatus.textContent = "Brain: " + data.provider + " / " + data.model + (data.semantic_memory ? " • Semantic memory" : " • Memory fallback") + (used.length ? " • Skills: " + used.join(", ") : "");
  }catch(e){
    thinking.remove();
    addMessage("Error: " + e.message,"atlas");
  }
}

async function teachAtlas(){
  if(!key){ toggleAccess(); return; }
  let text = prompt("What should Atlas remember?");
  if(!text) return;
  try{
    await api("/api/memory",{
      method:"POST",headers:authHeaders(true),
      body:JSON.stringify({content:text})
    });
    chatStatus.textContent = "Saved to Atlas memory.";
  }catch(e){
    chatStatus.textContent = "Memory error: " + e.message;
  }
}

async function rate(id,rating,correction){
  try{
    let data = await api("/api/feedback",{
      method:"POST",headers:authHeaders(true),
      body:JSON.stringify({message_id:id,rating,correction})
    });
    if(correction){
      chatStatus.textContent = data.skill_lesson_saved
        ? "Correction saved to memory and the active skill."
        : "Correction saved to Atlas memory.";
    }else{
      chatStatus.textContent = "Feedback saved.";
    }
    return true;
  }catch(e){
    chatStatus.textContent = "Feedback error: " + e.message;
    return false;
  }
}

async function correct(id,button){
  let text = prompt("What should Atlas have answered or done instead?");
  if(!text) return;
  if(await rate(id,-1,text)){
    button.textContent = "Correction saved";
    button.disabled = true;
  }
}

function updateSkillChip(){
  if(activeSkill){
    activeSkillChip.style.display = "flex";
    activeSkillChip.innerHTML = "";
    let name = document.createElement("span");
    name.textContent = "Using skill: " + activeSkill.name;
    name.style.flex = "1";
    let clear = document.createElement("button");
    clear.textContent = "Clear";
    clear.style.padding = "4px 8px";
    clear.onclick = clearActiveSkill;
    activeSkillChip.append(name,clear);
  }else{
    activeSkillChip.style.display = "none";
    activeSkillChip.innerHTML = "";
  }
}

function setActiveSkill(skill){
  activeSkill = {id:skill.id,name:skill.name};
  localStorage.setItem("atlas_active_skill",JSON.stringify(activeSkill));
  updateSkillChip();
  showPage("chat");
  chatStatus.textContent = "Atlas will use " + skill.name + " for this chat.";
}

function clearActiveSkill(){
  activeSkill = null;
  localStorage.removeItem("atlas_active_skill");
  updateSkillChip();
  chatStatus.textContent = "Active skill cleared.";
}

function openEditor(title,html){
  editorTitle.textContent = title;
  editorBody.innerHTML = html;
  editorStatus.textContent = "";
  editorOverlay.style.display = "block";
}

function closeEditor(){
  editorOverlay.style.display = "none";
  editorBody.innerHTML = "";
  editorStatus.textContent = "";
}

function formValue(id){ return document.getElementById(id).value.trim(); }

function fieldTypeOptions(selected){
  return ["text","note","rule","checklist","steps"].map(
    x=>`<option value="${x}" ${x===selected?"selected":""}>${x}</option>`
  ).join("");
}

async function loadProfile(){
  if(!key){ toggleAccess(); return; }
  try{
    let data = await api("/api/profile",{headers:authHeaders(false)});
    profileCache = data.sections || [];
    renderProfile();
  }catch(e){
    profileList.innerHTML = `<div class="empty">Could not load About Me: ${escapeHtml(e.message)}</div>`;
  }
}

function renderProfile(){
  profileList.innerHTML = "";
  if(!profileCache.length){
    profileList.innerHTML = '<div class="empty">No sections yet. Add only what you actually want Atlas to know.</div>';
    return;
  }
  for(const section of profileCache){
    let card = document.createElement("div");
    card.className = "card";
    let head = document.createElement("div");
    head.className = "sectionHeader";
    let left = document.createElement("div");
    left.innerHTML = `<div class="cardTitle">${escapeHtml(section.name)}</div><div class="small">${section.fields.length} field${section.fields.length===1?"":"s"}</div>`;
    let actions = document.createElement("div");
    actions.className = "row";
    let add = document.createElement("button");
    add.textContent = "+ Field";
    add.onclick = ()=>openProfileFieldEditor(section.id);
    let edit = document.createElement("button");
    edit.textContent = "•••";
    edit.onclick = ()=>openSectionEditor(section);
    actions.append(add,edit);
    head.append(left,actions);
    card.appendChild(head);

    for(const field of section.fields){
      let row = document.createElement("div");
      row.className = "field";
      row.onclick = ()=>openProfileFieldEditor(section.id,field);
      row.innerHTML = `<div class="spread"><div class="fieldLabel">${escapeHtml(field.label)}</div><div class="typeTag">${escapeHtml(field.field_type)}</div></div><div class="fieldValue">${escapeHtml(field.value || "(empty)")}</div>`;
      card.appendChild(row);
    }
    profileList.appendChild(card);
  }
}

function openSectionEditor(section=null){
  let editing = !!section;
  openEditor(editing ? "Edit Section" : "New Section",`
    <div class="formField"><label>Section name</label><input id="edSectionName" value="${editing?escapeAttr(section.name):""}" placeholder="e.g. Work, Gear, Preferences"></div>
    <div class="row">
      ${editing?'<button class="danger" onclick="deleteSection('+section.id+')">Delete</button>':""}
      <button class="primary" style="flex:1" onclick="saveSection(${editing?section.id:"null"})">Save</button>
    </div>
  `);
}

async function saveSection(id){
  let name = formValue("edSectionName");
  if(!name){ editorStatus.textContent="Give the section a name."; return; }
  try{
    await api(id?"/api/profile/sections/"+id:"/api/profile/sections",{
      method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify({name})
    });
    closeEditor(); await loadProfile();
  }catch(e){ editorStatus.textContent=e.message; }
}

async function deleteSection(id){
  if(!confirm("Delete this section and every field inside it?")) return;
  try{
    await api("/api/profile/sections/"+id,{method:"DELETE",headers:authHeaders(false)});
    closeEditor(); await loadProfile();
  }catch(e){ editorStatus.textContent=e.message; }
}

function openProfileFieldEditor(sectionId,field=null){
  let editing=!!field;
  openEditor(editing?"Edit Field":"New Field",`
    <div class="formField"><label>Field name</label><input id="edFieldLabel" value="${editing?escapeAttr(field.label):""}" placeholder="Anything you want Atlas to know"></div>
    <div class="formField"><label>Type</label><select id="edFieldType">${fieldTypeOptions(editing?field.field_type:"text")}</select></div>
    <div class="formField"><label>Value</label><textarea id="edFieldValue" placeholder="Teach Atlas the actual information">${editing?escapeHtml(field.value):""}</textarea></div>
    <div class="formField"><label class="toggleRow"><input id="edInclude" type="checkbox" ${!editing||field.include_in_chat?"checked":""}> Use this information in chat</label></div>
    <div class="row">
      ${editing?'<button class="danger" onclick="deleteProfileField('+field.id+')">Delete</button>':""}
      <button class="primary" style="flex:1" onclick="saveProfileField(${sectionId},${editing?field.id:"null"})">Save</button>
    </div>
  `);
}

async function saveProfileField(sectionId,id){
  let payload={
    section_id:sectionId,
    label:formValue("edFieldLabel"),
    field_type:document.getElementById("edFieldType").value,
    value:formValue("edFieldValue"),
    include_in_chat:document.getElementById("edInclude").checked
  };
  if(!payload.label){ editorStatus.textContent="Give the field a name."; return; }
  if(id) delete payload.section_id;
  try{
    await api(id?"/api/profile/fields/"+id:"/api/profile/fields",{
      method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)
    });
    closeEditor(); await loadProfile();
  }catch(e){ editorStatus.textContent=e.message; }
}

async function deleteProfileField(id){
  if(!confirm("Delete this field?")) return;
  try{
    await api("/api/profile/fields/"+id,{method:"DELETE",headers:authHeaders(false)});
    closeEditor(); await loadProfile();
  }catch(e){ editorStatus.textContent=e.message; }
}

async function loadSkills(){
  if(!key){ toggleAccess(); return; }
  try{
    let data = await api("/api/skills",{headers:authHeaders(false)});
    skillsCache=data.skills || [];
    if(currentSkill){
      currentSkill=skillsCache.find(x=>x.id===currentSkill.id) || null;
      if(currentSkill) renderSkillDetail(currentSkill);
      else showSkillsHome();
    }else renderSkillsList();
  }catch(e){
    skillsList.innerHTML=`<div class="empty">Could not load skills: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSkillsList(){
  skillsHome.style.display="block"; skillDetail.style.display="none";
  skillsList.innerHTML="";
  if(!skillsCache.length){
    skillsList.innerHTML='<div class="empty">No skills yet. Create a focused playbook for something you actually do.</div>';
    return;
  }
  for(const skill of skillsCache){
    let card=document.createElement("div");
    card.className="card skillCard";
    card.onclick=()=>openSkill(skill.id);
    card.innerHTML=`<div class="spread"><div><div class="cardTitle">${escapeHtml(skill.name)}</div><div class="small">${escapeHtml(skill.description || "No description yet")}</div></div><div class="typeTag">${skill.enabled?"ON":"OFF"}</div></div><div class="small" style="margin-top:8px">${skill.fields.length} fields • ${skill.lessons.length} learned correction${skill.lessons.length===1?"":"s"}</div>`;
    skillsList.appendChild(card);
  }
}

function openSkill(id){
  currentSkill=skillsCache.find(x=>x.id===id);
  if(currentSkill) renderSkillDetail(currentSkill);
}

function showSkillsHome(){
  currentSkill=null;
  skillsHome.style.display="block";
  skillDetail.style.display="none";
  renderSkillsList();
}

function renderSkillDetail(skill){
  skillsHome.style.display="none";
  skillDetail.style.display="block";
  skillDetail.innerHTML="";
  let top=document.createElement("div");
  top.className="stack";
  top.innerHTML=`<div class="row"><button onclick="showSkillsHome()">Back</button><button onclick="openSkillEditorById(${skill.id})">Edit</button><button class="primary" style="flex:1" onclick="useSkillById(${skill.id})">Use in Chat</button></div><div><div class="cardTitle" style="font-size:22px">${escapeHtml(skill.name)}</div><div class="small">${escapeHtml(skill.description || "No description yet")}</div></div>`;
  skillDetail.appendChild(top);

  let fields=document.createElement("div");
  fields.className="card";
  let fh=document.createElement("div");
  fh.className="spread";
  fh.innerHTML='<div><div class="cardTitle">Playbook</div><div class="small">Add only the knowledge Atlas needs for this skill.</div></div>';
  let add=document.createElement("button");
  add.textContent="+ Field";
  add.onclick=()=>openSkillFieldEditor(skill.id);
  fh.appendChild(add);
  fields.appendChild(fh);

  if(!skill.fields.length){
    let e=document.createElement("div"); e.className="empty"; e.style.marginTop="12px"; e.textContent="No fields yet.";
    fields.appendChild(e);
  }else{
    for(const field of skill.fields){
      let row=document.createElement("div");
      row.className="field";
      row.onclick=()=>openSkillFieldEditor(skill.id,field);
      row.innerHTML=`<div class="spread"><div class="fieldLabel">${escapeHtml(field.label)}</div><div class="typeTag">${escapeHtml(field.field_type)}</div></div><div class="fieldValue">${escapeHtml(field.value || "(empty)")}</div>`;
      fields.appendChild(row);
    }
  }
  skillDetail.appendChild(fields);

  if(skill.lessons.length){
    let lessons=document.createElement("div");
    lessons.className="card";
    lessons.innerHTML='<div class="cardTitle">Learned Corrections</div><div class="small">Corrections made while this skill was explicitly active.</div>';
    for(const lesson of skill.lessons){
      let d=document.createElement("div"); d.className="lesson"; d.textContent=lesson.correction; lessons.appendChild(d);
    }
    skillDetail.appendChild(lessons);
  }
}

function openSkillEditor(skill=null){
  let editing=!!skill;
  openEditor(editing?"Edit Skill":"New Skill",`
    <div class="formField"><label>Skill name</label><input id="edSkillName" value="${editing?escapeAttr(skill.name):""}" placeholder="e.g. Church Mix Workflow"></div>
    <div class="formField"><label>What is this skill for?</label><textarea id="edSkillDesc" placeholder="Keep this focused.">${editing?escapeHtml(skill.description):""}</textarea></div>
    ${editing?'<div class="formField"><label class="toggleRow"><input id="edSkillEnabled" type="checkbox" '+(skill.enabled?"checked":"")+'> Allow Atlas to use this skill</label></div>':""}
    <div class="row">
      ${editing?'<button class="danger" onclick="deleteSkill('+skill.id+')">Delete</button>':""}
      <button class="primary" style="flex:1" onclick="saveSkill(${editing?skill.id:"null"})">Save</button>
    </div>
  `);
}

function openSkillEditorById(id){
  let skill=skillsCache.find(x=>x.id===id); if(skill) openSkillEditor(skill);
}

async function saveSkill(id){
  let payload={
    name:formValue("edSkillName"),
    description:formValue("edSkillDesc")
  };
  if(id) payload.enabled=document.getElementById("edSkillEnabled").checked;
  if(!payload.name){ editorStatus.textContent="Give the skill a name."; return; }
  try{
    let saved=await api(id?"/api/skills/"+id:"/api/skills",{
      method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)
    });
    closeEditor(); currentSkill=id?{id}:null; await loadSkills();
    if(!id) openSkill(saved.id);
  }catch(e){ editorStatus.textContent=e.message; }
}

async function deleteSkill(id){
  if(!confirm("Delete this skill, its fields, and learned corrections?")) return;
  try{
    await api("/api/skills/"+id,{method:"DELETE",headers:authHeaders(false)});
    if(activeSkill && activeSkill.id===id) clearActiveSkill();
    closeEditor(); showSkillsHome(); await loadSkills();
  }catch(e){ editorStatus.textContent=e.message; }
}

function openSkillFieldEditor(skillId,field=null){
  let editing=!!field;
  openEditor(editing?"Edit Skill Field":"New Skill Field",`
    <div class="formField"><label>Field name</label><input id="edSkillFieldLabel" value="${editing?escapeAttr(field.label):""}" placeholder="e.g. Steps, Rules, Export settings"></div>
    <div class="formField"><label>Type</label><select id="edSkillFieldType">${fieldTypeOptions(editing?field.field_type:"note")}</select></div>
    <div class="formField"><label>Teach Atlas</label><textarea id="edSkillFieldValue" placeholder="Be as specific as you need.">${editing?escapeHtml(field.value):""}</textarea></div>
    <div class="row">
      ${editing?'<button class="danger" onclick="deleteSkillField('+field.id+')">Delete</button>':""}
      <button class="primary" style="flex:1" onclick="saveSkillField(${skillId},${editing?field.id:"null"})">Save</button>
    </div>
  `);
}

async function saveSkillField(skillId,id){
  let payload={
    skill_id:skillId,
    label:formValue("edSkillFieldLabel"),
    field_type:document.getElementById("edSkillFieldType").value,
    value:formValue("edSkillFieldValue")
  };
  if(!payload.label){ editorStatus.textContent="Give the field a name."; return; }
  if(id) delete payload.skill_id;
  try{
    await api(id?"/api/skills/fields/"+id:"/api/skills/fields",{
      method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)
    });
    closeEditor(); currentSkill={id:skillId}; await loadSkills();
  }catch(e){ editorStatus.textContent=e.message; }
}

async function deleteSkillField(id){
  if(!confirm("Delete this field?")) return;
  try{
    await api("/api/skills/fields/"+id,{method:"DELETE",headers:authHeaders(false)});
    closeEditor(); await loadSkills();
  }catch(e){ editorStatus.textContent=e.message; }
}

function useSkillById(id){
  let skill=skillsCache.find(x=>x.id===id); if(skill) setActiveSkill(skill);
}

async function loadBehavior(){
  if(!key){ toggleAccess(); return; }
  try{
    let data=await api("/api/behavior",{headers:authHeaders(false)});
    behaviorCache=data.fields || [];
    renderBehavior();
  }catch(e){
    behaviorList.innerHTML=`<div class="empty">Could not load behavior: ${escapeHtml(e.message)}</div>`;
  }
}

function renderBehavior(){
  behaviorList.innerHTML="";
  if(!behaviorCache.length){
    behaviorList.innerHTML='<div class="empty">No custom behavior rules yet. Atlas will use its clean default behavior until you add one.</div>';
    return;
  }
  for(const field of behaviorCache){
    let card=document.createElement("div");
    card.className="card";
    card.onclick=()=>openBehaviorEditor(field);
    card.innerHTML=`<div class="fieldLabel">${escapeHtml(field.label)}</div><div class="fieldValue">${escapeHtml(field.value || "(empty)")}</div>`;
    behaviorList.appendChild(card);
  }
}

function openBehaviorEditor(field=null){
  let editing=!!field;
  openEditor(editing?"Edit Behavior Rule":"New Behavior Rule",`
    <div class="formField"><label>Rule name</label><input id="edBehaviorLabel" value="${editing?escapeAttr(field.label):""}" placeholder="e.g. Response style, When to ask questions"></div>
    <div class="formField"><label>Instruction</label><textarea id="edBehaviorValue" placeholder="Tell Atlas exactly how you want this handled every time.">${editing?escapeHtml(field.value):""}</textarea></div>
    <div class="row">
      ${editing?'<button class="danger" onclick="deleteBehavior('+field.id+')">Delete</button>':""}
      <button class="primary" style="flex:1" onclick="saveBehavior(${editing?field.id:"null"})">Save</button>
    </div>
  `);
}

async function saveBehavior(id){
  let payload={label:formValue("edBehaviorLabel"),value:formValue("edBehaviorValue")};
  if(!payload.label){ editorStatus.textContent="Give the rule a name."; return; }
  try{
    await api(id?"/api/behavior/fields/"+id:"/api/behavior/fields",{
      method:id?"PATCH":"POST",headers:authHeaders(true),body:JSON.stringify(payload)
    });
    closeEditor(); await loadBehavior();
  }catch(e){ editorStatus.textContent=e.message; }
}

async function deleteBehavior(id){
  if(!confirm("Delete this behavior rule?")) return;
  try{
    await api("/api/behavior/fields/"+id,{method:"DELETE",headers:authHeaders(false)});
    closeEditor(); await loadBehavior();
  }catch(e){ editorStatus.textContent=e.message; }
}

function escapeHtml(value){
  return String(value ?? "").replace(/[&<>"']/g,c=>({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[c]));
}
function escapeAttr(value){ return escapeHtml(value); }

promptBox.addEventListener("keydown",e=>{
  if(e.key==="Enter" && !e.shiftKey){
    e.preventDefault(); sendMessage();
  }
});
</script>
</body>
</html>""")
