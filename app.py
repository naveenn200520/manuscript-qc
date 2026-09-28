import os
import re
import json
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, abort, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from models import (db, User, Team, UserTeam, StyleRule, RuleSection, RuleVersion,
                    UserPermission, ReferenceStyle, ReferenceRule,
                    Project, Manuscript, ErrorRecord, JobSheet, JobSheetInstruction,
                    AuditLog, CriticalRuleApproval)
from qc_engine import run_content_qc, restore_original
from job_sheet_parser import parse_job_sheet
from critical_rules import run_critical_rules, CRITICAL_RULES_REGISTRY
import tempfile
import mammoth

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-manuscript-key-123'
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # Support up to 500 MB file uploads
basedir = os.path.abspath(os.path.dirname(__file__))
import shutil
if os.environ.get('VERCEL') == '1':
    tmp_db_path = '/tmp/qc_platform.db'
    orig_db_path = os.path.join(basedir, 'qc_platform.db')
    if not os.path.exists(tmp_db_path) and os.path.exists(orig_db_path):
        shutil.copy2(orig_db_path, tmp_db_path)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + tmp_db_path
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'qc_platform.db')

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'timeout': 20}  # Helps prevent 'database is locked' errors under concurrent load
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Register custom Jinja2 filters
import json as _json
app.jinja_env.filters['fromjson'] = _json.loads

db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ─────────────────────────────────────────────
# PERMISSION HELPERS
# ─────────────────────────────────────────────

def is_admin():
    """Return True if current user is admin."""
    return current_user.is_authenticated and current_user.role == 'admin'

def get_user_team_ids(user=None):
    """Return list of team IDs the user is a member of."""
    u = user or current_user
    return [t.id for t in u.teams]

def has_permission(resource, level, team_id=None, user=None):
    """
    Check if user has the required permission level for a resource.
    Admin always returns True.
    level: 'VIEW' | 'EDIT'
    resource: 'style_rules' | 'reference_styles' | 'job_sheets' | 'manuscripts' | 'qc_results'
    """
    u = user or current_user
    if u.role == 'admin':
        return True

    # Check explicit permission record
    query = UserPermission.query.filter_by(user_id=u.id, resource=resource)
    if team_id:
        # Check team-specific OR global (team_id=None) permission
        perm = query.filter(
            (UserPermission.team_id == team_id) | (UserPermission.team_id == None)
        ).first()
    else:
        perm = query.first()

    if not perm or perm.level == 'NO_ACCESS':
        return False
    if level == 'VIEW':
        return perm.level in ('VIEW', 'EDIT')
    if level == 'EDIT':
        return perm.level == 'EDIT'
    return False

def require_permission(resource, level, team_id=None):
    """Call this in route handlers; aborts with 403 if permission missing."""
    if not has_permission(resource, level, team_id):
        abort(403)

def log_audit(action, object_type=None, object_id=None, old_value=None, new_value=None):
    """Write an audit log entry for the current user."""
    entry = AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action,
        object_type=object_type,
        object_id=object_id,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
    )
    db.session.add(entry)
    # NOTE: caller must commit

def snapshot_rule(rule):
    """Return JSON snapshot of a rule's current state."""
    return json.dumps({
        'id': rule.id, 'name': rule.name, 'team_id': rule.team_id,
        'section': rule.section, 'category': rule.category, 'rule_type': rule.rule_type,
        'scope': rule.scope, 'find_pattern': rule.find_pattern, 'replacement': rule.replacement,
        'expected_result': rule.expected_result, 'exceptions': rule.exceptions,
        'severity': rule.severity, 'priority': rule.priority, 'status': rule.status,
        'version': rule.version,
    })

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Default sections seeded on startup
DEFAULT_SECTIONS = [
    'Capitalization', 'Punctuation', 'Spelling', 'Grammar', 'Abbreviations',
    'Numbers', 'Units', 'Dates', 'Titles', 'Headings', 'Authors', 'Affiliations',
    'Figures', 'Tables', 'Figure Captions', 'Table Captions', 'References',
    'Citations', 'Formatting', 'Scientific Terms', 'Protected Terms', 'Team Specific Rules'
]

def init_db():
    with app.app_context():
        db.create_all()
        # Seed sections
        for s in DEFAULT_SECTIONS:
            if not RuleSection.query.filter_by(name=s).first():
                db.session.add(RuleSection(name=s))
        db.session.commit()

        if not User.query.filter_by(email='admin@qcplatform.com').first():
            admin = User(name='Admin User', email='admin@qcplatform.com',
                         password_hash=generate_password_hash('password123'), role='admin')
            db.session.add(admin)
            team = Team(name='Default Journal', description='Primary journal editing team')
            db.session.add(team)
            db.session.commit()
            db.session.add(UserTeam(user_id=admin.id, team_id=team.id))
            db.session.add(StyleRule(team_id=team.id, name='Figure Abbreviation',
                                     category='Style', rule_type='Text Replacement',
                                     section='Abbreviations', scope='Figure',
                                     find_pattern='Figure', replacement='Fig.',
                                     severity='Style', priority='Medium', status='Active'))
            db.session.add(StyleRule(team_id=team.id, name='Table Abbreviation',
                                     category='Style', rule_type='Text Replacement',
                                     section='Abbreviations', scope='Table',
                                     find_pattern='Table', replacement='Tab.',
                                     severity='Style', priority='Medium', status='Active'))
            db.session.commit()

init_db()

# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        login_id = request.form.get('login_id')
        user = User.query.filter((User.email == login_id) | (User.employee_code == login_id)).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ─────────────────────────────────────────────
