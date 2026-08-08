"""One-time data migration: copies real data from the SQLite database into a
Postgres database, preserving primary keys so foreign-key relationships stay
intact, then advances Postgres's identity sequences to match.

Safe to re-run: it wipes the target's content tables (RESTART IDENTITY) before
reloading, so an accidental app-triggered auto-seed on the target is not a
problem — this script always ends with exactly what's in the source SQLite file.

Usage:
    python3 migrate_to_postgres.py <sqlite_path> <postgres_dsn> [--yes]

Example:
    python3 migrate_to_postgres.py \\
        ~/Documents/quotecure_data/quotecure.db \\
        postgresql://localhost/quotecure_dev
"""
import os
import sys
import sqlite3

import psycopg2
import psycopg2.extras

# Content tables in FK-logical order (parents before children).
# schema_migrations is intentionally excluded — it reflects which code-level
# migrations have run against THIS Postgres schema, not user data, and is
# already populated correctly by the bootstrap step below.
TABLES = [
    'subs', 'surface_applicators', 'roles', 'work_types', 'suppliers',
    'surface_manufacturers', 'company_settings', 'packages', 'surface_additives',
    'commission_policy', 'users', 'sub_rates', 'surface_products', 'materials',
    'surface_applicator_rates', 'qualifiers', 'package_items', 'quotes',
    'quote_line_items', 'payment_schedules', 'quote_template',
]

# Table -> identity primary key column, or None for natural-key (TEXT) PKs
# that don't use a sequence.
IDENTITY_PK = {
    'subs': None, 'surface_applicators': None,
    'roles': 'role_id', 'work_types': 'work_type_id', 'suppliers': 'supplier_id',
    'surface_manufacturers': 'manufacturer_id', 'company_settings': 'id',
    'packages': 'package_id', 'surface_additives': 'additive_id',
    'commission_policy': 'id', 'users': 'user_id', 'sub_rates': 'id',
    'surface_products': 'product_id', 'materials': 'material_id',
    'surface_applicator_rates': 'id', 'qualifiers': 'qualifier_id',
    'package_items': 'id', 'quotes': 'quote_id', 'quote_line_items': 'id',
    'payment_schedules': 'id', 'quote_template': 'id',
}

# quote_id -> quotes, work_type_id -> work_types, etc. for post-load orphan checks.
FK_CHECKS = [
    ('users', 'role_id', 'roles', 'role_id'),
    ('sub_rates', 'work_type_id', 'work_types', 'work_type_id'),
    ('materials', 'supplier_id', 'suppliers', 'supplier_id'),
    ('surface_products', 'manufacturer_id', 'surface_manufacturers', 'manufacturer_id'),
    ('surface_applicator_rates', 'product_id', 'surface_products', 'product_id'),
    ('package_items', 'package_id', 'packages', 'package_id'),
    ('package_items', 'work_type_id', 'work_types', 'work_type_id'),
    ('qualifiers', 'work_type_id', 'work_types', 'work_type_id'),
    ('quote_template', 'work_type_id', 'work_types', 'work_type_id'),
    ('quote_line_items', 'quote_id', 'quotes', 'quote_id'),
    ('payment_schedules', 'quote_id', 'quotes', 'quote_id'),
]


def bootstrap_schema(postgres_dsn):
    """Create the schema on the target Postgres DB using the app's own
    init_db()/run_migrations()/init_* functions — the same code path the
    app uses on first request — so the schema is guaranteed to match what
    the app expects, with zero separate schema definition to maintain."""
    os.environ['DATABASE_URL'] = postgres_dsn
    import database
    database.init_db()
    conn = database.get_db()
    database.run_migrations(conn)
    database.init_auth(conn)
    database.init_materials(conn)
    database.init_company_settings(conn)
    database.init_pebble_pros_surfaces(conn)
    conn.close()
    print("Schema bootstrapped on target Postgres database.")


