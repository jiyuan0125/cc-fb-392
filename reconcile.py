#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reconcile.py — 账户日终余额对账

复算口径：每账户每日 日终余额 = 前一日日终余额 + 当日全部流水。
  - 前一日的基准：有快照取快照，无快照（缺失日）取本程序复算值。
  - 首日起算自 accounts.csv 的开户余额。
  - 流水金额以元记，快照余额以分记；非利息流水金额恰到分，直接换算。
  - 利息（摘要=计提利息）金额到千分位（0.001 元 = 0.1 分），入账口径题面未给，
    本程序用候选舍入规则对全部账户全部日子试算，取不符数最少的口径。
    实测唯一自洽口径：利息按 四舍五入到分（ties 远离零）入账。

输出（与数据同目录，重跑按键 账户+日期 去重，不重复登记）：
  discrepancy_register.csv      差异登记：复算余额与快照不符的账户日子
  missing_snapshot_register.csv 快照缺失登记：有流水（或在册）但整日无快照的账户日子
  summary.csv                   按账户类型（个人/对公/内部）的对齐率汇总
"""
import csv
import os
import sys
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN

INTEREST = '计提利息'

# 候选利息舍入口径：元 -> 分
RULES = {
    '四舍五入(half_up)': ROUND_HALF_UP,
    '银行家舍入(half_even)': ROUND_HALF_EVEN,
    '向下取整(floor)': ROUND_FLOOR,
    '向上取整(ceil)': ROUND_CEILING,
    '截断(truncate)': ROUND_DOWN,
}


def to_cents(amount_yuan, rounding=None):
    """元(Decimal) -> 分(int)。rounding 为 None 时要求恰可到分。"""
    v = amount_yuan * 100
    if rounding is None:
        return int(v)  # 非利息流水恰到分，int() 即精确值
    return int(v.quantize(Decimal('1'), rounding=rounding))


def load_data(data_dir):
    with open(os.path.join(data_dir, 'accounts.csv'), newline='', encoding='utf-8') as f:
        accounts = {r['账户']: {'类型': r['类型'], '开户余额': int(r['开户余额'])}
                    for r in csv.DictReader(f)}
    with open(os.path.join(data_dir, 'daily_snapshots.csv'), newline='', encoding='utf-8') as f:
        snaps = {(r['账户'], r['日期']): int(r['余额']) for r in csv.DictReader(f)}
    ledger = defaultdict(list)  # (账户, 日期) -> [(笔序, 摘要, Decimal金额)]
    with open(os.path.join(data_dir, 'ledger.csv'), newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            ledger[(r['账户'], r['日期'])].append((int(r['笔序']), r['摘要'], Decimal(r['金额'])))
    for v in ledger.values():
        v.sort()
    dates = sorted({d for (_, d) in snaps} | {d for (_, d) in ledger})
    return accounts, snaps, ledger, dates


def run_pass(accounts, snaps, ledger, dates, rounding):
    """按给定利息口径复算全部账户全部日子。

    返回 (diffs, missing)：
      diffs   [(账户, 日期, 复算余额, 快照余额, 差值)]  —— 有快照但对不上
      missing [(账户, 日期, 复算余额)]                 —— 整日无快照
    """
    diffs, missing = [], []
    for acc, info in accounts.items():
        prev = info['开户余额']  # 第一天从开户余额起算
        for d in dates:
            entries = ledger.get((acc, d), [])
            non_interest = sum(to_cents(a) for _, s, a in entries if s != INTEREST)
            interest = sum(to_cents(a, rounding) for _, s, a in entries if s == INTEREST)
            calc = prev + non_interest + interest
            snap = snaps.get((acc, d))
            if snap is None:
                missing.append((acc, d, calc))
                prev = calc          # 缺快照日：以复算值续链
            else:
                if calc != snap:
                    diffs.append((acc, d, calc, snap, snap - calc))
                prev = snap          # 日终余额来历 = 前一天快照 + 当天流水
    return diffs, missing


def derive_interest_rule(accounts, snaps, ledger, dates):
    """从快照与流水对照中试出利息入账口径：不符数最少者。"""
    scores = {}
    for name, rule in RULES.items():
        diffs, _ = run_pass(accounts, snaps, ledger, dates, rule)
        scores[name] = len(diffs)
    best = min(scores, key=lambda k: scores[k])
    return best, RULES[best], scores


def merge_register(path, fieldnames, rows, key=('账户', '日期')):
    """登记落盘：与既有登记合并，按键去重（重跑不重复）。"""
    existing = {}
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                existing[tuple(r[k] for k in key)] = r
    for r in rows:
        existing[tuple(r[k] for k in key)] = r  # 同键以最新复算结果为准
    out = [existing[k] for k in sorted(existing)]
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)
    return len(rows), len(out)


def main(data_dir='.'):
    accounts, snaps, ledger, dates = load_data(data_dir)

    # 1) 试出利息口径
    best_name, best_rule, scores = derive_interest_rule(accounts, snaps, ledger, dates)
    print('利息入账口径试算（不符账户日数，越少越优）：')
    for name, n in sorted(scores.items(), key=lambda kv: kv[1]):
        mark = '  <== 采用' if name == best_name else ''
        print(f'  {name}: {n}{mark}')

    # 2) 用选定口径正式复算
    diffs, missing = run_pass(accounts, snaps, ledger, dates, best_rule)

    # 3) 差异登记 / 快照缺失登记（重跑去重）
    diff_rows = [{'账户': a, '日期': d, '复算余额': c, '快照余额': s, '差值': diff}
                 for a, d, c, s, diff in diffs]
    miss_rows = [{'账户': a, '日期': d, '复算余额': c} for a, d, c in missing]
    n_diff, t_diff = merge_register(os.path.join(data_dir, 'discrepancy_register.csv'),
                                    ['账户', '日期', '复算余额', '快照余额', '差值'], diff_rows)
    n_miss, t_miss = merge_register(os.path.join(data_dir, 'missing_snapshot_register.csv'),
                                    ['账户', '日期', '复算余额'], miss_rows)
    print(f'\n差异登记：本次 {n_diff} 笔，登记簿累计 {t_diff} 笔 -> discrepancy_register.csv')
    for r in diff_rows:
        print(f"  {r['账户']} {r['日期']} 复算={r['复算余额']} 快照={r['快照余额']} 差值={r['差值']:+d}分")
    print(f'快照缺失登记：本次 {n_miss} 笔，登记簿累计 {t_miss} 笔 -> missing_snapshot_register.csv')
    for r in miss_rows:
        print(f"  {r['账户']} {r['日期']} 复算={r['复算余额']}")

    # 4) 按账户类型汇总对齐率（分母=有快照的账户日；缺快照日另册登记不计入）
    stat = defaultdict(lambda: {'快照天数': 0, '对齐': 0, '差异': 0, '缺快照': 0})
    diff_keys = {(a, d) for a, d, *_ in diffs}
    for (a, d) in snaps:
        t = accounts[a]['类型']
        stat[t]['快照天数'] += 1
        if (a, d) in diff_keys:
            stat[t]['差异'] += 1
        else:
            stat[t]['对齐'] += 1
    for a, d, _ in missing:
        stat[accounts[a]['类型']]['缺快照'] += 1
    summary_path = os.path.join(data_dir, 'summary.csv')
    with open(summary_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['账户类型', '快照天数', '对齐天数', '差异天数', '缺快照天数', '对齐率'])
        print('\n按账户类型汇总：')
        for t in ['个人', '对公', '内部']:
            s = stat[t]
            rate = s['对齐'] / s['快照天数'] if s['快照天数'] else 0.0
            w.writerow([t, s['快照天数'], s['对齐'], s['差异'], s['缺快照'], f'{rate:.4%}'])
            print(f"  {t}: 快照{s['快照天数']}天 对齐{s['对齐']} 差异{s['差异']} "
                  f"缺快照{s['缺快照']} 对齐率{rate:.2%}")
    print(f'汇总已写入 {summary_path}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
