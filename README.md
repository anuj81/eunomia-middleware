# eunomia-middleware

> **Governance-first natural-language query (NLQ) middleware for LLM-on-warehouse setups.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Eunomia sits between an LLM (Gemini today, swappable) and a MySQL warehouse and refuses to let the LLM run anything that isn't authorized. Identity comes from Keycloak (OIDC), policy comes from OpenMetadata (tag policies), and the middleware stays *transparent on the role* — it never decides who can see what, it just enforces what the catalog already said.

```
   Client (CLI / UI)          eunomia-cli (companion repo)
        │
        │  Authorization: Bearer <Keycloak JWT>
        ▼
   ┌────────────────────────────────────────────────────────────┐
   │                                                            │
   │              eunomia-middleware (this repo)                │
   │                                                            │
   │   1. JWT signature/iss/exp via Keycloak JWKS               │
   │   2. roles → access tag → OpenMetadata /search/query       │
   │   3. RAG (eunomia-rag) narrows to top-K for the prompt     │
   │   4. LLM generates MySQL SELECT                            │
   │   5. sqlglot AST validation against the FULL allowed set   │
   │   6. MySQL execution                                       │
   │   7. PII masking per OpenMetadata tags + JWT role          │
   │   8. SSE stream + audit record                             │
   │                                                            │
   └────────────────────────────────────────────────────────────┘
```

The full Phase 1 architecture, including the trust line and per-layer responsibilities, lives in [planning/middleware_architecture.md](../planning/middleware_architecture.md).

---

## Why

LLM-driven analytics tools that depend on the model to "be careful" with sensitive data are theater. Eunomia draws the trust boundary in code:

- **Identity is verified, not trusted** — every request carries a Keycloak JWT validated against the realm's JWKS.
- **Authorization lives in OpenMetadata** — role-to-view access is encoded as OM tag policies; the middleware has no hardcoded role map.
- **The LLM is sandboxed** — generated SQL must AST-validate against the OM-allowed view set before MySQL ever sees it.
- **PII is redacted post-execution** — based on OM tags + the compositional `eunomia-pii-unmask` Keycloak role.
- **Every request is audited** — dual-sink log (human-readable + JSON-lines for SIEM) captures the full trust trail.

The full demo: a user with `eunomia-agency-partner` asks "give me data from `core.dim_customers`" and Gemini *literally writes that SQL*. The validator rejects it; the retry loop hands the error back to Gemini; Gemini self-corrects to `SELECT * FROM marketing_regional_performance_view` (the only view that role can see). No data leaks.

---

## Repository layout (this repo)

```
eunomia-middleware/
├── config/
│   └── eunomia.yaml              single source of truth for runtime knobs
├── logs/                          rotating log files (gitignored)
├── src/
│   ├── api/
│   │   ├── auth.py                Keycloak JWT verify + mock provider toggle
│   │   └── routes.py              POST /v1/execute_nlq SSE
│   ├── catalog/
│   │   ├── base.py                CatalogClient ABC + DTOs
│   │   ├── om_access.py           role→tag → OM /search/query resolver
│   │   ├── composed.py            ComposedCatalog (legacy + Phase D paths)
│   │   ├── openmetadata.py        real OM REST (forwards user JWT)
│   │   ├── openmetadata_mock.py   in-memory fixture for offline dev/CI
│   │   ├── rag_base.py            RagClient ABC
│   │   ├── rag_client.py          HTTP client → eunomia-rag
│   │   └── rag_mock.py            keyword-overlap mock
│   ├── config/settings.py         pydantic-settings + YAML loader
│   ├── validation/
│   │   ├── sql_parser.py          sqlglot AST validation against allow list
│   │   └── pii_masker.py          PII redaction per role
│   ├── execution/db_client.py     MySQL exec
│   ├── generation/llm_client.py   Gemini async client
│   ├── audit_log.py               dual-sink audit (file + JSONL)
│   ├── logging_setup.py           stdlib logging w/ rotation
│   └── main.py                    FastAPI entrypoint + CLI flags
├── tests/                         117 pytest cases
├── seed_mysql.py                  one-time data seed for the warehouse
├── seed_openmetadata.py           OM database / schema / view seed
├── seed_om_policies.py            Phase D — users, tag classification + policies, teams
└── start-fastapi.sh
```

