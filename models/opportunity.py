from __future__ import annotations
import sqlite3
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import date, datetime

from db.database import execute_query


@dataclass
class Opportunity:
    id: Optional[int] = None
    company: str = ""
    role_title: str = ""
    job_family: Optional[str] = None       # A-E
    tier: Optional[int] = None             # 1-3
    stage: str = "Prospect"
    source: Optional[str] = None
    date_added: Optional[str] = None
    date_applied: Optional[str] = None
    date_closed: Optional[str] = None
    close_reason: Optional[str] = None
    fit_score: Optional[int] = None
    salary_range: Optional[str] = None
    jd_url: Optional[str] = None
    jd_raw: Optional[str] = None
    jd_keywords: Optional[str] = None     # JSON array string
    resume_version: Optional[str] = None
    next_action: Optional[str] = None
    next_action_date: Optional[str] = None
    notes: Optional[str] = None
    ai_fit_summary: Optional[str] = None  # JSON string
    tailored_resume: Optional[str] = None
    cover_letter: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Opportunity":
        return cls(**{k: row[k] for k in row.keys()})

    def to_dict(self) -> dict:
        d = asdict(self)
        # Parse JSON fields for convenience
        if d.get("jd_keywords"):
            try:
                d["jd_keywords_list"] = json.loads(d["jd_keywords"])
            except (json.JSONDecodeError, TypeError):
                d["jd_keywords_list"] = []
        if d.get("ai_fit_summary"):
            try:
                d["ai_fit_summary_parsed"] = json.loads(d["ai_fit_summary"])
            except (json.JSONDecodeError, TypeError):
                d["ai_fit_summary_parsed"] = {}
        return d


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_opportunity(
    company: str,
    role_title: str,
    job_family: str = None,
    tier: int = None,
    stage: str = "Prospect",
    source: str = None,
    salary_range: str = None,
    jd_url: str = None,
    jd_raw: str = None,
    jd_keywords: str = None,
    next_action: str = None,
    next_action_date: str = None,
    notes: str = None,
) -> int:
    """Insert a new opportunity and return its id."""
    sql = """
        INSERT INTO opportunities
          (company, role_title, job_family, tier, stage, source, salary_range,
           jd_url, jd_raw, jd_keywords, next_action, next_action_date, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    return execute_query(sql, (
        company, role_title, job_family, tier, stage, source, salary_range,
        jd_url, jd_raw, jd_keywords, next_action, next_action_date, notes
    ))


def get_opportunity(opp_id: int) -> Optional[Opportunity]:
    row = execute_query(
        "SELECT * FROM opportunities WHERE id = ?", (opp_id,), fetch="one"
    )
    return Opportunity.from_row(row) if row else None


def update_opportunity(opp_id: int, **kwargs) -> int:
    """Update arbitrary fields on an opportunity. Returns rowcount."""
    if not kwargs:
        return 0
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [opp_id]
    return execute_query(
        f"UPDATE opportunities SET {set_clause} WHERE id = ?", tuple(values)
    )


def list_opportunities(
    stage: str = None,
    tier: int = None,
    job_family: str = None,
    exclude_closed: bool = False,
) -> list[Opportunity]:
    conditions = []
    params = []

    if stage:
        conditions.append("stage = ?")
        params.append(stage)
    if tier:
        conditions.append("tier = ?")
        params.append(tier)
    if job_family:
        conditions.append("job_family = ?")
        params.append(job_family)
    if exclude_closed:
        conditions.append("stage != 'Closed'")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = execute_query(
        f"SELECT * FROM opportunities {where} ORDER BY tier ASC, date_added DESC",
        tuple(params),
        fetch="all"
    )
    return [Opportunity.from_row(r) for r in rows] if rows else []


def search_opportunities(query: str) -> list[Opportunity]:
    """Full-text search across company, role_title, notes."""
    like = f"%{query}%"
    rows = execute_query(
        """SELECT * FROM opportunities
           WHERE company LIKE ? OR role_title LIKE ? OR notes LIKE ?
           ORDER BY date_added DESC""",
        (like, like, like),
        fetch="all"
    )
    return [Opportunity.from_row(r) for r in rows] if rows else []
