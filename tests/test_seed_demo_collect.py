from unittest.mock import patch

from sqlalchemy.orm import sessionmaker


def _patch_seed_session(monkeypatch, test_engine):
    import scripts.seed_demo as seed_demo

    Session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(seed_demo, "SessionLocal", Session)
    monkeypatch.setattr(seed_demo, "_dart_company_name", lambda code, fallback: fallback)
    return seed_demo, Session


def _add_completed_task(Session, stock_code: str = "005930"):
    from app.db.models import CollectionTask

    db = Session()
    try:
        db.add(CollectionTask(stock_code=stock_code, status="completed", message="done"))
        db.commit()
    finally:
        db.close()


def test_collect_one_skips_completed_task_without_force(monkeypatch, test_engine):
    seed_demo, Session = _patch_seed_session(monkeypatch, test_engine)
    _add_completed_task(Session)

    with patch.object(seed_demo.DartService, "fetch_and_process_data") as mock_fetch:
        code, company_name, ok, reason = seed_demo._collect_one(
            "005930", "Samsung", "semiconductor", 1, 1
        )

    assert (code, company_name, ok, reason) == ("005930", "Samsung", True, None)
    mock_fetch.assert_not_called()


def test_collect_one_force_recollects_completed_task(monkeypatch, test_engine):
    seed_demo, Session = _patch_seed_session(monkeypatch, test_engine)
    _add_completed_task(Session)

    with patch.object(seed_demo.DartService, "fetch_and_process_data") as mock_fetch:
        seed_demo._collect_one("005930", "Samsung", "semiconductor", 1, 1, force=True)

    mock_fetch.assert_called_once_with("005930")
