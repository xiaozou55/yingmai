#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 数据采集

种子策略（实测决定，见 _work/数据体检报告.md 与 _work/种子候选.md）：
  以 Top-N 热门片为种子 → 93.7% 的片对没有共同演员，图是一盘散沙；
  改以**人物为种子** → 18 位候选全部互相连通。

用法:
  python collect.py init        建库，登记来源与 8 位种子人物
  python collect.py plan        从种子作品里选 200 部，写入 movie 表（待采集）
  python collect.py fetch       采集电影详情与演职员表
  python collect.py fetch --limit 20     先小批量试跑
  python collect.py status      查看进度

  python collect.py --proxy http://127.0.0.1:7897 <子命令>
"""
import os, sys, re, json, time, sqlite3, argparse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from probe_tmdb import make_session, api_get, ZH_LANGS, log   # noqa: E402

DB = os.path.join(ROOT, 'yingmai.db')
SCHEMA = os.path.join(HERE, 'schema.sql')
NOW = lambda: datetime.datetime.now().isoformat(timespec='seconds')

# 八人跨代际：老（成龙 1962– / 洪金宝 1961–）
#             中（刘德华 1982– / 梁朝伟 1982– / 张曼玉 1984– / 周星驰 1983–）
#             新（吴京 1996– / 易烊千玺 2015–）
# 互相之间有 73 对共同作品，刘德华是连接老中青的关键节点。
SEEDS = ['成龙', '洪金宝', '刘德华', '梁朝伟', '张曼玉', '周星驰', '吴京', '易烊千玺']

TARGET_FILMS = 200      # 课程样本规模（N01 验收集）
MIN_SEED_HITS = 2       # 「稠密优先」门槛：至少两粒种子共同出演
SOURCE_ID = 1


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def pick_person(sess, name, offline=False):
    """搜人物，优先中文主名、优先 Acting/Directing。"""
    d = api_get(sess, '/search/person', offline=offline, query=name, language='zh-CN')
    if not d or not d.get('results'):
        return None
    scored = []
    for c in d['results'][:5]:
        s = 0.0
        if c.get('name') == name:
            s += 3.0
        if re.search(r'[一-鿿]', c.get('name') or ''):
            s += 2.0
        if c.get('known_for_department') in ('Acting', 'Directing'):
            s += 1.5
        s += min(c.get('popularity') or 0, 100) / 100.0
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


# ---------- init ----------
def cmd_init(sess):
    if os.path.exists(DB):
        log(f'数据库已存在：{DB}')
        log('要重建请先删除该文件（会丢掉已采集数据）。')
        return
    c = conn()
    c.executescript(open(SCHEMA, encoding='utf-8').read())
    c.execute('INSERT INTO source(id,name,base_url,license,attribution,note) VALUES(?,?,?,?,?,?)',
              (SOURCE_ID, 'TMDB', 'https://developer.themoviedb.org/',
               '非商业使用需署名', 'This product uses the TMDB API but is not endorsed or certified by TMDB.',
               '单一主数据源。实测：大陆需代理；original_language 用 zh=国语 / cn=粤语（非标准）'))
    log(f'建库完成：{DB}')
    log(f'解析 {len(SEEDS)} 位种子人物…')
    for name in SEEDS:
        b = pick_person(sess, name)
        if not b:
            log(f'  ✗ {name} 搜不到')
            continue
        c.execute('INSERT OR REPLACE INTO seed(person_id,query_name,name,department) VALUES(?,?,?,?)',
                  (b['id'], name, b.get('name'), b.get('known_for_department')))
        log(f"  ✓ {name} → {b.get('name')} (id={b['id']})")
        time.sleep(0.15)
    c.commit()
    c.close()


# ---------- plan ----------
def cmd_plan(sess):
    c = conn()
    seeds = c.execute('SELECT person_id, query_name, name FROM seed').fetchall()
    if not seeds:
        log('没有种子，先跑 init'); return

    films = {}      # movie_id -> {seed_hits, 元数据}
    for s in seeds:
        d = api_get(sess, f"/person/{s['person_id']}/combined_credits", language='zh-CN')
        if not d:
            log(f"  ! {s['name']} 取不到作品"); continue
        n = 0
        for c2 in (d.get('cast') or []) + (d.get('crew') or []):
            if c2.get('media_type') != 'movie':
                continue                                  # 需求 1.2：限定电影
            if c2.get('original_language') not in ZH_LANGS:
                continue                                  # 华语片
            mid = c2['id']
            f = films.setdefault(mid, {
                'title': c2.get('title'), 'original_title': c2.get('original_title'),
                'release_date': c2.get('release_date') or '',
                'vote_count': c2.get('vote_count') or 0,
                'popularity': c2.get('popularity') or 0.0,
                'original_language': c2.get('original_language'),
                'seed_hits': 0})
            f['seed_hits'] += 1
            n += 1
        log(f"  {s['name']:8s} 华语电影 {n} 条")
        time.sleep(0.15)

    ranked = sorted(films.items(), key=lambda kv: (-kv[1]['seed_hits'], -kv[1]['vote_count']))
    dense = sum(1 for _, v in ranked if v['seed_hits'] >= MIN_SEED_HITS)
    picked = ranked[:TARGET_FILMS]
    log(f'\n候选 {len(ranked)} 部；其中 {dense} 部有 ≥{MIN_SEED_HITS} 粒种子共同出演')
    log(f'按「稠密优先 + 热度补足」取前 {len(picked)} 部')

    for mid, v in picked:
        rd = v['release_date'] or ''
        yr = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
        c.execute('''INSERT OR REPLACE INTO movie
                     (id,title,original_title,original_language,release_date,release_year,
                      vote_count,popularity,seed_hits,source_id)
                     VALUES(?,?,?,?,?,?,?,?,?,?)''',
                  (mid, v['title'], v['original_title'], v['original_language'], rd, yr,
                   v['vote_count'], v['popularity'], v['seed_hits'], SOURCE_ID))
    c.commit()

    hits = c.execute('SELECT seed_hits, COUNT(*) n FROM movie GROUP BY seed_hits ORDER BY seed_hits DESC').fetchall()
    log('\n入选片的「种子命中数」分布：')
    for h in hits:
        log(f"  {h['seed_hits']} 粒种子出演：{h['n']:3d} 部")
    yr = c.execute('SELECT MIN(release_year) a, MAX(release_year) b FROM movie').fetchone()
    log(f'年份跨度：{yr["a"]}–{yr["b"]}')
    c.close()


# ---------- fetch ----------
def cmd_fetch(sess, limit=None, refresh=False):
    c = conn()
    todo = c.execute('''SELECT m.id FROM movie m
                        WHERE NOT EXISTS (SELECT 1 FROM credit c WHERE c.movie_id=m.id)
                        ORDER BY m.seed_hits DESC, m.vote_count DESC''').fetchall()
    if limit:
        todo = todo[:limit]
    log(f'待采集 {len(todo)} 部电影')
    if not todo:
        c.close(); return

    for i, row in enumerate(todo, 1):
        mid = row['id']
        det = api_get(sess, f'/movie/{mid}', refresh=refresh, language='zh-CN')
        cre = api_get(sess, f'/movie/{mid}/credits', refresh=refresh, language='zh-CN')
        if not det or not cre:
            log(f'  [{i}/{len(todo)}] {mid} 取不到，跳过')
            continue
        ts = NOW()
        rd = det.get('release_date') or ''
        yr = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
        c.execute('''UPDATE movie SET title=?,original_title=?,original_language=?,
                     origin_country=?,release_date=?,release_year=?,runtime=?,genres=?,
                     vote_count=?,vote_average=?,popularity=?,overview=?,poster_path=?,fetched_at=?
                     WHERE id=?''',
                  (det.get('title'), det.get('original_title'), det.get('original_language'),
                   json.dumps(det.get('origin_country') or [], ensure_ascii=False),
                   rd, yr, det.get('runtime'),
                   json.dumps([g.get('name') for g in (det.get('genres') or [])], ensure_ascii=False),
                   det.get('vote_count'), det.get('vote_average'), det.get('popularity'),
                   det.get('overview'), det.get('poster_path'), ts, mid))

        n = 0
        for kind, lst in (('cast', cre.get('cast') or []), ('crew', cre.get('crew') or [])):
            for p in lst:
                pid = p.get('id')
                if not pid:
                    continue
                # 人物基础信息顺带入库（详情字段留空，需要时再单独拉 /person/{id}）
                c.execute('''INSERT OR IGNORE INTO person
                             (id,name,known_for_department,popularity,profile_path,source_id,fetched_at)
                             VALUES(?,?,?,?,?,?,?)''',
                          (pid, p.get('name'), p.get('known_for_department'),
                           p.get('popularity'), p.get('profile_path'), SOURCE_ID, ts))
                c.execute('''INSERT OR IGNORE INTO credit
                             (movie_id,person_id,credit_type,department,job,character_name,
                              billing_order,source_id,fetched_at)
                             VALUES(?,?,?,?,?,?,?,?,?)''',
                          (mid, pid, kind, p.get('department') or ('Acting' if kind == 'cast' else None),
                           p.get('job') or ('Actor' if kind == 'cast' else None),
                           p.get('character'), p.get('order'), SOURCE_ID, ts))
                n += 1
        c.commit()
        if i % 10 == 0 or i == len(todo):
            log(f'  [{i}/{len(todo)}] {det.get("title")}  {n} 条演职员')
        time.sleep(0.2)
    c.close()


# ---------- enrich ----------
def cmd_enrich(sess, limit=None, refresh=False):
    """补全人物详情：生日、出生地、别名（F04 档案 / F02 别名检索要用）。

    只补图谱里出现过的人（graph_node），不补全部 7000 个低番位演员。
    """
    c = conn()
    todo = c.execute('''SELECT p.id, p.name FROM person p
                        WHERE p.detail_fetched = 0
                          AND p.id IN (SELECT DISTINCT person_id FROM graph_node)
                        ORDER BY p.popularity DESC''').fetchall()
    if limit:
        todo = todo[:limit]
    log(f'待补全人物 {len(todo)} 位')
    if not todo:
        c.close(); return
    for i, row in enumerate(todo, 1):
        d = api_get(sess, f"/person/{row['id']}", refresh=refresh, language='zh-CN')
        if not d:
            log(f"  [{i}/{len(todo)}] {row['name']} 取不到")
            continue
        c.execute('''UPDATE person SET birthday=?, deathday=?, place_of_birth=?,
                     gender=?, also_known_as=?, profile_path=COALESCE(?,profile_path),
                     popularity=COALESCE(?,popularity), detail_fetched=1, fetched_at=?
                     WHERE id=?''',
                  (d.get('birthday'), d.get('deathday'), d.get('place_of_birth'),
                   d.get('gender'),
                   json.dumps(d.get('also_known_as') or [], ensure_ascii=False),
                   d.get('profile_path'), d.get('popularity'), NOW(), row['id']))
        if i % 25 == 0 or i == len(todo):
            c.commit()
            log(f'  [{i}/{len(todo)}] {row["name"]}')
        time.sleep(0.12)
    c.commit()
    n = c.execute('SELECT COUNT(*) FROM person WHERE detail_fetched=1').fetchone()[0]
    log(f'已补全 {n} 位')
    c.close()


# ---------- update（F24 增量更新） ----------
SYNC_DDL = '''
CREATE TABLE IF NOT EXISTS sync_log (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT,
    finished_at     TEXT,
    range_from      TEXT,
    range_to        TEXT,
    movies_checked  INTEGER,
    movies_changed  INTEGER,
    credits_added   INTEGER,
    credits_removed INTEGER,
    edges_before    INTEGER,
    edges_after     INTEGER,
    detail          TEXT,
    note            TEXT
);
'''


def cmd_update(sess, days=14, refresh=True, dry=False):
    """按 TMDB 的 /movie/changes 找出变动的电影，只重取它们，然后重算关系。

    不重跑全量采集 —— 那是 F20 的活。这里只处理「源站说这些条目动过了」的部分。
    """
    c = conn()
    c.executescript(SYNC_DDL)
    started = NOW()

    # 上次同步到哪天；没有记录就回溯 days 天
    last = c.execute('SELECT MAX(range_to) FROM sync_log').fetchone()[0]
    today = datetime.date.today()
    if last:
        start = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
    else:
        start = today - datetime.timedelta(days=days)
    if start > today:
        log(f'上次同步到 {last}，之后没有新变动'); c.close(); return
    log(f'检查变动区间 {start} → {today}')

    changed = set()
    failed = []          # 取不到的区间 —— 必须区分「源站没变动」和「我们没连上」
    cur = start
    while cur <= today:
        end = min(cur + datetime.timedelta(days=13), today)   # TMDB 单次上限 14 天
        page = 1
        while True:
            d = api_get(sess, '/movie/changes', refresh=refresh,
                        start_date=cur.isoformat(), end_date=end.isoformat(), page=page)
            if not d:
                log(f'  ✗ {cur}–{end} 取不到（网络或代理问题）')
                failed.append(f'{cur}–{end}')
                break
            for m in d.get('results') or []:
                changed.add(m['id'])
            if page >= (d.get('total_pages') or 1):
                break
            page += 1
            time.sleep(0.2)
        log(f'  {cur}–{end}　累计变动 {len(changed)} 条')
        cur = end + datetime.timedelta(days=1)
        time.sleep(0.2)

    if failed:
        log(f'\n!! {len(failed)} 个区间没取到，本次结果不完整：{failed}')
        log('   检查代理是否启动（本机 7897）。本次不写入同步记录，下次会重试这些区间。')
        c.close()
        return

    ours = {r['id'] for r in c.execute('SELECT id FROM movie')}
    hit = sorted(changed & ours)
    log(f'\n源站变动 {len(changed)} 条，其中属于本样本的 {len(hit)} 部')
    edges_before = c.execute('SELECT COUNT(*) FROM relation').fetchone()[0]

    added = removed = 0
    touched = []
    for i, mid in enumerate(hit, 1):
        old_n = c.execute('SELECT COUNT(*) FROM credit WHERE movie_id=?', (mid,)).fetchone()[0]
        det = api_get(sess, f'/movie/{mid}', refresh=refresh, language='zh-CN')
        cre = api_get(sess, f'/movie/{mid}/credits', refresh=refresh, language='zh-CN')
        if not det or not cre:
            log(f'  [{i}/{len(hit)}] {mid} 源站已不可用，跳过（保留本地记录）')
            continue
        rd = det.get('release_date') or ''
        yr = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
        c.execute('''UPDATE movie SET title=?,original_title=?,original_language=?,origin_country=?,
                     release_date=?,release_year=?,runtime=?,genres=?,vote_count=?,vote_average=?,
                     popularity=?,overview=?,poster_path=?,fetched_at=? WHERE id=?''',
                  (det.get('title'), det.get('original_title'), det.get('original_language'),
                   json.dumps(det.get('origin_country') or [], ensure_ascii=False), rd, yr,
                   det.get('runtime'),
                   json.dumps([g.get('name') for g in (det.get('genres') or [])], ensure_ascii=False),
                   det.get('vote_count'), det.get('vote_average'), det.get('popularity'),
                   det.get('overview'), det.get('poster_path'), NOW(), mid))
        # 先清掉旧的演职员再写，否则删掉的条目会残留（INSERT OR IGNORE 只增不删）
        c.execute('DELETE FROM credit WHERE movie_id=?', (mid,))
        ts = NOW()
        for kind, lst in (('cast', cre.get('cast') or []), ('crew', cre.get('crew') or [])):
            for p in lst:
                if not p.get('id'):
                    continue
                c.execute('''INSERT OR IGNORE INTO person
                             (id,name,known_for_department,popularity,profile_path,source_id,fetched_at)
                             VALUES(?,?,?,?,?,?,?)''',
                          (p['id'], p.get('name'), p.get('known_for_department'),
                           p.get('popularity'), p.get('profile_path'), SOURCE_ID, ts))
                c.execute('''INSERT OR IGNORE INTO credit
                             (movie_id,person_id,credit_type,department,job,character_name,
                              billing_order,source_id,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                          (mid, p['id'], kind,
                           p.get('department') or ('Acting' if kind == 'cast' else None),
                           p.get('job') or ('Actor' if kind == 'cast' else None),
                           p.get('character'), p.get('order'), SOURCE_ID, ts))
        new_n = c.execute('SELECT COUNT(*) FROM credit WHERE movie_id=?', (mid,)).fetchone()[0]
        added += max(0, new_n - old_n)
        removed += max(0, old_n - new_n)
        if new_n != old_n:
            touched.append(f'{det.get("title")}({old_n}→{new_n})')
        c.commit()
        time.sleep(0.2)

    c.commit()
    if dry:
        log('（--dry 模式，不重算关系）')
        edges_after = edges_before
    else:
        c.close()
        import subprocess
        log('\n重算关系…')
        r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'build.py'), 'relations'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        log(r.stdout.strip() or r.stderr.strip())
        c = conn()
        edges_after = c.execute('SELECT COUNT(*) FROM relation').fetchone()[0]

    c.execute('''INSERT INTO sync_log(started_at,finished_at,range_from,range_to,movies_checked,
                 movies_changed,credits_added,credits_removed,edges_before,edges_after,detail,note)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
              (started, NOW(), start.isoformat(), today.isoformat(), len(ours), len(hit),
               added, removed, edges_before, edges_after,
               json.dumps(touched[:50], ensure_ascii=False),
               '按 /movie/changes 增量更新'))
    c.commit(); c.close()

    log(f'\n本样本变动 {len(hit)} 部；演职员明细 +{added} / -{removed}')
    log(f'合作边 {edges_before} → {edges_after}')


def cmd_syncs(limit=10):
    c = conn()
    try:
        rows_ = c.execute('SELECT * FROM sync_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    except sqlite3.OperationalError:
        log('还没有同步记录'); c.close(); return
    if not rows_:
        log('还没有同步记录')
    for r in rows_:
        log(f"[{r['id']}] {r['started_at']}  {r['range_from']}→{r['range_to']}  "
            f"检查 {r['movies_checked']} 部 / 变动 {r['movies_changed']} 部  "
            f"明细 +{r['credits_added']}/-{r['credits_removed']}  "
            f"边 {r['edges_before']}→{r['edges_after']}")
    c.close()


# ---------- status ----------
def cmd_status(sess):
    c = conn()
    q = lambda s: c.execute(s).fetchone()[0]
    log(f"数据库      {DB}  ({os.path.getsize(DB)//1024} KB)" if os.path.exists(DB) else '数据库不存在')
    log(f"种子人物    {q('SELECT COUNT(*) FROM seed')}")
    log(f"电影        {q('SELECT COUNT(*) FROM movie')} 部"
        f"（已采集演职员 {q('SELECT COUNT(DISTINCT movie_id) FROM credit')} 部）")
    log(f"人物        {q('SELECT COUNT(*) FROM person')}")
    log(f"演职员明细  {q('SELECT COUNT(*) FROM credit')} 条")
    r = c.execute('''SELECT COUNT(*) n FROM credit c
                     WHERE c.credit_type='cast' AND COALESCE(c.billing_order,999)<10
                        OR c.job='Director' ''').fetchone()
    log(f"其中图谱取样范围内（前 10 主演 + 导演） {r['n']} 条"
        f" → 去重人物 {q('''SELECT COUNT(DISTINCT person_id) FROM credit
                             WHERE (credit_type='cast' AND COALESCE(billing_order,999)<10)
                                OR job='Director' ''')}")
    c.close()


def main():
    ap = argparse.ArgumentParser(description='影脉 数据采集')
    ap.add_argument('cmd', choices=['init', 'plan', 'fetch', 'enrich', 'update', 'syncs', 'status'])
    ap.add_argument('--days', type=int, default=14, help='update 时首次回溯的天数')
    ap.add_argument('--dry', action='store_true', help='update 时只取数不重算关系')
    ap.add_argument('--proxy', default=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'))
    ap.add_argument('--limit', type=int, default=None, help='fetch 时限制部数，便于试跑')
    ap.add_argument('--refresh', action='store_true', help='忽略 raw/ 缓存')
    args = ap.parse_args()

    sess = make_session(args.proxy)
    {'init': lambda: cmd_init(sess),
     'plan': lambda: cmd_plan(sess),
     'fetch': lambda: cmd_fetch(sess, args.limit, args.refresh),
     'enrich': lambda: cmd_enrich(sess, args.limit, args.refresh),
     'update': lambda: cmd_update(sess, args.days, refresh=True, dry=args.dry),
     'syncs': cmd_syncs,
     'status': lambda: cmd_status(sess)}[args.cmd]()


if __name__ == '__main__':
    main()
