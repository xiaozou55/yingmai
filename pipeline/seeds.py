#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 种子人物筛选

实测结论：Top-N 热门片作种子会得到 93.7% 断开的网络（见 _work/数据体检报告.md），
改为**以人物为种子**：拉种子人物的作品全集，再取其作品里的其他主要演员。

本脚本不采数据，只回答一个问题：**这几位候选，谁的底子够厚、跨度够长、彼此连得上。**

用法:
  python seeds.py                      # 用内置的跨代际候选名单
  python seeds.py --names 成龙 周星驰 梁朝伟
  python seeds.py --proxy http://127.0.0.1:7897

输出:
  _work/种子候选.md
"""
import os, sys, re, json, time, argparse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from probe_tmdb import (load_key, make_session, api_get, WORK_DIR, has_cjk, log, KEY, ZH_LANGS)

# 跨代际候选：老 / 中 / 新三代，演员为主、导演少量（导演是把一个剧组连起来的关键节点）
DEFAULT_NAMES = [
    # 老一代（1970s–1990s 起）
    '成龙', '洪金宝', '周润发', '张曼玉', '巩俐', '葛优',
    # 中一代（1980s–2000s 起）
    '刘德华', '梁朝伟', '周星驰', '周迅',
    # 新一代（2010s 起）
    '张子枫', '易烊千玺', '周冬雨', '沈腾', '吴京',
    # 导演（跨代的连接器）
    '张艺谋', '王家卫', '徐克',
]


def pick_person(sess, name, refresh, offline):
    """搜人物，返回最佳候选。优先中文主名、优先 Acting/Directing。"""
    d = api_get(sess, '/search/person', refresh=refresh, offline=offline,
                query=name, language='zh-CN')
    if not d or not d.get('results'):
        return None, []
    cands = d['results'][:5]
    scored = []
    for c in cands:
        s = 0.0
        if c.get('name') == name:
            s += 3.0
        if has_cjk(c.get('name')):
            s += 2.0
        if c.get('known_for_department') in ('Acting', 'Directing'):
            s += 1.5
        s += min(c.get('popularity', 0) or 0, 100) / 100.0
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1], [c for _, c in scored[:3]]


def person_stats(sess, pid, refresh, offline):
    d = api_get(sess, f'/person/{pid}/combined_credits', refresh=refresh, offline=offline,
                language='zh-CN')
    if not d:
        return None
    cast = d.get('cast') or []
    crew = d.get('crew') or []
    # 只要电影（需求报告 1.2：限定电影，不扩展电视剧），且语言码 zh 或 cn
    keep = lambda c: (c.get('media_type') == 'movie'
                      and c.get('original_language') in ZH_LANGS)
    zh_cast = [c for c in cast if keep(c)]
    zh_crew = [c for c in crew if keep(c)]
    years = []
    for c in zh_cast + zh_crew:
        rd = c.get('release_date') or c.get('first_air_date') or ''
        if len(rd) >= 4 and rd[:4].isdigit():
            years.append(int(rd[:4]))
    return {
        'cast_total': len(cast),
        'crew_total': len(crew),
        'zh_cast': len(zh_cast),
        'zh_crew': len(zh_crew),
        'year_min': min(years) if years else None,
        'year_max': max(years) if years else None,
        'span': (max(years) - min(years)) if years else 0,
        'film_ids': {c['id'] for c in zh_cast},
    }


def main():
    ap = argparse.ArgumentParser(description='影脉 种子人物筛选')
    ap.add_argument('--names', nargs='*', default=DEFAULT_NAMES, help='候选人物名单')
    ap.add_argument('--proxy', default=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'),
                    help='代理地址，默认读 HTTPS_PROXY')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--offline', action='store_true')
    args = ap.parse_args()

    sess = make_session(args.proxy)
    rows = []
    for i, name in enumerate(args.names, 1):
        best, alts = pick_person(sess, name, args.refresh, args.offline)
        if not best:
            log(f'  [{i}/{len(args.names)}] {name} —— 搜不到')
            rows.append({'query': name, 'name': None})
            continue
        st = person_stats(sess, best['id'], args.refresh, args.offline)
        if not st:
            log(f'  [{i}/{len(args.names)}] {name} —— 取不到作品')
            rows.append({'query': name, 'name': best.get('name'), 'id': best['id']})
            continue
        st.update({'query': name, 'id': best['id'], 'name': best.get('name'),
                   'dept': best.get('known_for_department'),
                   'alt': [c.get('name') for c in alts]})
        rows.append(st)
        log(f"  [{i}/{len(args.names)}] {name:6s} → {best.get('name'):16s} "
            f"华语作品 {st['zh_cast']}+{st['zh_crew']:4d}  "
            f"年代 {st['year_min']}–{st['year_max']}")
        time.sleep(0.15)

    # 两两之间的共同华语作品数（种子之间连不连得上）
    ok = [r for r in rows if r.get('film_ids')]
    overlap = {}
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            n = len(ok[i]['film_ids'] & ok[j]['film_ids'])
            if n:
                overlap[(ok[i]['name'], ok[j]['name'])] = n

    os.makedirs(WORK_DIR, exist_ok=True)
    out = os.path.join(WORK_DIR, '种子候选.md')
    L = ['# 影脉 · 种子人物候选\n', f'实测时间：{datetime.datetime.now().isoformat(timespec="seconds")}\n']
    L.append('## 1. 候选人底子\n')
    L.append('| 查询 | TMDB 主名 | 部门 | 华语作品(演/导) | 活跃年代 | 跨度 | 全部作品 |\n')
    L.append('|---|---|---|---|---|---|---|')
    for r in rows:
        if not r.get('name'):
            L.append(f"| {r['query']} | ✗ 搜不到 | | | | | |")
            continue
        if not r.get('film_ids'):
            L.append(f"| {r['query']} | {r['name']} | {r.get('dept','')} | 无数据 | | | |")
            continue
        L.append(f"| {r['query']} | {r['name']} | {r.get('dept','')} | "
                 f"{r['zh_cast']}+{r['zh_crew']} | {r['year_min']}–{r['year_max']} | "
                 f"{r['span']} 年 | {r['cast_total']}+{r['crew_total']} |")
    L.append('')
    L.append('## 2. 种子之间的共同作品（图能不能连起来）\n')
    if overlap:
        L.append('| 人物对 | 共同华语作品数 |\n|---|---|')
        for (a, b), n in sorted(overlap.items(), key=lambda x: -x[1]):
            L.append(f'| {a} × {b} | {n} |')
        L.append('')
        connected = len({x for pair in overlap for x in pair})
        L.append(f'> {len(ok)} 位候选里 {connected} 位与其他候选有共同作品。'
                 f'共同作品越多，选他们当种子得到的图越稠密。\n')
    else:
        L.append('> ⚠ 候选之间**没有任何共同作品**。这批人各自成岛，'
                 '选他们当种子会得到多个互不相连的簇。需要补入连接型人物。\n')

    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    log(f'\n报告: {out}')
    log(f'候选间有共同作品的对数: {len(overlap)}')


if __name__ == '__main__':
    main()