# DASHBOARD / PROJECTS
# ─────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    if is_admin():
        projects = Project.query.all()
    else:
        team_ids = get_user_team_ids()
        projects = Project.query.filter(Project.team_id.in_(team_ids)).all()

    total_projects = len(projects)
    pending_qc = len([p for p in projects if p.status != 'Finalized'])
    completed = len([p for p in projects if p.status == 'Finalized'])
    critical_errors = ErrorRecord.query.join(Manuscript).join(Project).filter(
        Project.team_id.in_([p.team_id for p in projects]),
        ErrorRecord.status == 'OPEN',
        ErrorRecord.severity == 'CRITICAL'
    ).count()
    return render_template('dashboard.html', projects=projects, total=total_projects,
                           pending=pending_qc, critical=critical_errors, completed=completed)

@app.route('/projects', methods=['GET'])
@login_required
def projects():
    if is_admin():
        all_projects = Project.query.all()
    else:
        team_ids = get_user_team_ids()
        all_projects = Project.query.filter(Project.team_id.in_(team_ids)).all()
    return render_template('projects.html', projects=all_projects)

@app.route('/projects/new', methods=['GET', 'POST'])
@login_required
def new_project():
    teams = current_user.teams if not is_admin() else Team.query.all()
    if request.method == 'POST':
        name = request.form.get('name')
        team_id = int(request.form.get('team_id'))

        # Enforce team membership for non-admins
        if not is_admin() and team_id not in get_user_team_ids():
            abort(403)

        def extract_text(file_obj):
            if not file_obj or file_obj.filename == '':
                return None
            fname = file_obj.filename.lower()
            try:
                if fname.endswith('.docx') or fname.endswith('.doc'):
                    return mammoth.extract_raw_text(file_obj).value
                elif fname.endswith('.xml') or fname.endswith('.txt'):
                    return file_obj.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
            return None

        file = request.files.get('manuscript_file')
        if not file or file.filename == '':
            flash('No author file selected', 'error')
            return redirect(request.url)

        author_text = extract_text(file)
        if author_text is None:
            flash('Unsupported file type or error reading author file.', 'error')
            return redirect(request.url)

        my_file = request.files.get('xml_file')
        my_text = extract_text(my_file)

        project = Project(name=name, team_id=team_id)
        db.session.add(project)
        db.session.commit()

        # Handle Job Sheet upload
        job_sheet_instructions = []
        job_sheet_file = request.files.get('job_sheet_file')
        if job_sheet_file and job_sheet_file.filename != '':
            js_bytes = job_sheet_file.read()
            with open('debug_jobsheet.pdf', 'wb') as f:
                f.write(js_bytes)
            job_sheet = JobSheet(project_id=project.id, filename=job_sheet_file.filename,
                                 uploaded_by=current_user.id)
            db.session.add(job_sheet)
            db.session.commit()
            project.job_sheet_id = job_sheet.id
            db.session.commit()

            instructions_data = parse_job_sheet(job_sheet_file.filename, js_bytes)
            job_sheet.parsed_status = 'Needs Review' if any(
                i['status'] == 'Manual Review Required' for i in instructions_data) else 'Parsed'

            for instr in instructions_data:
                jsi = JobSheetInstruction(
                    job_sheet_id=job_sheet.id, section=instr['section'],
                    instruction=instr['instruction'], source=instr['source'], status=instr['status']
                )
                db.session.add(jsi)

            # Auto-associate reference style from job sheet if BibliographyStyle found
            bib_instr = next((i for i in instructions_data if 'bibliography' in i['section'].lower()), None)
            if bib_instr:
                ref_style = ReferenceStyle.query.filter(
                    ReferenceStyle.name.ilike(bib_instr['instruction'])
                ).first()
                if ref_style:
                    project.reference_style_id = ref_style.id

            db.session.commit()
            # Reload instructions from DB for QC engine
            job_sheet_instructions = JobSheetInstruction.query.filter_by(
                job_sheet_id=job_sheet.id
            ).all()

        # Create manuscript — edited_text is working_text, original_text is author_text
        # The working_text stores the EDITED manuscript EXACTLY as uploaded — never auto-corrected
        edited_text = my_text if my_text else author_text
        manuscript = Manuscript(project_id=project.id, original_text=author_text,
                                working_text=edited_text)
        db.session.add(manuscript)
        db.session.commit()

        # Gather reference rules if a reference style is configured
        reference_rules = []
        if project.reference_style_id:
            ref_style = db.session.get(ReferenceStyle, project.reference_style_id)
            if ref_style:
                reference_rules = [r for r in ref_style.rules if r.is_active]

        # Gather team style rules
        team_rules = StyleRule.query.filter_by(team_id=team_id, is_active=True, status='Active').all()

        # Run the content-first QC engine
        errors, applied_rule_ids, total_available_rules = run_content_qc(
            original_text=author_text,
            edited_text=edited_text,
            job_sheet_instructions=job_sheet_instructions,
            reference_rules=reference_rules,
            style_rules=team_rules,
        )

        # ── Run Critical Rules (job-sheet-driven) ──
        critical_errors, critical_rule_ids, critical_run_log = run_critical_rules(
            original_text=author_text,
            edited_text=edited_text,
            job_sheet_instructions=job_sheet_instructions,
        )
        errors.extend(critical_errors)
        applied_rule_ids.extend(critical_rule_ids)

        # Record rule application counts
        manuscript.applied_rules_count = len(applied_rule_ids)
        manuscript.total_available_rules = total_available_rules + len(critical_rule_ids)
        
        # Log to audit log
        import json
        db.session.add(AuditLog(
            user_id=current_user.id,
            action='QC_RUN',
            object_type='Manuscript',
            object_id=manuscript.id,
            new_value=json.dumps({"applied_rule_ids": applied_rule_ids}),
        ))

        # Log critical rule runs to audit
        for run_entry in critical_run_log:
            db.session.add(AuditLog(
                user_id=current_user.id,
                action='CRITICAL_RULE_RUN',
                object_type='CriticalRule',
                object_id=manuscript.id,
                new_value=json.dumps(run_entry),
            ))

        # Store error records
        for e in errors:
            db.session.add(ErrorRecord(
                manuscript_id=manuscript.id,
                rule_id=e.get('rule_id') if isinstance(e.get('rule_id'), int) else None,
                error_type=e['error_type'],
                error_code=e.get('error_type'),
                severity=e['severity'],
                category=e.get('category', e['severity']),
                line_number=e.get('line_number'),
                column_number=e.get('column_number'),
                start_offset=e['start_offset'],
                end_offset=e['end_offset'],
                original_text=e.get('original_text'),
                edited_text=e.get('edited_text'),
                context_before=e.get('context_before'),
                context_after=e.get('context_after'),
                description=e.get('description'),
                suggested_action=e.get('suggested_action'),
                field=e.get('field'),
                status='OPEN',
            ))

        manuscript.status = 'QC Complete'
        db.session.commit()
        flash('Project created and QC completed.', 'success')
        return redirect(url_for('review_project', project_id=project.id))
    return render_template('new_project.html', teams=teams)

