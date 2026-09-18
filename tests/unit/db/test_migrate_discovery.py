from __future__ import annotations

from octop.infra.db.migrate import _discover, _split_pg_sql


def test_discover_sqlite_excludes_pg_files():
    files = _discover("sqlite")
    names = [p.name for _, p in files]
    assert any(n == "001_initial.sql" for n in names)
    assert not any(n.endswith(".pg.sql") for n in names)


def test_discover_postgresql_only_pg_files():
    files = _discover("postgresql")
    names = [p.name for _, p in files]
    assert "001_initial.pg.sql" in names
    assert not any(n.endswith(".sql") and not n.endswith(".pg.sql") for n in names)


def test_split_pg_sql_preserves_function_bodies_and_quoted_semicolons():
    sql = """
    -- migration header
    CREATE TABLE example (value TEXT);
    CREATE FUNCTION example_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $body$
    BEGIN
      NEW.value := 'kept;inside';
      RETURN NEW;
    END;
    $body$;
    CREATE TRIGGER example_trigger BEFORE INSERT ON example
      FOR EACH ROW EXECUTE FUNCTION example_fn();
    """

    statements = _split_pg_sql(sql)

    assert len(statements) == 3
    assert statements[0].endswith("CREATE TABLE example (value TEXT)")
    assert "'kept;inside'" in statements[1]
    assert "RETURN NEW;" in statements[1]
    assert statements[2].startswith("CREATE TRIGGER example_trigger")


def test_split_pg_sql_rejects_unterminated_dollar_quote():
    import pytest

    with pytest.raises(ValueError, match="unterminated"):
        _split_pg_sql("CREATE FUNCTION broken() RETURNS void AS $$ BEGIN;")
