"""UPG-01 : a database created before the cascade existed is upgraded in place without losing data."""
import sqlite3

from sqlalchemy import inspect

from lifeos.db import init_db, make_engine


def test_upg01_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE pillars (code VARCHAR(8) PRIMARY KEY, name VARCHAR(64), purpose TEXT, subdomains JSON,
                              sort_order INTEGER);
        CREATE TABLE goals (id INTEGER PRIMARY KEY, code VARCHAR(32), title VARCHAR(200), description TEXT,
            pillar_code VARCHAR(8), secondary_pillars JSON, target_metric VARCHAR(200), baseline VARCHAR(200),
            target_value VARCHAR(200), current_value VARCHAR(200), progress_evidence TEXT, target_date DATE,
            status VARCHAR(16), created_at DATETIME, updated_at DATETIME);
        INSERT INTO goals (id, code, title, pillar_code, status) VALUES (1, 'FIN-G001', 'Keep me', 'FIN', 'active');
    """)
    con.commit()
    con.close()
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    init_db(engine)  # idempotent
    cols = {c["name"] for c in inspect(engine).get_columns("goals")}
    assert "vision_id" in cols
    assert "key_result_id" in {c["name"] for c in inspect(engine).get_columns("tasks")}
    with engine.connect() as c:
        assert c.exec_driver_sql("select title from goals where id=1").scalar() == "Keep me"
