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
                    AuditLog, CriticalRuleApproval, JournalInstruction,
                    ImageCriterion, FigureInspection)
from qc_engine import run_content_qc, restore_original
from job_sheet_parser import parse_job_sheet
from critical_rules import run_critical_rules, CRITICAL_RULES_REGISTRY
from image_qc import (extract_figures_from_docx, save_uploaded_figure_file,
                      inspect_figure, find_figure_callouts, seed_default_image_criteria,
                      process_manuscript_figures)
import tempfile
import mammoth

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-manuscript-key-123'
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # Support up to 1 GB file uploads
basedir = os.path.abspath(os.path.dirname(__file__))
import shutil
db_uri = os.environ.get('DATABASE_URL')
if db_uri:
    if db_uri.startswith('postgres://'):
        db_uri = db_uri.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
elif os.environ.get('VERCEL') == '1':
    tmp_db_path = '/tmp/qc_platform.db'
    orig_db_path = os.path.join(basedir, 'qc_platform.db')
    if not os.path.exists(tmp_db_path) and os.path.exists(orig_db_path):
        shutil.copy2(orig_db_path, tmp_db_path)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + tmp_db_path
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'timeout': 20}
    }
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'qc_platform.db')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'timeout': 20}
    }
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Instant live reloading for CSS/JS assets

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

def extract_journal_number(project_or_name, job_sheet=None):
    """
    Extract journalNumber from Project ID / Name format: JOURNAL_YEAR_ARTICLE
    Example: '520_2026_23212' -> '520'
    Treats journalNumber as a string to preserve leading zeros (e.g. '052', '007').
    Fallback to job sheet instructions, job sheet filename, or manuscripts if project name does not contain standard format.
    """
    name = ""
    project = None
    if isinstance(project_or_name, str):
        name = project_or_name.strip()
    elif hasattr(project_or_name, 'name'):
        project = project_or_name
        name = (project_or_name.name or "").strip()
        if not job_sheet:
            if hasattr(project_or_name, 'job_sheet') and project_or_name.job_sheet:
                job_sheet = project_or_name.job_sheet
            elif hasattr(project_or_name, 'job_sheet_id') and project_or_name.job_sheet_id:
                try:
                    job_sheet = db.session.get(JobSheet, project_or_name.job_sheet_id)
                except Exception:
                    job_sheet = None

    # 1. JobSheet instructions (Highest authority for metadata from publisher XML/DOCX)
    if job_sheet and hasattr(job_sheet, 'instructions'):
        for inst in job_sheet.instructions:
            sec = (inst.section or "").strip().lower()
            text = (inst.instruction or "").strip()
            if sec in ('journal id', 'journal number', 'journal', 'journal i d', 'journal code', 'journal_id'):
                m = re.search(r'([A-Za-z0-9]+)', text)
                if m:
                    cand = m.group(1).strip()
                    if cand.isdigit() or len(cand) >= 2:
                        return cand
            m_text = re.search(r'journal\s*(?:id|number|#)?\s*[:=\-]?\s*([A-Za-z0-9]+)', text, re.IGNORECASE)
            if m_text:
                return m_text.group(1).strip()

    # 2. Strict Project ID format: JOURNAL_YEAR_ARTICLE (e.g. 520_2026_23212, 052_2026_111, 540-2026-03899)
    if name:
        m = re.match(r'^([A-Za-z0-9]+)[_\-](\d{4})[_\-]', name)
        if m:
            return m.group(1).strip()

    # 3. JobSheet filename (e.g. 540_2026_3899_JobSheet_200.xml)
    if job_sheet and hasattr(job_sheet, 'filename') and job_sheet.filename:
        m = re.match(r'^([A-Za-z0-9]+)[_\-](\d{4})', job_sheet.filename)
        if m:
            return m.group(1).strip()
        m_num = re.match(r'^(\d+)', job_sheet.filename)
        if m_num:
            return m_num.group(1).strip()

    # 4. Pure numeric project name (e.g. '540', '052')
    if name and re.match(r'^\d+$', name):
        return name

    # 5. Check if project name segment before delimiter matches an existing journal in DB
    if name:
        m = re.match(r'^([A-Za-z0-9]+)[_\-]', name)
        if m:
            cand = m.group(1).strip()
            try:
                if cand.isdigit() or JournalInstruction.query.filter_by(journal_number=cand, active=True).first():
                    return cand
            except Exception:
                if cand.isdigit():
                    return cand

    # 6. Check if project name directly matches an existing journal in DB
    if name:
        try:
            if JournalInstruction.query.filter_by(journal_number=name, active=True).first():
                return name
        except Exception:
            pass

    # 7. Check project team name
    if project and hasattr(project, 'team') and project.team and project.team.name:
        tname = project.team.name.strip()
        m_team = re.search(r'\b(\d{2,6})\b', tname)
        if m_team:
            return m_team.group(1).strip()
        try:
            if JournalInstruction.query.filter_by(journal_number=tname, active=True).first():
                return tname
        except Exception:
            pass

    return "Unknown"

app.jinja_env.filters['journal_number'] = extract_journal_number

