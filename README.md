# tutoria-worker

**Offline document & quiz processing service for the Tutoria platform.**

This service handles all CPU/memory-heavy background work so that
[tutoria-api](https://github.com/your-org/tutoria-api) (the student chat service)
stays lean and always-on. A PDF extraction job crashing this container has zero
impact on students chatting.

---

## What it does

| Responsibility | How |
|---|---|
| **Document text extraction** | pdfplumber → pypdf → AI vision fallback for PDFs, images, DOCX, XLSX |
| **AI quiz generation** | OpenAI / Gemini generate 50 questions per module at varying difficulty |
| **Quiz upload & validation** | Professors upload question banks (PDF, DOCX, XLSX) for parsing |
| **Prompt improvement** | Rate-limited AI endpoint that improves professor-written system prompts |
| **Daily maintenance** | Two background jobs run at 2 AM: extraction sweep + quiz regeneration |

---

## Architecture

```
tutoria-worker
├── app/
│   ├── api/routes/modules.py          # Quiz, extraction, prompt endpoints
│   ├── workers/
│   │   ├── document_extraction_worker.py   # Daily 2 AM: extract missing texts
│   │   └── quiz_maintenance_worker.py      # Daily 2 AM: regen stale quizzes
│   ├── services/
│   │   ├── document_extraction_service.py  # PDF/DOCX/XLSX → plain text
│   │   ├── quiz_generator.py               # AI quiz generation
│   │   ├── quiz_extractor.py               # Parse uploaded question files
│   │   ├── ai_service.py                   # OpenAI wrapper
│   │   ├── gemini_service.py               # Gemini wrapper
│   │   ├── blob_storage.py                 # S3 file storage
│   │   ├── key_manager.py                  # DB-managed encrypted API keys
│   │   └── formatting_service.py
│   ├── models/                         # SQLAlchemy models (subset of shared DB)
│   └── core/                           # Config, DB, security, auth guards
└── requirements.txt                    # Heavy deps: pdfplumber, pandas, pillow…
```

Shares the same PostgreSQL database as `tutoria-api` and `TutoriaApi` (.NET).
No Redis, no DynamoDB — this service is stateless between runs.

---

## Endpoints

All endpoints require authentication:
- `X-Internal-Api-Key` header — service-to-service calls from the .NET API
- `Authorization: Bearer <JWT>` — professor / super-admin dashboard calls

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v2/modules/{id}/improve-prompt` | AI-powered system prompt improvement (3/72h rate limit) |
| `POST` | `/api/v2/modules/{id}/extract-text` | Trigger text extraction for module files |
| `POST` | `/api/v2/modules/{id}/quizzes/generate` | Generate AI quiz questions |
| `POST` | `/api/v2/modules/{id}/quizzes/upload` | Upload question bank file |
| `GET`  | `/health` | Health check |

---

## Setup

```bash
cp .env.example .env
# fill in DATABASE_URL, OPENAI_API_KEY, AWS credentials, etc.

pip install -r requirements.txt
python run.py dev      # development (port 8001, hot reload)
python run.py         # production
```

### Docker

```bash
docker build -t tutoria-worker .
docker run --env-file .env -p 8001:8001 tutoria-worker
```

---

## Environment Variables

See [`.env.example`](.env.example) for the full list with descriptions.

Key variables:

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string — **same DB as tutoria-api** |
| `INTERNAL_API_KEY` | Must match the value in `tutoria-api` and the .NET API |
| `OPENAI_API_KEY` | Primary AI provider for quiz generation |
| `GEMINI_API_KEY` | Preferred for document extraction (faster, cheaper) |
| `S3_BUCKET_NAME` | Same S3 bucket as tutoria-api |
| `ENCRYPTION_KEY` | Must match tutoria-api (decrypts DB-managed provider keys) |

---

## Relationship to other services

```
TutoriaApi (.NET)  ──POST /api/v2/modules/{id}/extract-text──►  tutoria-worker
                   ──POST /api/v2/modules/{id}/quizzes/generate► tutoria-worker
                                  │
                             (both read/write)
                                  │
                             PostgreSQL DB
                                  │
tutoria-api        ──reads Quiz, File.extracted_text────────────► serves to students
```

---

## Deployment

Recommended: **Azure Container App** with:
- Min replicas: `0` (scale to zero when idle — daily jobs wake it up)
- Max replicas: `1` (no need for horizontal scale; heavy jobs are sequential)
- CPU: `2 vCPU`, Memory: `4 Gi` (pdfplumber + pandas can be hungry)

The container runs on port `8001` by default (override with `PORT` env var).
