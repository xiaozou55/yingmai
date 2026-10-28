#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 关系构建与质量检测（F26 / F27）

与 collect.py 分离：采集负责"原始记录"，本脚本负责"从明细推导关系"（N04 采集/存储/图计算分离）。

关系规则（需求报告 3.1 业务规则）：
  · 每对人物在同一部电影中只计一次 —— 靠 (a<b) + 按电影唯一 天然保证
  · 合作边为无向边，频次为共同电影数
  · 排除自连接 —— 靠 a<b 保证
  · 图谱取样为每部电影前 10 位主演 + 导演（首期关系仅纳入演员和导演）

用法:
  python build.py relations      构建合作边与关系证据
  python build.py quality        质量检测，输出清单
  python build.py export         导出前端 graph.json
  python build.py stats          查看图的规模
"""
import os, sys, io, re, json, sqlite3, argparse, datetime

if (sys.stdout.encoding or '').lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, 'yingmai.db')
WEB = os.path.join(ROOT, 'web')
NOW = lambda: datetime.datetime.now().isoformat(timespec='seconds')
log = lambda *a: print(*a, flush=True)

TOP_CAST_N = 10     # 每部电影取前 N 位主演

DERIVED_SQL = """
DROP TABLE IF EXISTS relation_evidence;
DROP TABLE IF EXISTS relation;
DROP TABLE IF EXISTS graph_node;

-- 图谱节点：每部电影前 10 位主演 + 导演
CREATE TABLE graph_node (
    movie_id   INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    role       TEXT,                 -- cast / director
    PRIMARY KEY (movie_id, person_id)
);

-- 关系证据：每条边由哪些作品支撑（F12）
CREATE TABLE relation_evidence (
    a_person_id INTEGER NOT NULL,
    b_person_id INTEGER NOT NULL,
    movie_id    INTEGER NOT NULL,
    PRIMARY KEY (a_person_id, b_person_id, movie_id)
);

