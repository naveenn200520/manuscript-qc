from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
import json

db = SQLAlchemy()

# ─────────────────────────────────────────────
# CORE USER / TEAM MODELS
# ─────────────────────────────────────────────

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    employee_code = db.Column(db.String(50), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    # roles: admin | team_leader | staff
    role = db.Column(db.String(50), nullable=False, default='staff')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    teams = db.relationship('Team', secondary='user_teams', backref=db.backref('members', lazy='dynamic'))
    permissions = db.relationship('UserPermission', foreign_keys='UserPermission.user_id', backref='user', lazy=True)

class Team(db.Model):
    __tablename__ = 'teams'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class UserTeam(db.Model):
    __tablename__ = 'user_teams'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)

# ─────────────────────────────────────────────
# PERMISSION MODEL
# ─────────────────────────────────────────────

class UserPermission(db.Model):
    """
    Granular per-user, per-resource permissions.
    resource: style_rules | reference_styles | job_sheets | manuscripts | qc_results
    level: NO_ACCESS | VIEW | EDIT
    team_id: NULL = applies to all teams; otherwise scoped to that team
    """
    __tablename__ = 'user_permissions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    resource = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    level = db.Column(db.String(20), nullable=False, default='VIEW')  # NO_ACCESS | VIEW | EDIT
    granted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    granted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ─────────────────────────────────────────────
# RULE SECTION
# ─────────────────────────────────────────────

class RuleSection(db.Model):
    """Admin-configurable rule sections (e.g. Capitalization, Abbreviations)."""
    __tablename__ = 'rule_sections'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ─────────────────────────────────────────────
# STYLE RULE (UPGRADED)
# ─────────────────────────────────────────────

class StyleRule(db.Model):
    __tablename__ = 'style_rules'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Classification
    name = db.Column(db.String(150), nullable=False)
    section = db.Column(db.String(150), nullable=True)   # e.g. Capitalization
    category = db.Column(db.String(50), nullable=False)  # Style, Language, Formatting, Critical
    rule_type = db.Column(db.String(100), nullable=True) # Text Replacement, Capitalization, etc.
    scope = db.Column(db.String(150), nullable=True)     # Article Title, Figure, etc.

    # Rule content
    find_pattern = db.Column(db.String(500), nullable=False)
    replacement = db.Column(db.String(500), nullable=False)
    expected_result = db.Column(db.String(500), nullable=True)
    exceptions = db.Column(db.Text, nullable=True)       # comma-separated or free text

    # Metadata
    severity = db.Column(db.String(50), nullable=True, default='Style')   # Style | Warning | Error
    priority = db.Column(db.String(20), nullable=True, default='Medium')  # Low | Medium | High
    status = db.Column(db.String(20), nullable=False, default='Active')   # Active | Inactive
    is_active = db.Column(db.Boolean, default=True)  # kept for backward compat

    # Versioning
    version = db.Column(db.Integer, default=1)

    # Soft delete
    deleted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    team = db.relationship('Team', backref=db.backref('rules', lazy=True))
    creator = db.relationship('User', foreign_keys=[created_by], backref='created_rules')
    versions = db.relationship('RuleVersion', backref='rule', lazy=True, foreign_keys='RuleVersion.rule_id')

# ─────────────────────────────────────────────
# RULE VERSION (SNAPSHOT)
# ─────────────────────────────────────────────

