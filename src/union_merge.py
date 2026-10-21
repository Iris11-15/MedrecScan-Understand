import json, sys, os, copy
D, TMP, N = sys.argv[1], sys.argv[2], int(sys.argv[3])
runs = [json.load(open(os.path.join(TMP, 'u%d.json' % i), encoding='utf-8')) for i in range(1, N + 1)]
base = copy.deepcopy(runs[0]); seen = {}; merged = []
for r in runs:
    for e in ((r.get('merged') or {}).get('条目') or []):
        k = (str(e.get('项目')), str(e.get('值')), str(e.get('出处')))
        if k in seen:
            seen[k]['_出现次数'] = seen[k].get('_出现次数', 1) + 1
            continue
        e = dict(e); e['_出现次数'] = 1; seen[k] = e; merged.append(e)
base['merged']['条目'] = merged
base['stats'] = dict(base.get('stats') or {})
base['stats'].update({'条目': len(merged), '并集轮数': N})
json.dump(base, open(os.path.join(D, 'structured.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('  union %d items' % len(merged))
