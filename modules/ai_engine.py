"""
ai_engine.py — All Anthropic API calls.
Every call is logged to activity_log with activity_type='AI Action'.
Only jd_raw, jd_keywords, resume text (from cache), and notes are sent to the API.
"""
from __future__ import annotations
import json
import logging
from typing import Optional

import anthropic

from config import CLAUDE_MODEL, OWNER_BACKGROUND_SUMMARY

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


def call_claude(system_prompt: str, user_message: str, max_tokens: int = 1000) -> str:
    """Core wrapper for all Claude API calls. Returns response text."""
    message = _get_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    return message.content[0].text


def _log_ai_action(function_name: str, opportunity_id: int = None, tokens_used: int = None):
    """Log every AI call to activity_log."""
    try:
        from models.activity import log_activity
        log_activity(
            activity_type="AI Action",
            description=f"AI function called: {function_name}",
            opportunity_id=opportunity_id,
            metadata={"model": CLAUDE_MODEL, "function_name": function_name, "tokens_used": tokens_used}
        )
    except Exception as e:
        logger.warning(f"Could not log AI action: {e}")


def _parse_json_response(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    return json.loads(cleaned.strip())


# ── AI FUNCTIONS ──────────────────────────────────────────────────────────────

def score_fit(resume_text: str, jd_text: str, opportunity_id: int = None) -> dict:
    """
    Score candidate fit against a JD.
    Returns a dict with fit_score, rationale, strengths, gaps, keywords, bullet rewrite.
    """
    system_prompt = (
        "You are a senior hiring manager evaluating candidates for data and analytics roles in fintech. "
        "Respond ONLY with valid JSON. No explanation outside the JSON object."
    )
    user_message = f"""Evaluate this candidate's resume against the job description below.

RESUME:
{resume_text}

JOB DESCRIPTION:
{jd_text}

Return this exact JSON structure:
{{
  "fit_score": <integer 1-10>,
  "score_rationale": "<2 sentences>",
  "top_strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "gaps_or_risks": ["<gap 1>", "<gap 2>", "<gap 3>"],
  "ats_keywords": ["<keyword 1>", "<keyword 2>", "<keyword 3>", "<keyword 4>", "<keyword 5>"],
  "suggested_bullet_rewrite": "<one improved bullet mirroring JD language>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=1200)
        result = _parse_json_response(response_text)
        _log_ai_action("score_fit", opportunity_id=opportunity_id)
        return result
    except Exception as e:
        logger.error(f"score_fit failed: {e}")
        raise


def extract_jd_structure(raw_text: str) -> dict:
    """
    Parse a raw job description into structured fields.
    Returns a dict with company, role_title, keywords, etc.
    """
    system_prompt = "You are a structured data extractor. Respond ONLY with valid JSON."
    user_message = f"""Extract structured fields from this job posting text.

TEXT:
{raw_text}

Return this exact JSON:
{{
  "company": "<company name or null>",
  "role_title": "<exact title>",
  "job_family_guess": "<one of: Analytics Manager, Data Manager, BI Manager, Decision Science, Director Stretch>",
  "required_skills": ["<skill>"],
  "preferred_skills": ["<skill>"],
  "keywords": ["<5-7 most important ATS keywords>"],
  "salary_range": "<string or null>",
  "remote_ok": <true or false or null>,
  "seniority": "<IC / Manager / Director / VP>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=800)
        result = _parse_json_response(response_text)
        _log_ai_action("extract_jd_structure")
        return result
    except Exception as e:
        logger.error(f"extract_jd_structure failed: {e}")
        raise


def draft_outreach(context: dict) -> dict:
    """
    Draft personalized outreach messages for a contact.
    context keys: contact_name, contact_title, company, contact_type, hook, my_background_summary
    Returns: {linkedin_note, inmail_or_email, subject_line}
    """
    background = context.get("my_background_summary", OWNER_BACKGROUND_SUMMARY)

    system_prompt = (
        "You are a professional career coach. Draft personalized outreach messages. "
        "Respond ONLY with valid JSON. Be specific, warm, and human — never generic or salesy."
    )
    user_message = f"""Draft outreach from me to {context['contact_name']}, {context['contact_title']} at {context['company']}.

Contact type: {context['contact_type']}
Hook / reason for reaching out: {context['hook']}
My background: {background}

Rules:
- LinkedIn connection note variant: under 300 characters, no line breaks
- InMail / email variant: under 150 words
- End with ONE low-friction ask (20-min call or single question)
- Do NOT use: "excited", "thrilled", "leveraging", "synergy", "reach out"
- Sound like a real person, not a template

Return this JSON:
{{
  "linkedin_note": "<under 300 chars>",
  "inmail_or_email": "<under 150 words>",
  "subject_line": "<email subject if sending via email>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=600)
        result = _parse_json_response(response_text)
        _log_ai_action("draft_outreach")
        return result
    except Exception as e:
        logger.error(f"draft_outreach failed: {e}")
        raise


def tailor_resume_bullets(bullets: list[str], jd_keywords: list[str], jd_context: str) -> dict:
    """
    Rewrite resume bullets to align with JD language.
    CRITICAL: Never change numbers/metrics.
    Returns {rewritten_bullets: [{original, rewritten, changes_made}], overall_notes}
    """
    bullets_numbered = "\n".join(f"{i+1}. {b}" for i, b in enumerate(bullets))
    keywords_str = ", ".join(jd_keywords)

    system_prompt = (
        "You are an expert resume writer for data and analytics professionals in fintech. "
        "Respond ONLY with valid JSON."
    )
    user_message = f"""Rewrite these resume bullets to better align with the job description language below.
CRITICAL RULES:
- Keep all numbers and metrics EXACTLY as they appear — never invent or change figures
- Maintain past tense, action-verb-first format
- Mirror JD terminology naturally in context — do not keyword-stuff
- Flag every change you make so the human can review it

My bullets:
{bullets_numbered}

JD keywords to mirror: {keywords_str}
JD context: {jd_context}

Return this JSON:
{{
  "rewritten_bullets": [
    {{
      "original": "<original bullet>",
      "rewritten": "<new bullet>",
      "changes_made": "<brief description of what changed and why>"
    }}
  ],
  "overall_notes": "<1-2 sentences on fit and remaining gaps>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=1500)
        result = _parse_json_response(response_text)
        _log_ai_action("tailor_resume_bullets")
        return result
    except Exception as e:
        logger.error(f"tailor_resume_bullets failed: {e}")
        raise


def generate_interview_prep(role_title: str, company: str, jd_text: str, opportunity_id: int = None) -> dict:
    """
    Generate interview prep materials for a role.
    Returns {behavioral_questions, technical_questions, questions_to_ask_them, company_briefing, watch_out_for}
    """
    system_prompt = (
        "You are an expert interview coach for data and analytics leadership roles in fintech. "
        "Respond ONLY with valid JSON."
    )
    user_message = f"""Prepare interview materials for: {role_title} at {company}

JD:
{jd_text}

Return this JSON:
{{
  "behavioral_questions": ["<q1>", "<q2>", "<q3>", "<q4>", "<q5>"],
  "technical_questions": ["<q1>", "<q2>", "<q3>"],
  "questions_to_ask_them": ["<q1>", "<q2>", "<q3>"],
  "company_briefing": "<3-4 sentences: what they do, their data/analytics angle, recent news if inferable from JD>",
  "watch_out_for": "<1-2 sentences on likely pain points or hard questions for this specific role>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=1200)
        result = _parse_json_response(response_text)
        _log_ai_action("generate_interview_prep", opportunity_id=opportunity_id)
        return result
    except Exception as e:
        logger.error(f"generate_interview_prep failed: {e}")
        raise


def draft_thank_you(
    interviewer_name: str,
    interviewer_title: str,
    company: str,
    key_moment: str,
    fit_point: str,
) -> str:
    """
    Draft a post-interview thank-you email.
    Returns raw string (first line is subject line).
    """
    system_prompt = (
        "Write a brief, genuine post-interview thank-you email. Under 120 words. "
        "No hollow phrases. No 'I was thrilled/excited/honored.' Sound like a real person."
    )
    user_message = f"""Write a thank-you email to {interviewer_name}, {interviewer_title} at {company}.
Key moment from our conversation: {key_moment}
One fit point I want to reinforce: {fit_point}
Include a subject line on the first line formatted as: Subject: <subject>"""

    try:
        result = call_claude(system_prompt, user_message, max_tokens=400)
        _log_ai_action("draft_thank_you")
        return result.strip()
    except Exception as e:
        logger.error(f"draft_thank_you failed: {e}")
        raise


def generate_cover_letter(
    resume_text: str,
    jd_text: str,
    company: str,
    role_title: str,
    opportunity_id: int = None,
) -> dict:
    """
    Write a cover letter tailored to the role.
    Returns {cover_letter: str} — plain text, paragraphs separated by \\n\\n.
    """
    system_prompt = (
        "You are a professional cover letter writer for data and analytics leadership roles in fintech. "
        "Write a concise, genuine cover letter — under 350 words. "
        "No hollow phrases. Never use 'thrilled', 'excited', 'passionate', 'leverage', 'synergy'. "
        "Sound like a real person making a direct, confident case. "
        "Respond ONLY with valid JSON. No explanation outside the JSON object."
    )
    user_message = f"""Write a cover letter for:
Role: {role_title} at {company}

Candidate background:
{OWNER_BACKGROUND_SUMMARY}

RESUME (for context):
{resume_text}

JOB DESCRIPTION:
{jd_text}

Rules:
- Opening: state the role and one concrete reason you're a fit — no flattery
- Middle 1-2 paragraphs: specific evidence from your background that matches the JD
- Closing: clear, low-key call to action
- Paragraphs separated by blank lines

Return this JSON:
{{
  "cover_letter": "<full cover letter as plain text, paragraphs separated by \\n\\n>"
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=700)
        result = _parse_json_response(response_text)
        _log_ai_action("generate_cover_letter", opportunity_id=opportunity_id)
        return result
    except Exception as e:
        logger.error(f"generate_cover_letter failed: {e}")
        raise


def generate_tailored_resume(resume_text: str, jd_text: str, opportunity_id: int = None) -> dict:
    """
    Rewrite the candidate's full resume tailored to a specific JD.
    Returns {tailored_resume: str, key_changes: list[str]}
    """
    system_prompt = (
        "You are an expert resume writer specializing in data and analytics leadership roles in fintech. "
        "Given the candidate's existing resume and a job description, rewrite the full resume tailored "
        "to that role. Preserve all true experience — never fabricate metrics or responsibilities. "
        "Optimize for ATS keywords from the JD without keyword-stuffing. "
        "Respond ONLY with valid JSON. No explanation outside the JSON object."
    )
    user_message = f"""Candidate background:
{OWNER_BACKGROUND_SUMMARY}

EXISTING RESUME:
{resume_text}

JOB DESCRIPTION:
{jd_text}

Rewrite the full resume optimized for this role. Keep all sections (Summary, Experience, Skills, Education).
Preserve every metric and number exactly. Mirror JD terminology naturally.

Return this JSON:
{{
  "tailored_resume": "<full rewritten resume as plain text>",
  "key_changes": ["<change 1>", "<change 2>", "<change 3>", "<change 4>", "<change 5>"]
}}"""

    try:
        response_text = call_claude(system_prompt, user_message, max_tokens=3000)
        result = _parse_json_response(response_text)
        _log_ai_action("generate_tailored_resume", opportunity_id=opportunity_id)
        return result
    except Exception as e:
        logger.error(f"generate_tailored_resume failed: {e}")
        raise


def generate_daily_digest(
    today_queue: list[dict],
    followup_needed: list[dict],
    pipeline_summary: list[dict],
) -> str:
    """
    Generate a plain-text daily briefing.
    Returns a string under 300 words.
    """
    system_prompt = (
        "You are a job search operations assistant. Generate a concise daily briefing. "
        "Plain text, no markdown. Under 300 words."
    )
    user_message = f"""Generate a daily job search digest.

Today's queue:
{json.dumps(today_queue, indent=2)}

Opportunities needing follow-up:
{json.dumps(followup_needed, indent=2)}

Active pipeline summary:
{json.dumps(pipeline_summary, indent=2)}

Format:
1. TODAY'S PRIORITIES (top 3 actions)
2. FOLLOW-UP ALERTS (who to nudge)
3. PIPELINE HEALTH (1 sentence on overall momentum)
4. ONE SUGGESTION (what to focus energy on today)"""

    try:
        result = call_claude(system_prompt, user_message, max_tokens=600)
        _log_ai_action("generate_daily_digest")
        return result.strip()
    except Exception as e:
        logger.error(f"generate_daily_digest failed: {e}")
        raise