class RuleVersion(db.Model):
    """Immutable snapshot of a rule saved before each edit."""
    __tablename__ = 'rule_versions'
    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('style_rules.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    snapshot_json = db.Column(db.Text, nullable=False)  # JSON of rule fields at that version
    saved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    saved_at = db.Column(db.DateTime, default=datetime.utcnow)

# ─────────────────────────────────────────────
# REFERENCE STYLES
# ─────────────────────────────────────────────

class ReferenceStyle(db.Model):
    __tablename__ = 'reference_styles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    rules = db.relationship('ReferenceRule', backref='style', lazy=True)

class ReferenceRule(db.Model):
    __tablename__ = 'reference_rules'
    id = db.Column(db.Integer, primary_key=True)
    ref_style_id = db.Column(db.Integer, db.ForeignKey('reference_styles.id'), nullable=False)
    category = db.Column(db.String(100), nullable=False)  # Author format, DOI, etc.
    rule_text = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ─────────────────────────────────────────────
# PROJECT / MANUSCRIPT
# ─────────────────────────────────────────────

class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    job_sheet_id = db.Column(db.Integer, db.ForeignKey('job_sheets.id'), nullable=True)
    reference_style_id = db.Column(db.Integer, db.ForeignKey('reference_styles.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(50), default='Pending QC')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    team = db.relationship('Team', backref=db.backref('projects', lazy=True))
    manuscripts = db.relationship('Manuscript', backref='project', lazy=True)
    job_sheet = db.relationship('JobSheet', foreign_keys=[job_sheet_id], backref='project_ref', lazy=True)
    reference_style = db.relationship('ReferenceStyle', backref='projects', lazy=True)

class Manuscript(db.Model):
    __tablename__ = 'manuscripts'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    original_text = db.Column(db.Text, nullable=False)
    working_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='Uploaded')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    applied_rules_count = db.Column(db.Integer, default=0)
    total_available_rules = db.Column(db.Integer, default=0)
    errors = db.relationship('ErrorRecord', backref='manuscript', lazy=True)
    figures = db.relationship('FigureInspection', backref='manuscript', lazy=True, cascade='all, delete-orphan')

# ─────────────────────────────────────────────
# JOB SHEET
# ─────────────────────────────────────────────

class JobSheet(db.Model):
    __tablename__ = 'job_sheets'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    filename = db.Column(db.String(255), nullable=False)
    version = db.Column(db.Integer, default=1)
    status = db.Column(db.String(50), default='Active')
    parsed_status = db.Column(db.String(50), default='Pending')
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    instructions = db.relationship('JobSheetInstruction', backref='job_sheet', lazy=True)

class JobSheetInstruction(db.Model):
    __tablename__ = 'job_sheet_instructions'
    id = db.Column(db.Integer, primary_key=True)
    job_sheet_id = db.Column(db.Integer, db.ForeignKey('job_sheets.id'), nullable=False)
    section = db.Column(db.String(150), nullable=False)
    instruction = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(100), default='Job Sheet')
    status = db.Column(db.String(50), default='Detected')

# ─────────────────────────────────────────────
# ERROR RECORD (UPGRADED)
# ─────────────────────────────────────────────

class ErrorRecord(db.Model):
    __tablename__ = 'error_records'
    id = db.Column(db.Integer, primary_key=True)
    manuscript_id = db.Column(db.Integer, db.ForeignKey('manuscripts.id'), nullable=False)
    rule_id = db.Column(db.Integer, db.ForeignKey('style_rules.id'), nullable=True)

    # Error classification
    # SPELLING | AFFILIATION | AUTHOR | NUMERIC | SCIENTIFIC_TERM |
    # REFERENCE | JOB_SHEET_STYLE | CONTENT_CHANGE | MISSING_CONTENT | ADDED_CONTENT
    error_type = db.Column(db.String(50), nullable=False, default='CONTENT_CHANGE')
    # Structured error code from critical rules (e.g. AUTHOR_NAME_CHANGED, EMAIL_MISSING)
    error_code = db.Column(db.String(100), nullable=True)
    # CRITICAL | REVIEW | WARNING
    severity = db.Column(db.String(20), nullable=False, default='REVIEW')
    # Legacy category kept for backward compat with dashboard queries
    category = db.Column(db.String(50), nullable=True)

    # Location in the EDITED manuscript
    line_number = db.Column(db.Integer, nullable=True)
    column_number = db.Column(db.Integer, nullable=True)
    start_offset = db.Column(db.Integer, nullable=False, default=0)
    end_offset = db.Column(db.Integer, nullable=False, default=0)

    # Content: what the original says vs what the edited says
    original_text = db.Column(db.Text, nullable=True)    # from Original Manuscript
    edited_text = db.Column(db.Text, nullable=True)      # from Edited Manuscript (what is actually in the doc)

    # Context snippets for display
    context_before = db.Column(db.String(200), nullable=True)
    context_after = db.Column(db.String(200), nullable=True)

    # Description of the error for the reviewer
    description = db.Column(db.Text, nullable=True)

    # Suggested action for the reviewer (critical rules)
    suggested_action = db.Column(db.Text, nullable=True)

    # Field/column identifier (e.g. "Author Name", "Article Title")
    field = db.Column(db.String(150), nullable=True)

    # OPEN | RESTORED | KEPT | IGNORED | REVIEWED | APPROVED
    status = db.Column(db.String(50), default='OPEN')
    resolved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Legacy aliases for backward compat (dashboard error count queries use 'category')
    @property
    def found_text(self):
        return self.edited_text or ''

    @property
    def expected_text(self):
        return self.original_text or ''

    @property
    def location_index(self):
        return self.start_offset

# ─────────────────────────────────────────────
# CRITICAL RULE APPROVAL
# ─────────────────────────────────────────────

class CriticalRuleApproval(db.Model):
    """Record of an authorized reviewer explicitly approving a critical error.

    Required before Export Report, Final Submit, or Mark Done can proceed
    when unresolved critical errors exist.
    """
    __tablename__ = 'critical_rule_approvals'
    id = db.Column(db.Integer, primary_key=True)
    error_id = db.Column(db.Integer, db.ForeignKey('error_records.id'), nullable=False)
    manuscript_id = db.Column(db.Integer, db.ForeignKey('manuscripts.id'), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    approved_at = db.Column(db.DateTime, default=datetime.utcnow)

    error = db.relationship('ErrorRecord', backref=db.backref('approvals', lazy=True, cascade='all, delete-orphan'))
    approver = db.relationship('User', foreign_keys=[approved_by])

# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)   # CREATE | EDIT | DEACTIVATE | PERMISSION_CHANGE | CRITICAL_RULE_RUN | CRITICAL_RULE_APPROVAL etc.
    object_type = db.Column(db.String(100), nullable=True) # StyleRule | ReferenceStyle | UserPermission | CriticalRule etc.
    object_id = db.Column(db.Integer, nullable=True)
    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', foreign_keys=[user_id], backref='audit_logs')

# ─────────────────────────────────────────────
# JOURNAL INSTRUCTION LIBRARY
# ─────────────────────────────────────────────

class JournalInstruction(db.Model):
    """
    Scalable Journal Instruction Library.
    Supports thousands of journals and instructions with composite indexing
    on (journal_number, active).
    """
    __tablename__ = 'journal_instructions'
    id = db.Column(db.Integer, primary_key=True)
    journal_number = db.Column(db.String(50), nullable=False, index=True)  # string to preserve leading zeros, e.g. "052", "520"
    journal_name = db.Column(db.String(255), nullable=True)
    category = db.Column(db.String(100), nullable=False, default='General')  # e.g. References, Affiliation, Title, Formatting
    instruction_text = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), nullable=False, default='Reminder Only')  # 'Reminder Only' | 'Validation Rule'
    priority = db.Column(db.String(20), nullable=False, default='Medium')  # 'High' | 'Medium' | 'Low'
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    source = db.Column(db.String(100), nullable=True, default='Manual')  # 'XWiki' | 'Manual' | 'Import'
    version = db.Column(db.String(50), nullable=True, default='1.0')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    creator = db.relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        db.Index('idx_journal_active', 'journal_number', 'active'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'journalNumber': self.journal_number,
            'journalName': self.journal_name or f"Journal {self.journal_number}",
            'category': self.category or 'General',
            'instructionText': self.instruction_text,
            'type': self.type,
            'priority': self.priority,
            'active': self.active,
            'source': self.source or 'Manual',
            'version': self.version or '1.0',
            'createdAt': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
            'updatedAt': self.updated_at.strftime('%Y-%m-%d %H:%M') if self.updated_at else ''
        }


