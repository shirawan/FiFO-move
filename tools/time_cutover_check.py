"""Run through Odoo shell on a restored local test database, never production.

Set FIFO_TIMING_BATCH_ID to a reviewed move ID, and select a local database
whose name begins fifo_. The complete cutover lock/check is rolled back.
"""
import os
from time import perf_counter

assert env.cr.dbname.startswith('fifo_'), 'Use an isolated restored local test database named fifo_*'
batch = env['company.financial.cutover'].browse(int(os.environ['FIFO_TIMING_BATCH_ID'])).exists()
assert len(batch) == 1 and batch.state != 'done', 'Choose a reviewed unfinished test move'
batch._operator()
try:
    env.cr.execute("SET LOCAL lock_timeout = '100ms'")
    started = perf_counter()
    batch._lock()
    elapsed = perf_counter() - started
    print(f'Full cutover lock and freshness check: {elapsed:.3f} seconds; {len(batch._lock_tables())} tables.')
    print(f'Five attempts at this duration alone: {elapsed * 5:.3f} seconds; planning and posting add time.')
finally:
    env.cr.rollback()
