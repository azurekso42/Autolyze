# Autolyze Backend

FastAPI backend for the Autolyze AI platform.
Handles dataset analytics - powered by a local LLM via LM Studio.

---

## Prerequisites

| Tool | Notes |
|------|-------|
| Python 3.11+ | https://www.python.org/downloads/ |
| LM Studio | https://lmstudio.ai - load `qwen2.5-14b-instruct` and start the local server |

---

## Setup

```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the backend
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Before starting, set `SECRET_KEY` and either `AUTH_USERS_JSON` or `AUTH_USER_EMAIL` + `AUTH_USER_PASSWORD_HASH`.
Optional token lifetimes:

- `ACCESS_TOKEN_EXPIRE_MINUTES` defaults to `15`
- `REFRESH_TOKEN_EXPIRE_DAYS` defaults to `30`
- `AUTH_REFRESH_COOKIE_SECURE` defaults to `false` for local HTTP development
- `AUTH_REFRESH_COOKIE_SAMESITE` defaults to `lax`

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/health` | Health check |
| POST | `/login` | Exchange credentials for an access token plus a refresh cookie |
| POST | `/auth/refresh` | Rotate the refresh cookie into a new access token |
| POST | `/auth/logout` | Clear the refresh cookie |
| GET  | `/api/v1/auth/verify` | Verify the current access token |
| POST | `/api/v1/data/analyze` | Analyze dataset and generate AI insights |
| POST | `/api/v1/data/chat` | Ask a follow-up question about the analyzed dataset |
| POST | `/api/v1/data/export` | Export the computed results as a multi-sheet Excel workbook |

Interactive API docs are disabled in this deployment.

---

## LM Studio

1. Open LM Studio and download `qwen2.5-14b-instruct`
2. Go to **Local Server** tab
3. Load the model and click **Start Server**
4. Confirm it's running at `http://localhost:1234`