# ─────────────────────────────────────────────
# JOURNAL GUARD: IMAGE QUALITY CRITERIA & FIGURE INSPECTION
# ─────────────────────────────────────────────

class ImageCriterion(db.Model):
    """
    Journal Guard: Image Quality Criteria / Journal Artwork Specifications.
    Supports CRUD operations for global defaults ('ALL') or journal-specific standards.
    """
    __tablename__ = 'image_criteria'
    id = db.Column(db.Integer, primary_key=True)
    journal_number = db.Column(db.String(50), nullable=False, default='ALL', index=True)
    category = db.Column(db.String(100), nullable=False, default='Halftone / Photo') # Line Art | Halftone / Photo | Combination Art | Color Artwork
    min_dpi = db.Column(db.Integer, nullable=False, default=300)
    recommended_dpi = db.Column(db.Integer, nullable=False, default=300)
    min_width_px = db.Column(db.Integer, nullable=False, default=800)
    min_height_px = db.Column(db.Integer, nullable=False, default=600)
    max_file_size_mb = db.Column(db.Float, nullable=False, default=50.0)
    allowed_color_modes = db.Column(db.String(200), nullable=False, default='RGB, CMYK, Grayscale')
    allowed_formats = db.Column(db.String(200), nullable=False, default='TIFF, EPS, PNG, JPEG, PDF')
    min_sharpness_score = db.Column(db.Float, nullable=False, default=30.0)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'journal_number': self.journal_number,
            'category': self.category,
            'min_dpi': self.min_dpi,
            'recommended_dpi': self.recommended_dpi,
            'min_width_px': self.min_width_px,
            'min_height_px': self.min_height_px,
            'max_file_size_mb': self.max_file_size_mb,
            'allowed_color_modes': self.allowed_color_modes,
            'allowed_formats': self.allowed_formats,
            'min_sharpness_score': self.min_sharpness_score,
            'notes': self.notes or '',
            'is_active': self.is_active,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
            'updated_at': self.updated_at.strftime('%Y-%m-%d %H:%M') if self.updated_at else ''
        }


