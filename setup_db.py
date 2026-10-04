#!/usr/bin/env python3
"""Run once as the RDS master user to create a restricted ETL login/table."""
import getpass
import os
from pathlib import Path
from pipeline import connect_db


def main():
    from psycopg2 import sql
    admin_user = os.environ.get('PGUSER')
    if not admin_user or admin_user == 'sales_etl':
        raise SystemExit('Set PGUSER to your RDS master username for setup only')
    admin_password = getpass.getpass('RDS master password: ')
    app_password = getpass.getpass('Choose a new sales_etl password (at least 12 characters): ')
    if len(app_password) < 12 or app_password != getpass.getpass('Confirm sales_etl password: '):
        raise SystemExit('Password is too short or confirmation differs')
    connection = connect_db(user=admin_user, password=admin_password)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'sales_etl'")
                if cursor.fetchone():
                    cursor.execute(sql.SQL('ALTER ROLE sales_etl LOGIN PASSWORD {}').format(sql.Literal(app_password)))
                else:
                    cursor.execute(sql.SQL('CREATE ROLE sales_etl LOGIN PASSWORD {}').format(sql.Literal(app_password)))
                cursor.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO sales_etl').format(
                    sql.Identifier(os.environ.get('PGDATABASE', 'retail'))))
                cursor.execute(Path(__file__).with_name('schema.sql').read_text())
    finally:
        connection.close()
    print('Setup complete. Now set PGUSER=sales_etl and use its password for the pipeline.')


if __name__ == '__main__':
    main()
