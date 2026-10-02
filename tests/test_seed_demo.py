from unittest.mock import MagicMock, patch

from sqlalchemy.orm import sessionmaker


def _patch_seed_db(monkeypatch, test_engine):
    from app.db.database import Base
    import app.db.models  # noqa: F401
    import scripts.seed_demo as seed_demo

    Base.metadata.create_all(bind=test_engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(seed_demo, "SessionLocal", Session)
    return seed_demo, Session


def _patch_existing_model_paths(monkeypatch, tmp_path):
    from app.services import forecast_service

    nonfin = tmp_path / "model_nonfin.pkl"
    fin = tmp_path / "model_fin.pkl"
    nonfin.write_bytes(b"")
    fin.write_bytes(b"")

    monkeypatch.setattr(forecast_service, "MODEL_PATH", nonfin)
    monkeypatch.setattr(forecast_service, "MODEL_PATH_FIN", fin)


def test_seed_demo_idempotent_user_and_watchlist(monkeypatch, test_engine):
    seed_demo, Session = _patch_seed_db(monkeypatch, test_engine)
    monkeypatch.setattr(
        "sys.argv",
        ["seed_demo", "--skip-collect", "--searchlog-multiplier", "2"],
    )

    assert seed_demo.main() == 0
    assert seed_demo.main() == 0

    from app.db.models import SearchLog, User, Watchlist

    db = Session()
    try:
        users = db.query(User).filter(User.email == "demo@chatdart.local").all()
        assert len(users) == 1
        user = users[0]
        assert user.is_verified is True
        assert db.query(Watchlist).filter(Watchlist.user_id == user.id).count() == 5
        assert db.query(SearchLog).filter(SearchLog.user_id == user.id).count() == 20
    finally:
        db.close()


def test_seed_demo_skip_collect_does_not_call_dart(monkeypatch, test_engine):
    seed_demo, _ = _patch_seed_db(monkeypatch, test_engine)
    monkeypatch.setattr("sys.argv", ["seed_demo", "--skip-collect"])

    with patch.object(seed_demo.DartService, "fetch_and_process_data") as mock_fetch, \
         patch.object(seed_demo.DartService, "initialize") as mock_initialize:
        assert seed_demo.main() == 0

    mock_fetch.assert_not_called()
    mock_initialize.assert_not_called()


def test_ai_warm_codes_cover_all_demo_stocks():
    import scripts.seed_demo as seed_demo

    demo_codes = {code for code, _name, _group in seed_demo.DEMO_STOCKS}

    assert set(seed_demo.AI_WARM_CODES) == demo_codes


def test_demo_stocks_include_expanded_largecaps():
    import scripts.seed_demo as seed_demo

    codes = [code for code, _name, _group in seed_demo.DEMO_STOCKS]
    # 코드 중복 없음
    assert len(codes) == len(set(codes))
    # 신규 대형주 5종목 포함
    expected_new = {"373220", "207940", "068270", "005490", "006400"}
    assert expected_new.issubset(set(codes))
    # 총 15종목
    assert len(codes) == 15


def test_warm_ai_cache_disabled_returns_skip(monkeypatch, test_engine):
    seed_demo, Session = _patch_seed_db(monkeypatch, test_engine)
    db = Session()
    try:
        ok, status = seed_demo.warm_ai_cache(db, user=None, enabled=False)
    finally:
        db.close()

    assert ok is False
    assert "--warm-ai" in status


def test_warm_ai_cache_warms_all_codes(monkeypatch, test_engine, tmp_path):
    seed_demo, Session = _patch_seed_db(monkeypatch, test_engine)
    monkeypatch.setattr(seed_demo, "_config_value", lambda name: "test-key")
    _patch_existing_model_paths(monkeypatch, tmp_path)

    fake_resp = MagicMock(summary_available=True)
    calls = []

    def _fake(request, keyword, db, current_user):
        calls.append(keyword)
        return fake_resp

    db = Session()
    try:
        with patch("app.api.v1.finance.get_ai_analysis", side_effect=_fake):
            ok, status = seed_demo.warm_ai_cache(db, user=None, enabled=True)
    finally:
        db.close()

    assert ok is True
    assert calls == seed_demo.AI_WARM_CODES
    assert f"{len(seed_demo.AI_WARM_CODES)}종목 워밍" in status


def test_warm_ai_cache_reports_failures(monkeypatch, test_engine, tmp_path):
    seed_demo, Session = _patch_seed_db(monkeypatch, test_engine)
    monkeypatch.setattr(seed_demo, "_config_value", lambda name: "test-key")
    _patch_existing_model_paths(monkeypatch, tmp_path)

    def _fake(request, keyword, db, current_user):
        if keyword == seed_demo.AI_WARM_CODES[0]:
            raise RuntimeError("boom")
        return MagicMock(summary_available=True)

    db = Session()
    try:
        with patch("app.api.v1.finance.get_ai_analysis", side_effect=_fake):
            ok, status = seed_demo.warm_ai_cache(db, user=None, enabled=True)
    finally:
        db.close()

    assert ok is True
    assert "실패 1" in status
    assert seed_demo.AI_WARM_CODES[0] in status
