from app.core.security import hash_password, verify_password
from app.db.models import User
from app.main import _seed_demo_user, settings


def test_seed_demo_creates_user_when_not_exists(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_EMAIL", "seed_demo@example.com")
    monkeypatch.setattr(settings, "DEMO_PASSWORD", "local-demo-pass-123")

    _seed_demo_user(db_session)

    user = db_session.query(User).filter_by(email="seed_demo@example.com").first()
    assert user is not None
    assert user.is_admin is False
    assert user.is_active is True
    assert user.is_verified is True


def test_seed_demo_resets_password_when_user_exists(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_EMAIL", "dup_demo@example.com")
    monkeypatch.setattr(settings, "DEMO_PASSWORD", "local-demo-pass-123")
    db_session.add(User(
        email="dup_demo@example.com",
        hashed_password=hash_password("oldpass123!A"),
        is_admin=False,
        is_active=True,
        is_verified=True,
    ))
    db_session.commit()

    _seed_demo_user(db_session)

    user = db_session.query(User).filter_by(email="dup_demo@example.com").one()
    assert verify_password("local-demo-pass-123", user.hashed_password)
    assert user.is_active is True
    assert user.is_verified is True


def test_seed_demo_skips_when_env_not_set(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_EMAIL", "")
    monkeypatch.setattr(settings, "DEMO_PASSWORD", "")

    _seed_demo_user(db_session)

    assert db_session.query(User).count() == 0
