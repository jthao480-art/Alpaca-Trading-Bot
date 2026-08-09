import json
from pathlib import Path

ledger = json.loads(Path('data/trade_ledger.json').read_text())

all_entries = []
open_count = 0
closed_no_price = 0

for symbol, entries in ledger.items():
    for e in entries:
        status = e.get('status', '')
        if status == 'open':
            open_count += 1
            continue
        if status == 'closed':
            ep = float(e.get('entry_price') or 0)
            xp = float(e.get('exit_price') or 0)
            reason = e.get('close_reason', 'unknown')
            agent = e.get('strategy', 'unknown')
            score = e.get('metadata', {}).get('score', 0)
            if ep <= 0:
                closed_no_price += 1
                continue
            if xp <= 0:
                closed_no_price += 1
                all_entries.append({'symbol': symbol, 'pnl_pct': None, 'agent': agent, 'score': score, 'reason': reason})
                continue
            pnl_pct = (xp - ep) / ep * 100
            all_entries.append({'symbol': symbol, 'pnl_pct': pnl_pct, 'agent': agent, 'score': score, 'reason': reason})

print(f'Open entries: {open_count}')
print(f'Closed entries (no exit price): {closed_no_price}')

priced = [e for e in all_entries if e['pnl_pct'] is not None]
wins = [e for e in priced if e['pnl_pct'] > 0]
losses = [e for e in priced if e['pnl_pct'] <= 0]

print(f'Closed with price: {len(priced)}')
if priced:
    print(f'Winners: {len(wins)} ({len(wins)/len(priced)*100:.0f}%)')
    print(f'Losers: {len(losses)} ({len(losses)/len(priced)*100:.0f}%)')
    if wins:
        print(f'Avg win: +{sum(w["pnl_pct"] for w in wins)/len(wins):.2f}%')
    if losses:
        print(f'Avg loss: {sum(l["pnl_pct"] for l in losses)/len(losses):.2f}%')

    print()
    print('By exit reason:')
    reasons = {}
    for e in priced:
        r = e['reason']
        reasons.setdefault(r, {'wins': 0, 'losses': 0})
        if e['pnl_pct'] > 0:
            reasons[r]['wins'] += 1
        else:
            reasons[r]['losses'] += 1
    for r, d in sorted(reasons.items()):
        total_r = d['wins'] + d['losses']
        print(f'  {r}: {d["wins"]}W/{d["losses"]}L ({d["wins"]/total_r*100:.0f}% win rate)')

    print()
    print('By agent:')
    agents = {}
    for e in priced:
        a = e['agent']
        agents.setdefault(a, {'wins': 0, 'losses': 0, 'pnl': []})
        if e['pnl_pct'] > 0:
            agents[a]['wins'] += 1
        else:
            agents[a]['losses'] += 1
        agents[a]['pnl'].append(e['pnl_pct'])
    for agent, data in sorted(agents.items()):
        total_a = data['wins'] + data['losses']
        avg_pnl = sum(data['pnl']) / len(data['pnl'])
        print(f'  {agent}: {data["wins"]}W/{data["losses"]}L avg={avg_pnl:.2f}%')
