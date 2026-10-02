from app.core.security import hash_password
from app.db.models import User
from app.main import _seed_admin_user, settings


def test_seed_admin_creates_user_when_not_exists(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "seed_admin@example.com")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "seedpass123")

    _seed_admin_user(db_session)

    user = db_session.query(User).filter_by(email="seed_admin@example.com").first()
    assert user is not None
    assert user.is_admin is True
    assert user.is_active is True
    assert user.is_verified is True


def test_seed_admin_promotes_existing_non_admin_user(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "existing_admin@example.com")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "seedpass123")

    db_session.add(User(
        email="existing_admin@example.com",
        hashed_password=hash_password("oldpass123"),
        is_admin=False,
        is_active=True,
        is_verified=True,
    ))
    db_session.commit()

    _seed_admin_user(db_session)

    users = db_session.query(User).filter_by(email="existing_admin@example.com").all()
    assert len(users) == 1
    assert users[0].is_admin is True


def test_seed_admin_skips_when_env_not_set(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")

    _seed_admin_user(db_session)

    assert db_session.query(User).count() == 0
