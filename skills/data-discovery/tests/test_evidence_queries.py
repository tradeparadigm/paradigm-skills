"""Execute collectors against typed Parquet: no network or mocked query engine."""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import duckdb
import polars as pl

SKILLS = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recap = load('sql_recap', SKILLS / 'options-recap/scripts/collect_recap.py')
analysis = load('sql_analysis', SKILLS / 'block-analyst/scripts/collect_analysis.py')
START = dt.datetime(2026, 9, 8, 7, 5, 19, tzinfo=dt.timezone.utc)
END = START + dt.timedelta(hours=1)


def execute(tmp_path, name, rows, start=START, end=END):
    path = tmp_path / 'rows.parquet'
    pl.DataFrame(rows).write_parquet(path)
    query = next(q for q in recap.build_queries('BTC', start, end) if q.name == name)
    with duckdb.connect() as con:
        result = con.execute(query.sql.replace('__PATHS__', recap.sql_list([str(path)])))
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


def test_incomplete_valuation_is_not_a_total(tmp_path):
    rows = [dict(exchange='bybit-options', timestamp='2026-09-08T07:30:00Z',
                 symbol=f'BTC-{i}', side='buy', amount=1.0, price=2.0,
                 iv=0.62 if i else None, index_price=70000.0,
                 turnover_usd=2.0 if i else None, block_id=None, id=str(i))
            for i in range(30)]
    result = execute(tmp_path, 'option_trades_bybit-options', rows)
    aggregate = next(row for row in result if row['record_type'] == 'aggregate')
    assert aggregate['trade_count'] == 30
    assert aggregate['premium_turnover_usd'] is None
    assert aggregate['known_premium_turnover_usd'] == 58
    assert aggregate['missing_turnover_count'] == 1
    assert aggregate['iv_count'] == 29
    assert aggregate['sampled_trade_count'] == 25
    assert len(result) == 26


def surface_row(symbol, expiry, timestamp='2026-09-08T07:55:00Z'):
    return dict(exchange='deribit', timestamp=timestamp, symbol=symbol,
                expirationDate=expiry, strikePrice=70000.0, optionType='call',
                markIV=62.0, bestBidIV=61.0, bestAskIV=63.0, markPrice=0.1,
                bestBidPrice=0.09, bestAskPrice=0.11, delta=0.5,
                gamma=0.01, vega=1.0, theta=-1.0, openInterest=100.0,
                underlyingPrice=70000.0)


def test_missing_open_is_not_relabelled_and_all_expiries_survive(tmp_path):
    rows = [surface_row(f'expired-{i}', '2026-09-08T08:00:00Z') for i in range(35)]
    rows += [surface_row(f'active-{day}', f'2026-09-{day:02d}T08:00:00Z')
             for day in range(9, 29)]
    result = execute(tmp_path, 'option_surface_deribit', rows)
    assert {row['observation'] for row in result} == {'latest'}
    assert len({row['expirationDate'] for row in result}) == 20
    assert not any(row['symbol'].startswith('expired') for row in result)
    assert {row['snapshot_symbol_count'] for row in result} == {20}
    assert {row['selected_node_count'] for row in result} == {40}


def test_open_and_latest_are_independently_selected(tmp_path):
    rows = [surface_row('BTC-A', '2026-09-11T08:00:00Z', timestamp)
            for timestamp in ('2026-09-08T07:06:00Z', '2026-09-08T07:55:00Z')]
    result = execute(tmp_path, 'option_surface_deribit', rows)
    assert {(r['observation'], r['timestamp']) for r in result} == {
        ('window_open', '2026-09-08T07:06:00Z'), ('latest', '2026-09-08T07:55:00Z')}


def test_block_sample_does_not_cut_the_51st_leg(tmp_path):
    rows = [dict(exchange='deribit', timestamp='2026-09-08T07:30:00Z',
                 block_trade_id='block-A' if i < 3 else 'block-B',
                 block_rfq_id=None, trade_id=str(i)) for i in range(51)]
    result = execute(tmp_path, 'venue_blocks_deribit', rows)
    assert len(result) == 51
    assert sum(row['block_trade_id'] == 'block-A' for row in result) == 3


def test_single_bucket_can_supply_both_anchors(tmp_path):
    rows = [surface_row('BTC-A', '2026-09-11T08:00:00Z', '2026-09-08T07:06:00Z')]
    result = execute(tmp_path, 'option_surface_deribit', rows, end=START + dt.timedelta(minutes=2))
    assert {row['observation'] for row in result} == {'window_open', 'latest'}


def test_ids_are_case_sensitive_and_underscore_is_literal():
    with duckdb.connect() as con:
        con.execute("CREATE TABLE ids(rfq_id VARCHAR)")
        con.executemany('INSERT INTO ids VALUES (?)', [(x,) for x in
                        ('r_AbC', 'DRFQv2-r_AbC', 'GRFQ-r_AbC', 'r_abc', 'unrelated-rXAbC')])
        result = con.execute('SELECT rfq_id FROM ids WHERE ' +
                             analysis.rfq_predicate('rfq_id', 'r_AbC')).fetchall()
        assert {row[0] for row in result} == {'r_AbC', 'DRFQv2-r_AbC', 'GRFQ-r_AbC'}
        result = con.execute('SELECT rfq_id FROM ids WHERE ' +
                             analysis.rfq_predicate('rfq_id', 'DRFQv2-r_AbC')).fetchall()
        assert result == [('DRFQv2-r_AbC',)]
