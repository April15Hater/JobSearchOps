"""
workflow.py — Sequence management: follow-up queue, stage transitions, stale record alerts.
"""
from __future__ import annotations
import logging
from datetime import date, timedelta

from db.database import execute_query
from models.opportunity import get_opportunity, update_opportunity
from models.activity import log_activity

logger = logging.getLogger(__name__)


STAGE_TRANSITIONS: dict[str, tuple[str, int]] = {
    "Prospect":         ("Find contact and send outreach", 0),
    "Warm Lead":        ("Follow up if no response in 3 days", 3),
    "Applied":          ("Follow up with contact; check for recruiter screen", 5),
    "Recruiter Screen": ("Send thank-you; await HM invite", 1),
    "HM Interview":     ("Send thank-you; prepare for loop", 1),
    "Loop":             ("Send thank-yous to all interviewers; await decision", 2),
    "Offer Pending":    ("Review offer; research comp benchmarks", 3),
}


def calculate_next_action(stage: str) -> tuple[str, int]:
    """
    Return (next_action_text, days_from_now) based on stage.
    Falls back to a generic prompt for unknown stages.
    """
    return STAGE_TRANSITIONS.get(stage, ("Review and set next step", 7))


def get_followup_queue() -> list[dict]:
    """
    Return contacts where outreach_day0 was 3 or 7 days ago
    and response_status is still 'Pending'.
    """
    today = date.today().isoformat()
    day3 = (date.today() - timedelta(days=3)).isoformat()
    day7 = (date.today() - timedelta(days=7)).isoformat()

    rows = execute_query(
        """SELECT c.id as contact_id, c.full_name, c.title, c.company,
                  c.outreach_day0, c.outreach_day3, c.outreach_day7,
                  c.response_status, c.contact_type,
                  o.id as opportunity_id, o.company as opp_company,
                  o.role_title, o.stage,
                  CASE
                    WHEN c.outreach_day0 = ? THEN 'Day 3 follow-up due'
                    WHEN c.outreach_day0 = ? THEN 'Day 7 follow-up due'
                  END as followup_reason
           FROM contacts c
           JOIN opportunities o ON o.id = c.opportunity_id
           WHERE c.response_status = 'Pending'
             AND (c.outreach_day0 = ? OR c.outreach_day0 = ?)
             AND o.stage != 'Closed'
           ORDER BY c.outreach_day0 ASC""",
        (day3, day7, day3, day7),
        fetch="all"
    )
    return [dict(r) for r in rows] if rows else []


def advance_stage(opportunity_id: int, new_stage: str, note: str = None, close_reason: str = None) -> None:
    """
    Update an opportunity's stage, recalculate next_action_date, and log to activity_log.
    """
    opp = get_opportunity(opportunity_id)
    if not opp:
        raise ValueError(f"Opportunity {opportunity_id} not found.")

    old_stage = opp.stage
    next_action_text, days_out = calculate_next_action(new_stage)
    next_action_date = (date.today() + timedelta(days=days_out)).isoformat()

    update_kwargs = {
        "stage": new_stage,
        "next_action": next_action_text,
        "next_action_date": next_action_date,
    }
    if new_stage == "Closed":
        update_kwargs["date_closed"] = date.today().isoformat()
        if close_reason:
            update_kwargs["close_reason"] = close_reason
    if new_stage == "Applied" and old_stage != "Applied":
        update_kwargs["date_applied"] = date.today().isoformat()

    update_opportunity(opportunity_id, **update_kwargs)

    description = f"Stage changed: {old_stage} → {new_stage}"
    if note:
        description += f" | Note: {note}"

    log_activity(
        activity_type="Stage Change",
        description=description,
        opportunity_id=opportunity_id,
        metadata={"old_stage": old_stage, "new_stage": new_stage, "next_action_date": next_action_date},
    )
    logger.info(f"Opp {opportunity_id}: {old_stage} → {new_stage} (next action in {days_out}d)")


def flag_stale_records(days_stale: int = 7) -> list[dict]:
    """
    Return open opportunities with no update in N days.
    """
    cutoff = (date.today() - timedelta(days=days_stale)).isoformat()
    rows = execute_query(
        """SELECT id, company, role_title, stage, updated_at, next_action, next_action_date
           FROM opportunities
           WHERE stage != 'Closed'
             AND updated_at < ?
           ORDER BY updated_at ASC""",
        (cutoff,),
        fetch="all"
    )
    return [dict(r) for r in rows] if rows else []


def get_today_queue() -> list[dict]:
    """Pull from the today_queue view."""
    rows = execute_query("SELECT * FROM today_queue", fetch="all")
    return [dict(r) for r in rows] if rows else []


def get_pipeline_summary() -> list[dict]:
    """Pull from the pipeline_summary view."""
    rows = execute_query("SELECT * FROM pipeline_summary", fetch="all")
    return [dict(r) for r in rows] if rows else []
