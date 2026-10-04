#!/usr/bin/env python3
"""S3 CSV -> validate -> private RDS PostgreSQL -> SQL reports -> S3."""
import argparse
import csv
import getpass
import json
import logging
import os
import subprocess
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

FIELDS = ['order_id', 'order_date', 'city', 'category', 'quantity', 'unit_price', 'status']
UPSERT = '''
INSERT INTO lab.orders (order_id, order_date, city, category, quantity, unit_price, status)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (order_id) DO UPDATE SET
    order_date = EXCLUDED.order_date, city = EXCLUDED.city,
    category = EXCLUDED.category, quantity = EXCLUDED.quantity,
    unit_price = EXCLUDED.unit_price, status = EXCLUDED.status
'''
REPORT = '''
SELECT order_date, city, COUNT(*) AS completed_orders, SUM(revenue) AS revenue_pkr
FROM lab.orders WHERE status = 'completed'
GROUP BY order_date, city ORDER BY order_date, city
'''


def clean_csv(path):
    clean, rejected, seen = [], [], set()
    total = duplicates = 0
    with path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError('Expected CSV header: ' + ','.join(FIELDS))
        for raw in reader:
            total += 1
            original = {key: raw.get(key) or '' for key in FIELDS}
            try:
                if None in raw or any(raw.get(key) is None for key in FIELDS):
                    raise ValueError('wrong number of columns')
                row = {key: value.strip() for key, value in original.items()}
                if any(not value for value in row.values()):
                    raise ValueError('missing required field')
                order_id = int(row['order_id'])
                if not 0 < order_id <= 9223372036854775807:
                    raise ValueError('order_id outside positive BIGINT range')
                order_date = date.fromisoformat(row['order_date'])
                if order_date.isoformat() != row['order_date']:
                    raise ValueError('date must be YYYY-MM-DD')
                quantity = int(row['quantity'])
                if not 1 <= quantity <= 1000000:
                    raise ValueError('quantity must be between 1 and 1000000')
                price = Decimal(row['unit_price'])
                if not price.is_finite() or not 0 <= price <= Decimal('9999999999.99'):
                    raise ValueError('unit_price outside allowed range')
                if price * 100 != (price * 100).to_integral_value():
                    raise ValueError('unit_price supports at most two decimal places')
                status = row['status'].lower()
                if status not in {'completed', 'pending', 'cancelled'}:
                    raise ValueError('unsupported status')
                if order_id in seen:
                    duplicates += 1
                    raise ValueError('duplicate order_id: first valid occurrence kept')
                seen.add(order_id)
                clean.append((order_id, order_date, row['city'].title(),
                              row['category'].title(), quantity, price, status))
            except (ValueError, InvalidOperation) as error:
                rejected.append({**original, 'reason': str(error)})
    return clean, rejected, {'input_rows': total, 'accepted_rows': len(clean),
                             'rejected_rows': len(rejected), 'duplicate_rows': duplicates}


def connect_db(user=None, password=None):
    import psycopg2
    host = os.environ.get('PGHOST')
    cert = os.environ.get('PGSSLROOTCERT')
    if not host or not cert or not Path(cert).is_file():
        raise ValueError('Set PGHOST to the RDS DNS endpoint and PGSSLROOTCERT to the CA file')
    return psycopg2.connect(
        host=host, port=5432, dbname=os.environ.get('PGDATABASE', 'retail'),
        user=user or os.environ.get('PGUSER', 'sales_etl'),
        password=password if password is not None else getpass.getpass('ETL database password: '),
        sslmode='verify-full', sslrootcert=cert, connect_timeout=5,
        application_name='vpc-retail-etl', options='-c statement_timeout=30000',
    )


def aws_s3(region, *arguments):
    subprocess.run(['aws', 's3', *map(str, arguments), '--region', region,
                    '--cli-connect-timeout', '5', '--cli-read-timeout', '15'],
                   check=True, env={**os.environ, 'AWS_PAGER': '', 'AWS_MAX_ATTEMPTS': '2'})


def write_csv(path, columns, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--batch-date', required=True)
    args = parser.parse_args()
    if date.fromisoformat(args.batch_date).isoformat() != args.batch_date:
        parser.error('batch-date must be YYYY-MM-DD')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    work = Path('work') / args.batch_date
    work.mkdir(parents=True, exist_ok=True)
    output = work / 'output'
    output.mkdir(exist_ok=True)
    aws_s3(args.region, 'cp', f's3://{args.bucket}/raw/{args.batch_date}/orders.csv', work / 'orders.csv')
    clean, rejected, metrics = clean_csv(work / 'orders.csv')
    assert metrics['input_rows'] == metrics['accepted_rows'] + metrics['rejected_rows']
    connection = connect_db()
    try:
        # All accepted inserts/updates commit together; an error rolls back the batch.
        with connection:
            with connection.cursor() as cursor:
                cursor.executemany(UPSERT, clean)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(REPORT)
                report = cursor.fetchall()
                cursor.execute('SELECT COUNT(*), COALESCE(SUM(revenue),0) FROM lab.orders')
                count, revenue = cursor.fetchone()
                metrics.update(database_rows=count, database_revenue_pkr=str(revenue))
    finally:
        connection.close()
    write_csv(output / 'daily_sales.csv',
              ['order_date', 'city', 'completed_orders', 'revenue_pkr'], report)
    write_csv(output / 'rejected_orders.csv', FIELDS + ['reason'],
              [[row[key] for key in FIELDS + ['reason']] for row in rejected])
    (output / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    prefix = f's3://{args.bucket}/reports/{args.batch_date}/'
    aws_s3(args.region, 'rm', prefix + '_SUCCESS.json')
    aws_s3(args.region, 'cp', str(output) + '/', prefix, '--recursive')
    aws_s3(args.region, 'cp', output / 'metrics.json', prefix + '_SUCCESS.json')
    logging.info('Complete: %s', json.dumps(metrics, sort_keys=True))


if __name__ == '__main__':
    main()
