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

ALLOWED_CRITICAL_CODES = [
    'ARTICLE_TITLE_CHANGED',
    'ARTICLE_TITLE_CASE_INVALID',
    'AUTHOR_NAME_CHANGED',
    'AFFILIATION_CHANGED',
    'AFFILIATION_SUPERSCRIPT_INVALID',
    'CORRESPONDING_AUTHOR_NAME_CHANGED',
    'CORRESPONDING_AUTHOR_NAME_MISSING',
    'CORRESPONDING_AUTHOR_ASTERISK_MISSING',
    'EMAIL_MISSING',
    'EMAIL_INVALID',
    'EMAIL_CHANGED',
    'REFERENCE_NUMBER_DUPLICATE',
    'REFERENCE_NUMBER_GAP',
    'REFERENCE_NUMBER_OUT_OF_ORDER',
    'REFERENCE_CITATION_ORDER_INVALID',
    'REFERENCE_ALPHABETICAL_ORDER_INVALID',
    'UNNUMBERED_REFERENCE_HAS_NUMBER',
    'SECTION_HEADING_HAS_NUMBER',
]

def get_rule_group(error_type_or_code):
    """Map error code or type to one of the 5 allowed rule groups."""
    c = str(error_type_or_code or '').upper()
    if 'TITLE' in c:
        return 'article_title'
    elif 'AUTHOR' in c and 'CORRESPONDING' not in c:
        return 'author_name'
    elif 'AFFILIATION' in c:
        return 'affiliation'
    elif 'CORRESPONDING' in c or 'EMAIL' in c:
        return 'corresponding_author_name'
    elif 'REF' in c or 'CITATION' in c or 'SECTION_HEADING' in c:
        return 'references'
    return 'article_title'

app.jinja_env.filters['rule_group'] = get_rule_group