@app.route('/projects/<int:project_id>/review')
@login_required
def review_project(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    # Team isolation check
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    manuscript = project.manuscripts[0]
    
    # 1. Clear old Job Sheet style errors
    ErrorRecord.query.filter(
        ErrorRecord.manuscript_id == manuscript.id,
        ErrorRecord.error_type.in_(['JOB_SHEET_STYLE', 'JOB_SHEET_STYLE_ERROR', 'SECTION_HEADING_SENTENCE_CASE'])
    ).delete(synchronize_session=False)
    db.session.commit()

    # 2. Get Job Sheet instructions and Style Rules
    job_sheet_instructions = []
    if project.job_sheet_id:
        job_sheet_instructions = JobSheetInstruction.query.filter_by(job_sheet_id=project.job_sheet_id).all()
        
    team_rules = StyleRule.query.filter_by(team_id=project.team_id, is_active=True, status='Active').all()

    # 3. Re-run style rules only
    errors, applied_rule_ids, total_available_rules = run_content_qc(
        original_text=manuscript.original_text,
        edited_text=manuscript.working_text,
        job_sheet_instructions=job_sheet_instructions,
        style_rules=team_rules,
        run_spelling=False,
        run_content=False,
        run_references=False,
        run_job_sheet=True
    )
    
    # ── Also clear and re-run Critical Rules ──
    critical_error_codes = [
        'AUTHOR_NAME_CHANGED', 'AFFILIATION_CHANGED',
        'EMAIL_MISSING', 'EMAIL_INVALID', 'EMAIL_CHANGED',
        'ARTICLE_TITLE_CHANGED',
        'REFERENCE_NUMBER_DUPLICATE', 'REFERENCE_NUMBER_GAP', 'REFERENCE_NUMBER_OUT_OF_ORDER',
        'REFERENCE_CITATION_ORDER_INVALID',
        'REFERENCE_ALPHABETICAL_ORDER_INVALID',
        'UNNUMBERED_REFERENCE_HAS_NUMBER', 'SECTION_HEADING_HAS_NUMBER',
    ]
    
    existing_critical = ErrorRecord.query.filter(
        ErrorRecord.manuscript_id == manuscript.id,
        ErrorRecord.error_code.in_(critical_error_codes)
    ).all()
    
    existing_map = {}
    for err in existing_critical:
        sig = f"{err.error_code}:{err.original_text}:{err.edited_text}"
        existing_map[sig] = err

    critical_errors, critical_rule_ids, critical_run_log = run_critical_rules(
        original_text=manuscript.original_text,
        edited_text=manuscript.working_text,
        job_sheet_instructions=job_sheet_instructions,
    )
    
    # We only extend applied_rule_ids
    applied_rule_ids.extend(critical_rule_ids)

    # Log critical rule runs to audit
    for run_entry in critical_run_log:
        db.session.add(AuditLog(
            user_id=current_user.id,
            action='CRITICAL_RULE_RUN',
            object_type='CriticalRule',
            object_id=manuscript.id,
            new_value=json.dumps(run_entry),
        ))

    # Update manuscript rule counts
    manuscript.applied_rules_count = len(applied_rule_ids)
    manuscript.total_available_rules = total_available_rules + len(critical_rule_ids)
    
    # Helper to calculate line and column from offset
    def calc_line_col(text, offset):
        if offset is None: return 1, 1
        before = text[:offset]
        line = before.count('\n') + 1
        col = offset - before.rfind('\n') if '\n' in before else offset + 1
        return line, col

    # Insert style/job-sheet errors (which were cleared earlier in the flow if applicable)
    for err_dict in errors:
        ln, col = calc_line_col(manuscript.working_text, err_dict.get('start_offset'))
        new_err = ErrorRecord(
            manuscript_id=manuscript.id,
            error_type=err_dict['error_type'],
            error_code=err_dict.get('error_type'),
            severity=err_dict['severity'],
            start_offset=err_dict['start_offset'],
            end_offset=err_dict['end_offset'],
            line_number=ln,
            column_number=col,
            original_text=err_dict['original_text'],
            edited_text=err_dict['edited_text'],
            description=err_dict['description'],
            suggested_action=err_dict.get('suggested_action'),
            field=err_dict.get('field'),
            status='OPEN',
            rule_id=err_dict.get('rule_id') if isinstance(err_dict.get('rule_id'), int) else None,
        )
        db.session.add(new_err)
        
    # Handle critical errors separately to preserve state
    for err_dict in critical_errors:
        sig = f"{err_dict.get('error_type')}:{err_dict['original_text']}:{err_dict['edited_text']}"
        if sig in existing_map:
            # Error already exists, keep it so status/approval is preserved
            del existing_map[sig]
        else:
            ln, col = calc_line_col(manuscript.working_text, err_dict.get('start_offset'))
            # New error
            new_err = ErrorRecord(
                manuscript_id=manuscript.id,
                error_type=err_dict['error_type'],
                error_code=err_dict.get('error_type'),
                severity=err_dict['severity'],
                start_offset=err_dict['start_offset'],
                end_offset=err_dict['end_offset'],
                line_number=ln,
                column_number=col,
                original_text=err_dict['original_text'],
                edited_text=err_dict['edited_text'],
                description=err_dict['description'],
                suggested_action=err_dict.get('suggested_action'),
                field=err_dict.get('field'),
                status='OPEN',
                rule_id=err_dict.get('rule_id') if isinstance(err_dict.get('rule_id'), int) else None,
            )
            db.session.add(new_err)
            
    # Delete stale critical errors that are no longer detected
    for err in existing_map.values():
        db.session.delete(err)
        
    db.session.commit()

    def matches_job_sheet(rule, instr):
        r_name = rule.name.lower().strip()
        r_name_no = r_name.replace(' ', '')
        r_sec = (rule.section or '').lower().strip()
        i_sec = instr.section.lower().strip()
        i_val = instr.instruction.lower().strip()
        i_val_no = i_val.replace(' ', '')
        
        if i_sec in (r_name, r_sec): return True
        if i_val_no and i_val_no in (r_name_no, r_sec.replace(' ', '')): return True
        if r_name and (r_name in i_sec or r_name_no in i_val_no): return True
        return False

    assigned_rules_stats = []
    js_instrs_active = [i for i in job_sheet_instructions if i.status != 'Manual Review Required']
    
    # 1. Add all parsed job sheet instructions to the stats modal so the user sees them
    for instr in js_instrs_active:
        # Check if there are any JOB_SHEET_STYLE errors mentioning this section
        issues_found = ErrorRecord.query.filter_by(manuscript_id=manuscript.id, error_type='JOB_SHEET_STYLE').filter(ErrorRecord.description.like(f"%{instr.section}%")).count()
        
        # Also check if it matched any dynamic rules and sum those issues
        matched_rules = [r for r in team_rules if r.is_active and matches_job_sheet(r, instr)]
        for r in matched_rules:
            issues_found += ErrorRecord.query.filter_by(manuscript_id=manuscript.id, rule_id=r.id).count()

        assigned_rules_stats.append({
            'name': instr.section,
            'section': 'Job Sheet',
            'scope': 'Document',
            'expected': instr.instruction,
            'status': 'Applied' if issues_found > 0 else 'Checked',
            'issues': issues_found
        })

    # Add Critical Rules to stats
    for rule_key, rule_def in CRITICAL_RULES_REGISTRY.items():
        if rule_def['id'] in critical_rule_ids:
            crit_issues = len([e for e in critical_errors if e.get('error_type') in (
                rule_def['id'],
                # Handle email rule which has multiple codes
                'EMAIL_MISSING', 'EMAIL_INVALID', 'EMAIL_CHANGED',
            ) and rule_key in ('email',) or e.get('error_type') == rule_def['id']])
            assigned_rules_stats.append({
                'name': rule_def['name'],
                'section': 'Critical Rule',
                'scope': 'Document',
                'expected': rule_def['description'],
                'status': 'Applied' if crit_issues > 0 else 'Checked',
                'issues': crit_issues,
                'severity': 'Critical',
            })

    # 4. Fetch all errors for rendering
    all_errors = ErrorRecord.query.filter_by(manuscript_id=manuscript.id).order_by(
        ErrorRecord.start_offset
    ).all()
    
    open_errors = [e for e in all_errors if e.status == 'OPEN']
    total_errors = len(all_errors)
    resolved_errors = total_errors - len(open_errors)
    progress = int((resolved_errors / total_errors) * 100) if total_errors > 0 else 100

    # Summary stats
    critical_count = len([e for e in all_errors if e.severity == 'CRITICAL' and e.status == 'OPEN'])
    review_count = len([e for e in all_errors if e.severity == 'REVIEW' and e.status == 'OPEN'])
    style_count = len([e for e in all_errors if e.severity == 'Style' and e.status == 'OPEN'])
    review_count += style_count # Include Style errors in the review count
    warning_count = len([e for e in all_errors if e.severity == 'WARNING' and e.status == 'OPEN'])

    # ── Critical error blocking ──
    # Unresolved critical errors that have NOT been explicitly approved block export/finalize/mark-done
    open_critical = [e for e in all_errors if e.severity == 'CRITICAL' and e.status == 'OPEN']
    approved_critical_ids = set()
    if open_critical:
        approved_critical_ids = {a.error_id for a in CriticalRuleApproval.query.filter(
            CriticalRuleApproval.manuscript_id == manuscript.id
        ).all()}
    unapproved_critical = [e for e in open_critical if e.id not in approved_critical_ids]
    critical_blocked = len(unapproved_critical) > 0

    # QC status
    qc_status = 'PASS' if critical_count == 0 and review_count == 0 else 'REVIEW REQUIRED'

    return render_template('review.html', project=project, manuscript=manuscript,
                           errors=all_errors, open_errors=open_errors,
                           progress=progress, total_errors=total_errors,
                           resolved_errors=resolved_errors,
                           critical_count=critical_count, review_count=review_count,
                           warning_count=warning_count, qc_status=qc_status,
                           assigned_rules_stats=assigned_rules_stats,
                           critical_blocked=critical_blocked,
                           unapproved_critical_count=len(unapproved_critical))


@app.route('/projects/<int:project_id>/qc-data')
@login_required
def qc_data(project_id):
    """JSON API for the interactive review page."""
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    manuscript = project.manuscripts[0]
    all_errors = ErrorRecord.query.filter_by(manuscript_id=manuscript.id).order_by(
        ErrorRecord.start_offset
    ).all()

    errors_json = []
    for e in all_errors:
        errors_json.append({
            'id': e.id,
            'error_type': e.error_type,
            'severity': e.severity,
            'line_number': e.line_number,
            'column_number': e.column_number,
            'start_offset': e.start_offset,
            'end_offset': e.end_offset,
            'original_text': e.original_text,
            'edited_text': e.edited_text,
            'context_before': e.context_before,
            'context_after': e.context_after,
            'description': e.description,
            'status': e.status,
        })

    open_count = len([e for e in all_errors if e.status == 'OPEN'])
    critical_count = len([e for e in all_errors if e.severity == 'CRITICAL' and e.status == 'OPEN'])
    review_count = len([e for e in all_errors if e.severity == 'REVIEW' and e.status == 'OPEN'])
    warning_count = len([e for e in all_errors if e.severity == 'WARNING' and e.status == 'OPEN'])

    return jsonify({
        'manuscript_text': manuscript.working_text,
        'original_text': manuscript.original_text,
        'errors': errors_json,
        'summary': {
            'total': len(all_errors),
            'open': open_count,
            'critical': critical_count,
            'review': review_count,
            'warning': warning_count,
            'resolved': len(all_errors) - open_count,
            'qc_status': 'PASS' if critical_count == 0 and review_count == 0 else 'REVIEW REQUIRED',
        }
    })

@app.route('/projects/<int:project_id>/job-sheet')
@login_required
def view_job_sheet(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    if not project.job_sheet_id:
        flash('No Job Sheet associated with this project.', 'error')
        return redirect(url_for('review_project', project_id=project_id))
    job_sheet = db.session.get(JobSheet, project.job_sheet_id)
    instructions = JobSheetInstruction.query.filter_by(job_sheet_id=job_sheet.id).all()
    return render_template('job_sheet_instructions.html', project=project,
                           job_sheet=job_sheet, instructions=instructions)

@app.route('/projects/<int:project_id>/job-sheet/reupload', methods=['POST'])
@login_required
def reupload_job_sheet(project_id):
    """Re-upload and re-parse the job sheet with a new file."""
    project = db.session.get(Project, project_id)
    if not project:
        abort(404)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)

    new_file = request.files.get('job_sheet_file')
    if not new_file or new_file.filename == '':
        flash('No file selected for re-upload.', 'error')
        return redirect(url_for('view_job_sheet', project_id=project_id))

    js_bytes = new_file.read()

    # Delete old instructions
    if project.job_sheet_id:
        old_js = db.session.get(JobSheet, project.job_sheet_id)
        if old_js:
            JobSheetInstruction.query.filter_by(job_sheet_id=old_js.id).delete()
            db.session.delete(old_js)
            db.session.commit()

    # Create new job sheet record
    job_sheet = JobSheet(project_id=project.id, filename=new_file.filename,
                         uploaded_by=current_user.id)
    db.session.add(job_sheet)
    db.session.commit()
    project.job_sheet_id = job_sheet.id
    db.session.commit()

    instructions_data = parse_job_sheet(new_file.filename, js_bytes)
    job_sheet.parsed_status = 'Needs Review' if any(
        i['status'] == 'Manual Review Required' for i in instructions_data) else 'Parsed'

    for instr in instructions_data:
        jsi = JobSheetInstruction(
            job_sheet_id=job_sheet.id, section=instr['section'],
            instruction=instr['instruction'], source=instr['source'], status=instr['status']
        )
        db.session.add(jsi)
    db.session.commit()

    detected = sum(1 for i in instructions_data if i['status'] == 'Detected')
    flash(f'Job Sheet re-uploaded successfully. {detected} rules detected.', 'success')
    return redirect(url_for('view_job_sheet', project_id=project_id))

@app.route('/errors/<int:error_id>/action', methods=['POST'])
@login_required
def resolve_error(error_id):
    error = db.session.get(ErrorRecord, error_id)
    if not error:
        abort(404)
    project = error.manuscript.project
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)

    action = request.form.get('action') or request.json.get('action') if request.is_json else request.form.get('action')
    manuscript = error.manuscript

    if action == 'RESTORE_ORIGINAL':
        # Replace ONLY the exact error range with original text
        new_text = restore_original(manuscript.working_text, error)
        if new_text != manuscript.working_text:
            manuscript.working_text = new_text
        error.status = 'RESTORED'
        error.resolved_by = current_user.id
        error.resolved_at = datetime.utcnow()
    elif action == 'KEEP_EDIT':
        # Keep the edited text as-is, mark resolved
        error.status = 'KEPT'
        error.resolved_by = current_user.id
        error.resolved_at = datetime.utcnow()
    elif action == 'IGNORE':
        # Ignore the error, no text change
        error.status = 'IGNORED'
        error.resolved_by = current_user.id
        error.resolved_at = datetime.utcnow()
    elif action == 'MARK_REVIEWED':
        # Mark as reviewed by human
        error.status = 'REVIEWED'
        error.resolved_by = current_user.id
        error.resolved_at = datetime.utcnow()
    elif action == 'APPROVE_CRITICAL':
        # Authorized reviewer explicitly approves a critical error
        # This does NOT resolve the error but allows export/finalize to proceed
        reason = ''
        if request.is_json:
            reason = request.json.get('reason', '')
        else:
            reason = request.form.get('reason', '')

        approval = CriticalRuleApproval(
            error_id=error.id,
            manuscript_id=manuscript.id,
            approved_by=current_user.id,
            reason=reason,
        )
        db.session.add(approval)
        error.status = 'APPROVED'
        error.resolved_by = current_user.id
        error.resolved_at = datetime.utcnow()

        # Audit log
        log_audit('CRITICAL_RULE_APPROVAL', 'ErrorRecord', error.id,
                  error.error_code or error.error_type,
                  json.dumps({'reason': reason, 'error_code': error.error_code or error.error_type}))
    else:
        # Unknown action — treat as ignore
        error.status = 'IGNORED'

    db.session.commit()

    # If AJAX request, return JSON
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'error_id': error.id,
            'new_status': error.status,
            'working_text': manuscript.working_text,
        })

    return redirect(url_for('review_project', project_id=project.id))

