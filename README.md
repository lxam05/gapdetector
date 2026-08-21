# Gap Detector

Gap Detector is a full-stack competitor-research application that turns public customer feedback into structured product and positioning insights. Given a company or domain, it searches the web, extracts useful evidence and uses an LLM to identify recurring complaints, unmet needs, competitive strengths and opportunities.

> The original hosted service is no longer online, but the complete application can be run locally.

## What it does

- Collects search results and public web content through Serper and `trafilatura`
- Pulls and filters Google Play reviews for supported mobile-app targets
- Produces structured competitor reports with Anthropic Claude
- Supports side-by-side competitor analysis and product-specific strategy
- Provides preview and full-report workflows with API-cost guardrails
- Implements email/password authentication with JWT access and refresh tokens
- Supports report credits, one-off purchases and subscriptions through Stripe Checkout
- Verifies Stripe webhook signatures before applying account entitlements
- Renders a server-side interface using Jinja2 and Tailwind CSS

## Stack

- **Backend:** Python, FastAPI, SQLAlchemy 2, Alembic
- **Database:** PostgreSQL with `asyncpg`
- **Research pipeline:** Serper, `httpx`, `trafilatura`, Google Play Scraper
- **AI:** Anthropic Claude; OpenAI is used by the preview-analysis path
- **Frontend:** Jinja2 templates, Tailwind CSS and vanilla JavaScript
- **Infrastructure:** Railway-ready configuration, optional Redis and Cloudflare Turnstile
- **Payments:** Stripe Checkout and signature-verified webhooks

## How the analysis pipeline works

```text
Company or domain
       |
       v
Search results + website pages + optional Play Store reviews
       |
       v
Content extraction, filtering and deduplication
       |
       v
Preview analysis and usage guardrails
       |
       v
Claude full report
       |
       v
Structured dashboard and saved report
```

## Run locally

### Prerequisites

- Python 3.11 or newer
- PostgreSQL 14 or newer
- A [Serper](https://serper.dev/) API key
- An [Anthropic](https://console.anthropic.com/) API key
- An OpenAI API key if you want to exercise the preview-analysis workflow

Stripe, Redis, Turnstile and transactional email are optional for basic local development.

### 1. Clone the repository

```bash
git clone https://github.com/lxam05/gapdetector.git
cd gapdetector
```

### 2. Create a virtual environment and install dependencies

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Create the PostgreSQL database

With PostgreSQL running locally:

```bash
createdb gapdetector
```

If your PostgreSQL username, password or port differs, adjust `DATABASE_URL` in the next step.

### 4. Configure the environment

```bash
cp .env.example .env
```

At minimum, update these values:

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/gapdetector
JWT_SECRET_KEY=replace-with-a-long-random-value
SERPER_API_KEY=your-serper-key
ANTHROPIC_API_KEY=your-anthropic-key
```

Generate a suitable local JWT secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The Google OAuth fields in `.env.example` are placeholders because the current OAuth routes are not implemented. When SMTP and MailerSend are left unset, verification and password-reset emails are written to the application logs for local testing.

### 5. Apply database migrations

```bash
alembic upgrade head
```

### 6. Start the application

```bash
uvicorn main:app --reload
```

Open [http://localhost:8000](http://localhost:8000). FastAPI's generated API documentation is available at [http://localhost:8000/docs](http://localhost:8000/docs).

## Optional integrations

### Stripe

To test purchases and report credits, add Stripe test-mode keys and Price IDs to `.env`:

```dotenv
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
PRICE_SINGLE_REPORT=price_...
PRICE_BUNDLE_5=price_...
PRICE_SUB_MONTHLY=price_...
SUCCESS_URL_BASE=http://localhost:8000
```

Forward test webhooks to the application:

```bash
stripe listen --forward-to http://localhost:8000/stripe/webhook
```

See [CREDITS_WEBHOOK_TESTING.md](CREDITS_WEBHOOK_TESTING.md) for the complete credits and subscription flow.

### Redis and Turnstile

Redis enables distributed rate limits across multiple application instances. Without `REDIS_URL`, the application falls back to in-process limits suitable for local development. Cloudflare Turnstile can be enabled with `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY`.

## Project structure

```text
app/
  api/routes/       HTTP endpoints for auth, scans, checkout and webhooks
  core/             Configuration and security helpers
  db/               SQLAlchemy engine and session management
  models/           PostgreSQL models
  schemas/          Request and response models
  services/         Analysis, email, guardrails and Stripe logic
alembic/             Database migrations
templates/           Jinja2 pages
images/              Static assets
main.py              FastAPI application and research pipeline
playstore.py         Google Play review collection and filtering
```

## Security and privacy

- Never commit `.env`, API keys, Stripe secrets or OAuth credentials.
- Use Stripe test-mode credentials for local development.
- The analyzer requests public webpages supplied by search results; only scan targets you are permitted to access and respect applicable website terms.
- Reports are generated by an LLM and may contain mistakes. Treat them as research assistance, not verified facts.
- Use restrictive CORS origins, Redis-backed limits and Turnstile before exposing the service publicly.

## Current status

This repository is maintained as a portfolio project. The former public deployment and `gapdetector.com` domain are no longer active.