@app.context_processor
def inject_rule_helpers():
    return {
        'get_rule_group': get_rule_group,
        'extract_journal_number': extract_journal_number,
        'app_name': 'Journal Guard',
        'app_subtitle': 'Intelligent Manuscript & Artwork Quality Assurance',
        'asset_version': int(datetime.utcnow().timestamp())
    }

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

        if ReferenceStyle.query.count() == 0:
            admin_user = User.query.filter_by(role='admin').first()
            admin_id = admin_user.id if admin_user else 1

            vancouver = ReferenceStyle(name='Vancouver', description='Numbered citation style according to order of appearance in text.', is_active=True, created_by=admin_id)
            db.session.add(vancouver)
            db.session.commit()
            db.session.add(ReferenceRule(ref_style_id=vancouver.id, category='Citation numbering', rule_text='References must be numbered consecutively in order of first appearance.'))
            db.session.add(ReferenceRule(ref_style_id=vancouver.id, category='DOI', rule_text='Include DOI when available.'))

            harvard = ReferenceStyle(name='Harvard', description='Author-year alphabetical reference style.', is_active=True, created_by=admin_id)
            db.session.add(harvard)
            db.session.commit()
            db.session.add(ReferenceRule(ref_style_id=harvard.id, category='Author format', rule_text='References listed alphabetically by primary author surname.'))

            apa = ReferenceStyle(name='APA 7th', description='American Psychological Association reference style.', is_active=True, created_by=admin_id)
            db.session.add(apa)
            db.session.commit()
            db.session.add(ReferenceRule(ref_style_id=apa.id, category='Author format', rule_text='Author surname followed by initials and publication year.'))
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