-- 合作边：无向、去重、频次=共同电影数
CREATE TABLE relation (
    a_person_id INTEGER NOT NULL,
    b_person_id INTEGER NOT NULL,
    weight      INTEGER NOT NULL,
    first_year  INTEGER,
    last_year   INTEGER,
    PRIMARY KEY (a_person_id, b_person_id)
);
"""


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def cmd_relations():
    c = conn()
    c.executescript(DERIVED_SQL)

    c.execute(f'''INSERT INTO graph_node(movie_id, person_id, role)
                  SELECT movie_id, person_id, 'director' FROM credit
                  WHERE job = 'Director'
                  ON CONFLICT DO NOTHING''')
    c.execute(f'''INSERT INTO graph_node(movie_id, person_id, role)
                  SELECT movie_id, person_id, 'cast' FROM credit
                  WHERE credit_type = 'cast' AND COALESCE(billing_order, 999) < {TOP_CAST_N}
                  ON CONFLICT DO NOTHING''')

    # a < b 同时保证了：无向去重、排除自连接、每对每片只计一次
    c.execute('''INSERT INTO relation_evidence(a_person_id, b_person_id, movie_id)
                 SELECT n1.person_id, n2.person_id, n1.movie_id
                 FROM graph_node n1 JOIN graph_node n2
                   ON n1.movie_id = n2.movie_id AND n1.person_id < n2.person_id''')

    c.execute('''INSERT INTO relation(a_person_id, b_person_id, weight, first_year, last_year)
                 SELECT e.a_person_id, e.b_person_id, COUNT(*),
                        MIN(m.release_year), MAX(m.release_year)
                 FROM relation_evidence e JOIN movie m ON m.id = e.movie_id
                 GROUP BY e.a_person_id, e.b_person_id''')

    c.execute('CREATE INDEX idx_rel_a ON relation(a_person_id)')
    c.execute('CREATE INDEX idx_rel_b ON relation(b_person_id)')
    c.execute('CREATE INDEX idx_ev_pair ON relation_evidence(a_person_id, b_person_id)')
    c.commit()
    log(f"图谱节点实例（人物×电影） {c.execute('SELECT COUNT(*) FROM graph_node').fetchone()[0]}")
    log(f"图谱去重人物               {c.execute('SELECT COUNT(DISTINCT person_id) FROM graph_node').fetchone()[0]}")
    log(f"合作边     {c.execute('SELECT COUNT(*) FROM relation').fetchone()[0]}")
    log(f"关系证据   {c.execute('SELECT COUNT(*) FROM relation_evidence').fetchone()[0]}")
    c.close()


def cmd_stats():
    c = conn()
    q = lambda s: c.execute(s).fetchone()[0]
    n_nodes = q('SELECT COUNT(DISTINCT person_id) FROM graph_node')
    n_edges = q('SELECT COUNT(*) FROM relation')
    dens = 2 * n_edges / (n_nodes * (n_nodes - 1)) if n_nodes > 1 else 0
    log(f'图谱节点   {n_nodes}')
    log(f'合作边     {n_edges}   密度 {dens:.5f}')
    log(f'平均度     {2 * n_edges / n_nodes:.1f}' if n_nodes else '')
    log('\n合作次数最多的 10 对：')
    for r in c.execute('''SELECT p1.name a, p2.name b, r.weight w, r.first_year f, r.last_year l
                          FROM relation r
                          JOIN person p1 ON p1.id = r.a_person_id
                          JOIN person p2 ON p2.id = r.b_person_id
                          ORDER BY r.weight DESC LIMIT 10'''):
        log(f"   {r['a']} × {r['b']}   {r['w']} 部   {r['f']}–{r['l']}")
    log('\n连接最多的人物（度中心性）Top 10：')
    for r in c.execute('''SELECT p.name, COUNT(*) d FROM (
                            SELECT a_person_id pid FROM relation
                            UNION ALL SELECT b_person_id FROM relation) t
                          JOIN person p ON p.id = t.pid
                          GROUP BY t.pid ORDER BY d DESC LIMIT 10'''):
        log(f"   {r['name']:14s} {r['d']} 位合作者")
    c.close()


def cmd_quality():
    c = conn()
    q = lambda s: c.execute(s).fetchone()[0]
    issues = []

    def chk(name, n, detail=''):
        flag = '✓' if n == 0 else '!'
        log(f'  {flag} {name:34s} {n:6d}  {detail}')
        if n:
            issues.append((name, n, detail))

    log('【1】完整性')
    chk('电影缺发行年份', q("SELECT COUNT(*) FROM movie WHERE release_year IS NULL"))
    chk('电影缺中文标题', q("SELECT COUNT(*) FROM movie WHERE title IS NULL OR title=''"))
    chk('人物缺姓名', q("SELECT COUNT(*) FROM person WHERE name IS NULL OR name=''"))
    chk('演职员缺人物 ID', q("SELECT COUNT(*) FROM credit WHERE person_id IS NULL"))
    chk('cast 缺番位 order', q("SELECT COUNT(*) FROM credit WHERE credit_type='cast' AND billing_order IS NULL"))

    log('\n【2】关系规则（应为 0）')
    chk('自连接边', q('SELECT COUNT(*) FROM relation WHERE a_person_id = b_person_id'))
    chk('无向边未归一（a 未小于 b）', q('SELECT COUNT(*) FROM relation WHERE a_person_id >= b_person_id'))
    chk('重复边（同一对出现多行）', q('''SELECT COUNT(*) FROM (
            SELECT a_person_id,b_person_id FROM relation
            GROUP BY 1,2 HAVING COUNT(*)>1)'''))
    chk('同对同片重复证据', q('''SELECT COUNT(*) FROM (
            SELECT a_person_id,b_person_id,movie_id,COUNT(*) n FROM relation_evidence
            GROUP BY 1,2,3 HAVING n>1)'''))

    log('\n【3】一致性')
    chk('证据引用了不存在的电影', q('''SELECT COUNT(*) FROM relation_evidence e
            WHERE NOT EXISTS (SELECT 1 FROM movie m WHERE m.id=e.movie_id)'''))
    chk('合作边无对应证据', q('''SELECT COUNT(*) FROM relation r
            WHERE NOT EXISTS (SELECT 1 FROM relation_evidence e
                              WHERE e.a_person_id=r.a_person_id AND e.b_person_id=r.b_person_id)'''))
    bad_w = q('''SELECT COUNT(*) FROM relation r
                 WHERE r.weight <> (SELECT COUNT(*) FROM relation_evidence e
                     WHERE e.a_person_id=r.a_person_id AND e.b_person_id=r.b_person_id)''')
    chk('频次与证据数不符', bad_w)
    chk('孤立图谱节点（无任何合作边）', q('''SELECT COUNT(*) FROM (SELECT DISTINCT person_id FROM graph_node) g
            WHERE NOT EXISTS (SELECT 1 FROM relation r
                              WHERE r.a_person_id=g.person_id OR r.b_person_id=g.person_id)'''))

    log('\n【4】数据源已知问题')
    langs = dict(c.execute('SELECT original_language, COUNT(*) FROM movie GROUP BY 1').fetchall())
    log(f'  语言码分布：{langs}')
    odd = [k for k in langs if k not in ('zh', 'cn')]
    chk('出现 zh/cn 之外的语言码', len(odd), str(odd))
    chk('重复电影 ID', q('SELECT COUNT(*) FROM (SELECT id FROM movie GROUP BY id HAVING COUNT(*)>1)'))
    chk('重复人物 ID', q('SELECT COUNT(*) FROM (SELECT id FROM person GROUP BY id HAVING COUNT(*)>1)'))
    dup_name = q('''SELECT COUNT(*) FROM (SELECT name FROM person
                     WHERE name IS NOT NULL GROUP BY name HAVING COUNT(*)>1)''')
    log(f'  ℹ 同名不同 ID 的人物：{dup_name} 组（按 ID 关联，不合并，仅作提示）')

    log('\n【5】样本范围')
    r = c.execute('SELECT MIN(release_year) a, MAX(release_year) b, COUNT(*) n FROM movie').fetchone()
    log(f"  电影 {r['n']} 部，年份 {r['a']}–{r['b']}")
    hits = c.execute('SELECT seed_hits, COUNT(*) n FROM movie GROUP BY 1 ORDER BY 1 DESC').fetchall()
    log('  种子命中数分布：' + '、'.join(f"{h['seed_hits']}粒 {h['n']}部" for h in hits))

    log(f'\n{"="*54}')
    log(f'共 {len(issues)} 类问题' if issues else '全部检查通过')
    c.close()


def cmd_export(min_weight=1):
    c = conn()
    os.makedirs(WEB, exist_ok=True)
    nodes = {}
    for r in c.execute('''SELECT p.id, p.name, COUNT(*) deg
                          FROM (SELECT a_person_id pid FROM relation
                                UNION ALL SELECT b_person_id FROM relation) t
                          JOIN person p ON p.id = t.pid
                          GROUP BY t.pid'''):
        nodes[r['id']] = {'id': r['id'], 'name': r['name'], 'degree': r['deg']}
    edges = []
    for r in c.execute('''SELECT a_person_id a, b_person_id b, weight w,
                                 first_year f, last_year l
                          FROM relation WHERE weight >= ? ORDER BY weight DESC''', (min_weight,)):
        if r['a'] in nodes and r['b'] in nodes:
            edges.append({'source': r['a'], 'target': r['b'], 'weight': r['w'],
                          'first': r['f'], 'last': r['l']})
    # 种子人物标记
    seeds = {r['person_id'] for r in c.execute('SELECT person_id FROM seed')}
    for n in nodes.values():
        n['seed'] = n['id'] in seeds

    data = {'generated_at': NOW(), 'source': 'TMDB',
            'scope': '华语片（original_language zh/cn）· 每部前 10 主演 + 导演',
            'nodes': list(nodes.values()), 'edges': edges}
    p = os.path.join(WEB, 'graph.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    log(f'导出 {p}：{len(nodes)} 节点 / {len(edges)} 边（weight≥{min_weight}）')
    c.close()


def main():
    ap = argparse.ArgumentParser(description='影脉 关系构建与质量检测')
    ap.add_argument('cmd', choices=['relations', 'quality', 'export', 'stats'])
    ap.add_argument('--min-weight', type=int, default=1, help='export 时的最低合作次数')
    a = ap.parse_args()
    if a.cmd == 'relations':
        cmd_relations()
    elif a.cmd == 'quality':
        cmd_quality()
    elif a.cmd == 'export':
        cmd_export(a.min_weight)
    else:
        cmd_stats()


if __name__ == '__main__':
    main()