Companion repos:
- [`eunomia-cli`](https://github.com/anuj81/eunomia-cli) — Device Code login + ask command
- [`eunomia-rag`](https://github.com/anuj81/eunomia-rag) — FastAPI + Qdrant catalog/retrieval service
- `eunomia-infrastructure` — Keycloak realm + OpenMetadata SSO config + end-to-end verification harness

---

## Quickstart

This service expects the rest of the Eunomia stack to be reachable. The full path is:

```bash
# 1. Bring up Keycloak + OpenMetadata + MySQL + Qdrant (see eunomia-infrastructure/)
cd ../eunomia-infrastructure
docker-compose up -d

# 2. Seed Phase D tag policies + role-teams in OpenMetadata
cd ../eunomia-middleware
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — set GEMINI_API_KEY etc.

python seed_openmetadata.py   # create OM database/schema/views (one-time)
python seed_om_policies.py    # Phase D — users, tag classification, deny policies

# 3. Index the catalog into Qdrant
cd ../eunomia-rag
./venv/bin/python -m src.indexer --reset

# 4. Start the RAG service
./venv/bin/python -m src.main &

# 5. Start the middleware
cd ../eunomia-middleware
EUNOMIA_AUTH__PROVIDER=keycloak \
EUNOMIA_OPENMETADATA__MOCK=false \
EUNOMIA_RAG__MOCK=false \
./start-fastapi.sh
```

Service is now live on `http://localhost:8000`. Drive it via `eunomia-cli`:

```bash
cd ../eunomia-cli
./venv/bin/python -m src.main login     # Keycloak Device Code flow
./venv/bin/python -m src.main ask "what is our daily revenue last week"
```

### Offline / CI mode

For unit testing or hacking without Keycloak / OpenMetadata / MySQL up, flip the mock toggles:

```bash
EUNOMIA_AUTH__PROVIDER=mock           # accept string-match dev tokens
EUNOMIA_OPENMETADATA__MOCK=true       # in-memory fixture catalog
EUNOMIA_RAG__ENABLED=false            # skip RAG narrowing entirely
./start-fastapi.sh
```

Dev tokens accepted by the mock provider: `finance-token`, `external-auditor-token`, `marketing-token`, `agency-token`, `om-admin-token`. Each carries the same Phase D role claim shape as a real Keycloak token.

---

## Configuration

Everything lives in `config/eunomia.yaml`. Precedence (highest wins):

```
CLI flags  >  EUNOMIA_<SECTION>__<FIELD> env  >  YAML  >  built-in defaults
```

| Section | Key fields |
|---|---|
| `logging` | `level` (DEBUG/INFO/WARN/ERROR), `log_dir`, `console`, `file`, `rotation.{max_bytes, backup_count}` |
| `server` | `host`, `port`, `reload` |
| `openmetadata` | `mock` (true→fixture, false→real OM), `url`, `username` |
| `database` | `driver: mysql`, `host`, `port`, `user`, `name` |
| `llm` | `provider: gemini`, `model`, `max_retries` |
| `rag` | `enabled`, `mock`, `url`, `top_k`, `timeout_seconds` |
| `auth` | `provider` (mock / keycloak), `keycloak.{issuer, audience, jwks_cache_seconds}`, `unmask_pii_role`, `admin_bypass_role` |
| `authz` | `role_to_access_tag` (Keycloak realm role → OM tag FQN) |
| `audit` | `enabled`, `log_dir`, `human_file`, `jsonl_file`, rotation |

Secrets are **env-only** and a startup error is raised if found in YAML. Recognized well-known names: `OPENMETADATA_PASSWORD`, `DB_PASSWORD`, `GEMINI_API_KEY`, `RAG_API_KEY`.

---

## Audit log

Every NLQ produces one record across two sinks:

```
logs/audit.log
2026-05-13T04:24:27.465+00:00 | id=4804ccc4 | user=auditor.bob roles=[eunomia-external-auditor] provider=keycloak | query='show me the payment history' | allowed=1 prompt_top_k=1 | sql_attempts=1 rows=2 | masked_pii=card_last_four,email,first_name,last_name unmask=False | dur_ms=508 status=OK
```

```json
// logs/audit.jsonl
{"request_id":"4804ccc4-...","sub":"...","preferred_username":"auditor.bob","roles":["eunomia-external-auditor"],"auth_provider":"keycloak","unmask_pii":false,"query":"show me the payment history","allowed_views":["finance_customer_payment_history_view"],"executed_sql":"SELECT * FROM finance_customer_payment_history_view LIMIT 10;","rows_returned":2,"pii_columns_masked":["card_last_four","email","first_name","last_name"],"status":"ok","duration_ms":508,"...":"..."}
```

The `request_id` is also returned to the client in the final SSE event so support tickets can link straight to the trust trail.

---

## Development

```bash
pip install -r requirements.txt
pytest -v
```

117 tests covering settings, logging, catalog client hierarchy, RAG mocks, ComposedCatalog dispatch, JWT verification (with a synthetic JWKS — no live Keycloak needed for unit tests), OM-search resolver, PII masker, audit log.

### Adding a new role

1. Add the realm role to `eunomia-infrastructure/keycloak/realm-export.json` and re-import the realm.
2. Add the OM tag (`eunomia-access.<new-role>`) and tag the views that role may see — `seed_om_policies.py` handles this; just extend its `VIEW_ACCESS` map.
3. Add the role→tag line in `config/eunomia.yaml` under `authz.role_to_access_tag`.

The middleware code does not change.

---

## License

[Apache 2.0](LICENSE)
