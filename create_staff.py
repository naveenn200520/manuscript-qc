from app import app, db
from models import User, Team, UserTeam
from werkzeug.security import generate_password_hash

with app.app_context():
    # Check if staff exists
    staff = User.query.filter_by(email='staff@qcplatform.com').first()
    if not staff:
        staff = User(name='Staff Member', email='staff@qcplatform.com', password_hash=generate_password_hash('password123'), role='staff')
        db.session.add(staff)
        db.session.commit()
        
        # Add staff to the default team
        team = Team.query.filter_by(name='Default Journal').first()
        if team:
            db.session.add(UserTeam(user_id=staff.id, team_id=team.id))
            db.session.commit()
        print("Staff user created successfully.")
    else:
        print("Staff user already exists.")
