"""
web/routes.py — All Flask routes for the local dashboard.
"""
import json
import os
from datetime import date, timedelta

from flask import render_template, request, redirect, url_for, jsonify, flash

from models.opportunity import list_opportunities, get_opportunity, update_opportunity, create_opportunity
from models.contact import list_contacts, get_contact, update_contact, create_contact
from models.activity import get_activity_log, log_activity
from modules.workflow import (
    get_today_queue, get_pipeline_summary, get_followup_queue,
    flag_stale_records, advance_stage, calculate_next_action
)
from config import STAGE_ORDER, JOB_FAMILIES, RESUME_CACHE_PATH


def register_routes(app):

    @app.route("/")
    def dashboard():
        today_queue = get_today_queue()
        pipeline = get_pipeline_summary()
        stale = flag_stale_records(days_stale=7)
        # Build stage counts dict
        stage_counts = {row["stage"]: row["count"] for row in pipeline}
        return render_template(
            "dashboard.html",
            today_queue=today_queue,
            pipeline=pipeline,
            stage_counts=stage_counts,
            stale=stale,
            stage_order=STAGE_ORDER,
        )

    @app.route("/run-digest", methods=["POST"])
    def run_digest():
        from modules.digest import run_daily_digest
        digest = run_daily_digest(write_log=True)
        return jsonify({"digest": digest})

    @app.route("/opportunities")
    def opportunities():
        stage_filter = request.args.get("stage")
        tier_filter = request.args.get("tier", type=int)
        family_filter = request.args.get("job_family")
        opps = list_opportunities(
            stage=stage_filter,
            tier=tier_filter,
            job_family=family_filter,
            exclude_closed=False,
        )
        return render_template(
            "opportunities.html",
            opportunities=opps,
            stage_order=STAGE_ORDER,
            job_families=JOB_FAMILIES,
            current_stage=stage_filter,
            current_tier=tier_filter,
            current_family=family_filter,
        )

    @app.route("/opportunity/<int:opp_id>")
    def opportunity_detail(opp_id):
        opp = get_opportunity(opp_id)
        if not opp:
            return "Opportunity not found", 404
        contacts = list_contacts(opportunity_id=opp_id)
        activity = get_activity_log(opportunity_id=opp_id)
        import json
        fit_summary = None
        if opp.ai_fit_summary:
            try:
                fit_summary = json.loads(opp.ai_fit_summary)
            except Exception:
                pass
        keywords = []
        if opp.jd_keywords:
            try:
                keywords = json.loads(opp.jd_keywords)
            except Exception:
                pass
        return render_template(
            "opportunity.html",
            opp=opp,
            contacts=contacts,
            activity=activity,
            fit_summary=fit_summary,
            keywords=keywords,
            stage_order=STAGE_ORDER,
            job_families=JOB_FAMILIES,
        )

    @app.route("/opportunity/<int:opp_id>/advance", methods=["POST"])
    def advance_opp(opp_id):
        new_stage = request.form.get("new_stage")
        note = request.form.get("note", "")
        if new_stage:
            advance_stage(opp_id, new_stage, note=note or None)
        return redirect(url_for("opportunity_detail", opp_id=opp_id))

    @app.route("/opportunity/<int:opp_id>/note", methods=["POST"])
    def add_note(opp_id):
        note_text = request.form.get("note", "").strip()
        if note_text:
            opp = get_opportunity(opp_id)
            existing = opp.notes or ""
            from datetime import date
            new_notes = f"{existing}\n[{date.today()}] {note_text}".strip()
            update_opportunity(opp_id, notes=new_notes)
            log_activity(
                activity_type="Note Added",
                description=note_text,
                opportunity_id=opp_id,
            )
        return redirect(url_for("opportunity_detail", opp_id=opp_id))

    @app.route("/contacts")
    def contacts():
        all_contacts = list_contacts()
        # Color-code by response_status
        status_colors = {
            "Pending": "#f59e0b",
            "Responded": "#10b981",
            "No Response": "#ef4444",
            "Meeting Scheduled": "#3b82f6",
        }
        followups = get_followup_queue()
        followup_ids = {f["contact_id"] for f in followups}
        return render_template(
            "contacts.html",
            contacts=all_contacts,
            status_colors=status_colors,
            followup_ids=followup_ids,
        )

    @app.route("/contact/<int:contact_id>/mark-response", methods=["POST"])
    def mark_response(contact_id):
        status = request.form.get("status", "Responded")
        update_contact(contact_id, response_status=status)
        contact = get_contact(contact_id)
        log_activity(
            activity_type="Response Received",
            description=f"Response status updated to: {status}",
            opportunity_id=contact.opportunity_id if contact else None,
            contact_id=contact_id,
        )
        return redirect(url_for("contacts"))

    @app.route("/opportunity/<int:opp_id>/score-fit", methods=["POST"])
    def score_fit_route(opp_id):
        from modules.ai_engine import score_fit
        opp = get_opportunity(opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not opp.jd_raw:
            return jsonify({"error": "No JD text found for this opportunity. Re-add the job using the Add Job form."}), 400
        if not os.path.exists(RESUME_CACHE_PATH):
            return jsonify({"error": f"Resume cache not found at '{RESUME_CACHE_PATH}'. Run 'python main.py score-fit <id>' from the CLI once to cache your resume."}), 400
        resume_text = open(RESUME_CACHE_PATH).read().strip()
        if not resume_text:
            return jsonify({"error": "Resume cache is empty. Run score-fit from the CLI to populate it."}), 400
        result = score_fit(resume_text, opp.jd_raw, opportunity_id=opp_id)
        update_opportunity(opp_id, fit_score=result["fit_score"], ai_fit_summary=json.dumps(result))
        return jsonify(result)

    @app.route("/opportunity/<int:opp_id>/interview-prep", methods=["POST"])
    def interview_prep_route(opp_id):
        from modules.ai_engine import generate_interview_prep
        opp = get_opportunity(opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not opp.jd_raw:
            return jsonify({"error": "No JD text found for this opportunity."}), 400
        result = generate_interview_prep(opp.role_title, opp.company, opp.jd_raw, opp_id)
        return jsonify(result)

    @app.route("/opportunity/<int:opp_id>/add-contact", methods=["POST"])
    def add_contact_route(opp_id):
        opp = get_opportunity(opp_id)
        if not opp:
            return "Opportunity not found", 404
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            return redirect(url_for("opportunity_detail", opp_id=opp_id))
        title = request.form.get("title", "").strip() or None
        company = request.form.get("company", "").strip() or opp.company
        linkedin_url = request.form.get("linkedin_url", "").strip() or None
        email = request.form.get("email", "").strip() or None
        contact_type = request.form.get("contact_type", "Recruiter")
        notes = request.form.get("notes", "").strip() or None
        contact_id = create_contact(
            full_name=full_name,
            opportunity_id=opp_id,
            title=title,
            company=company,
            linkedin_url=linkedin_url,
            email=email,
            contact_type=contact_type,
            notes=notes,
        )
        log_activity(
            activity_type="Note Added",
            description=f"Contact added: {full_name} ({contact_type})",
            opportunity_id=opp_id,
            contact_id=contact_id,
        )
        return redirect(url_for("opportunity_detail", opp_id=opp_id))

    @app.route("/add-job", methods=["GET", "POST"])
    def add_job():
        from modules.ingester import ingest_jd
        if request.method == "GET":
            return render_template("add_job.html", step=1)

        step = request.form.get("step", "extract")

        if step == "extract":
            source_input = request.form.get("source_input", "").strip()
            if not source_input:
                return render_template("add_job.html", step=1, error="Please enter a URL or paste a job description.")
            try:
                structured = ingest_jd(source_input)
            except Exception as e:
                return render_template("add_job.html", step=1, error=f"Failed to extract JD: {str(e)}")
            family_reverse = {v: k for k, v in JOB_FAMILIES.items()}
            guessed_family = family_reverse.get(structured.get("job_family_guess", ""), "")
            extracted = {
                "company": structured.get("company") or "",
                "role_title": structured.get("role_title") or "",
                "job_family": guessed_family,
                "salary_range": structured.get("salary_range") or "",
                "keywords": structured.get("keywords") or [],
                "jd_raw": structured.get("raw_text") or "",
                "jd_url": structured.get("source_url") or "",
                "jd_keywords": json.dumps(structured.get("keywords") or []),
            }
            return render_template("add_job.html", step=2, extracted=extracted, job_families=JOB_FAMILIES)

        if step == "save":
            company = request.form.get("company", "").strip()
            role_title = request.form.get("role_title", "").strip()
            if not company or not role_title:
                return render_template("add_job.html", step=1, error="Company and role title are required.")
            job_family = request.form.get("job_family", "").strip() or None
            try:
                tier = int(request.form.get("tier", "2"))
            except ValueError:
                tier = 2
            source_label = request.form.get("source_label", "LinkedIn")
            salary_range = request.form.get("salary_range", "").strip() or None
            jd_raw = request.form.get("jd_raw", "") or None
            jd_url = request.form.get("jd_url", "").strip() or None
            jd_keywords = request.form.get("jd_keywords", "[]") or "[]"
            next_action_text, days_out = calculate_next_action("Prospect")
            next_action_date = (date.today() + timedelta(days=days_out)).isoformat()
            opp_id = create_opportunity(
                company=company,
                role_title=role_title,
                job_family=job_family,
                tier=tier,
                stage="Prospect",
                source=source_label,
                salary_range=salary_range,
                jd_url=jd_url,
                jd_raw=jd_raw,
                jd_keywords=jd_keywords,
                next_action=next_action_text,
                next_action_date=next_action_date,
            )
            log_activity(
                activity_type="Note Added",
                description="Opportunity created via web UI",
                opportunity_id=opp_id,
            )
            return redirect(url_for("opportunity_detail", opp_id=opp_id))

        return redirect(url_for("add_job"))