class FigureInspection(db.Model):
    """
    Journal Guard: Inspected Figure Record for a Manuscript.
    Stores DPI, resolution, dimensions, color mode, blur/sharpness metrics,
    and verification issues.
    """
    __tablename__ = 'figure_inspections'
    id = db.Column(db.Integer, primary_key=True)
    manuscript_id = db.Column(db.Integer, db.ForeignKey('manuscripts.id'), nullable=False, index=True)
    figure_label = db.Column(db.String(100), nullable=False, default='Figure') # e.g. "Figure 1", "Fig. 2"
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False) # static relative URL for web display
    thumbnail_path = db.Column(db.String(500), nullable=True)
    format = db.Column(db.String(50), nullable=True) # PNG, TIFF, JPEG, etc.
    color_mode = db.Column(db.String(50), nullable=True) # RGB, CMYK, Grayscale, etc.
    dpi_x = db.Column(db.Float, default=0.0)
    dpi_y = db.Column(db.Float, default=0.0)
    pixel_width = db.Column(db.Integer, default=0)
    pixel_height = db.Column(db.Integer, default=0)
    print_width_inches = db.Column(db.Float, default=0.0)
    print_height_inches = db.Column(db.Float, default=0.0)
    file_size_kb = db.Column(db.Float, default=0.0)
    sharpness_score = db.Column(db.Float, default=0.0)
    is_blurry = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(30), default='PASS') # PASS | WARNING | FAIL | OVERRIDDEN
    issues_json = db.Column(db.Text, default='[]') # JSON array of issues
    is_cited_in_text = db.Column(db.Boolean, default=True)
    override_reason = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reviewer = db.relationship('User', foreign_keys=[reviewed_by])

    @property
    def issues(self):
        try:
            return json.loads(self.issues_json or '[]')
        except Exception:
            return []

    def to_dict(self):
        return {
            'id': self.id,
            'manuscript_id': self.manuscript_id,
            'figure_label': self.figure_label,
            'original_filename': self.original_filename,
            'file_path': self.file_path,
            'thumbnail_path': self.thumbnail_path or self.file_path,
            'format': self.format,
            'color_mode': self.color_mode,
            'dpi_x': round(self.dpi_x, 1),
            'dpi_y': round(self.dpi_y, 1),
            'effective_dpi': round(min(self.dpi_x, self.dpi_y) if (self.dpi_x > 0 and self.dpi_y > 0) else max(self.dpi_x, self.dpi_y), 1),
            'pixel_width': self.pixel_width,
            'pixel_height': self.pixel_height,
            'print_width_inches': round(self.print_width_inches, 2),
            'print_height_inches': round(self.print_height_inches, 2),
            'file_size_kb': round(self.file_size_kb, 1),
            'sharpness_score': round(self.sharpness_score, 1),
            'is_blurry': self.is_blurry,
            'status': self.status,
            'issues': self.issues,
            'is_cited_in_text': self.is_cited_in_text,
            'override_reason': self.override_reason or '',
            'reviewed_by_name': self.reviewer.name if self.reviewer else '',
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''
        }

