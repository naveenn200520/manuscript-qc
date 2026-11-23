import os
from io import BytesIO

import pytest
from docx import Document

original_database_url = os.environ.get('DATABASE_URL')
os.environ['DATABASE_URL'] = 'sqlite://'

from app import app, db
from models import ErrorRecord, Manuscript, Project, Team, User, UserTeam

if original_database_url is None:
    os.environ.pop('DATABASE_URL', None)
else:
    os.environ['DATABASE_URL'] = original_database_url

app.config['TESTING'] = True


@pytest.fixture(scope='module')
def admin_client():
    assert app.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite://'
    with app.app_context():
        admin = User.query.filter_by(role='admin').first()
        team = Team.query.first()
        assert admin is not None and team is not None
        admin_id = admin.id
        team_id = team.id

    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_id)
        session['_fresh'] = True

    yield client, team_id

    with app.app_context():
        db.session.remove()


def test_admin_can_create_user_with_selected_team(admin_client):
    client, team_id = admin_client
    response = client.post(
        '/admin/users',
        data={
            'name': 'Pipeline Test User',
            'email': 'pipeline-test-user@example.test',
            'employee_code': 'PIPELINE-TEST-USER',
            'password': 'temporary-test-password',
            'role': 'staff',
            'team_ids': [str(team_id)],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b'User registered successfully.' in response.data
    with app.app_context():
        user = User.query.filter_by(email='pipeline-test-user@example.test').first()
        assert user is not None
        assert UserTeam.query.filter_by(user_id=user.id, team_id=team_id).first() is not None


def create_review_project(team_id, original_text, working_text, old_error=None):
    with app.app_context():
        project = Project(name='Pipeline Test', team_id=team_id)
        db.session.add(project)
        db.session.flush()
        manuscript = Manuscript(
            project_id=project.id,
            original_text=original_text,
            working_text=working_text,
        )
        db.session.add(manuscript)
        db.session.flush()
        if old_error:
            db.session.add(ErrorRecord(
                manuscript_id=manuscript.id,
                error_type=old_error['error_type'],
                severity='CRITICAL',
                category='CRITICAL',
                start_offset=0,
                end_offset=1,
                description=old_error['description'],
                original_text=old_error['original_text'],
                edited_text=old_error['edited_text'],
                status=old_error['status'],
            ))
        db.session.commit()
        return project.id, manuscript.id


def make_docx(text):
    document = Document()
    for paragraph in text.splitlines():
        document.add_paragraph(paragraph)
    content = BytesIO()
    document.save(content)
    content.seek(0)
    return content


def test_unreadable_final_pdf_is_rejected_not_compared_as_author_copy(admin_client):
    client, team_id = admin_client
    author_text = 'An example article with readable content.'
    response = client.post(
        '/projects/new',
        data={
            'team_id': str(team_id),
            'author_extracted_text': author_text,
            'manuscript_file': (BytesIO(author_text.encode('utf-8')), 'author.txt'),
            'xml_file': (BytesIO(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF'), 'final.pdf'),
        },
        headers={
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json',
        },
    )

    assert response.status_code == 400
    assert response.json['success'] is False
    assert 'final manuscript file could not be read' in response.json['error'].lower()
    with app.app_context():
        assert Project.query.count() == 0


def test_docx_upload_runs_qc_on_extracted_final_text(admin_client):
    client, team_id = admin_client
    original = """A Correct Title

Jane Doe

Department A

Corresponding author: jane@example.org

References
[1] Doe J. Example 2020.
"""
    edited = original.replace('A Correct Title', 'A Changed Title')
    response = client.post(
        '/projects/new',
        data={
            'team_id': str(team_id),
            'manuscript_file': (make_docx(original), 'author.docx'),
            'xml_file': (make_docx(edited), 'final.docx'),
        },
        headers={
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json',
        },
    )

    assert response.status_code == 200
    assert response.json['success'] is True
    with app.app_context():
        manuscript = Project.query.filter_by(id=response.json['project_id']).first().manuscripts[0]
        errors = ErrorRecord.query.filter_by(
            manuscript_id=manuscript.id,
            error_type='ARTICLE_TITLE_CHANGED',
        ).all()
        assert errors


def test_empty_xml_final_is_rejected_instead_of_falling_back_to_author(admin_client):
    client, team_id = admin_client
    author_text = 'An example article with readable content.'
    with app.app_context():
        project_count = Project.query.count()
    response = client.post(
        '/projects/new',
        data={
            'team_id': str(team_id),
            'author_extracted_text': author_text,
            'manuscript_file': (BytesIO(author_text.encode('utf-8')), 'author.txt'),
            'xml_file': (BytesIO(b'<?xml version="1.0"?><document/>'), 'final.xml'),
        },
        headers={
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json',
        },
    )

    assert response.status_code == 400
    assert response.json['success'] is False
    assert 'contains no readable text' in response.json['error'].lower()
    with app.app_context():
        assert Project.query.count() == project_count


def test_corresponding_author_mismatch_is_preserved_in_review(admin_client):
    client, team_id = admin_client
    original = """Effects of Novel Therapeutic Compounds on Health Outcomes

John Smith¹,²*

¹ Department of Medicine, University A
² Institute of Research, University B

* Corresponding author: John Smith, john.smith@university.edu

References
[1] Smith J. Medicine 2020.
[2] Research B. Institute 2021.
"""
    edited = original.replace('John Smith¹,²*', 'Jane Doe¹,²*')
    project_id, manuscript_id = create_review_project(team_id, original, edited)

    response = client.get(f'/projects/{project_id}/review')

    assert response.status_code == 200
    with app.app_context():
        errors = ErrorRecord.query.filter_by(
            manuscript_id=manuscript_id,
            error_type='CORRESPONDING_AUTHOR_NAME_MISMATCH',
        ).all()
        assert errors


def test_new_errors_are_open_after_previously_resolved_error(admin_client):
    client, team_id = admin_client
    original = """A Correct Title

Jane Doe

Department A

Corresponding author: jane@example.org

References
[1] Doe J. Example 2020.
"""
    edited = original.replace('A Correct Title', 'A New Incorrect Title')
    project_id, manuscript_id = create_review_project(
        team_id,
        original,
        edited,
        old_error={
            'error_type': 'ARTICLE_TITLE_CHANGED',
            'description': 'A different title error from the earlier review.',
            'original_text': 'Old title',
            'edited_text': 'Older title',
            'status': 'IGNORED',
        },
    )

    response = client.get(f'/projects/{project_id}/review')

    assert response.status_code == 200
    with app.app_context():
        title_errors = ErrorRecord.query.filter_by(
            manuscript_id=manuscript_id,
            error_type='ARTICLE_TITLE_CHANGED',
        ).all()
        assert title_errors
        assert any(error.status == 'OPEN' for error in title_errors)
