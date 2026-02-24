# Job Search Ops

A personal job search pipeline manager built on SQLite, with a Click CLI, a local Flask dashboard, and Claude AI for fit scoring, outreach drafting, and interview prep.

---

## Features

- **Pipeline tracking** — 8-stage funnel (Prospect → Closed) with tier priorities and job family labels
- **AI fit scoring** — Claude evaluates your resume against a JD and returns a 1–10 score, strengths, gaps, and ATS keywords
- **Outreach drafting** — generates a LinkedIn connection note, InMail, and email for each contact
- **Resume tailoring** — rewrites your bullets to mirror JD language without changing any metrics
- **Interview prep** — behavioral and technical questions, company briefing, and questions to ask
- **Follow-up queue** — surfaces contacts due for Day 3 and Day 7 follow-ups
- **Daily digest** — AI-generated briefing: today's priorities, follow-up alerts, and pipeline health
- **Local web dashboard** — filterable opportunity list, contact tracking, activity log, stage advancement
- **CSV export** — one command dumps the full pipeline to a dated CSV

---

## Setup

**Requirements:** Python 3.10+

```bash
pip install -r requirements.txt
```

Copy `.env.template` to `.env` and fill in your key:

```bash
cp .env.template .env
```

```
ANTHROPIC_API_KEY=your_key_here
DB_PATH=jobsearch.db          # optional, defaults to jobsearch.db
RESUME_CACHE_PATH=.resume_cache.txt  # optional
```

The database is created automatically on first run.

---

## CLI Usage

```bash
python main.py <command> [args]
```

| Command | What it does |
|---|---|
| `add-job` | Ingest a job posting (URL or pasted text), parse with AI, add to pipeline |
| `score-fit <opp_id>` | Score resume fit against a JD (1–10 with rationale) |
| `add-contact <opp_id>` | Attach a contact (HM, recruiter, peer, etc.) to an opportunity |
| `send-outreach <contact_id>` | Draft AI outreach, confirm, and log Day 0 |
| `follow-up` | Show contacts due for Day 3 / Day 7 follow-up and mark as sent |
| `advance <opp_id> <stage>` | Move an opportunity to a new pipeline stage |
| `prep <opp_id>` | Generate interview prep materials |
| `tailor <opp_id>` | Rewrite resume bullets to match JD keywords |
| `digest` | Print and log a daily AI-generated briefing |
| `dashboard` | Launch the local web UI at http://127.0.0.1:5001 |
| `list [--stage STAGE]` | List all open opportunities in a table |
| `export` | Export all opportunities to a dated CSV |

### Typical workflow for a new opportunity

```bash
# 1. Add the job
python main.py add-job

# 2. Score fit against your resume
python main.py score-fit 1

# 3. Add a contact and draft outreach
python main.py add-contact 1
python main.py send-outreach 1

# 4. Check follow-ups each morning
python main.py follow-up

# 5. Advance stage after a recruiter call
python main.py advance 1 "Recruiter Screen"

# 6. Prep for interviews
python main.py prep 1
```

---

## Web Dashboard

```bash
python main.py dashboard
# Opens at http://127.0.0.1:5001
```

The dashboard is local-only (binds to 127.0.0.1) with no authentication. It provides:

- **/** — Today's queue, pipeline stage counts, stale opportunity alerts
- **/opportunities** — Full opportunity list with stage/tier/job-family filters
- **/opportunity/\<id\>** — Detail view: JD keywords, fit summary, contacts, activity log, stage advancement
- **/contacts** — All contacts with response-status color coding and follow-up highlights

---

## Pipeline Stages

```
Prospect → Warm Lead → Applied → Recruiter Screen → HM Interview → Loop → Offer Pending → Closed
```

Each stage transition recalculates `next_action_date` and logs to the activity trail.

---

## Job Families

| Code | Label |
|---|---|
| A | Analytics Manager |
| B | Data Manager |
| C | BI Manager |
| D | Decision Science |
| E | Director Stretch |

---

## Project Structure

```
├── main.py              CLI entry point (Click commands)
├── config.py            Environment config, constants, owner context
├── requirements.txt
├── .env.template
│
├── models/              Data models and CRUD
│   ├── opportunity.py
│   ├── contact.py
│   └── activity.py
│
├── modules/             Business logic
│   ├── ai_engine.py     All Anthropic API calls
│   ├── workflow.py      Stage transitions, follow-up queue, stale alerts
│   ├── ingester.py      JD parsing from URL or pasted text
│   └── digest.py        Daily digest generation
│
├── db/
│   ├── database.py      SQLite connection and query wrapper
│   └── schema.sql       Tables, triggers, and views
│
├── web/
│   ├── app.py           Flask app initialization
│   └── routes.py        Dashboard route handlers
│
├── templates/           Jinja2 HTML templates
│   ├── base.html
│   ├── dashboard.html
│   ├── opportunities.html
│   ├── opportunity.html
│   └── contacts.html
│
└── tests/
    ├── test_ai_engine.py
    └── test_workflow.py
```

---

## Running Tests

```bash
pytest tests/ -v
```

Tests use a mocked Anthropic client and an in-memory SQLite database — no tokens burned, no files written.

---

## Data & Privacy

- Resume text is cached locally in `.resume_cache.txt` and never written to the database
- The Anthropic API key is loaded from `.env` and never logged or committed
- The database is a local SQLite file (`jobsearch.db` by default)
- `.resume_cache.txt` and `.env` should be added to `.gitignore` if you version-control this directory