@app.route('/manifest.json')
def manifest():
    return send_file(os.path.join(basedir, 'static', 'manifest.json'), mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    response = send_file(os.path.join(basedir, 'static', 'sw.js'), mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    return response

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
    total_figures = FigureInspection.query.count()
    figure_issues = FigureInspection.query.filter(FigureInspection.status.in_(['WARNING', 'FAIL'])).count()
    return render_template('dashboard.html', projects=projects, total=total_projects,
                           pending=pending_qc, critical=critical_errors, completed=completed,
                           total_figures=total_figures, figure_issues=figure_issues)

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
            if not file_obj or getattr(file_obj, 'filename', '') == '':
                return None
            fname = file_obj.filename.lower()
            sup_map = {
                '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
                '+': '⁺', '-': '⁻', '*': '*', '†': '†', '‡': '‡',
                '§': '§', '¶': '¶', ',': ','
            }
            try:
                if fname.endswith('.docx') or fname.endswith('.doc'):
                    # 1. Fast XML parse from docx zip without decompressing media
                    try:
                        import zipfile, xml.etree.ElementTree as ET
                        file_obj.seek(0)
                        lines = []
                        with zipfile.ZipFile(file_obj) as z:
                            xml_bytes = z.read('word/document.xml')
                            root = ET.fromstring(xml_bytes)
                            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                            for p in root.iterfind('.//w:p', ns):
                                p_parts = []
                                for child in p:
                                    tag = child.tag
                                    if tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r':
                                        vert = child.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr/{http://schemas.openxmlformats.org/wordprocessingml/2006/main}vertAlign')
                                        is_sup = vert is not None and vert.attrib.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') == 'superscript'
                                        for r_child in child:
                                            if r_child.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t':
                                                t = r_child.text or ''
                                                if is_sup:
                                                    t = ''.join(sup_map.get(c, c) for c in t)
                                                p_parts.append(t)
                                            elif r_child.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab':
                                                p_parts.append('\t')
                                            elif r_child.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br':
                                                p_parts.append('\n')
                                lines.append(''.join(p_parts))
                        extracted = '\n'.join(lines)
                        if extracted.strip():
                            return extracted
                    except Exception:
                        pass

                    # 2. Fallback to python-docx
                    try:
                        import docx
                        file_obj.seek(0)
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

                    # 3. Fallback to mammoth
                    try:
                        file_obj.seek(0)
                        return mammoth.extract_raw_text(file_obj).value
                    except Exception:
                        pass
                elif fname.endswith('.xml') or fname.endswith('.txt'):
                    file_obj.seek(0)
                    return file_obj.read().decode('utf-8', errors='ignore')
                elif fname.endswith('.pdf'):
                    # 1. Primary: Extract text using pdfplumber
                    try:
                        import pdfplumber
                        file_obj.seek(0)
                        pages_text = []
                        with pdfplumber.open(file_obj) as pdf:
                            for page in pdf.pages:
                                t = page.extract_text()
                                if t:
                                    pages_text.append(t)
                        extracted = '\n\n'.join(pages_text)
                        if extracted and extracted.strip():
                            return extracted
                    except Exception as e:
                        print(f"[PDF Extract] pdfplumber fallback: {e}")

                    # 2. Fallback: pdfminer.six
                    try:
                        from pdfminer.high_level import extract_text as pdfminer_extract
                        file_obj.seek(0)
                        extracted = pdfminer_extract(file_obj)
                        if extracted and extracted.strip():
                            return extracted
                    except Exception as e:
                        print(f"[PDF Extract] pdfminer fallback: {e}")

                    # 3. Fallback: pypdfium2
                    try:
                        import pypdfium2 as pdfium
                        file_obj.seek(0)
                        pdf = pdfium.PdfDocument(file_obj.read())
                        pages_text = [page.get_textpage().get_text_range() for page in pdf]
                        extracted = '\n\n'.join(p for p in pages_text if p and p.strip())
                        if extracted and extracted.strip():
                            return extracted
                    except Exception as e:
                        print(f"[PDF Extract] pypdfium2 fallback: {e}")

                    # 4. Fallback in case raw text/XML was saved with a .pdf extension
                    try:
                        file_obj.seek(0)
                        raw = file_obj.read().decode('utf-8', errors='ignore')
                        if raw.strip().startswith('<') or len(raw.strip()) > 30:
                            return raw
                    except Exception:
                        pass
            except Exception:
                pass
            return None

        # Check if text was pre-extracted on client-side (for instant upload of large docx files)
        author_text = request.form.get('author_extracted_text')
        if not author_text or not author_text.strip():
            file = request.files.get('manuscript_file')
            author_text = extract_text(file)

        if not author_text or not author_text.strip():
            err_msg = 'No author file content provided or file could not be read.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
                return jsonify({'success': False, 'error': err_msg}), 400
            flash(err_msg, 'error')
            return redirect(request.url)

        my_text = request.form.get('edited_extracted_text')
        if not my_text or not my_text.strip():
            my_file = request.files.get('xml_file')
            my_text = extract_text(my_file)

        if not name or not str(name).strip():
            m_file = request.files.get('manuscript_file')
            if m_file and getattr(m_file, 'filename', ''):
                name = os.path.splitext(os.path.basename(m_file.filename))[0]
            else:
                name = f"Project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        project = Project(name=name, team_id=team_id)
        db.session.add(project)
        db.session.commit()

        # Handle Job Sheet upload
        job_sheet_instructions = []
        job_sheet_file = request.files.get('job_sheet_file')
        if job_sheet_file and job_sheet_file.filename != '':
            js_bytes = job_sheet_file.read()
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

        # Normalize newlines
        if author_text:
            author_text = author_text.replace('\r\n', '\n').replace('\r', '\n')
        if my_text:
            my_text = my_text.replace('\r\n', '\n').replace('\r', '\n')

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

        # ── Journal Guard: Image Quality & Artwork Inspection ──
        try:
            author_file_obj = request.files.get('manuscript_file')
            edited_file_obj = request.files.get('xml_file')
            figure_files = request.files.getlist('figure_files')
            figures_zip = request.files.get('figures_zip')
            
            process_manuscript_figures(
                manuscript=manuscript,
                author_file=author_file_obj,
                edited_file=edited_file_obj,
                figure_files=figure_files,
                figures_zip=figures_zip,
                base_dir=basedir,
                db_session=db.session,
                ImageCriterionModel=ImageCriterion,
                FigureInspectionModel=FigureInspection
            )
        except Exception as fig_err:
            print(f"[Journal Guard] Image QC error: {fig_err}")

        # Explicit Audit Log entry for staff running files through QC
        author_fname = getattr(request.files.get('manuscript_file'), 'filename', '') or 'Manuscript'
        log_audit(
            action='RUN_QC',
            object_type='Project',
            object_id=project.id,
            old_value=None,
            new_value=f"Ran QC on '{project.name}' (File: {author_fname}) - {len(errors)} critical issues detected"
        )

        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({
                'success': True,
                'project_id': project.id,
                'redirect_url': url_for('review_project', project_id=project.id)
            })
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
    type_status = {}
    all_previously_resolved = bool(existing_records) and all(r.status != 'OPEN' for r in existing_records)
    for r in existing_records:
        key = (r.error_type, (r.description or '').strip())
        if r.status != 'OPEN':
            preserved_status[key] = r.status
            if r.error_type not in type_status:
                type_status[r.error_type] = r.status

    # Normalize any carriage returns in existing manuscript text
    if manuscript.original_text and '\r' in manuscript.original_text:
        manuscript.original_text = manuscript.original_text.replace('\r\n', '\n').replace('\r', '\n')
    if manuscript.working_text and '\r' in manuscript.working_text:
        manuscript.working_text = manuscript.working_text.replace('\r\n', '\n').replace('\r', '\n')
    db.session.commit()

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
        desc_clean = (err_dict.get('description') or '').strip()
        key = (err_dict['error_type'], desc_clean)
        st = preserved_status.get(key)
        if not st:
            st = type_status.get(err_dict['error_type'])
        if not st and all_previously_resolved:
            st = 'IGNORED'
        if not st:
            st = 'OPEN'
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

    # Refresh figure inspections against text callouts & criteria
    try:
        j_num = extract_journal_number(project, project.job_sheet)
        crit_list = ImageCriterion.query.filter(
            ImageCriterion.is_active == True,
            ImageCriterion.journal_number.in_([j_num, 'ALL'])
        ).all()
        if not crit_list:
            crit_list = ImageCriterion.query.filter_by(is_active=True).all()
        text_calls = find_figure_callouts(manuscript.working_text)
        for fig in manuscript.figures:
            abs_p = os.path.join(basedir, fig.file_path.replace('/', os.sep))
            if os.path.exists(abs_p):
                insp = inspect_figure(abs_p, criteria_list=crit_list, text_callouts=text_calls, figure_label=fig.figure_label)
                if fig.status != 'OVERRIDDEN':
                    fig.status = insp['status']
                fig.issues_json = json.dumps(insp['issues'])
                fig.is_cited_in_text = insp['is_cited_in_text']
    except Exception as e:
        print(f"[Journal Guard] Error refreshing figures on rerun: {e}")

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
    journal_number = extract_journal_number(project)

    figures = manuscript.figures
    fig_summary = {
        'total': len(figures),
        'pass': len([f for f in figures if f.status == 'PASS']),
        'warning': len([f for f in figures if f.status == 'WARNING']),
        'fail': len([f for f in figures if f.status == 'FAIL']),
        'overridden': len([f for f in figures if f.status == 'OVERRIDDEN'])
    }

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
                           get_rule_group=get_rule_group,
                           journal_number=journal_number,
                           figures=figures,
                           figures_summary=fig_summary)


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

    figures_json = [f.to_dict() for f in manuscript.figures]

    return jsonify({
        'manuscript_text': manuscript.working_text,
        'original_text': manuscript.original_text,
        'errors': errors_json,
        'figures': figures_json,
        'figures_summary': {
            'total': len(figures_json),
            'pass': len([f for f in figures_json if f['status'] == 'PASS']),
            'warning': len([f for f in figures_json if f['status'] == 'WARNING']),
            'fail': len([f for f in figures_json if f['status'] == 'FAIL']),
            'overridden': len([f for f in figures_json if f['status'] == 'OVERRIDDEN'])
        },
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
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'message': 'All remaining errors were skipped.', 'unlocked': True})
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
# JOURNAL INSTRUCTION LIBRARY (API & ADMIN)
# ─────────────────────────────────────────────

@app.route('/api/projects/<int:project_id>/journal-instructions')
@login_required
def api_project_journal_instructions(project_id):
    """
    JSON API for the QC Review popup and full instruction drawer.
    Strictly isolated: Never returns instructions belonging to another journal.
    Uses indexed database query by (journal_number, active).
    """
    project = db.session.get(Project, project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if not is_admin() and project.team_id not in get_user_team_ids():
        return jsonify({'error': 'Access denied'}), 403

    journal_number = extract_journal_number(project)
    if not journal_number or journal_number == 'Unknown':
        return jsonify({
            'success': True,
            'journalNumber': 'Unknown',
            'journalName': 'Unknown Journal',
            'reminders': [],
            'totalReminders': 0,
            'allInstructions': [],
            'categories': {}
        })

    # Indexed query on (journal_number, active)
    active_instructions = JournalInstruction.query.filter_by(
        journal_number=journal_number,
        active=True
    ).all()

    # Determine journal name
    journal_name = f"Journal {journal_number}"
    for inst in active_instructions:
        if inst.journal_name and len(inst.journal_name.strip()) > 0 and len(inst.journal_name.strip()) <= 80 and not inst.journal_name.strip().lower().startswith('line ') and '\n' not in inst.journal_name:
            journal_name = inst.journal_name.strip()
            break

    # If journal_name is still default f"Journal {journal_number}", check Job Sheet for Journal Title
    if journal_name == f"Journal {journal_number}" and project.job_sheet_id:
        js = project.job_sheet or db.session.get(JobSheet, project.job_sheet_id)
        if js and js.instructions:
            for i in js.instructions:
                if (i.section or '').strip().lower() in ('journal title', 'journal name'):
                    if i.instruction and i.instruction.strip():
                        journal_name = f"{i.instruction.strip()} (Journal {journal_number})"
                        break

    PRIORITY_WEIGHT = {'High': 1, 'Medium': 2, 'Low': 3}

    # 1. Active Reminder Only instructions, sorted by priority (High > Medium > Low)
    reminders = [i for i in active_instructions if i.type == 'Reminder Only']
    reminders.sort(key=lambda x: (PRIORITY_WEIGHT.get(x.priority, 2), -x.id))
    # Slice the first 5 reminders
    reminders_top5 = [i.to_dict() for i in reminders[:5]]

    # 2. All active instructions for this journal
    all_sorted = sorted(
        active_instructions,
        key=lambda x: (x.category or 'General', PRIORITY_WEIGHT.get(x.priority, 2), -x.id)
    )
    all_dicts = [i.to_dict() for i in all_sorted]

    # 3. Group by category
    categories = {}
    for d in all_dicts:
        cat = d['category'] or 'General'
        categories.setdefault(cat, []).append(d)

    return jsonify({
        'success': True,
        'journalNumber': journal_number,
        'journalName': journal_name,
        'reminders': reminders_top5,
        'totalReminders': len(reminders),
        'allInstructions': all_dicts,
        'categories': categories
    })


@app.route('/admin/journal-instructions')
@login_required
def admin_journal_instructions():
    """Admin page: central Journal Instruction Library."""
    if not is_admin():
        abort(403)

    instructions = JournalInstruction.query.order_by(
        JournalInstruction.journal_number.asc(),
        JournalInstruction.priority.asc(),
        JournalInstruction.id.desc()
    ).all()

    total_count = len(instructions)
    active_count = sum(1 for i in instructions if i.active)
    reminder_count = sum(1 for i in instructions if i.type == 'Reminder Only')
    validation_count = sum(1 for i in instructions if i.type == 'Validation Rule')
    distinct_journals = sorted(list({i.journal_number for i in instructions}))
    unique_journals_count = len(distinct_journals)

    return render_template(
        'journal_instructions.html',
        instructions=instructions,
        total_count=total_count,
        active_count=active_count,
        reminder_count=reminder_count,
        validation_count=validation_count,
        distinct_journals=distinct_journals,
        unique_journals_count=unique_journals_count
    )


@app.route('/admin/journal-instructions/create', methods=['POST'])
@login_required
def admin_journal_instructions_create():
    """Add a new journal instruction."""
    if not is_admin():
        abort(403)

    j_num = (request.form.get('journal_number') or '').strip()
    if not j_num:
        flash('Journal Number is required.', 'error')
        return redirect(url_for('admin_journal_instructions'))

    inst_text = (request.form.get('instruction_text') or '').strip()
    if not inst_text:
        flash('Instruction text is required.', 'error')
        return redirect(url_for('admin_journal_instructions'))

    j_name = (request.form.get('journal_name') or '').strip() or None
    category = (request.form.get('category') or 'General').strip()
    inst_type = (request.form.get('type') or 'Reminder Only').strip()
    priority = (request.form.get('priority') or 'Medium').strip()
    source = (request.form.get('source') or 'Manual').strip()
    version = (request.form.get('version') or '1.0').strip()
    is_active = bool(request.form.get('active'))

    instruction = JournalInstruction(
        journal_number=j_num,  # string preserving leading zeros
        journal_name=j_name,
        category=category,
        instruction_text=inst_text,
        type=inst_type,
        priority=priority,
        source=source,
        version=version,
        active=is_active,
        created_by=current_user.id
    )
    db.session.add(instruction)
    db.session.commit()

    log_audit('CREATE_JOURNAL_INSTRUCTION', 'JournalInstruction', instruction.id, None,
              f"Created instruction for Journal {j_num} ({inst_type}) by {current_user.name}")
    db.session.commit()

    flash(f'Instruction for Journal {j_num} created successfully.', 'success')
    return redirect(url_for('admin_journal_instructions'))


@app.route('/admin/journal-instructions/<int:instruction_id>/edit', methods=['POST'])
@login_required
def admin_journal_instructions_edit(instruction_id):
    """Edit an existing journal instruction."""
    if not is_admin():
        abort(403)

    inst = db.session.get(JournalInstruction, instruction_id)
    if not inst:
        flash('Instruction not found.', 'error')
        return redirect(url_for('admin_journal_instructions'))

    j_num = (request.form.get('journal_number') or '').strip()
    if j_num:
        inst.journal_number = j_num
    j_name = (request.form.get('journal_name') or '').strip()
    inst.journal_name = j_name or None
    inst.category = (request.form.get('category') or 'General').strip()
    inst.instruction_text = (request.form.get('instruction_text') or inst.instruction_text).strip()
    inst.type = (request.form.get('type') or inst.type).strip()
    inst.priority = (request.form.get('priority') or inst.priority).strip()
    inst.source = (request.form.get('source') or inst.source).strip()
    inst.version = (request.form.get('version') or inst.version).strip()
    inst.active = bool(request.form.get('active'))
    inst.updated_at = datetime.utcnow()

    db.session.commit()

    log_audit('EDIT_JOURNAL_INSTRUCTION', 'JournalInstruction', inst.id, None,
              f"Updated instruction {inst.id} for Journal {inst.journal_number} by {current_user.name}")
    db.session.commit()

    flash(f'Instruction {inst.id} updated successfully.', 'success')
    return redirect(url_for('admin_journal_instructions'))


@app.route('/admin/journal-instructions/<int:instruction_id>/toggle', methods=['POST'])
@login_required
def admin_journal_instructions_toggle(instruction_id):
    """Activate or deactivate a journal instruction."""
    if not is_admin():
        abort(403)

    inst = db.session.get(JournalInstruction, instruction_id)
    if not inst:
        flash('Instruction not found.', 'error')
        return redirect(url_for('admin_journal_instructions'))

    inst.active = not inst.active
    inst.updated_at = datetime.utcnow()
    db.session.commit()

    status_str = 'activated' if inst.active else 'deactivated'
    log_audit('TOGGLE_JOURNAL_INSTRUCTION', 'JournalInstruction', inst.id, None,
              f"{status_str.capitalize()} instruction {inst.id} for Journal {inst.journal_number}")
    db.session.commit()

    flash(f"Instruction {inst.id} {status_str}.", 'success')
    return redirect(url_for('admin_journal_instructions'))


@app.route('/admin/journal-instructions/<int:instruction_id>/delete', methods=['POST'])
@login_required
def admin_journal_instructions_delete(instruction_id):
    """Delete a journal instruction."""
    if not is_admin():
        abort(403)

    inst = db.session.get(JournalInstruction, instruction_id)
    if not inst:
        flash('Instruction not found.', 'error')
        return redirect(url_for('admin_journal_instructions'))

    j_num = inst.journal_number
    db.session.delete(inst)
    db.session.commit()

    log_audit('DELETE_JOURNAL_INSTRUCTION', 'JournalInstruction', instruction_id, None,
              f"Deleted instruction {instruction_id} for Journal {j_num} by {current_user.name}")
    db.session.commit()

    flash(f'Instruction deleted successfully.', 'success')
    return redirect(url_for('admin_journal_instructions'))


@app.route('/admin/journal-instructions/import-xwiki', methods=['POST'])
@login_required
def admin_journal_instructions_import_xwiki():
    """Import journal instructions from XWiki with source and version tracking."""
    if not is_admin():
        abort(403)

    source = (request.form.get('source') or 'XWiki').strip()
    version = (request.form.get('version') or '1.0').strip()
    payload = (request.form.get('payload') or '').strip()

    file = request.files.get('import_file')
    if file and file.filename:
        try:
            file_content = file.read().decode('utf-8', errors='ignore')
            if file_content.strip():
                payload = file_content.strip()
        except Exception as e:
            flash(f'Error reading uploaded file: {e}', 'error')
            return redirect(url_for('admin_journal_instructions'))

    if not payload:
        flash('Please provide data to import (paste JSON/CSV or upload a file).', 'error')
        return redirect(url_for('admin_journal_instructions'))

    imported_count = 0

    # Try parsing as JSON first
    parsed_json = None
    try:
        parsed_json = json.loads(payload)
    except Exception:
        pass

    if isinstance(parsed_json, list):
        for item in parsed_json:
            if not isinstance(item, dict):
                continue
            j_num = str(item.get('journalNumber') or item.get('journal_number') or '').strip()
            text = str(item.get('instructionText') or item.get('instruction_text') or item.get('instruction') or '').strip()
            if not j_num or not text:
                continue

            j_name = str(item.get('journalName') or item.get('journal_name') or '').strip() or None
            category = str(item.get('category') or 'General').strip()
            raw_type = str(item.get('type') or 'Reminder Only').strip()
            inst_type = 'Validation Rule' if 'validation' in raw_type.lower() else 'Reminder Only'
            raw_pri = str(item.get('priority') or 'Medium').strip().capitalize()
            priority = raw_pri if raw_pri in ('High', 'Medium', 'Low') else 'Medium'
            active = bool(item.get('active', True))

            inst = JournalInstruction(
                journal_number=j_num,
                journal_name=j_name,
                category=category,
                instruction_text=text,
                type=inst_type,
                priority=priority,
                source=source,
                version=version,
                active=active,
                created_by=current_user.id
            )
            db.session.add(inst)
            imported_count += 1

    else:
        # Parse CSV or pipe-delimited XWiki table
        import csv
        import io
        lines = [line.strip() for line in payload.splitlines() if line.strip()]

        is_pipe_table = any(line.startswith('|') for line in lines)
        if is_pipe_table:
            for line in lines:
                parts = [p.strip() for p in line.split('|')]
                parts = [p for p in parts if p != '']
                if len(parts) >= 2:
                    j_num = parts[0].strip()
                    if j_num.lower() in ('journal', 'journalnumber', 'journal id', '#'):
                        continue
                    text = parts[1].strip()
                    raw_type = parts[2].strip() if len(parts) > 2 else 'Reminder Only'
                    inst_type = 'Validation Rule' if 'validation' in raw_type.lower() else 'Reminder Only'
                    raw_pri = (parts[3].strip() if len(parts) > 3 else 'Medium').capitalize()
                    priority = raw_pri if raw_pri in ('High', 'Medium', 'Low') else 'Medium'
                    category = parts[4].strip() if len(parts) > 4 else 'General'
                    j_name = parts[5].strip() if len(parts) > 5 else None

                    inst = JournalInstruction(
                        journal_number=j_num,
                        journal_name=j_name,
                        category=category,
                        instruction_text=text,
                        type=inst_type,
                        priority=priority,
                        source=source,
                        version=version,
                        active=True,
                        created_by=current_user.id
                    )
                    db.session.add(inst)
                    imported_count += 1
        else:
            reader = csv.reader(io.StringIO(payload))
            for row in reader:
                if not row or len(row) < 2:
                    continue
                j_num = row[0].strip()
                if j_num.lower() in ('journalnumber', 'journal_number', 'journal', 'journal id'):
                    continue
                text = row[1].strip()
                raw_type = row[2].strip() if len(row) > 2 else 'Reminder Only'
                inst_type = 'Validation Rule' if 'validation' in raw_type.lower() else 'Reminder Only'
                raw_pri = (row[3].strip() if len(row) > 3 else 'Medium').capitalize()
                priority = raw_pri if raw_pri in ('High', 'Medium', 'Low') else 'Medium'
                category = row[4].strip() if len(row) > 4 else 'General'
                j_name = row[5].strip() if len(row) > 5 else None

                inst = JournalInstruction(
                    journal_number=j_num,
                    journal_name=j_name,
                    category=category,
                    instruction_text=text,
                    type=inst_type,
                    priority=priority,
                    source=source,
                    version=version,
                    active=True,
                    created_by=current_user.id
                )
                db.session.add(inst)
                imported_count += 1

    if imported_count > 0:
        db.session.commit()
        log_audit('IMPORT_XWIKI_JOURNAL_INSTRUCTIONS', 'JournalInstruction', None, None,
                  f"Imported {imported_count} instructions from {source} (v{version}) by {current_user.name}")
        db.session.commit()
        flash(f'Successfully imported {imported_count} journal instruction(s) from {source} (v{version}).', 'success')
    else:
        flash('No valid journal instructions could be parsed from the provided input.', 'error')

    return redirect(url_for('admin_journal_instructions'))

# ─────────────────────────────────────────────
# JOURNAL GUARD: IMAGE QUALITY CRITERIA (CRUD)
# ─────────────────────────────────────────────

@app.route('/admin/image-criteria')
@login_required
def admin_image_criteria():
    if not is_admin() and current_user.role != 'team_leader':
        abort(403)

    journal_filter = request.args.get('journal', '').strip()
    category_filter = request.args.get('category', '').strip()
    status_filter = request.args.get('status', '').strip()

    query = ImageCriterion.query
    if journal_filter:
        query = query.filter(ImageCriterion.journal_number.ilike(f'%{journal_filter}%'))
    if category_filter:
        query = query.filter_by(category=category_filter)
    if status_filter == 'active':
        query = query.filter_by(is_active=True)
    elif status_filter == 'inactive':
        query = query.filter_by(is_active=False)

    criteria = query.order_by(ImageCriterion.journal_number, ImageCriterion.category).all()

    total_count = ImageCriterion.query.count()
    active_count = ImageCriterion.query.filter_by(is_active=True).count()
    unique_journals = db.session.query(db.func.count(db.func.distinct(ImageCriterion.journal_number))).scalar() or 0

    return render_template('image_criteria.html',
                           criteria=criteria,
                           total_count=total_count,
                           active_count=active_count,
                           unique_journals=unique_journals,
                           journal_filter=journal_filter,
                           category_filter=category_filter,
                           status_filter=status_filter)


@app.route('/admin/image-criteria/create', methods=['POST'])
@login_required
def create_image_criterion():
    if not is_admin() and current_user.role != 'team_leader':
        abort(403)

    try:
        journal_number = (request.form.get('journal_number') or 'ALL').strip().upper()
        category = request.form.get('category') or 'Halftone / Photo'
        min_dpi = int(request.form.get('min_dpi') or 300)
        recommended_dpi = int(request.form.get('recommended_dpi') or min_dpi)
        min_width_px = int(request.form.get('min_width_px') or 800)
        min_height_px = int(request.form.get('min_height_px') or 600)
        max_file_size_mb = float(request.form.get('max_file_size_mb') or 50.0)
        allowed_color_modes = request.form.get('allowed_color_modes') or 'RGB, CMYK, Grayscale'
        allowed_formats = request.form.get('allowed_formats') or 'TIFF, EPS, PNG, JPEG, PDF'
        min_sharpness_score = float(request.form.get('min_sharpness_score') or 30.0)
        notes = request.form.get('notes', '').strip()

        criterion = ImageCriterion(
            journal_number=journal_number,
            category=category,
            min_dpi=min_dpi,
            recommended_dpi=recommended_dpi,
            min_width_px=min_width_px,
            min_height_px=min_height_px,
            max_file_size_mb=max_file_size_mb,
            allowed_color_modes=allowed_color_modes,
            allowed_formats=allowed_formats,
            min_sharpness_score=min_sharpness_score,
            notes=notes,
            is_active=True,
            created_by=current_user.id
        )
        db.session.add(criterion)
        db.session.commit()
        log_audit('CREATE', 'ImageCriterion', criterion.id, None, f"Created {category} for Journal {journal_number}")
        flash(f'Successfully created Image Criterion for Journal {journal_number} ({category}).', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to create criterion: {str(e)}', 'error')

    return redirect(url_for('admin_image_criteria'))


@app.route('/admin/image-criteria/<int:criterion_id>/edit', methods=['POST'])
@login_required
def edit_image_criterion(criterion_id):
    if not is_admin() and current_user.role != 'team_leader':
        abort(403)

    criterion = db.session.get(ImageCriterion, criterion_id)
    if not criterion:
        abort(404)

    try:
        old_val = json.dumps(criterion.to_dict())
        criterion.journal_number = (request.form.get('journal_number') or criterion.journal_number).strip().upper()
        criterion.category = request.form.get('category') or criterion.category
        criterion.min_dpi = int(request.form.get('min_dpi') or criterion.min_dpi)
        criterion.recommended_dpi = int(request.form.get('recommended_dpi') or criterion.recommended_dpi)
        criterion.min_width_px = int(request.form.get('min_width_px') or criterion.min_width_px)
        criterion.min_height_px = int(request.form.get('min_height_px') or criterion.min_height_px)
        criterion.max_file_size_mb = float(request.form.get('max_file_size_mb') or criterion.max_file_size_mb)
        criterion.allowed_color_modes = request.form.get('allowed_color_modes') or criterion.allowed_color_modes
        criterion.allowed_formats = request.form.get('allowed_formats') or criterion.allowed_formats
        criterion.min_sharpness_score = float(request.form.get('min_sharpness_score') or criterion.min_sharpness_score)
        criterion.notes = request.form.get('notes', criterion.notes)
        criterion.is_active = request.form.get('is_active') == 'true' or request.form.get('is_active') == '1' or 'is_active' in request.form

        db.session.commit()
        log_audit('EDIT', 'ImageCriterion', criterion.id, old_val, json.dumps(criterion.to_dict()))
        flash('Image Criterion updated successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to update criterion: {str(e)}', 'error')

    return redirect(url_for('admin_image_criteria'))


@app.route('/admin/image-criteria/<int:criterion_id>/toggle', methods=['POST'])
@login_required
def toggle_image_criterion(criterion_id):
    if not is_admin() and current_user.role != 'team_leader':
        abort(403)
    criterion = db.session.get(ImageCriterion, criterion_id)
    if not criterion:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    criterion.is_active = not criterion.is_active
    db.session.commit()
    log_audit('EDIT', 'ImageCriterion', criterion.id, None, f"Toggled is_active to {criterion.is_active}")
    return jsonify({'success': True, 'is_active': criterion.is_active})


@app.route('/admin/image-criteria/<int:criterion_id>/delete', methods=['POST'])
@login_required
def delete_image_criterion(criterion_id):
    if not is_admin():
        abort(403)
    criterion = db.session.get(ImageCriterion, criterion_id)
    if not criterion:
        abort(404)

    db.session.delete(criterion)
    db.session.commit()
    log_audit('DELETE', 'ImageCriterion', criterion_id, None, "Deleted ImageCriterion")
    flash('Image Criterion deleted successfully.', 'success')
    return redirect(url_for('admin_image_criteria'))


@app.route('/admin/image-criteria/reset-defaults', methods=['POST'])
@login_required
def reset_image_criteria_defaults():
    if not is_admin():
        abort(403)
    count = seed_default_image_criteria(db, ImageCriterion)
    flash(f'Publishing image standards synced ({count} defaults).', 'success')
    return redirect(url_for('admin_image_criteria'))


# ─────────────────────────────────────────────
# JOURNAL GUARD: FIGURE INSPECTION & OVERRIDE APIS
# ─────────────────────────────────────────────

@app.route('/api/manuscripts/<int:manuscript_id>/figures')
@login_required
def api_manuscript_figures(manuscript_id):
    manuscript = db.session.get(Manuscript, manuscript_id)
    if not manuscript:
        return jsonify({'success': False, 'error': 'Manuscript not found'}), 404
    return jsonify({
        'success': True,
        'figures': [f.to_dict() for f in manuscript.figures]
    })


@app.route('/api/figures/<int:figure_id>/override', methods=['POST'])
@login_required
def override_figure(figure_id):
    fig = db.session.get(FigureInspection, figure_id)
    if not fig:
        return jsonify({'success': False, 'error': 'Figure not found'}), 404

    reason = request.json.get('reason', '') if request.is_json else request.form.get('reason', '')
    if not reason.strip():
        return jsonify({'success': False, 'error': 'Approval/Override reason is required'}), 400

    fig.status = 'OVERRIDDEN'
    fig.override_reason = reason.strip()
    fig.reviewed_by = current_user.id
    fig.reviewed_at = datetime.utcnow()
    db.session.commit()

    log_audit('FIGURE_OVERRIDE', 'FigureInspection', fig.id, None, f"Overridden by {current_user.name}: {reason}")
    return jsonify({'success': True, 'figure': fig.to_dict()})


@app.route('/api/manuscripts/<int:manuscript_id>/recheck-figures', methods=['POST'])
@login_required
def recheck_manuscript_figures(manuscript_id):
    manuscript = db.session.get(Manuscript, manuscript_id)
    if not manuscript:
        return jsonify({'success': False, 'error': 'Manuscript not found'}), 404

    project = manuscript.project
    journal_num = extract_journal_number(project, project.job_sheet)
    criteria_list = ImageCriterion.query.filter(
        ImageCriterion.is_active == True,
        ImageCriterion.journal_number.in_([journal_num, 'ALL'])
    ).all()
    if not criteria_list:
        criteria_list = ImageCriterion.query.filter_by(is_active=True).all()

    text_callouts = find_figure_callouts(manuscript.working_text or manuscript.original_text)

    updated_count = 0
    for fig in manuscript.figures:
        abs_p = os.path.join(basedir, fig.file_path.replace('/', os.sep))
        if os.path.exists(abs_p):
            insp = inspect_figure(abs_p, criteria_list=criteria_list, text_callouts=text_callouts, figure_label=fig.figure_label)
            fig.format = insp['format']
            fig.color_mode = insp['color_mode']
            fig.dpi_x = insp['dpi_x']
            fig.dpi_y = insp['dpi_y']
            fig.pixel_width = insp['pixel_width']
            fig.pixel_height = insp['pixel_height']
            fig.print_width_inches = insp['print_width_inches']
            fig.print_height_inches = insp['print_height_inches']
            fig.file_size_kb = insp['file_size_kb']
            fig.sharpness_score = insp['sharpness_score']
            fig.is_blurry = insp['is_blurry']
            if fig.status != 'OVERRIDDEN':
                fig.status = insp['status']
            fig.issues_json = json.dumps(insp['issues'])
            fig.is_cited_in_text = insp['is_cited_in_text']
            updated_count += 1

    db.session.commit()
    return jsonify({
        'success': True,
        'updated_count': updated_count,
        'figures': [f.to_dict() for f in manuscript.figures]
    })


@app.route('/api/manuscripts/<int:manuscript_id>/upload-figure', methods=['POST'])
@login_required
def upload_manuscript_figure(manuscript_id):
    manuscript = db.session.get(Manuscript, manuscript_id)
    if not manuscript:
        return jsonify({'success': False, 'error': 'Manuscript not found'}), 404

    file = request.files.get('figure_file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    fig_output_dir = os.path.join(basedir, 'static', 'uploads', 'figures', str(manuscript.project_id))
    idx = len(manuscript.figures) + 1
    fig_data = save_uploaded_figure_file(file, fig_output_dir, figure_index=idx)

    project = manuscript.project
    journal_num = extract_journal_number(project, project.job_sheet)
    criteria_list = ImageCriterion.query.filter(
        ImageCriterion.is_active == True,
        ImageCriterion.journal_number.in_([journal_num, 'ALL'])
    ).all()
    if not criteria_list:
        criteria_list = ImageCriterion.query.filter_by(is_active=True).all()

    text_callouts = find_figure_callouts(manuscript.working_text or manuscript.original_text)
    custom_label = request.form.get('figure_label') or f"Figure {idx}"
    insp = inspect_figure(fig_data['abs_file_path'], criteria_list=criteria_list, text_callouts=text_callouts, figure_label=custom_label)

    fig_rec = FigureInspection(
        manuscript_id=manuscript.id,
        figure_label=custom_label,
        original_filename=fig_data['original_filename'],
        file_path=fig_data['file_path'],
        thumbnail_path=fig_data.get('thumbnail_path'),
        format=insp['format'],
        color_mode=insp['color_mode'],
        dpi_x=insp['dpi_x'],
        dpi_y=insp['dpi_y'],
        pixel_width=insp['pixel_width'],
        pixel_height=insp['pixel_height'],
        print_width_inches=insp['print_width_inches'],
        print_height_inches=insp['print_height_inches'],
        file_size_kb=insp['file_size_kb'],
        sharpness_score=insp['sharpness_score'],
        is_blurry=insp['is_blurry'],
        status=insp['status'],
        issues_json=json.dumps(insp['issues']),
        is_cited_in_text=insp['is_cited_in_text']
    )
    db.session.add(fig_rec)
    db.session.commit()

    return jsonify({
        'success': True,
        'figure': fig_rec.to_dict()
    })


@app.route('/download-app-zip')
@login_required
def download_app_zip():
    """Package the latest Journal Guard application into a ZIP file and send directly to browser."""
    import io
    import zipfile
    base_dir = os.path.dirname(os.path.abspath(__file__))
    memory_file = io.BytesIO()
    exclude_dirs = {'.git', '__pycache__', 'venv', '.env', '.pytest_cache'}
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for file in files:
                if file.endswith('.pyc') or file.endswith('.zip'):
                    continue
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, base_dir)
                zf.write(full_path, os.path.join('Journal_Guard', rel_path))
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name='Journal_Guard_App.zip'
    )


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
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
