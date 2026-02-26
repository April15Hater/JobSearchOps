"""
web/routes.py — All Flask routes for the local dashboard.
"""
import csv
import io
import json
import os
import re
from datetime import date, timedelta

from flask import render_template, request, redirect, url_for, jsonify, flash, make_response, send_file

from models.opportunity import list_opportunities, get_opportunity, update_opportunity, create_opportunity
from models.contact import list_contacts, get_contact, update_contact, create_contact
from models.activity import get_activity_log, log_activity
from modules.workflow import (
    get_today_queue, get_pipeline_summary, get_followup_queue,
    flag_stale_records, advance_stage, calculate_next_action
)
from config import (
    STAGE_ORDER, JOB_FAMILIES, RESUME_CACHE_PATH, APP_SETTINGS_PATH,
    SMTP_HOST, SMTP_PORT, SMTP_FROM, SENDER_NAME,
)


def _load_app_settings() -> dict:
    """Load app_settings.json; return empty dict if missing or malformed."""
    try:
        if os.path.exists(APP_SETTINGS_PATH):
            with open(APP_SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


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

    @app.route("/run-feed-poll", methods=["POST"])
    def run_feed_poll():
        from modules.job_feed import poll_feeds, load_feed_config
        cfg = load_feed_config()
        if not cfg["urls"]:
            return jsonify({"error": "No feed URLs configured. Add RSS URLs in Settings → Job Feeds."}), 400
        resume_text = ""
        if cfg.get("auto_score") and os.path.exists(RESUME_CACHE_PATH):
            try:
                resume_text = open(RESUME_CACHE_PATH, encoding="utf-8").read().strip()
            except Exception:
                pass
        result = poll_feeds(
            cfg["urls"],
            cfg["keywords"],
            auto_score=cfg.get("auto_score", False),
            min_score=cfg.get("min_score", 0),
            resume_text=resume_text,
        )
        return jsonify(result)

    @app.route("/contact/<int:contact_id>/mark-outreach-sent", methods=["POST"])
    def mark_outreach_sent(contact_id):
        contact = get_contact(contact_id)
        if not contact:
            return jsonify({"error": "Contact not found"}), 404
        today_str = date.today().isoformat()
        if not contact.outreach_day0:
            update_contact(contact_id, outreach_day0=today_str)
            # Advance opportunity from Prospect → Warm Lead
            if contact.opportunity_id:
                opp = get_opportunity(contact.opportunity_id)
                if opp and opp.stage == "Prospect":
                    advance_stage(contact.opportunity_id, "Warm Lead", note="Outreach sent")
        log_activity(
            activity_type="Outreach Sent",
            description=f"Outreach marked as sent to {contact.full_name}",
            opportunity_id=contact.opportunity_id,
            contact_id=contact_id,
        )
        return jsonify({"ok": True, "day0": today_str})

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
        include_closed = request.args.get("include_closed") == "1"
        # Exclude closed by default unless explicitly requested or filtering to Closed
        exclude_closed = not include_closed and stage_filter != "Closed"
        opps = list_opportunities(
            stage=stage_filter,
            tier=tier_filter,
            job_family=family_filter,
            exclude_closed=exclude_closed,
        )
        return render_template(
            "opportunities.html",
            opportunities=opps,
            stage_order=STAGE_ORDER,
            job_families=JOB_FAMILIES,
            current_stage=stage_filter,
            current_tier=tier_filter,
            current_family=family_filter,
            include_closed=include_closed,
        )

    @app.route("/opportunity/<int:opp_id>")
    def opportunity_detail(opp_id):
        opp = get_opportunity(opp_id)
        if not opp:
            return "Opportunity not found", 404
        contacts = list_contacts(opportunity_id=opp_id)
        activity = get_activity_log(opportunity_id=opp_id)
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
        close_reason = request.form.get("close_reason") or None
        if new_stage:
            advance_stage(opp_id, new_stage, note=note or None, close_reason=close_reason)
        return redirect(url_for("opportunity_detail", opp_id=opp_id))

    @app.route("/opportunities/score-unscored", methods=["POST"])
    def score_unscored():
        from modules.ai_engine import score_fit
        from db.database import execute_query
        if not os.path.exists(RESUME_CACHE_PATH):
            return jsonify({"error": "No resume cached. Save your resume in Settings first."}), 400
        resume_text = open(RESUME_CACHE_PATH, encoding="utf-8").read().strip()
        if not resume_text:
            return jsonify({"error": "Resume cache is empty. Save your resume in Settings."}), 400
        rows = execute_query(
            "SELECT id, jd_raw FROM opportunities WHERE fit_score IS NULL AND jd_raw IS NOT NULL AND jd_raw != ''",
            fetch="all",
        )
        if not rows:
            return jsonify({"scored": 0, "skipped": 0, "message": "All opportunities already have a fit score."})
        scored = skipped = errors = 0
        for row in rows:
            opp_id, jd_raw = row["id"], row["jd_raw"]
            if not jd_raw or not jd_raw.strip():
                skipped += 1
                continue
            try:
                result = score_fit(resume_text, jd_raw, opportunity_id=opp_id)
                update_opportunity(opp_id, fit_score=result["fit_score"], ai_fit_summary=json.dumps(result))
                log_activity(
                    activity_type="AI Action",
                    description=f"Fit scored (batch): {result['fit_score']}/10",
                    opportunity_id=opp_id,
                )
                scored += 1
            except Exception as e:
                errors += 1
        return jsonify({"scored": scored, "skipped": skipped, "errors": errors})

    @app.route("/opportunities/bulk-advance", methods=["POST"])
    def bulk_advance():
        opp_ids = [int(x) for x in request.form.getlist("opp_ids[]") if x.strip().isdigit()]
        new_stage = request.form.get("new_stage", "").strip()
        note = request.form.get("note", "").strip() or None
        close_reason = request.form.get("close_reason") or None
        if not opp_ids or not new_stage:
            return jsonify({"error": "No opportunities or stage selected."}), 400
        updated = 0
        for opp_id in opp_ids:
            try:
                advance_stage(opp_id, new_stage, note=note, close_reason=close_reason)
                updated += 1
            except Exception:
                pass
        return jsonify({"updated": updated, "total": len(opp_ids)})

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

    @app.route("/opportunity/<int:opp_id>/generate-resume", methods=["POST"])
    def generate_resume_route(opp_id):
        from modules.ai_engine import generate_tailored_resume
        opp = get_opportunity(opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not opp.jd_raw:
            return jsonify({"error": "No JD text on this opportunity. Add a job description first."}), 400
        try:
            resume_text = open(RESUME_CACHE_PATH).read().strip()
        except FileNotFoundError:
            return jsonify({"error": "No resume found. Add your resume in Settings."}), 400
        if len(resume_text) < 100:
            return jsonify({"error": "Resume too short. Update it in Settings."}), 400
        result = generate_tailored_resume(resume_text, opp.jd_raw, opportunity_id=opp_id)
        update_opportunity(opp_id, tailored_resume=result.get("tailored_resume", ""))
        return jsonify(result)

    @app.route("/opportunity/<int:opp_id>/generate-cover-letter", methods=["POST"])
    def generate_cover_letter_route(opp_id):
        from modules.ai_engine import generate_cover_letter
        opp = get_opportunity(opp_id)
        if not opp:
            return jsonify({"error": "Opportunity not found"}), 404
        if not opp.jd_raw:
            return jsonify({"error": "No JD text on this opportunity. Add a job description first."}), 400
        try:
            resume_text = open(RESUME_CACHE_PATH).read().strip()
        except FileNotFoundError:
            return jsonify({"error": "No resume found. Add your resume in Settings."}), 400
        if len(resume_text) < 100:
            return jsonify({"error": "Resume too short. Update it in Settings."}), 400
        result = generate_cover_letter(
            resume_text, opp.jd_raw, opp.company, opp.role_title, opportunity_id=opp_id
        )
        update_opportunity(opp_id, cover_letter=result.get("cover_letter", ""))
        return jsonify({"ok": True, "cover_letter": result.get("cover_letter", "")})

    @app.route("/opportunity/<int:opp_id>/export-resume")
    def export_resume_docx(opp_id):
        import traceback
        try:
            from modules.docx_builder import build_resume_docx
            opp = get_opportunity(opp_id)
            if not opp or not opp.tailored_resume:
                return redirect(url_for("opportunity_detail", opp_id=opp_id))
            settings = _load_app_settings()
            template_path = settings.get("resume_template_path", "").strip() or None
            docx_bytes = build_resume_docx(opp.tailored_resume, template_path)
            safe_company = re.sub(r"[^\w\-]", "_", opp.company or "company")
            safe_role = re.sub(r"[^\w\-]", "_", opp.role_title or "role")
            filename = f"{safe_company}_{safe_role}_resume.docx"
            return send_file(
                io.BytesIO(docx_bytes),
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                as_attachment=True,
                download_name=filename,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Export failed: {type(e).__name__}: {e}\n\n{traceback.format_exc()}</pre>", 500

    @app.route("/opportunity/<int:opp_id>/export-cover-letter")
    def export_cover_letter_docx(opp_id):
        import traceback
        try:
            from modules.docx_builder import build_cover_letter_docx
            opp = get_opportunity(opp_id)
            if not opp or not opp.cover_letter:
                return redirect(url_for("opportunity_detail", opp_id=opp_id))
            settings = _load_app_settings()
            template_path = settings.get("cover_letter_template_path", "").strip() or None
            docx_bytes = build_cover_letter_docx(opp.cover_letter, template_path)
            safe_company = re.sub(r"[^\w\-]", "_", opp.company or "company")
            safe_role = re.sub(r"[^\w\-]", "_", opp.role_title or "role")
            filename = f"{safe_company}_{safe_role}_cover_letter.docx"
            return send_file(
                io.BytesIO(docx_bytes),
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                as_attachment=True,
                download_name=filename,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Export failed: {type(e).__name__}: {e}\n\n{traceback.format_exc()}</pre>", 500

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
            try:
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
            except Exception as e:
                import traceback
                traceback.print_exc()
                return render_template("add_job.html", step=1, error=f"Failed to save opportunity: {e}")
            return redirect(url_for("opportunity_detail", opp_id=opp_id))

        return redirect(url_for("add_job"))

    @app.route("/contact/<int:contact_id>/draft-outreach", methods=["POST"])
    def draft_outreach_route(contact_id):
        from modules.ai_engine import draft_outreach
        contact = get_contact(contact_id)
        if not contact:
            return jsonify({"error": "Contact not found"}), 404
        hook = request.form.get("hook", "").strip()
        if not hook:
            return jsonify({"error": "Please enter a hook (reason for reaching out)."}), 400
        opp = get_opportunity(contact.opportunity_id) if contact.opportunity_id else None
        company = contact.company or (opp.company if opp else "their company")
        context = {
            "contact_name": contact.full_name,
            "contact_title": contact.title or "Professional",
            "company": company,
            "contact_type": contact.contact_type or "Other",
            "hook": hook,
        }
        result = draft_outreach(context)
        return jsonify(result)

    @app.route("/contact/<int:contact_id>/send-email", methods=["POST"])
    def send_email_route(contact_id):
        from modules.mailer import send_email
        contact = get_contact(contact_id)
        if not contact:
            return jsonify({"error": "Contact not found"}), 404
        if not contact.email:
            return jsonify({"error": "No email address on file for this contact."}), 400
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        email_type = request.form.get("email_type", "outreach")
        if not subject or not body:
            return jsonify({"error": "Subject and body are required."}), 400
        try:
            send_email(contact.email, subject, body)
        except Exception as e:
            return jsonify({"error": f"Failed to send: {str(e)}"}), 500
        today_str = date.today().isoformat()
        if email_type == "outreach" and not contact.outreach_day0:
            update_contact(contact_id, outreach_day0=today_str)
            # Advance opportunity from Prospect → Warm Lead
            if contact.opportunity_id:
                opp = get_opportunity(contact.opportunity_id)
                if opp and opp.stage == "Prospect":
                    advance_stage(contact.opportunity_id, "Warm Lead", note="Outreach email sent")
        activity_label = "Outreach Sent" if email_type == "outreach" else "Follow-Up Sent"
        log_activity(
            activity_type=activity_label,
            description=f"Email sent to {contact.full_name} <{contact.email}>: {subject}",
            opportunity_id=contact.opportunity_id,
            contact_id=contact_id,
        )
        return jsonify({"ok": True, "message": f"Sent to {contact.email}"})

    @app.route("/contact/<int:contact_id>/draft-thank-you", methods=["POST"])
    def draft_thank_you_route(contact_id):
        from modules.ai_engine import draft_thank_you
        contact = get_contact(contact_id)
        if not contact:
            return jsonify({"error": "Contact not found"}), 404
        key_moment = request.form.get("key_moment", "").strip()
        fit_point = request.form.get("fit_point", "").strip()
        if not key_moment or not fit_point:
            return jsonify({"error": "Key moment and fit point are required."}), 400
        opp = get_opportunity(contact.opportunity_id) if contact.opportunity_id else None
        company = contact.company or (opp.company if opp else "the company")
        try:
            raw = draft_thank_you(
                interviewer_name=contact.full_name,
                interviewer_title=contact.title or "the team",
                company=company,
                key_moment=key_moment,
                fit_point=fit_point,
            )
            lines = raw.strip().split("\n")
            subject = f"Thank you — {company}"
            body = raw.strip()
            if lines[0].lower().startswith("subject:"):
                subject = lines[0][8:].strip()
                body = "\n".join(lines[1:]).strip()
            log_activity(
                activity_type="AI Action",
                description=f"Thank-you email drafted for {contact.full_name}",
                opportunity_id=contact.opportunity_id,
                contact_id=contact_id,
            )
            return jsonify({"subject": subject, "body": body})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/export")
    def export_csv():
        opps = list_opportunities(exclude_closed=False)
        if not opps:
            return redirect(url_for("opportunities"))
        fields = ["id", "company", "role_title", "job_family", "tier", "stage", "source",
                  "fit_score", "salary_range", "next_action", "next_action_date",
                  "date_added", "date_applied", "jd_url"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for opp in opps:
            writer.writerow({f: getattr(opp, f, "") for f in fields})
        filename = f"jobsearch_export_{date.today().isoformat()}.csv"
        resp = make_response(output.getvalue())
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp

    @app.route("/contact/<int:contact_id>/mark-followup", methods=["POST"])
    def mark_followup(contact_id):
        contact = get_contact(contact_id)
        if not contact or not contact.outreach_day0:
            return redirect(url_for("contacts"))
        days_since = (date.today() - date.fromisoformat(contact.outreach_day0)).days
        today_str = date.today().isoformat()
        if days_since >= 6:
            update_contact(contact_id, outreach_day7=today_str)
            desc = f"Day 7 follow-up sent to {contact.full_name}"
        else:
            update_contact(contact_id, outreach_day3=today_str)
            desc = f"Day 3 follow-up sent to {contact.full_name}"
        log_activity(
            activity_type="Follow-Up Sent",
            description=desc,
            opportunity_id=contact.opportunity_id,
            contact_id=contact_id,
        )
        return redirect(url_for("contacts"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        error = None
        saved = request.args.get("saved") == "1"

        # --- Load persisted app settings ---
        app_settings = {}
        if os.path.exists(APP_SETTINGS_PATH):
            try:
                with open(APP_SETTINGS_PATH, encoding="utf-8") as f:
                    app_settings = json.load(f)
            except Exception:
                pass
        digest_time = app_settings.get("digest_time", "08:00")
        feed_urls_text = app_settings.get("feed_urls", "")
        feed_keywords_text = app_settings.get("feed_keywords", "")
        feed_auto_score = bool(app_settings.get("feed_auto_score", False))
        try:
            feed_min_score = int(app_settings.get("feed_min_score", 0))
        except (TypeError, ValueError):
            feed_min_score = 0
        smtp_host = app_settings.get("smtp_host", SMTP_HOST)
        smtp_port = app_settings.get("smtp_port", str(SMTP_PORT))
        smtp_from = app_settings.get("smtp_from", SMTP_FROM)
        sender_name = app_settings.get("sender_name", SENDER_NAME)
        resume_template_path = app_settings.get("resume_template_path", "")
        cover_letter_template_path = app_settings.get("cover_letter_template_path", "")

        # --- Load resume text ---
        resume_text = ""
        if os.path.exists(RESUME_CACHE_PATH):
            try:
                resume_text = open(RESUME_CACHE_PATH, encoding="utf-8").read()
            except Exception as e:
                error = f"Could not read resume file: {e}"

        if request.method == "POST":
            section = request.form.get("section", "resume")

            if section == "feeds":
                feed_urls_text = request.form.get("feed_urls", "").strip()
                feed_keywords_text = request.form.get("feed_keywords", "").strip()
                feed_auto_score = request.form.get("feed_auto_score") == "1"
                try:
                    feed_min_score = int(request.form.get("feed_min_score", "0") or "0")
                    feed_min_score = max(0, min(10, feed_min_score))
                except (TypeError, ValueError):
                    feed_min_score = 0
                try:
                    app_settings["feed_urls"] = feed_urls_text
                    app_settings["feed_keywords"] = feed_keywords_text
                    app_settings["feed_auto_score"] = feed_auto_score
                    app_settings["feed_min_score"] = feed_min_score
                    with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
                        json.dump(app_settings, f, indent=2)
                    return redirect(url_for("settings") + "?saved=1")
                except Exception as e:
                    error = f"Could not save feed settings: {e}"
            elif section == "templates":
                resume_template_path = request.form.get("resume_template_path", "").strip()
                cover_letter_template_path = request.form.get("cover_letter_template_path", "").strip()
                try:
                    app_settings["resume_template_path"] = resume_template_path
                    app_settings["cover_letter_template_path"] = cover_letter_template_path
                    with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
                        json.dump(app_settings, f, indent=2)
                    return redirect(url_for("settings") + "?saved=1")
                except Exception as e:
                    error = f"Could not save template settings: {e}"
            elif section == "smtp":
                smtp_host = request.form.get("smtp_host", "").strip() or SMTP_HOST
                smtp_port = request.form.get("smtp_port", "").strip() or str(SMTP_PORT)
                smtp_from = request.form.get("smtp_from", "").strip() or SMTP_FROM
                sender_name = request.form.get("sender_name", "").strip() or SENDER_NAME
                try:
                    app_settings["smtp_host"] = smtp_host
                    app_settings["smtp_port"] = smtp_port
                    app_settings["smtp_from"] = smtp_from
                    app_settings["sender_name"] = sender_name
                    with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
                        json.dump(app_settings, f, indent=2)
                    return redirect(url_for("settings") + "?saved=1")
                except Exception as e:
                    error = f"Could not save SMTP settings: {e}"
            else:
                new_text = request.form.get("resume_text", "")
                new_digest_time = request.form.get("digest_time", "08:00").strip() or "08:00"
                try:
                    with open(RESUME_CACHE_PATH, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    app_settings["digest_time"] = new_digest_time
                    with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
                        json.dump(app_settings, f, indent=2)
                    if new_digest_time != digest_time:
                        try:
                            from modules.scheduler import reschedule
                            reschedule(new_digest_time)
                        except Exception:
                            pass
                    return redirect(url_for("settings") + "?saved=1")
                except Exception as e:
                    error = f"Could not save settings: {e}"
                    resume_text = new_text
                    digest_time = new_digest_time

        return render_template(
            "settings.html",
            resume_text=resume_text, saved=saved, error=error,
            resume_path=RESUME_CACHE_PATH, digest_time=digest_time,
            smtp_host=smtp_host, smtp_port=smtp_port,
            smtp_from=smtp_from, sender_name=sender_name,
            feed_urls_text=feed_urls_text, feed_keywords_text=feed_keywords_text,
            feed_auto_score=feed_auto_score, feed_min_score=feed_min_score,
            resume_template_path=resume_template_path,
            cover_letter_template_path=cover_letter_template_path,
        )

    @app.route("/metrics")
    def metrics():
        from db.database import execute_query

        # Stage counts, avg fit, avg days open
        stage_rows = execute_query("""
            SELECT stage, COUNT(*) as count,
                   ROUND(AVG(fit_score), 1) as avg_fit,
                   ROUND(AVG(julianday('now') - julianday(date_added)), 0) as avg_days_open
            FROM opportunities
            GROUP BY stage
            ORDER BY CASE stage
                WHEN 'Prospect' THEN 1 WHEN 'Warm Lead' THEN 2 WHEN 'Applied' THEN 3
                WHEN 'Recruiter Screen' THEN 4 WHEN 'HM Interview' THEN 5
                WHEN 'Loop' THEN 6 WHEN 'Offer Pending' THEN 7 WHEN 'Closed' THEN 8
                ELSE 9 END
        """, fetch='all')
        stages = [dict(r) for r in (stage_rows or [])]
        max_count = max((s['count'] for s in stages), default=1)
        for s in stages:
            s['pct'] = round(s['count'] / max_count * 100)

        # Top-line totals
        totals_row = execute_query("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN stage != 'Closed' THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN stage = 'Applied' THEN 1 ELSE 0 END) as applied,
                SUM(CASE WHEN stage IN ('Recruiter Screen','HM Interview','Loop','Offer Pending') THEN 1 ELSE 0 END) as interviews,
                SUM(CASE WHEN stage = 'Closed' AND close_reason = 'Accepted' THEN 1 ELSE 0 END) as offers
            FROM opportunities
        """, fetch='one')
        totals = dict(totals_row) if totals_row else {}

        # Close reasons
        close_rows = execute_query("""
            SELECT COALESCE(close_reason, 'Unknown') as reason, COUNT(*) as count
            FROM opportunities WHERE stage = 'Closed'
            GROUP BY close_reason ORDER BY count DESC
        """, fetch='all')
        close_reasons = [dict(r) for r in (close_rows or [])]

        # Source breakdown
        source_rows = execute_query("""
            SELECT source, COUNT(*) as total,
                   SUM(CASE WHEN stage NOT IN ('Closed','Prospect') THEN 1 ELSE 0 END) as progressed,
                   SUM(CASE WHEN stage = 'Closed' AND close_reason = 'Accepted' THEN 1 ELSE 0 END) as accepted
            FROM opportunities WHERE source IS NOT NULL
            GROUP BY source ORDER BY total DESC
        """, fetch='all')
        sources = [dict(r) for r in (source_rows or [])]

        # Tier breakdown
        tier_rows = execute_query("""
            SELECT tier, COUNT(*) as total,
                   SUM(CASE WHEN stage NOT IN ('Closed','Prospect') THEN 1 ELSE 0 END) as progressed,
                   ROUND(AVG(fit_score), 1) as avg_fit
            FROM opportunities WHERE tier IS NOT NULL
            GROUP BY tier ORDER BY tier
        """, fetch='all')
        tiers = [dict(r) for r in (tier_rows or [])]

        # Outreach / contact stats
        outreach_row = execute_query("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outreach_day0 IS NOT NULL THEN 1 ELSE 0 END) as contacted,
                SUM(CASE WHEN response_status IN ('Responded','Meeting Scheduled') THEN 1 ELSE 0 END) as responded,
                SUM(CASE WHEN response_status = 'Meeting Scheduled' THEN 1 ELSE 0 END) as meetings
            FROM contacts
        """, fetch='one')
        outreach = dict(outreach_row) if outreach_row else {}
        contacted = outreach.get('contacted') or 0
        outreach['response_rate'] = (
            round(outreach['responded'] / contacted * 100, 1) if contacted > 0 else 0
        )

        # Fit score distribution (scores 1-10)
        fit_rows = execute_query("""
            SELECT fit_score, COUNT(*) as count
            FROM opportunities WHERE fit_score IS NOT NULL
            GROUP BY fit_score ORDER BY fit_score
        """, fetch='all')
        fit_map = {r['fit_score']: r['count'] for r in (fit_rows or [])}
        fit_max = max(fit_map.values(), default=1)
        fit_scores = [
            {'score': i, 'count': fit_map.get(i, 0), 'pct': round(fit_map.get(i, 0) / fit_max * 100)}
            for i in range(1, 11)
        ]

        # Job family breakdown
        family_rows = execute_query("""
            SELECT job_family, COUNT(*) as total,
                   SUM(CASE WHEN stage != 'Closed' THEN 1 ELSE 0 END) as active,
                   ROUND(AVG(fit_score), 1) as avg_fit
            FROM opportunities WHERE job_family IS NOT NULL
            GROUP BY job_family ORDER BY total DESC
        """, fetch='all')
        families = [dict(r) for r in (family_rows or [])]

        # Recent activity — last 14 days, excluding noise
        activity_rows = execute_query("""
            SELECT activity_type, COUNT(*) as count
            FROM activity_log
            WHERE created_at >= date('now', '-14 days')
              AND activity_type NOT IN ('AI Action', 'Note Added')
            GROUP BY activity_type ORDER BY count DESC
        """, fetch='all')
        recent_activity = [dict(r) for r in (activity_rows or [])]

        return render_template(
            "metrics.html",
            stages=stages,
            totals=totals,
            close_reasons=close_reasons,
            sources=sources,
            tiers=tiers,
            outreach=outreach,
            fit_scores=fit_scores,
            families=families,
            recent_activity=recent_activity,
            job_families=JOB_FAMILIES,
        )
