import json, glob, os, sys
vers = sorted({os.path.basename(f)[3:-5] for f in glob.glob('out/ir_*.json')}, key=lambda v: tuple(map(int, v.split('.'))))
cells = {}; allids = set(); reasons = {}
for v in vers:
    ir = json.load(open(f'out/ir_{v}.json')); rep = json.load(open(f'out/conv_{v}/report.json'))['dags']
    load = json.load(open(f'out/conv_{v}/load.json')) if os.path.exists(f'out/conv_{v}/load.json') and os.path.getsize(f'out/conv_{v}/load.json') else {}
    for d in ir['dags']:
        did = d['dag_id']; allids.add(did); r = rep[did]
        cells[(did, v)] = ('LOADFAIL' if did in load else 'conv') if r['status'] == 'converted' else 'def'
        if r['status'] != 'converted': reasons[(did, v)] = r['reason'][:140]
print('%-24s' % 'dag', ' '.join(v.rjust(5) for v in vers))
for i in sorted(allids):
    print('%-24s' % i, ' '.join(cells.get((i, v), '-').rjust(5) for v in vers))
tot = {v: sum(1 for i in allids if cells.get((i, v)) == 'conv') for v in vers}; n = {v: sum(1 for i in allids if (i, v) in cells) for v in vers}
print('%-24s' % 'converted/extracted', ' '.join(f"{tot[v]}/{n[v]}".rjust(5) for v in vers))
if len(sys.argv) > 1:
    for (i, v), r in sorted(reasons.items()):
        if v == sys.argv[1]: print(' ', i, '::', r)
