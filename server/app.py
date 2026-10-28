#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 后端服务（FastAPI）

只读地暴露 SQLite 里的采集结果。图谱计算用 NetworkX。

启动:
  python server/app.py              # http://127.0.0.1:8000
  python -m uvicorn server.app:app --reload --port 8000

接口与需求编号对应：
  F02 /api/search/person          F03 /api/search/movie
  F04 /api/person/{id}            F05 /api/movie/{id}
  F07 /api/graph                  F08 /api/common/{a}/{b}
  F09 /api/frequency/{id}         F10 /api/path
  F11 /api/graph（筛选参数）        F12 /api/evidence/{a}/{b}
  F13 /api/timeline/{id}          F14 /api/communities
  F15 /api/betweenness            F16 /api/compare/{a}/{b}
  F17 /api/recommend/{id}         F27 /api/quality
"""
import os, sys, io, json, sqlite3, functools, datetime, hashlib, secrets
from typing import Optional

import networkx as nx
from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'yingmai.db')
WEB = os.path.join(ROOT, 'web')

app = FastAPI(title='影脉 · 影视人物合作关系与作品发现平台', version='0.1')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])


@app.middleware('http')
async def no_cache_frontend(request, call_next):
    """前端资源要求每次协商缓存。

    否则改了 app.js / style.css 后，浏览器还拿旧的，页面看起来"没生效"。
    StaticFiles 带 ETag 和 Last-Modified，没变会回 304，变了一定拿到新的。
    """
    resp = await call_next(request)
    if not request.url.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- 登录态原语（必须定义在使用它的接口之前） ----------
_now = lambda: datetime.datetime.now()
_iso = lambda: _now().isoformat(timespec='seconds')


def current_user(authorization: Optional[str] = Header(None)):
    """可选登录：没带 token 返回 None，不报错（只读接口允许匿名）。"""
    if not authorization or not authorization.startswith('Bearer '):
        return None
    tok = authorization[7:].strip()
    try:
        c = db()
        r = c.execute('''SELECT u.id, u.username, u.role FROM session s
                         JOIN app_user u ON u.id = s.user_id
                         WHERE s.token = ? AND s.expires_at > ?''', (tok, _iso())).fetchone()
        c.close()
    except sqlite3.OperationalError:
        return None          # 账号表还没建好时按未登录处理
    return dict(r) if r else None


def require_user(u=Depends(current_user)):
    if not u:
        raise HTTPException(401, '请先登录')
    return u


def require_admin(u=Depends(current_user)):
    if not u:
        raise HTTPException(401, '请先登录')
    if u.get('role') != 'admin':
        raise HTTPException(403, '需要管理员权限')
    return u


# ---------- 图谱（进程内缓存，数据是静态的） ----------
@functools.lru_cache(maxsize=1)
def full_graph():
    g = nx.Graph()
    c = db()
    seeds = {r[0] for r in c.execute('SELECT person_id FROM seed')}
    for r in c.execute('''SELECT p.id, p.name, p.known_for_department dept, p.birthday
                          FROM person p WHERE p.id IN
                          (SELECT DISTINCT person_id FROM graph_node)'''):
        g.add_node(r['id'], name=r['name'], dept=r['dept'], birthday=r['birthday'],
                   seed=r['id'] in seeds)
    for r in c.execute('''SELECT a_person_id a, b_person_id b, weight w,
                                 first_year f, last_year l FROM relation'''):
        g.add_edge(r['a'], r['b'], weight=r['w'], first=r['f'], last=r['l'])
    c.close()
    return g


def subgraph(min_weight=1, year_from=None, year_to=None, max_degree=None):
    """按 F11 的筛选条件取子图。年份筛的是合作发生的年份区间是否有交集。"""
    g = full_graph()
    keep = [(u, v) for u, v, d in g.edges(data=True)
            if d['weight'] >= min_weight
            and (year_from is None or (d['last'] or 0) >= year_from)
            and (year_to is None or (d['first'] or 9999) <= year_to)]
    h = g.edge_subgraph(keep).copy()
    h.remove_nodes_from([n for n in list(h.nodes) if h.degree(n) == 0])
    if max_degree:
        h.remove_nodes_from([n for n in list(h.nodes) if h.degree(n) > max_degree])
    return h


def node_brief(g, nid):
    n = g.nodes[nid]
    return {'id': nid, 'name': n.get('name'), 'dept': n.get('dept'),
            'degree': g.degree(nid), 'birthday': n.get('birthday'),
            'seed': bool(n.get('seed'))}


def graph_payload(h, focus=None):
    return {
        'nodes': [node_brief(h, n) for n in h.nodes],
        'edges': [{'source': u, 'target': v, 'weight': d['weight'],
                   'first': d.get('first'), 'last': d.get('last')}
                  for u, v, d in h.edges(data=True)],
        'focus': focus,
    }


# ---------- 概览 ----------
@app.get('/api/stats')
def api_stats():
    c = db()
    q = lambda s: c.execute(s).fetchone()[0]
    out = {
        'movies': q('SELECT COUNT(*) FROM movie'),
        'persons': q('SELECT COUNT(*) FROM person'),
        'credits': q('SELECT COUNT(*) FROM credit'),
        'graph_nodes': q('SELECT COUNT(DISTINCT person_id) FROM graph_node'),
        'graph_edges': q('SELECT COUNT(*) FROM relation'),
        'year_min': q('SELECT MIN(release_year) FROM movie'),
        'year_max': q('SELECT MAX(release_year) FROM movie'),
        'scope': '华语片（TMDB original_language zh/cn）· 每部前 10 主演 + 导演',
        'source': 'TMDB',
    }
    c.close()
    return out


@app.get('/api/highlight')
def api_highlight(min_weight: int = 5):
    """登录页品牌区用：随机挑一对合作较多的组合，展示平台在做什么。"""
    c = db()
    r = c.execute('''SELECT r.weight w, r.first_year f, r.last_year l,
                            p1.id id1, p1.name n1, p2.id id2, p2.name n2
                     FROM relation r
                     JOIN person p1 ON p1.id = r.a_person_id
                     JOIN person p2 ON p2.id = r.b_person_id
                     WHERE r.weight >= ?
                     ORDER BY RANDOM() LIMIT 1''', (min_weight,)).fetchone()
    c.close()
    if not r:
        return {'found': False}
    return {'found': True, 'weight': r['w'], 'first': r['f'], 'last': r['l'],
            'a': {'id': r['id1'], 'name': r['n1']},
            'b': {'id': r['id2'], 'name': r['n2']}}


# ---------- F02 人物检索 ----------
@app.get('/api/search/person')
def api_search_person(q: str = '', limit: int = 20, only_graph: bool = True):
    c = db()
    like = f'%{q}%'
    sql = '''SELECT p.id, p.name, p.known_for_department dept, p.birthday,
                    p.place_of_birth, p.also_known_as, p.profile_path, p.popularity
             FROM person p WHERE (p.name LIKE ? OR p.also_known_as LIKE ?)'''
    if only_graph:
        sql += ' AND p.id IN (SELECT DISTINCT person_id FROM graph_node)'
    sql += ''' ORDER BY (SELECT COUNT(*) FROM relation r
                          WHERE r.a_person_id=p.id OR r.b_person_id=p.id) DESC,
                        p.popularity DESC LIMIT ?'''
    out = rows(c.execute(sql, (like, like, limit)))
    for r in out:
        r['aka'] = json.loads(r.pop('also_known_as') or '[]')[:4]
    c.close()
    return {'query': q, 'count': len(out), 'results': out}


# ---------- F03 作品检索 ----------
@app.get('/api/search/movie')
def api_search_movie(q: str = '', year: Optional[int] = None,
                     year_from: Optional[int] = None, year_to: Optional[int] = None,
                     genre: Optional[str] = None, limit: int = 30):
    c = db()
    where, args = ['(title LIKE ? OR original_title LIKE ?)'], [f'%{q}%', f'%{q}%']
    if year:
        where.append('release_year = ?'); args.append(year)
    if year_from:
        where.append('release_year >= ?'); args.append(year_from)
    if year_to:
        where.append('release_year <= ?'); args.append(year_to)
    if genre:
        where.append('genres LIKE ?'); args.append(f'%{genre}%')
    args.append(limit)
    out = rows(c.execute(
        f'''SELECT id, title, original_title, release_year, release_date, runtime,
                   genres, original_language, vote_count, vote_average, poster_path, seed_hits
            FROM movie WHERE {' AND '.join(where)}
            ORDER BY seed_hits DESC, vote_count DESC LIMIT ?''', args))
    for r in out:
        r['genres'] = json.loads(r['genres'] or '[]')
    c.close()
    return {'query': q, 'count': len(out), 'results': out}


# ---------- F04 人物档案 ----------
@app.get('/api/person/{pid}')
def api_person(pid: int, top: int = 30):
    c = db()
    p = c.execute('SELECT * FROM person WHERE id=?', (pid,)).fetchone()
    if not p:
        c.close(); raise HTTPException(404, '人物不存在')
    p = dict(p)
    p['aka'] = json.loads(p.pop('also_known_as') or '[]')
    films = rows(c.execute('''SELECT DISTINCT m.id, m.title, m.release_year, m.release_date,
                                     m.genres, m.vote_count, m.poster_path,
                                     c.job, c.character_name, c.billing_order
                              FROM credit c JOIN movie m ON m.id = c.movie_id
                              WHERE c.person_id = ?
                              ORDER BY COALESCE(m.release_year, 0) DESC''', (pid,)))
    for f in films:
        f['genres'] = json.loads(f['genres'] or '[]')
    top_collab = rows(c.execute('''SELECT p.id, p.name, r.weight, r.first_year, r.last_year
                                   FROM relation r
                                   JOIN person p ON p.id = CASE WHEN r.a_person_id=? THEN r.b_person_id ELSE r.a_person_id END
                                   WHERE r.a_person_id=? OR r.b_person_id=?
                                   ORDER BY r.weight DESC LIMIT ?''', (pid, pid, pid, top)))
    c.close()
    return {'person': p, 'works': films, 'top_collaborators': top_collab,
            'in_graph': bool(top_collab) or pid in full_graph()}


# ---------- F05 作品详情 ----------
@app.get('/api/movie/{mid}')
def api_movie(mid: int):
    c = db()
    m = c.execute('SELECT * FROM movie WHERE id=?', (mid,)).fetchone()
    if not m:
        c.close(); raise HTTPException(404, '作品不存在')
    m = dict(m)
    m['genres'] = json.loads(m['genres'] or '[]')
    m['origin_country'] = json.loads(m['origin_country'] or '[]')
    cast = rows(c.execute('''SELECT p.id, p.name, c.character_name, c.billing_order
                             FROM credit c JOIN person p ON p.id = c.person_id
                             WHERE c.movie_id=? AND c.credit_type='cast'
                             ORDER BY COALESCE(c.billing_order, 999)''', (mid,)))
    crew = rows(c.execute('''SELECT p.id, p.name, c.job, c.department
                             FROM credit c JOIN person p ON p.id = c.person_id
                             WHERE c.movie_id=? AND c.credit_type='crew'
                             ORDER BY c.job''', (mid,)))
    c.close()
    return {'movie': m, 'cast': cast, 'crew': crew}


# ---------- F07 / F11 合作图谱 ----------
@app.get('/api/graph')
def api_graph(person_id: Optional[int] = None, hops: int = 1, min_weight: int = 1,
              year_from: Optional[int] = None, year_to: Optional[int] = None,
              limit: int = 120, max_degree: Optional[int] = None):
    h = subgraph(min_weight, year_from, year_to, max_degree)
    if person_id is not None:
        if person_id not in h:
            raise HTTPException(404, '该人物在当前筛选条件下没有合作边')
        keep = {person_id}
        frontier = {person_id}
        for _ in range(max(1, hops)):
            nxt = set()
            for n in frontier:
                nxt |= set(h.neighbors(n))
            keep |= nxt
            frontier = nxt
        h = h.subgraph(keep).copy()
    if h.number_of_nodes() > limit:
        # 按度数保留最重要的节点，并提示被截断
        top = sorted(h.nodes, key=lambda n: -h.degree(n))[:limit]
        h = h.subgraph(top).copy()
    h.remove_nodes_from([n for n in list(h.nodes) if h.degree(n) == 0])
    payload = graph_payload(h, person_id)
    payload['truncated'] = h.number_of_nodes() >= limit
    payload['total_in_scope'] = subgraph(min_weight, year_from, year_to, max_degree).number_of_nodes()
    return payload


# ---------- F08 共同作品 ----------
@app.get('/api/common/{a}/{b}')
def api_common(a: int, b: int):
    c = db()
    out = rows(c.execute('''SELECT m.id, m.title, m.release_year, m.release_date, m.poster_path,
                                   ca.job ja, ca.character_name cha, ca.billing_order oa,
                                   cb.job jb, cb.character_name chb, cb.billing_order ob
                            FROM credit ca
                            JOIN credit cb ON cb.movie_id = ca.movie_id AND cb.person_id = ?
                            JOIN movie m ON m.id = ca.movie_id
                            WHERE ca.person_id = ?
                            GROUP BY m.id
                            ORDER BY COALESCE(m.release_year,0)''', (b, a)))
    pa = c.execute('SELECT name FROM person WHERE id=?', (a,)).fetchone()
    pb = c.execute('SELECT name FROM person WHERE id=?', (b,)).fetchone()
    c.close()
    return {'a': {'id': a, 'name': pa['name'] if pa else None},
            'b': {'id': b, 'name': pb['name'] if pb else None},
            'count': len(out), 'works': out}


# ---------- F09 合作频次 ----------
@app.get('/api/frequency/{pid}')
def api_frequency(pid: int, limit: int = 30, min_weight: int = 1):
    c = db()
    out = rows(c.execute('''SELECT p.id, p.name, p.known_for_department dept,
                                   r.weight, r.first_year, r.last_year
                            FROM relation r
                            JOIN person p ON p.id = CASE WHEN r.a_person_id=? THEN r.b_person_id ELSE r.a_person_id END
                            WHERE (r.a_person_id=? OR r.b_person_id=?) AND r.weight >= ?
                            ORDER BY r.weight DESC, r.last_year DESC LIMIT ?''',
                         (pid, pid, pid, min_weight, limit)))
    c.close()
    return {'person_id': pid, 'count': len(out), 'collaborators': out}


# ---------- F10 关系路径 ----------
@app.get('/api/path')
def api_path(a: int, b: int, max_hops: int = 3, min_weight: int = 1, k: int = 3):
    h = subgraph(min_weight)
    if a not in h or b not in h:
        return {'a': a, 'b': b, 'found': False,
                'note': '起点或终点在当前样本与条件下没有合作边'}
    try:
        paths = []
        for p in nx.shortest_simple_paths(h, a, b, weight=None):
            paths.append(p)
            if len(paths) >= k:
                break
        paths = [p for p in paths if len(p) - 1 <= max_hops]
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        paths = []
    if not paths:
        return {'a': a, 'b': b, 'found': False,
                'note': '当前样本与条件下未找到路径；无路径仅表示样本内没找到，不代表现实中无关联'}
    c = db()
    names = {r['id']: r['name'] for r in c.execute('SELECT id, name FROM person')}
    out = []
    for p in paths:
        ev = []
        for u, v in zip(p, p[1:]):
            lo, hi = min(u, v), max(u, v)
            films = rows(c.execute('''SELECT m.id, m.title, m.release_year
                                      FROM relation_evidence e JOIN movie m ON m.id = e.movie_id
                                      WHERE e.a_person_id=? AND e.b_person_id=?
                                      ORDER BY COALESCE(m.release_year,0)''', (lo, hi)))
            ev.append({'from': names.get(u), 'to': names.get(v),
                       'weight': h[u][v]['weight'], 'works': films})
        out.append({'hops': len(p) - 1, 'nodes': [names.get(n) for n in p], 'ids': p, 'edges': ev})
    c.close()
    return {'a': a, 'b': b, 'found': True, 'paths': out}


# ---------- F12 关系证据 ----------
@app.get('/api/evidence/{a}/{b}')
def api_evidence(a: int, b: int):
    lo, hi = min(a, b), max(a, b)
    c = db()
    out = rows(c.execute('''SELECT m.id, m.title, m.release_year, m.release_date,
                                   m.original_language, m.fetched_at
                            FROM relation_evidence e JOIN movie m ON m.id = e.movie_id
                            WHERE e.a_person_id=? AND e.b_person_id=?
                            ORDER BY COALESCE(m.release_year,0)''', (lo, hi)))
    c.close()
    return {'a': a, 'b': b, 'count': len(out),
            'source': 'TMDB', 'works': out,
            'note': '合作仅指共同参与作品，不代表私人友谊或直接对手戏'}


# ---------- F13 合作时间线 ----------
@app.get('/api/timeline/{pid}')
def api_timeline(pid: int, year_from: Optional[int] = None, year_to: Optional[int] = None):
    c = db()
    sql = '''SELECT m.release_year y, COUNT(DISTINCT m.id) n
             FROM credit c JOIN movie m ON m.id = c.movie_id
             WHERE c.person_id = ? AND m.release_year IS NOT NULL'''
    args = [pid]
    if year_from:
        sql += ' AND m.release_year >= ?'; args.append(year_from)
    if year_to:
        sql += ' AND m.release_year <= ?'; args.append(year_to)
    sql += ' GROUP BY m.release_year ORDER BY m.release_year'
    years = rows(c.execute(sql, args))
    # 每年新增的合作对象
    collab = rows(c.execute('''SELECT y, COUNT(DISTINCT other) n FROM (
                                 SELECT m.release_year y,
                                        CASE WHEN r.a_person_id=? THEN r.b_person_id ELSE r.a_person_id END other
                                 FROM relation_evidence e
                                 JOIN movie m ON m.id = e.movie_id
                                 JOIN relation r ON r.a_person_id=e.a_person_id
                                                 AND r.b_person_id=e.b_person_id
                                 WHERE e.a_person_id=? OR e.b_person_id=?)
                               WHERE y IS NOT NULL GROUP BY y ORDER BY y''', (pid, pid, pid)))
    c.close()
    return {'person_id': pid, 'works_by_year': years, 'new_collaborators_by_year': collab,
            'note': '按发行年统计，不代表实际拍摄时间'}


# ---------- F14 合作群体 ----------
@app.get('/api/communities')
def api_communities(min_weight: int = 3, top: int = 12):
    h = subgraph(min_weight)
    if h.number_of_nodes() == 0:
        return {'communities': []}
    comms = nx.community.greedy_modularity_communities(h, weight='weight')
    c = db()
    names = {r['id']: r['name'] for r in c.execute('SELECT id, name FROM person')}
    c.close()
    out = []
    for i, com in enumerate(sorted(comms, key=len, reverse=True)[:top]):
        sub = h.subgraph(com)
        core = sorted(com, key=lambda n: -sub.degree(n))[:8]
        out.append({'id': i, 'size': len(com),
                    'core': [{'id': n, 'name': names.get(n), 'degree': sub.degree(n)} for n in core]})
    return {'min_weight': min_weight, 'count': len(comms), 'communities': out,
            'algorithm': 'greedy_modularity_communities（按合作频次加权）',
            'note': '群体由样本内的合作边聚类得到，只反映当前样本范围'}


# ---------- F15 连接人物分析 ----------
@app.get('/api/betweenness')
def api_betweenness(min_weight: int = 2, top: int = 20):
    h = subgraph(min_weight)
    if h.number_of_nodes() < 3:
        return {'betweenness': []}
    bc = nx.betweenness_centrality(h, weight=None, k=min(400, h.number_of_nodes()), seed=42)
    c = db()
    names = {r['id']: r['name'] for r in c.execute('SELECT id, name FROM person')}
    c.close()
    ranked = sorted(bc.items(), key=lambda x: -x[1])[:top]
    return {'min_weight': min_weight, 'nodes': h.number_of_nodes(), 'edges': h.number_of_edges(),
            'betweenness': [{'id': n, 'name': names.get(n), 'score': round(s, 6),
                             'degree': h.degree(n)} for n, s in ranked],
            'algorithm': 'NetworkX betweenness_centrality（近似，抽样 400 源点）',
            'note': '中介中心性表示该人物在样本网络中连接不同群体的程度，不代表行业地位'}


# ---------- F16 人物合作对比 ----------
@app.get('/api/compare/{a}/{b}')
def api_compare(a: int, b: int):
    c = db()

    def profile(pid):
        p = c.execute('SELECT id, name, known_for_department FROM person WHERE id=?', (pid,)).fetchone()
        if not p:
            raise HTTPException(404, f'人物 {pid} 不存在')
        films = rows(c.execute('''SELECT m.id, m.title, m.release_year, m.genres
                                  FROM credit c JOIN movie m ON m.id=c.movie_id
                                  WHERE c.person_id=?''', (pid,)))
        genres = {}
        for f in films:
            for g in json.loads(f['genres'] or '[]'):
                genres[g] = genres.get(g, 0) + 1
        collab = c.execute('''SELECT COUNT(*) FROM relation
                              WHERE a_person_id=? OR b_person_id=?''', (pid, pid)).fetchone()[0]
        years = [f['release_year'] for f in films if f['release_year']]
        return {'id': pid, 'name': p['name'], 'dept': p['known_for_department'],
                'works': len(films), 'collaborators': collab,
                'year_from': min(years) if years else None,
                'year_to': max(years) if years else None,
                'genres': sorted(genres.items(), key=lambda x: -x[1])[:8]}

    pa, pb = profile(a), profile(b)
    common = rows(c.execute('''SELECT m.id, m.title, m.release_year
                               FROM credit ca JOIN credit cb ON cb.movie_id=ca.movie_id AND cb.person_id=?
                               JOIN movie m ON m.id=ca.movie_id
                               WHERE ca.person_id=? GROUP BY m.id''', (b, a)))
    c.close()
    return {'a': pa, 'b': pb, 'common_works': common}


# ---------- F17 作品推荐 ----------
@app.get('/api/recommend/{pid}')
def api_recommend(pid: int, limit: int = 12, u=Depends(current_user)):
    """基于「喜欢的演员 / 已看作品」做规则推荐，不生成事实关系。

    排除范围 = 该人物本人参演的作品 ∪ 当前用户标记为「看过」的作品（F17 验收要求）。
    """
    c = db()
    p = c.execute('SELECT name FROM person WHERE id=?', (pid,)).fetchone()
    if not p:
        c.close(); raise HTTPException(404, '人物不存在')

    # 已看：该人物参与的作品
    seen = {r['id'] for r in c.execute('SELECT movie_id id FROM credit WHERE person_id=?', (pid,))}
    # 已看：用户自己标记的
    mine = set()
    liked, disliked = set(), set()
    if u:
        mine = {r['movie_id'] for r in
                c.execute("SELECT movie_id FROM favorite WHERE user_id=? AND list_type='seen'",
                          (u['id'],))}
        for r in c.execute('SELECT movie_id, action FROM feedback WHERE user_id=?', (u['id'],)):
            (liked if r['action'] == 'like' else disliked).add(r['movie_id'])
        seen |= mine | disliked          # 不感兴趣的也排除掉
    # 该人物的合作者
    collabs = {r['pid'] for r in c.execute('''SELECT CASE WHEN a_person_id=? THEN b_person_id
                                                          ELSE a_person_id END pid
                                                FROM relation WHERE a_person_id=? OR b_person_id=?''',
                                           (pid, pid, pid))}
    # 候选：合作者的作品，排除已看
    cand = {}
    if collabs:
        qs = ','.join('?' * len(collabs))
        for r in c.execute(f'''SELECT m.id, m.title, m.release_year, m.genres, m.vote_count,
                                      c.person_id pid
                               FROM credit c JOIN movie m ON m.id=c.movie_id
                               WHERE c.person_id IN ({qs})''', tuple(collabs)):
            if r['id'] in seen:
                continue
            d = cand.setdefault(r['id'], {'id': r['id'], 'title': r['title'],
                                          'release_year': r['release_year'],
                                          'vote_count': r['vote_count'],
                                          'genres': json.loads(r['genres'] or '[]'),
                                          'why': set()})
            d['why'].add(r['pid'])
    # 该人物最常合作的类型，作为加分项
    fav = {}
    for r in c.execute('''SELECT m.genres g FROM credit c JOIN movie m ON m.id=c.movie_id
                          WHERE c.person_id=?''', (pid,)):
        for g in json.loads(r['g'] or '[]'):
            fav[g] = fav.get(g, 0) + 1
    # 反馈调整偏好：点过「喜欢」的片的类型，作为加分项
    like_genres = {}
    if liked:
        qs = ','.join('?' * len(liked))
        for r in c.execute(f'SELECT genres FROM movie WHERE id IN ({qs})', tuple(liked)):
            for g in json.loads(r['genres'] or '[]'):
                like_genres[g] = like_genres.get(g, 0) + 1

    names = {r['id']: r['name'] for r in c.execute('SELECT id, name FROM person')}
    c.close()

    out = []
    for d in cand.values():
        ghit = [g for g in d['genres'] if g in fav]
        lhit = [g for g in d['genres'] if g in like_genres]
        d['score'] = (len(d['why']) * 2 + len(ghit) + len(lhit) * 1.5
                      + (d['vote_count'] or 0) / 5000.0)
        d['why'] = [names.get(x) for x in list(d['why'])[:3]]
        d['genre_hit'] = ghit[:3]
        d['like_hit'] = lhit[:3]
        out.append(d)
    out.sort(key=lambda x: -x['score'])
    return {'person': {'id': pid, 'name': p['name']},
            'basis': '基于共同合作者与类型偏好，不生成事实关系',
            'excluded_own_works': len(seen) - len(mine) - len(disliked),
            'excluded_my_seen': len(mine),
            'excluded_disliked': len(disliked),
            'liked_count': len(liked),
            'count': len(out), 'results': out[:limit]}


# ---------- F27 质量清单 ----------
@app.get('/api/quality')
def api_quality():
    p = os.path.join(ROOT, '_work', 'quality.json')
    if os.path.exists(p):
        return json.load(open(p, encoding='utf-8'))
    return {'note': '尚未生成，先运行 python pipeline/build.py quality'}


# ================= F01 账号管理 · F06 收藏片单 · F29 操作日志 =================
# 密码加盐哈希（N02），收藏按用户隔离；会话用不透明 token，存 localStorage。

APP_DDL = '''
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',   -- user / admin
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id),
    created_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS favorite (
    user_id    INTEGER NOT NULL REFERENCES app_user(id),
    movie_id   INTEGER NOT NULL,
    list_type  TEXT NOT NULL DEFAULT 'want',      -- want 待看 / seen 看过
    created_at TEXT,
    PRIMARY KEY (user_id, movie_id, list_type)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER,
    action  TEXT,
    detail  TEXT,
    at      TEXT
);
'''

SESSION_DAYS = 30
PBKDF2_ROUNDS = 200_000


def init_app_tables():
    c = db()
    c.executescript(APP_DDL)
    c.commit()
    c.close()


init_app_tables()

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return h.hex(), salt


def audit(user_id, action, detail=''):
    c = db()
    c.execute('INSERT INTO audit_log(user_id, action, detail, at) VALUES(?,?,?,?)',
              (user_id, action, detail, _iso()))
    c.commit()
    c.close()


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class FavBody(BaseModel):
    movie_id: int
    list_type: str = 'want'          # want / seen


@app.post('/api/auth/register')
def api_register(body: Credentials):
    c = db()
    if c.execute('SELECT 1 FROM app_user WHERE username=?', (body.username,)).fetchone():
        c.close(); raise HTTPException(409, '用户名已被占用')
    pw_hash, salt = hash_pw(body.password)
    # 第一个注册的人成为管理员，否则纠错复核没人能点（F28/F29）
    role = 'admin' if c.execute('SELECT COUNT(*) FROM app_user').fetchone()[0] == 0 else 'user'
    c.execute('INSERT INTO app_user(username,password_hash,salt,role,created_at) VALUES(?,?,?,?,?)',
              (body.username, pw_hash, salt, role, _iso()))
    c.commit()
    uid = c.execute('SELECT id FROM app_user WHERE username=?', (body.username,)).fetchone()['id']
    tok = _new_session(c, uid)
    c.close()
    audit(uid, 'register', body.username)
    return {'token': tok, 'user': {'id': uid, 'username': body.username, 'role': role}}


@app.post('/api/auth/login')
def api_login(body: Credentials):
    c = db()
    r = c.execute('SELECT * FROM app_user WHERE username=?', (body.username,)).fetchone()
    if not r:
        c.close(); raise HTTPException(401, '用户名或密码不正确')
    got, _ = hash_pw(body.password, r['salt'])
    # 定长比较，避免时序侧信道
    if not secrets.compare_digest(got, r['password_hash']):
        c.close(); raise HTTPException(401, '用户名或密码不正确')
    tok = _new_session(c, r['id'])
    c.close()
    audit(r['id'], 'login', body.username)
    return {'token': tok, 'user': {'id': r['id'], 'username': r['username'], 'role': r['role']}}


def _new_session(c, uid):
    tok = secrets.token_urlsafe(32)
    c.execute('INSERT INTO session(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',
              (tok, uid, _iso(),
               (_now() + datetime.timedelta(days=SESSION_DAYS)).isoformat(timespec='seconds')))
    c.commit()
    return tok


@app.post('/api/auth/logout')
def api_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith('Bearer '):
        c = db()
        c.execute('DELETE FROM session WHERE token=?', (authorization[7:].strip(),))
        c.commit(); c.close()
    return {'ok': True}


@app.get('/api/me')
def api_me(u=Depends(current_user)):
    if not u:
        return {'logged_in': False}
    c = db()
    n = c.execute('SELECT COUNT(*) FROM favorite WHERE user_id=?', (u['id'],)).fetchone()[0]
    c.close()
    return {'logged_in': True, 'user': u, 'favorites': n}


@app.get('/api/favorites')
def api_favorites(list_type: Optional[str] = None, u=Depends(require_user)):
    c = db()
    sql = '''SELECT f.movie_id, f.list_type, f.created_at, m.title, m.release_year,
                    m.poster_path, m.genres, m.vote_count
             FROM favorite f JOIN movie m ON m.id = f.movie_id
             WHERE f.user_id = ?'''
    args = [u['id']]
    if list_type:
        sql += ' AND f.list_type = ?'; args.append(list_type)
    sql += ' ORDER BY f.created_at DESC'
    out = rows(c.execute(sql, args))
    for r in out:
        r['genres'] = json.loads(r['genres'] or '[]')
    c.close()
    return {'count': len(out), 'items': out}


@app.post('/api/favorites')
def api_fav_add(body: FavBody, u=Depends(require_user)):
    if body.list_type not in ('want', 'seen'):
        raise HTTPException(400, 'list_type 只能是 want 或 seen')
    c = db()
    if not c.execute('SELECT 1 FROM movie WHERE id=?', (body.movie_id,)).fetchone():
        c.close(); raise HTTPException(404, '作品不存在')
    c.execute('''INSERT OR REPLACE INTO favorite(user_id,movie_id,list_type,created_at)
                 VALUES(?,?,?,?)''', (u['id'], body.movie_id, body.list_type, _iso()))
    c.commit(); c.close()
    audit(u['id'], 'favorite_add', f'{body.movie_id}:{body.list_type}')
    return {'ok': True, 'movie_id': body.movie_id, 'list_type': body.list_type}


@app.delete('/api/favorites/{movie_id}')
def api_fav_del(movie_id: int, list_type: str = 'want', u=Depends(require_user)):
    c = db()
    c.execute('DELETE FROM favorite WHERE user_id=? AND movie_id=? AND list_type=?',
              (u['id'], movie_id, list_type))
    c.commit(); c.close()
    audit(u['id'], 'favorite_del', f'{movie_id}:{list_type}')
    return {'ok': True}


@app.get('/api/auth/log')
def api_auth_log(limit: int = 50, u=Depends(require_user)):
    """F29 审计记录（本人可见自己的操作）。"""
    c = db()
    out = rows(c.execute('''SELECT action, detail, at FROM audit_log
                            WHERE user_id=? ORDER BY id DESC LIMIT ?''', (u['id'], limit)))
    c.close()
    return {'count': len(out), 'entries': out}


# ---------- F30 备份（列表 / 创建 / 校验） ----------
# 恢复不放在 Web 上：服务本身占着数据库，边跑边覆盖自己容易出问题。恢复走 CLI。
BACKUP_DIR = os.path.join(ROOT, 'backups')


@app.get('/api/backups')
def api_backups():
    sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
    try:
        import backup as bk
        return {'dir': BACKUP_DIR, 'items': bk.all_snapshots()}
    except Exception as e:
        return {'dir': BACKUP_DIR, 'items': [], 'error': str(e)}


@app.post('/api/backups')
def api_backup_create(note: str = '', u=Depends(require_admin)):
    sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
    import backup as bk
    name = bk.cmd_create(note or f'由 {u["username"]} 在管理页创建')
    return {'ok': bool(name), 'name': name}


@app.post('/api/backups/{name}/verify')
def api_backup_verify(name: str, u=Depends(require_admin)):
    sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
    import backup as bk
    import io as _io, contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = bk.cmd_verify(name)
    return {'ok': bool(ok), 'log': buf.getvalue()}


# ---------- F24 同步记录 ----------
@app.get('/api/syncs')
def api_syncs(limit: int = 20):
    c = db()
    try:
        out = rows(c.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT ?', (limit,)))
    except sqlite3.OperationalError:
        out = []
    c.close()
    return {'count': len(out), 'items': out}


# ================= F28 纠错管理 · F18 推荐反馈 =================
# F28：用户提交纠错（带来源证据）→ 管理员复核 → 接受则改数据并留版本记录
# F18：对推荐结果点「喜欢 / 不感兴趣」→ 调整后续推荐

MORE_DDL = '''
CREATE TABLE IF NOT EXISTS correction (
    id            INTEGER PRIMARY KEY,
    target_type   TEXT NOT NULL,        -- movie / person
    target_id     INTEGER NOT NULL,
    field         TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT NOT NULL,
    reason        TEXT,
    source_url    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending / accepted / rejected
    submitted_by  INTEGER,
    reviewed_by   INTEGER,
    review_note   TEXT,
    created_at    TEXT,
    reviewed_at   TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    user_id    INTEGER NOT NULL,
    movie_id   INTEGER NOT NULL,
    action     TEXT NOT NULL,           -- like / dislike
    created_at TEXT,
    PRIMARY KEY (user_id, movie_id)
);
'''

# 允许纠正的字段白名单：只开放有明确来源可核对的展示字段
CORRECTABLE = {
    'movie':  {'title', 'original_title', 'release_year'},
    'person': {'name'},
}


def init_more_tables():
    c = db()
    c.executescript(MORE_DDL)
    c.commit()
    c.close()


init_more_tables()


class CorrectionBody(BaseModel):
    target_type: str
    target_id: int
    field: str
    new_value: str
    old_value: Optional[str] = None
    reason: str = ''
    source_url: str = ''


class ReviewBody(BaseModel):
    action: str                 # accept / reject
    note: str = ''


class FeedbackBody(BaseModel):
    movie_id: int
    action: str                 # like / dislike


@app.get('/api/corrections')
def api_corrections(status: Optional[str] = None, limit: int = 100):
    c = db()
    sql = '''SELECT c.*, su.username submitter, ru.username reviewer,
                    m.title movie_title, p.name person_name
             FROM correction c
             LEFT JOIN app_user su ON su.id = c.submitted_by
             LEFT JOIN app_user ru ON ru.id = c.reviewed_by
             LEFT JOIN movie  m ON c.target_type='movie'  AND m.id = c.target_id
             LEFT JOIN person p ON c.target_type='person' AND p.id = c.target_id'''
    args = []
    if status:
        sql += ' WHERE c.status = ?'; args.append(status)
    sql += ' ORDER BY c.id DESC LIMIT ?'; args.append(limit)
    out = rows(c.execute(sql, args))
    n = {r['status']: r['n'] for r in
         c.execute('SELECT status, COUNT(*) n FROM correction GROUP BY status')}
    c.close()
    return {'count': len(out), 'by_status': n, 'items': out}


@app.post('/api/corrections')
def api_correction_submit(body: CorrectionBody, u=Depends(require_user)):
    if body.target_type not in CORRECTABLE:
        raise HTTPException(400, f'target_type 只能是 {list(CORRECTABLE)}')
    if body.field not in CORRECTABLE[body.target_type]:
        raise HTTPException(400, f'{body.target_type} 可纠正的字段：{sorted(CORRECTABLE[body.target_type])}')
    if not body.new_value.strip():
        raise HTTPException(400, '更正后的值不能为空')
    c = db()
    tbl = 'movie' if body.target_type == 'movie' else 'person'
    cur = c.execute(f'SELECT {body.field} v FROM {tbl} WHERE id=?', (body.target_id,)).fetchone()
    if not cur:
        c.close(); raise HTTPException(404, '目标记录不存在')
    c.execute('''INSERT INTO correction(target_type,target_id,field,old_value,new_value,reason,
                 source_url,status,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)''',
              (body.target_type, body.target_id, body.field,
               body.old_value if body.old_value is not None else str(cur['v']),
               body.new_value.strip(), body.reason, body.source_url, u['id'], _iso()))
    c.commit()
    cid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.close()
    audit(u['id'], 'correction_submit', f'{body.target_type}:{body.target_id}.{body.field}')
    return {'ok': True, 'id': cid, 'status': 'pending'}


@app.post('/api/corrections/{cid}/review')
def api_correction_review(cid: int, body: ReviewBody, u=Depends(require_admin)):
    if body.action not in ('accept', 'reject'):
        raise HTTPException(400, 'action 只能是 accept 或 reject')
    c = db()
    r = c.execute('SELECT * FROM correction WHERE id=?', (cid,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404, '纠错记录不存在')
    if r['status'] != 'pending':
        c.close(); raise HTTPException(409, f"该记录已处理（{r['status']}）")
    if body.action == 'accept':
        tbl = 'movie' if r['target_type'] == 'movie' else 'person'
        val = r['new_value']
        if r['field'] == 'release_year':
            if not val.isdigit():
                c.close(); raise HTTPException(400, 'release_year 必须是数字')
            val = int(val)
        c.execute(f'UPDATE {tbl} SET {r["field"]}=? WHERE id=?', (val, r['target_id']))
    c.execute('''UPDATE correction SET status=?, reviewed_by=?, review_note=?, reviewed_at=?
                 WHERE id=?''',
              ('accepted' if body.action == 'accept' else 'rejected',
               u['id'], body.note, _iso(), cid))
    c.commit()
    c.close()
    audit(u['id'], 'correction_review', f'{cid}:{body.action}')
    return {'ok': True, 'id': cid, 'status': 'accepted' if body.action == 'accept' else 'rejected'}


@app.get('/api/feedback')
def api_feedback_list(u=Depends(require_user)):
    c = db()
    out = rows(c.execute('''SELECT f.movie_id, f.action, f.created_at, m.title, m.release_year
                            FROM feedback f JOIN movie m ON m.id = f.movie_id
                            WHERE f.user_id=? ORDER BY f.created_at DESC''', (u['id'],)))
    c.close()
    return {'count': len(out), 'items': out}


@app.post('/api/feedback')
def api_feedback(body: FeedbackBody, u=Depends(require_user)):
    if body.action not in ('like', 'dislike'):
        raise HTTPException(400, 'action 只能是 like 或 dislike')
    c = db()
    if not c.execute('SELECT 1 FROM movie WHERE id=?', (body.movie_id,)).fetchone():
        c.close(); raise HTTPException(404, '作品不存在')
    c.execute('''INSERT OR REPLACE INTO feedback(user_id,movie_id,action,created_at)
                 VALUES(?,?,?,?)''', (u['id'], body.movie_id, body.action, _iso()))
    c.commit(); c.close()
    audit(u['id'], 'feedback', f'{body.movie_id}:{body.action}')
    return {'ok': True, 'movie_id': body.movie_id, 'action': body.action}


@app.delete('/api/feedback/{movie_id}')
def api_feedback_del(movie_id: int, u=Depends(require_user)):
    c = db()
    c.execute('DELETE FROM feedback WHERE user_id=? AND movie_id=?', (u['id'], movie_id))
    c.commit(); c.close()
    return {'ok': True}


# ---------- 前端静态页 ----------
# 挂在根路径：页面里的相对引用（style.css / app.js / person.html）才能正确解析。
# 必须在所有 /api 路由之后挂载，否则会把接口一起吃掉。
if os.path.isdir(WEB):
    app.mount('/', StaticFiles(directory=WEB, html=True), name='web')


if __name__ == '__main__':
    import uvicorn
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print(f'数据库 {DB}')
    print('打开 http://127.0.0.1:8000')
    uvicorn.run(app, host='127.0.0.1', port=8000, log_level='info')