def wipe_content_tables(pg_conn):
    cur = pg_conn.cursor()
    cur.execute(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY")
    pg_conn.commit()
    print(f"Wiped {len(TABLES)} content tables on target (schema_migrations left untouched).")


def copy_table(sqlite_conn, pg_conn, table):
    scur = sqlite_conn.cursor()
    scur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in scur.fetchall()]
    col_list = ', '.join(cols)

    scur.execute(f"SELECT {col_list} FROM {table}")
    rows = scur.fetchall()
    if not rows:
        print(f"  {table}: 0 rows (nothing to copy)")
        return 0

    pcur = pg_conn.cursor()
    psycopg2.extras.execute_values(
        pcur,
        f"INSERT INTO {table} ({col_list}) VALUES %s",
        [tuple(row) for row in rows],
    )
    pg_conn.commit()
    print(f"  {table}: copied {len(rows)} rows")
    return len(rows)


def advance_sequences(pg_conn):
    cur = pg_conn.cursor()
    for table, pk in IDENTITY_PK.items():
        if pk is None:
            continue
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"COALESCE((SELECT MAX({pk}) FROM {table}), 1), "
            f"(SELECT MAX({pk}) FROM {table}) IS NOT NULL)",
            (table, pk),
        )
    pg_conn.commit()
    print("Advanced identity sequences on all tables with a serial primary key.")


def verify_row_counts(sqlite_conn, pg_conn):
    print("\nRow count verification:")
    all_ok = True
    for table in TABLES:
        s_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pcur = pg_conn.cursor()
        pcur.execute(f"SELECT COUNT(*) FROM {table}")
        p_count = pcur.fetchone()[0]
        status = "OK" if s_count == p_count else "MISMATCH"
        if s_count != p_count:
            all_ok = False
        print(f"  {table:28s} sqlite={s_count:5d}  postgres={p_count:5d}  [{status}]")
    return all_ok


def verify_fk_integrity(pg_conn):
    print("\nForeign-key integrity checks (orphan rows should be 0):")
    all_ok = True
    cur = pg_conn.cursor()
    for child, child_col, parent, parent_col in FK_CHECKS:
        cur.execute(
            f"SELECT COUNT(*) FROM {child} c "
            f"LEFT JOIN {parent} p ON c.{child_col} = p.{parent_col} "
            f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL"
        )
        orphans = cur.fetchone()[0]
        status = "OK" if orphans == 0 else "ORPHANS FOUND"
        if orphans != 0:
            all_ok = False
        print(f"  {child}.{child_col} -> {parent}.{parent_col}: {orphans} orphans [{status}]")
    return all_ok


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    sqlite_path = os.path.expanduser(sys.argv[1])
    postgres_dsn = sys.argv[2]
    auto_yes = '--yes' in sys.argv

    if not os.path.exists(sqlite_path):
        print(f"SQLite file not found: {sqlite_path}")
        sys.exit(1)

    if not auto_yes:
        confirm = input(
            f"This will WIPE all content tables in the target Postgres database and "
            f"reload from {sqlite_path}. Continue? [y/N] "
        )
        if confirm.strip().lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    print(f"\nSource (read-only): {sqlite_path}")
    print(f"Target: {postgres_dsn}\n")

    bootstrap_schema(postgres_dsn)

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    pg_conn = psycopg2.connect(postgres_dsn)

    wipe_content_tables(pg_conn)

    print("\nCopying tables:")
    for table in TABLES:
        copy_table(sqlite_conn, pg_conn, table)

    advance_sequences(pg_conn)

    counts_ok = verify_row_counts(sqlite_conn, pg_conn)
    fks_ok = verify_fk_integrity(pg_conn)

    sqlite_conn.close()
    pg_conn.close()

    print()
    if counts_ok and fks_ok:
        print("Migration verified successfully — row counts match and no orphaned foreign keys.")
    else:
        print("MIGRATION HAS ISSUES — see MISMATCH/ORPHANS FOUND lines above. Do not proceed to production.")
        sys.exit(1)


if __name__ == '__main__':
    main()