@app.route('/projects/<int:project_id>/finalize', methods=['POST'])
@login_required
def finalize_project(project_id):
    project = db.session.get(Project, project_id)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)

    # ── Block if unresolved critical errors without approval ──
    manuscript = project.manuscripts[0] if project.manuscripts else None
    if manuscript:
        open_critical = ErrorRecord.query.filter_by(
            manuscript_id=manuscript.id, severity='CRITICAL', status='OPEN'
        ).all()
        approved_ids = {a.error_id for a in CriticalRuleApproval.query.filter_by(
            manuscript_id=manuscript.id
        ).all()}
        unapproved = [e for e in open_critical if e.id not in approved_ids]
        if unapproved:
            flash(
                f'Cannot finalize: {len(unapproved)} unresolved critical error(s) must be '
                f'restored, corrected, or explicitly approved by an authorized reviewer.',
                'error'
            )
            return redirect(url_for('review_project', project_id=project.id))

    project.status = 'Finalized'
    log_audit('FINALIZE', 'Project', project.id, 'Pending QC', 'Finalized')
    db.session.commit()
    flash('Project finalized successfully.', 'success')
    return redirect(url_for('review_project', project_id=project.id))

@app.route('/projects/<int:project_id>/download', methods=['GET'])
@login_required
def download_project(project_id):
    project = db.session.get(Project, project_id)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    manuscript = project.manuscripts[0]

    # ── Block export if unresolved critical errors without approval ──
    open_critical = ErrorRecord.query.filter_by(
        manuscript_id=manuscript.id, severity='CRITICAL', status='OPEN'
    ).all()
    approved_ids = {a.error_id for a in CriticalRuleApproval.query.filter_by(
        manuscript_id=manuscript.id
    ).all()}
    unapproved = [e for e in open_critical if e.id not in approved_ids]
    if unapproved:
        flash(
            f'Cannot export: {len(unapproved)} unresolved critical error(s) must be '
            f'restored, corrected, or explicitly approved by an authorized reviewer.',
            'error'
        )
        return redirect(url_for('review_project', project_id=project.id))

    fmt = request.args.get('fmt', 'txt').lower()
    text = manuscript.working_text

    safe_name = re.sub(r'[^\w\-_.]', '_', project.name)

    if fmt == 'docx':
        try:
            from docx import Document as DocxDocument
            doc = DocxDocument()
            doc.add_heading(project.name, 0)
            for line in text.splitlines():
                doc.add_paragraph(line)
            fd, path = tempfile.mkstemp(suffix='.docx')
            os.close(fd)
            doc.save(path)
            return send_file(path, as_attachment=True,
                             download_name=f"{safe_name}_final.docx",
                             mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        except ImportError:
            flash('python-docx is not installed on the server. Please contact your administrator.', 'error')
            return redirect(url_for('review_project', project_id=project_id))

    elif fmt == 'xml':
        import xml.etree.ElementTree as ET
        root = ET.Element('manuscript')
        root.set('project', project.name)
        body = ET.SubElement(root, 'body')
        for i, line in enumerate(text.splitlines(), 1):
            p = ET.SubElement(body, 'p')
            p.set('n', str(i))
            p.text = line
        xml_bytes = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        fd, path = tempfile.mkstemp(suffix='.xml')
        with os.fdopen(fd, 'wb') as f:
            f.write(xml_bytes)
        return send_file(path, as_attachment=True,
                         download_name=f"{safe_name}_final.xml",
                         mimetype='application/xml')

    else:
        # Default: plain text
        fd, path = tempfile.mkstemp(suffix='.txt')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        return send_file(path, as_attachment=True,
                         download_name=f"{safe_name}_final.txt",
                         mimetype='text/plain')

@app.route('/projects/<int:project_id>/skip_all', methods=['POST'])
@login_required
def skip_all_errors(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        return redirect(url_for('projects'))
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    manuscript = project.manuscripts[0] if project.manuscripts else None
    if manuscript:
        for e in ErrorRecord.query.filter_by(manuscript_id=manuscript.id, status='OPEN').all():
            e.status = 'IGNORED'
            e.resolved_by = current_user.id
            e.resolved_at = datetime.utcnow()
        db.session.commit()
        flash('All remaining errors were skipped.', 'info')
    return redirect(url_for('review_project', project_id=project.id))

@app.route('/projects/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    project = db.session.get(Project, project_id)
    if not is_admin() and project.team_id not in get_user_team_ids():
        abort(403)
    if project:
        for ms in project.manuscripts:
            ErrorRecord.query.filter_by(manuscript_id=ms.id).delete()
            db.session.delete(ms)
        db.session.delete(project)
        db.session.commit()
    return redirect(url_for('projects'))

# ─────────────────────────────────────────────
# ADMIN: TEAMS
# ─────────────────────────────────────────────

@app.route('/admin/teams', methods=['GET', 'POST'])
@login_required
def admin_teams():
    if not is_admin():
        abort(403)
    if request.method == 'POST':
        name = request.form.get('name')
        desc = request.form.get('description')
        if name:
            new_team = Team(name=name, description=desc)
            db.session.add(new_team)
            db.session.commit()
            log_audit('CREATE', 'Team', new_team.id, None, name)
            db.session.commit()
            flash('Team created.', 'success')
        return redirect(url_for('admin_teams'))
    teams = Team.query.all()
    return render_template('admin_teams.html', teams=teams)

# ─────────────────────────────────────────────
# ADMIN: USERS
# ─────────────────────────────────────────────

@app.route('/admin/users', methods=['GET', 'POST'])
@login_required
def admin_users():
    if not is_admin():
        abort(403)
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        employee_code = request.form.get('employee_code')
        password = request.form.get('password')
        role = request.form.get('role')
        team_ids = request.form.getlist('team_ids')
        if name and email and employee_code and password:
            try:
                new_user = User(name=name, email=email, employee_code=employee_code,
                                password_hash=generate_password_hash(password), role=role)
                db.session.add(new_user)
                db.session.commit()
                for tid in team_ids:
                    db.session.add(UserTeam(user_id=new_user.id, team_id=tid))
                db.session.commit()
                log_audit('CREATE', 'User', new_user.id, None, name)
                db.session.commit()
                flash('User registered successfully.', 'success')
            except Exception:
                db.session.rollback()
                flash('Error creating user. Employee Code or Email might already exist.', 'error')
        return redirect(url_for('admin_users'))
    users = User.query.all()
    teams = Team.query.all()
    return render_template('admin_users.html', users=users, teams=teams)

# ─────────────────────────────────────────────
# STYLE RULES (admin full CRUD; others read-only)
# ─────────────────────────────────────────────

RULE_TYPES = [
    'Text Replacement', 'Capitalization', 'Punctuation', 'Spelling',
    'Pattern/Regex', 'Structure', 'Required Content', 'Forbidden Content',
    'Reference Rule', 'Formatting Rule', 'Job Sheet Rule'
]

@app.route('/admin/rules', methods=['GET', 'POST'])
@login_required
def admin_rules():
    # Admins: full access. Others: redirect to read-only view
    if not is_admin():
        return redirect(url_for('style_rules'))

    if request.method == 'POST':
        rule = StyleRule(
            team_id=request.form.get('team_id'),
            name=request.form.get('name'),
            section=request.form.get('section'),
            category=request.form.get('category'),
            rule_type=request.form.get('rule_type'),
            scope=request.form.get('scope'),
            find_pattern=request.form.get('find_pattern'),
            replacement=request.form.get('replacement'),
            expected_result=request.form.get('expected_result'),
            exceptions=request.form.get('exceptions'),
            severity=request.form.get('severity', 'Style'),
            priority=request.form.get('priority', 'Medium'),
            status=request.form.get('status', 'Active'),
            is_active=True,
            created_by=current_user.id,
            version=1,
        )
        db.session.add(rule)
        db.session.commit()
        log_audit('CREATE', 'StyleRule', rule.id, None, rule.name)
        db.session.commit()
        flash('Rule added successfully.', 'success')
        return redirect(url_for('admin_rules'))

    teams = Team.query.all()
    sections = RuleSection.query.order_by(RuleSection.name).all()
    rules = StyleRule.query.order_by(StyleRule.team_id, StyleRule.section, StyleRule.name).all()
    return render_template('admin_rules.html', teams=teams, rules=rules,
                           sections=sections, rule_types=RULE_TYPES, is_admin=True)

@app.route('/style-rules')
@login_required
def style_rules():
    """Read-only style rules page for Team Leader / Staff."""
    team_ids = get_user_team_ids()
    rules = StyleRule.query.filter(
        StyleRule.team_id.in_(team_ids),
        StyleRule.status == 'Active'
    ).order_by(StyleRule.team_id, StyleRule.section, StyleRule.name).all()
    teams = Team.query.filter(Team.id.in_(team_ids)).all()
    sections = RuleSection.query.order_by(RuleSection.name).all()
    return render_template('admin_rules.html', teams=teams, rules=rules,
                           sections=sections, rule_types=RULE_TYPES, is_admin=False)

@app.route('/admin/rules/<int:rule_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_rule(rule_id):
    if not is_admin():
        abort(403)
    rule = db.session.get(StyleRule, rule_id)
    if not rule:
        abort(404)

    if request.method == 'POST':
        # Save version snapshot BEFORE editing
        snapshot = snapshot_rule(rule)
        db.session.add(RuleVersion(rule_id=rule.id, version=rule.version,
                                   snapshot_json=snapshot, saved_by=current_user.id))
        old_name = rule.name
        rule.name = request.form.get('name', rule.name)
        rule.section = request.form.get('section', rule.section)
        rule.category = request.form.get('category', rule.category)
        rule.rule_type = request.form.get('rule_type', rule.rule_type)
        rule.scope = request.form.get('scope', rule.scope)
        rule.find_pattern = request.form.get('find_pattern', rule.find_pattern)
        rule.replacement = request.form.get('replacement', rule.replacement)
        rule.expected_result = request.form.get('expected_result', rule.expected_result)
        rule.exceptions = request.form.get('exceptions', rule.exceptions)
        rule.severity = request.form.get('severity', rule.severity)
        rule.priority = request.form.get('priority', rule.priority)
        rule.status = request.form.get('status', rule.status)
        rule.is_active = (rule.status == 'Active')
        rule.team_id = request.form.get('team_id', rule.team_id)
        rule.version += 1
        rule.updated_at = datetime.utcnow()
        db.session.commit()
        log_audit('EDIT', 'StyleRule', rule.id, snapshot, snapshot_rule(rule))
        db.session.commit()
        flash(f'Rule "{rule.name}" updated to v{rule.version}.', 'success')
        return redirect(url_for('admin_rules'))

    teams = Team.query.all()
    sections = RuleSection.query.order_by(RuleSection.name).all()
    return render_template('edit_rule.html', rule=rule, teams=teams,
                           sections=sections, rule_types=RULE_TYPES)

@app.route('/admin/rules/<int:rule_id>/deactivate', methods=['POST'])
@login_required
def deactivate_rule(rule_id):
    if not is_admin():
        abort(403)
    rule = db.session.get(StyleRule, rule_id)
    if not rule:
        abort(404)
    old_status = rule.status
    rule.status = 'Inactive'
    rule.is_active = False
    rule.deleted_by = current_user.id
    rule.deleted_at = datetime.utcnow()
    db.session.commit()
    log_audit('DEACTIVATE', 'StyleRule', rule.id, old_status, 'Inactive')
    db.session.commit()
    flash(f'Rule "{rule.name}" has been deactivated.', 'info')
    return redirect(url_for('admin_rules'))

@app.route('/admin/rules/<int:rule_id>/versions')
@login_required
def rule_versions(rule_id):
    if not is_admin():
        abort(403)
    rule = db.session.get(StyleRule, rule_id)
    if not rule:
        abort(404)
    versions = RuleVersion.query.filter_by(rule_id=rule_id).order_by(RuleVersion.version.desc()).all()
    return render_template('rule_versions.html', rule=rule, versions=versions)

# ─────────────────────────────────────────────
# REFERENCE STYLES
# ─────────────────────────────────────────────

@app.route('/admin/reference-styles', methods=['GET', 'POST'])
@login_required
def reference_styles():
    if not is_admin():
        # Non-admin: read-only view of reference styles
        styles = ReferenceStyle.query.filter_by(is_active=True).all()
        return render_template('reference_styles.html', styles=styles, is_admin=False)

    if request.method == 'POST':
        name = request.form.get('name')
        desc = request.form.get('description')
        if name:
            style = ReferenceStyle(name=name, description=desc, created_by=current_user.id)
            db.session.add(style)
            db.session.commit()
            log_audit('CREATE', 'ReferenceStyle', style.id, None, name)
            db.session.commit()
            flash('Reference style created.', 'success')
        return redirect(url_for('reference_styles'))

    styles = ReferenceStyle.query.all()
    return render_template('reference_styles.html', styles=styles, is_admin=True)

@app.route('/admin/reference-styles/<int:style_id>/rules/add', methods=['POST'])
@login_required
def add_reference_rule(style_id):
    if not is_admin():
        abort(403)
    style = db.session.get(ReferenceStyle, style_id)
    if not style:
        abort(404)
    category = request.form.get('category')
    rule_text = request.form.get('rule_text')
    if category and rule_text:
        rule = ReferenceRule(ref_style_id=style_id, category=category, rule_text=rule_text)
        db.session.add(rule)
        db.session.commit()
        log_audit('CREATE', 'ReferenceRule', rule.id, None, f"{style.name}: {category}")
        db.session.commit()
        flash('Rule added to reference style.', 'success')
    return redirect(url_for('reference_styles'))

@app.route('/admin/reference-styles/<int:style_id>/deactivate', methods=['POST'])
@login_required
def deactivate_reference_style(style_id):
    if not is_admin():
        abort(403)
    style = db.session.get(ReferenceStyle, style_id)
    if style:
        style.is_active = False
        db.session.commit()
        log_audit('DEACTIVATE', 'ReferenceStyle', style.id, 'Active', 'Inactive')
        db.session.commit()
        flash(f'Reference style "{style.name}" deactivated.', 'info')
    return redirect(url_for('reference_styles'))

# ─────────────────────────────────────────────
# ACCESS MANAGEMENT
# ─────────────────────────────────────────────

RESOURCES = ['style_rules', 'reference_styles', 'job_sheets', 'manuscripts', 'qc_results']
LEVELS = ['NO_ACCESS', 'VIEW', 'EDIT']

@app.route('/admin/access')
@login_required
def access_management():
    if not is_admin():
        abort(403)
    users = User.query.filter(User.role != 'admin').order_by(User.name).all()
    teams = Team.query.all()
    return render_template('access_management.html', users=users, teams=teams,
                           resources=RESOURCES)

@app.route('/admin/access/<int:user_id>', methods=['GET', 'POST'])
@login_required
def manage_user_access(user_id):
    if not is_admin():
        abort(403)
    target_user = db.session.get(User, user_id)
    if not target_user:
        abort(404)
    teams = Team.query.all()

    if request.method == 'POST':
        # Team membership
        selected_team_ids = [int(t) for t in request.form.getlist('team_ids')]
        # Remove old team memberships
        UserTeam.query.filter_by(user_id=user_id).delete()
        for tid in selected_team_ids:
            db.session.add(UserTeam(user_id=user_id, team_id=tid))
        db.session.commit()

        # Rebuild permissions
        UserPermission.query.filter_by(user_id=user_id).delete()
        db.session.commit()
        for resource in RESOURCES:
            level = request.form.get(f'perm_{resource}', 'NO_ACCESS')
            perm = UserPermission(user_id=user_id, resource=resource, level=level,
                                  granted_by=current_user.id)
            db.session.add(perm)

        db.session.commit()
        log_audit('PERMISSION_CHANGE', 'User', user_id, None,
                  f"Permissions updated for {target_user.name} by {current_user.name}")
        db.session.commit()
        flash(f'Permissions updated for {target_user.name}.', 'success')
        return redirect(url_for('access_management'))

    existing_perms = {p.resource: p.level for p in target_user.permissions}
    user_team_ids = get_user_team_ids(target_user)
    return render_template('manage_user_access.html', target_user=target_user, teams=teams,
                           user_team_ids=user_team_ids, resources=RESOURCES, levels=LEVELS,
                           existing_perms=existing_perms)

# ─────────────────────────────────────────────
# AUDIT LOGS
# ─────────────────────────────────────────────

@app.route('/admin/audit-logs')
@login_required
def audit_logs():
    if not is_admin():
        abort(403)
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(500).all()
    return render_template('audit_logs.html', logs=logs)

# ─────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