@app.context_processor
def inject_rule_helpers():
    return {'get_rule_group': get_rule_group}

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
                    # Preserve Word superscripts as Unicode superscript characters
                    sup_map = {
                        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
                        '+': '⁺', '-': '⁻', '*': '*', '†': '†', '‡': '‡',
                        '§': '§', '¶': '¶', ',': ','
                    }
                    try:
                        import docx
                        doc = docx.Document(file_obj)
                        lines = []
                        for p in doc.paragraphs:
                            p_parts = []
                            for r in p.runs:
                                t = r.text
                                if not t:
                                    continue
                                is_sup = False
                                if r.font.superscript:
                                    is_sup = True
                                elif r._r.rPr is not None:
                                    vert = r._r.rPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}vertAlign')
                                    if vert is not None and vert.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') == 'superscript':
                                        is_sup = True
                                if is_sup:
                                    t = ''.join(sup_map.get(c, c) for c in t)
                                p_parts.append(t)
                            lines.append(''.join(p_parts))
                        extracted = '\n'.join(lines)
                        if extracted.strip():
                            return extracted
                    except Exception:
                        pass
                    # Fallback to mammoth
                    file_obj.seek(0)
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

        # ── Strict Validation Output Policy: Run ONLY the 5 Critical Rule Groups ──
        critical_errors, critical_rule_ids, critical_run_log = run_critical_rules(
            original_text=author_text,
            edited_text=edited_text,
            job_sheet_instructions=job_sheet_instructions,
        )
        errors = critical_errors
        applied_rule_ids = critical_rule_ids

        # Exactly 5 mandatory checks
        manuscript.applied_rules_count = 5
        manuscript.total_available_rules = 5
        
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

        # Store error records — ONLY Critical severity errors from the 5 mandatory groups
        for e in errors:
            db.session.add(ErrorRecord(
                manuscript_id=manuscript.id,
                rule_id=e.get('rule_id') if isinstance(e.get('rule_id'), int) else None,
                error_type=e['error_type'],
                error_code=e.get('error_type'),
                severity='CRITICAL',
                category='CRITICAL',
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
    
    # 1. Purge any non-allowed / legacy / non-critical errors (spelling, style, content, info, review, warning)
    ErrorRecord.query.filter(
        ErrorRecord.manuscript_id == manuscript.id,
        (~ErrorRecord.error_type.in_(ALLOWED_CRITICAL_CODES)) | (ErrorRecord.severity != 'CRITICAL')
    ).delete(synchronize_session=False)
    db.session.commit()

    # 2. Get Job Sheet instructions if any
    job_sheet_instructions = []
    if project.job_sheet_id:
        job_sheet_instructions = JobSheetInstruction.query.filter_by(job_sheet_id=project.job_sheet_id).all()

    # 3. Existing records for this manuscript to preserve resolved/approved status
    existing_records = ErrorRecord.query.filter_by(manuscript_id=manuscript.id).all()
    preserved_status = {}
    for r in existing_records:
        key = (r.error_type, r.description)
        if r.status != 'OPEN' and key not in preserved_status:
            preserved_status[key] = r.status

    # 4. Run ONLY the 5 mandatory Critical rule groups
    critical_errors, critical_rule_ids, critical_run_log = run_critical_rules(
        original_text=manuscript.original_text,
        edited_text=manuscript.working_text,
        job_sheet_instructions=job_sheet_instructions,
    )

    # Log critical rule runs to audit
    import json
    for run_entry in critical_run_log:
        db.session.add(AuditLog(
            user_id=current_user.id,
            action='CRITICAL_RULE_RUN',
            object_type='CriticalRule',
            object_id=manuscript.id,
            new_value=json.dumps(run_entry),
        ))

    manuscript.applied_rules_count = 5
    manuscript.total_available_rules = 5

    def calc_line_col(text, offset):
        if offset is None: return 1, 1
        before = text[:offset]
        line = before.count('\n') + 1
        col = offset - before.rfind('\n') if '\n' in before else offset + 1
        return line, col

    # Delete all previous error records for this manuscript so no stale ghost errors remain
    for r in existing_records:
        db.session.delete(r)
    db.session.flush()

    # Re-insert strictly the active critical errors with preserved status and accurate line/col
    for err_dict in critical_errors:
        ln, col = calc_line_col(manuscript.working_text, err_dict.get('start_offset'))
        key = (err_dict['error_type'], err_dict.get('description'))
        st = preserved_status.get(key, 'OPEN')
        new_err = ErrorRecord(
            manuscript_id=manuscript.id,
            error_type=err_dict['error_type'],
            error_code=err_dict.get('error_type'),
            severity='CRITICAL',
            category='CRITICAL',
            start_offset=err_dict.get('start_offset', 0),
            end_offset=err_dict.get('end_offset', 0),
            line_number=err_dict.get('line_number') or ln,
            column_number=err_dict.get('column_number') if isinstance(err_dict.get('column_number'), int) else col,
            original_text=err_dict.get('original_text'),
            edited_text=err_dict.get('edited_text'),
            description=err_dict.get('description'),
            suggested_action=err_dict.get('suggested_action'),
            field=err_dict.get('field'),
            status=st,
            rule_id=None,
        )
        db.session.add(new_err)

    db.session.commit()

    # 5 Critical Rule Groups stats
    group_meta = [
        ('article_title', 'Article Title', 'Must match approved original title'),
        ('author_name', 'Author Name(s)', 'Must match approved original authors'),
        ('affiliation', 'Affiliation(s)', 'Must match approved original and have valid superscript mapping'),
        ('corresponding_author_name', 'Corresponding Author Name', 'Required, must match approved original correspondence and email'),
        ('references', 'References', 'Sequential numbering, citation order, or alphabetical order'),
    ]
    assigned_rules_stats = []
    for g_id, g_name, g_exp in group_meta:
        g_issues = len([e for e in critical_errors if get_rule_group(e.get('error_type')) == g_id])
        assigned_rules_stats.append({
            'name': g_name,
            'section': 'Mandatory Rule',
            'scope': 'Document',
            'expected': g_exp,
            'status': 'Applied' if g_issues > 0 else 'Checked',
            'issues': g_issues,
            'severity': 'Critical',
        })

    # Fetch all errors for rendering (strictly allowed critical codes)
    all_errors = ErrorRecord.query.filter_by(manuscript_id=manuscript.id).filter(
        ErrorRecord.error_type.in_(ALLOWED_CRITICAL_CODES),
        ErrorRecord.severity == 'CRITICAL'
    ).order_by(ErrorRecord.start_offset).all()

    open_errors = [e for e in all_errors if e.status == 'OPEN']
    total_errors = len(all_errors)
    resolved_errors = total_errors - len(open_errors)
    progress = int((resolved_errors / total_errors) * 100) if total_errors > 0 else 100

    # Summary stats by 5 groups
    rule_group_counts = {
        'article_title': len([e for e in all_errors if get_rule_group(e.error_type) == 'article_title' and e.status == 'OPEN']),
        'author_name': len([e for e in all_errors if get_rule_group(e.error_type) == 'author_name' and e.status == 'OPEN']),
        'affiliation': len([e for e in all_errors if get_rule_group(e.error_type) == 'affiliation' and e.status == 'OPEN']),
        'corresponding_author_name': len([e for e in all_errors if get_rule_group(e.error_type) == 'corresponding_author_name' and e.status == 'OPEN']),
        'references': len([e for e in all_errors if get_rule_group(e.error_type) == 'references' and e.status == 'OPEN']),
    }
    critical_count = sum(rule_group_counts.values())
    review_count = 0
    warning_count = 0

    # Critical error blocking
    open_critical = [e for e in all_errors if e.severity == 'CRITICAL' and e.status == 'OPEN']
    approved_critical_ids = set()
    if open_critical:
        approved_critical_ids = {a.error_id for a in CriticalRuleApproval.query.filter(
            CriticalRuleApproval.manuscript_id == manuscript.id
        ).all()}
    unapproved_critical = [e for e in open_critical if e.id not in approved_critical_ids]
    critical_blocked = len(unapproved_critical) > 0

    qc_status = 'PASS' if critical_count == 0 else 'CRITICAL ERROR FOUND'

    return render_template('review.html', project=project, manuscript=manuscript,
                           errors=all_errors, open_errors=open_errors,
                           progress=progress, total_errors=total_errors,
                           resolved_errors=resolved_errors,
                           critical_count=critical_count, review_count=0,
                           warning_count=0, qc_status=qc_status,
                           assigned_rules_stats=assigned_rules_stats,
                           critical_blocked=critical_blocked,
                           unapproved_critical_count=len(unapproved_critical),
                           rule_group_counts=rule_group_counts,
                           get_rule_group=get_rule_group)


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
    all_errors = ErrorRecord.query.filter_by(manuscript_id=manuscript.id).filter(
        ErrorRecord.error_type.in_(ALLOWED_CRITICAL_CODES),
        ErrorRecord.severity == 'CRITICAL'
    ).order_by(
        ErrorRecord.start_offset
    ).all()

    errors_json = []
    for e in all_errors:
        errors_json.append({
            'id': e.id,
            'error_type': e.error_type,
            'severity': 'CRITICAL',
            'line_number': e.line_number,
            'column_number': e.column_number,
            'start_offset': e.start_offset,
            'end_offset': e.end_offset,
            'original_text': e.original_text,
            'edited_text': e.edited_text,
            'context_before': e.context_before,
            'context_after': e.context_after,
            'description': e.description,
            'suggested_action': e.suggested_action,
            'group': get_rule_group(e.error_type),
            'status': e.status,
        })

    open_count = len([e for e in all_errors if e.status == 'OPEN'])
    critical_count = open_count

    return jsonify({
        'manuscript_text': manuscript.working_text,
        'original_text': manuscript.original_text,
        'errors': errors_json,
        'summary': {
            'total': len(all_errors),
            'open': open_count,
            'critical': critical_count,
            'review': 0,
            'warning': 0,
            'resolved': len(all_errors) - open_count,
            'qc_status': 'PASS' if critical_count == 0 else 'CRITICAL ERROR FOUND',
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
