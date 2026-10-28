#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 数据探针（TMDB）

用途：动手开发前，用少量样本实测 TMDB 数据的**可获得性与形态**，
      把「能不能做」从猜测变成结论（对应需求报告 2.1「开发前须验证 API 凭据、
      网络可达性和 20 部电影样本」）。

采集范围（2026-10-14 定）：**华语片**，按 original_language=zh 筛选，目标 200 部。
种子来源 /discover/movie —— 不能用 /movie/popular，那是全球榜，会混进大量非华语片。

用法:
  python probe_tmdb.py                    # 默认探 20 部热门华语片
  python probe_tmdb.py --n 50             # 探 50 部
  python probe_tmdb.py --year-from 1980 --year-to 2025
  python probe_tmdb.py --proxy http://127.0.0.1:7897
  python probe_tmdb.py --refresh          # 忽略 raw/ 缓存，重新联网拉取
  python probe_tmdb.py --offline          # 只用 raw/ 缓存重放，不联网

API key 来源（二选一）:
  1. 环境变量 TMDB_API_KEY
  2. pipeline/tmdb_apikey.txt（一行纯文本，**不要提交到 git**）

输出:
  raw/                    原始 JSON 快照（可重放，也是断网时的兜底）
  _work/数据体检报告.md    实测报告，可直接摘进需求文档
"""
import os, sys, io, re, json, time, argparse, datetime, hashlib

# 幂等包装：别的脚本可能已经包过了。重复包会让旧 wrapper 被回收时关掉底层 buffer。
if (sys.stdout.encoding or '').lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(ROOT, 'raw')
WORK_DIR = os.path.join(ROOT, '_work')
API = 'https://api.themoviedb.org/3'
UA = 'yingmai-course/1.0 (mailto:student@hdu.edu.cn)'
CJK = re.compile(r'[一-鿿]')
NOW = lambda: datetime.datetime.now().isoformat(timespec='seconds')
log = lambda *a: print(*a, flush=True)

# 取样规则（先按此探针，实测后再定 N）
TOP_CAST_N = 10          # 每部电影取 cast 前 N 位
PERSON_SAMPLE = 8        # 抽几个主要演员探 person 接口

# 采集范围：华语片。TMDB 用的是**非标准**语言码，实测（2026-10-14）：
#   'zh' = 国语/普通话片（志愿军、唐人街探案…）      discover 共 512 部
#   'cn' = 粤语片（花样年华、重庆森林、少林足球…）   discover 共 337 部
# 只筛 'zh' 会丢掉全部粤语片，占华语条目约一半。必须两个都要。
# 若日后改为「只要大陆出品」，把 LANGUAGE 换成 None 并设 ORIGIN_COUNTRY='CN'。
LANGUAGE = 'zh|cn'
ZH_LANGS = ('zh', 'cn')
ORIGIN_COUNTRY = None


def load_key():
    k = os.environ.get('TMDB_API_KEY', '').strip()
    if k:
        return k
    for p in (os.path.join(HERE, 'tmdb_apikey.txt'), os.path.join(ROOT, 'tmdb_apikey.txt')):
        if os.path.exists(p):
            return open(p, encoding='utf-8').read().strip()
    return ''


KEY = load_key()


def make_session(proxy):
    import requests
    s = requests.Session()
    s.headers['User-Agent'] = UA
    s.headers['Accept'] = 'application/json'
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
        log(f'  代理: {proxy}')
    return s


def cache_path(path, params):
    key = path + '?' + '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
    h = hashlib.md5(key.encode('utf-8')).hexdigest()[:10]
    safe = re.sub(r'[^0-9A-Za-z]+', '_', path).strip('_')[:60]
    return os.path.join(RAW_DIR, f'{safe}.{h}.json')


def api_get(sess, path, *, refresh=False, offline=False, tries=3, **params):
    """带缓存与重试的 GET。命中 raw/ 缓存则不发请求（省钱也省时间）。"""
    params = {k: v for k, v in params.items() if v is not None}
    cp = cache_path(path, params)
    if os.path.exists(cp) and not refresh:
        return json.load(open(cp, encoding='utf-8'))
    if offline:
        return None
    p = dict(params, api_key=KEY)
    for i in range(tries):
        try:
            r = sess.get(API + path, params=p, timeout=25)
        except Exception as e:
            log(f'   ! 请求异常 {type(e).__name__}: {str(e)[:100]}')
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 200:
            os.makedirs(RAW_DIR, exist_ok=True)
            with open(cp, 'w', encoding='utf-8') as f:
                json.dump(r.json(), f, ensure_ascii=False)
            return r.json()
        if r.status_code == 401:
            log('   !! 401 未授权：API key 缺失或无效')
            sys.exit(3)
        if r.status_code == 429:
            wait = int(r.headers.get('Retry-After', '3') or 3)
            log(f'   ! 429 限流，等待 {wait}s')
            time.sleep(wait)
            continue
        log(f'   ! HTTP {r.status_code} {path}')
        time.sleep(1.0 * (i + 1))
    return None


def precheck(sess):
    """连通性 + 鉴权。不通就给出一句话诊断，别让人对着超时猜。"""
    log('【0】连通性与鉴权')
    t0 = time.time()
    try:
        r = sess.get(API + '/configuration', timeout=15,
                     params={'api_key': KEY} if KEY else None)
    except Exception as e:
        log(f'  ✗ 连不上 api.themoviedb.org（{type(e).__name__}: {str(e)[:120]}）')
        log('')
        log('  这个域名在中国大陆被 DNS 污染（会解析到 Twitter/Dropbox 段的假 IP），')
        log('  必须走代理。先启动代理客户端，然后：')
        log('    export HTTPS_PROXY=http://127.0.0.1:7897')
        log('    curl -s -o /dev/null -w "%{http_code}\\n" --max-time 15 \\')
        log('         https://api.themoviedb.org/3/configuration')
        log('  返回 401 = 通了（只是没带 key）；返回 000 = 还没通。')
        sys.exit(1)
    dt = time.time() - t0
    if r.status_code == 401:
        if KEY:
            log(f'  ✓ 网络通（{dt:.2f}s），但 key 被拒绝（401）')
            log('  !! 检查 pipeline/tmdb_apikey.txt 里是不是 v3 auth 那一串；')
            log('     如果是 v4 Read Access Token（很长的一串 JWT），本脚本用不了。')
        else:
            log(f'  ✓ 网络通（{dt:.2f}s），但未带 key —— 符合预期')
            log('  !! 没有 API key，只能做连通性验证，无法取数据。')
            log('     申请：https://www.themoviedb.org/settings/api （非商业免费）')
            log('     设置：环境变量 TMDB_API_KEY，或写入 pipeline/tmdb_apikey.txt')
        sys.exit(3)
    if r.status_code == 200:
        log(f'  ✓ 网络通且鉴权成功（{dt:.2f}s）' + ('（已带 key）' if KEY else '（未带 key）'))
        return True
    log(f'  ✗ 意外状态码 {r.status_code}')
    sys.exit(1)


def pctl(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def has_cjk(s):
    return bool(s and CJK.search(s))


def probe_movies(sess, n, refresh, offline, yfrom, yto, min_votes):
    log(f'\n【1】拉取 {n} 部华语电影'
        f'（种子 /discover/movie，original_language={LANGUAGE}，'
        f'{yfrom}–{yto}，vote_count≥{min_votes}）')
    movies, page = [], 1
    while len(movies) < n:
        d = api_get(sess, '/discover/movie', refresh=refresh, offline=offline,
                    language='zh-CN',
                    with_original_language=LANGUAGE,
                    with_origin_country=ORIGIN_COUNTRY,
                    sort_by='popularity.desc',
                    page=page,
                    **{'vote_count.gte': min_votes,
                       'primary_release_date.gte': f'{yfrom}-01-01',
                       'primary_release_date.lte': f'{yto}-12-31'})
        if not d or not d.get('results'):
            break
        movies += d['results']
        page += 1
        if page > d.get('total_pages', 1):
            break
    movies = movies[:n]
    log(f'  得到 {len(movies)} 部')
    return movies


def probe_credits(sess, movies, refresh, offline):
    log(f'\n【2】逐部拉取演职员表（/movie/{{id}}/credits，language=zh-CN）')
    rows = []
    for i, m in enumerate(movies, 1):
        c = api_get(sess, f"/movie/{m['id']}/credits", refresh=refresh,
                    offline=offline, language='zh-CN')
        if not c:
            log(f'  [{i}/{len(movies)}] {m["id"]} 取不到 credits，跳过')
            continue
        # /discover 的结果里没有 origin_country 和 genres，得单独取详情
        det = api_get(sess, f"/movie/{m['id']}", refresh=refresh,
                      offline=offline, language='zh-CN') or {}
        cast = c.get('cast') or []
        crew = c.get('crew') or []
        directors = [p for p in crew if p.get('job') == 'Director']
        # 同一人在同一部片里出现几次（验证"同人物对同电影只计一次"的必要性）
        ids = [p['id'] for p in cast] + [p['id'] for p in crew]
        dup = len(ids) - len(set(ids))
        rows.append({
            'id': m['id'],
            'title': det.get('title') or m.get('title'),
            'original_title': det.get('original_title') or m.get('original_title'),
            'original_language': det.get('original_language') or m.get('original_language'),
            'origin_country': det.get('origin_country') or [],
            'genres': [g.get('name') for g in (det.get('genres') or [])],
            'runtime': det.get('runtime'),
            'vote_count': m.get('vote_count'),
            'release_date': det.get('release_date') or m.get('release_date') or '',
            'popularity': m.get('popularity'),
            'cast': cast,
            'crew': crew,
            'n_cast': len(cast),
            'n_crew': len(crew),
            'n_director': len(directors),
            'dup_roles': dup,
            'has_order': all('order' in p for p in cast[:5]) if cast else False,
        })
        if i % 10 == 0 or i == len(movies):
            log(f'  [{i}/{len(movies)}] …')
        time.sleep(0.2)   # TMDB 免费额度约 50 req/s，保持礼貌
    log(f'  成功 {len(rows)} 部')
    return rows


def probe_persons(sess, rows, refresh, offline):
    log(f'\n【3】抽 {PERSON_SAMPLE} 位主要演员探人物接口')
    picks = []
    for r in rows:
        for p in sorted(r['cast'], key=lambda x: x.get('order', 999))[:2]:
            if p['id'] not in [q['id'] for q in picks]:
                picks.append(p)
        if len(picks) >= PERSON_SAMPLE:
            break
    picks = picks[:PERSON_SAMPLE]

    out = []
    for p in picks:
        d = api_get(sess, f"/person/{p['id']}", refresh=refresh, offline=offline,
                    language='zh-CN')
        cc = api_get(sess, f"/person/{p['id']}/combined_credits", refresh=refresh,
                     offline=offline, language='zh-CN')
        aka = (d or {}).get('also_known_as') or []
        cast_credits = (cc or {}).get('cast') or []
        crew_credits = (cc or {}).get('crew') or []
        out.append({
            'id': p['id'],
            'name': p.get('name'),
            'name_cjk': has_cjk(p.get('name')),
            'aka_cjk': [a for a in aka if has_cjk(a)],
            'aka_total': len(aka),
            'department': (d or {}).get('known_for_department'),
            'birthday': (d or {}).get('birthday'),
            'n_cast_credits': len(cast_credits),
            'n_crew_credits': len(crew_credits),
        })
        log(f"  {p.get('name')}  作品 {len(cast_credits)}+{len(crew_credits)}  "
            f"中文别名 {len(out[-1]['aka_cjk'])}")
        time.sleep(0.12)
    return out


def analyze(rows, persons, n_target):
    a = {}
    cast_sizes = [r['n_cast'] for r in rows]
    crew_sizes = [r['n_crew'] for r in rows]
    a['movies'] = len(rows)
    a['cast_min'] = min(cast_sizes) if cast_sizes else 0
    a['cast_p50'] = pctl(cast_sizes, 50)
    a['cast_p90'] = pctl(cast_sizes, 90)
    a['cast_max'] = max(cast_sizes) if cast_sizes else 0
    a['cast_mean'] = round(sum(cast_sizes) / len(cast_sizes), 1) if cast_sizes else 0
    a['crew_mean'] = round(sum(crew_sizes) / len(crew_sizes), 1) if crew_sizes else 0
    a['order_ok'] = sum(1 for r in rows if r['has_order'])
    a['dup_movies'] = sum(1 for r in rows if r['dup_roles'] > 0)
    a['dup_total'] = sum(r['dup_roles'] for r in rows)

    # 全量人物 vs 取样规则下的人物
    all_ids, top_ids = set(), set()
    for r in rows:
        for p in r['cast'] + r['crew']:
            all_ids.add(p['id'])
        for p in sorted(r['cast'], key=lambda x: x.get('order', 999))[:TOP_CAST_N]:
            top_ids.add(p['id'])
        for p in r['crew']:
            if p.get('job') == 'Director':
                top_ids.add(p['id'])
    a['persons_all'] = len(all_ids)
    a['persons_top'] = len(top_ids)
    a['persons_per_movie_all'] = round(len(all_ids) / len(rows), 1) if rows else 0
    a['cap_top'] = n_target * (TOP_CAST_N + 1)   # 按规则的人数上界

    # 演员重合度：这批片之间有多少共同演员（决定图谱稠密还是散点）
    casts = [set(p['id'] for p in r['cast']) for r in rows]
    shared = []
    for i in range(len(casts)):
        for j in range(i + 1, len(casts)):
            shared.append(len(casts[i] & casts[j]))
    a['pair_shared_mean'] = round(sum(shared) / len(shared), 2) if shared else 0
    a['pair_shared_max'] = max(shared) if shared else 0
    a['pair_zero_pct'] = round(100.0 * sum(1 for s in shared if s == 0) / len(shared), 1) if shared else 0
    a['edge_estimate'] = sum(s * (s - 1) // 2 for s in shared) if shared else 0

    # 地域与年份分布（验证「华语片」口径筛得对不对，以及 F13 时间线有没有料）
    a['origin_counter'] = {}
    for r in rows:
        for c in (r.get('origin_country') or ['?']):
            a['origin_counter'][c] = a['origin_counter'].get(c, 0) + 1
    a['no_origin'] = sum(1 for r in rows if not r.get('origin_country'))
    a['no_date'] = sum(1 for r in rows if not r.get('release_date'))
    a['decade_counter'] = {}
    for r in rows:
        d = r.get('release_date') or ''
        if len(d) >= 4 and d[:4].isdigit():
            dec = (int(d[:4]) // 10) * 10
            a['decade_counter'][dec] = a['decade_counter'].get(dec, 0) + 1
    years = [r['release_date'][:4] for r in rows if r.get('release_date')]
    a['year_min'] = min(years) if years else '—'
    a['year_max'] = max(years) if years else '—'

    # 中文覆盖
    a['title_zh'] = sum(1 for r in rows if has_cjk(r.get('title')))
    a['lang_counter'] = {}
    for r in rows:
        k = r.get('original_language') or '?'
        a['lang_counter'][k] = a['lang_counter'].get(k, 0) + 1
    a['person_name_cjk'] = sum(1 for p in persons if p['name_cjk'])
    a['person_aka_cjk'] = sum(1 for p in persons if p['aka_cjk'])
    a['person_sample'] = len(persons)
    a['credits_mean'] = round(sum(p['n_cast_credits'] + p['n_crew_credits']
                                  for p in persons) / len(persons), 1) if persons else 0
    return a


def report(rows, persons, a, n_target):
    os.makedirs(WORK_DIR, exist_ok=True)
    out = os.path.join(WORK_DIR, '数据体检报告.md')
    L = []
    L.append('# 影脉 · TMDB 数据体检报告\n')
    L.append(f'实测时间：{NOW()}　·　样本：{a["movies"]} 部电影　'
             f'（种子 `/discover/movie`，original_language={LANGUAGE}，'
             f'目标规模 {n_target} 部）\n')

    L.append('## 1. 数据可获得性\n')
    L.append(f'- 已成功取得 **{a["movies"]}** 部电影的演职员表，接口 `/movie/{{id}}/credits` 可用\n')
    L.append(f'- cast 的 `order` 字段可用率 {a["order_ok"]}/{a["movies"]}，'
             f'可用于「取前 {TOP_CAST_N} 位主演」的取样规则\n')

    L.append('\n## 2. 演职员规模（决定图谱会不会爆）\n')
    L.append('| 指标 | 值 |\n|---|---|')
    L.append(f'| 每部 cast 人数 中位数 / P90 / 最大 | {a["cast_p50"]:.0f} / {a["cast_p90"]:.0f} / {a["cast_max"]} |')
    L.append(f'| 每部 cast 平均 | {a["cast_mean"]} |')
    L.append(f'| 每部 crew 平均 | {a["crew_mean"]} |')
    L.append(f'| 本样本去重人物数（全量 cast+crew） | {a["persons_all"]} |')
    L.append(f'| 本样本去重人物数（前 {TOP_CAST_N} 主演 + 导演） | {a["persons_top"]} |')
    L.append(f'| 全量口径下 每部平均人物数 | {a["persons_per_movie_all"]} |\n')

    proj = a['persons_per_movie_all'] * n_target
    L.append(f'**外推到 {n_target} 部**：\n')
    L.append(f'- 取全量 cast+crew：约 **{proj:.0f} 人** —— '
             f'{"超出 N01 的 5000 人上限，必须收窄口径" if proj > 5000 else "在 N01 的 5000 人上限内"}')
    L.append(f'- 按「前 {TOP_CAST_N} 主演 + 导演」：人数上界 `{n_target} × {TOP_CAST_N + 1} = {a["cap_top"]}`，'
             f'**结构性保证不超 5000**，且图谱可读\n')

    L.append('\n## 3. 合作网络的稠密度（这批片单能不能看出关系）\n')
    L.append('| 指标 | 值 |\n|---|---|')
    L.append(f'| 任两部片平均共同演员数 | {a["pair_shared_mean"]} |')
    L.append(f'| 最大共同演员数 | {a["pair_shared_max"]} |')
    L.append(f'| 完全无共同演员的片对占比 | {a["pair_zero_pct"]}% |')
    L.append(f'| 本样本可形成的合作边数（估） | {a["edge_estimate"]} |\n')
    if a['pair_zero_pct'] > 50:
        L.append('> ⚠ 过半片对没有任何共同演员，说明这份片单偏散。'
                 '图谱会呈现为大量孤立小簇，F09/F14/F15 的结论会很弱。'
                 '建议改用「若干位演员/导演的作品全集」作种子。\n')
    else:
        L.append('> 片单整体连通性尚可，可以支撑图谱演示。\n')

    L.append('\n## 4. 多职务情况（验证「每对人物每部电影只计一次」）\n')
    L.append(f'- {a["dup_movies"]}/{a["movies"]} 部电影存在同一人在片中担任多个职务，'
             f'共 {a["dup_total"]} 人次\n')
    L.append('> 若不去重，同一部片会把同一对人物的合作次数重复累加，F09 频次和 F12 证据都会失真。'
             '需求报告业务规则里已写死此条，实测确认**必要**。\n')

    L.append('\n## 5. 地域与年份分布（验证「华语片」口径）\n')
    origins = sorted(a['origin_counter'].items(), key=lambda x: -x[1])
    L.append('| 出品国家/地区 | 部数 |\n|---|---|')
    for k, v in origins:
        L.append(f'| {k} | {v} |')
    L.append(f'| （无 origin_country） | {a["no_origin"]} |\n')
    L.append(f'年份范围：{a["year_min"]}–{a["year_max"]}，缺失发行日期 {a["no_date"]} 部\n')
    L.append('| 年代 | 部数 |\n|---|---|')
    for k in sorted(a['decade_counter']):
        L.append(f'| {k}s | {a["decade_counter"][k]} |')
    L.append('')
    if len(a['decade_counter']) <= 2:
        L.append('> ⚠ 样本集中在少数年代，F13 合作时间线会退化成一根柱子。'
                 '若时间线是 P0，片单要跨年代抽样，不能只按 popularity 取前 N。\n')
    else:
        L.append('> 年代跨度尚可，F13 时间线有内容可画。\n')

    L.append('\n## 6. 中文覆盖（影响 F02 检索与 N03 易用性）\n')
    L.append('| 指标 | 值 |\n|---|---|')
    L.append(f'| 有中文标题的电影 | {a["title_zh"]}/{a["movies"]} |')
    L.append(f'| 抽样的 {a["person_sample"]} 位演员中 主名含中文 | {a["person_name_cjk"]} |')
    L.append(f'| 抽样演员中 别名含中文 | {a["person_aka_cjk"]} |')
    L.append(f'| 抽样演员平均作品数（cast+crew） | {a["credits_mean"]} |\n')
    langs = sorted(a['lang_counter'].items(), key=lambda x: -x[1])
    L.append('原始语言分布：' + '、'.join(f'{k} {v} 部' for k, v in langs) +
             '　（应全部为 zh，否则说明筛选条件没生效）\n')
    if a['person_sample'] and a['person_name_cjk'] == a['person_sample']:
        L.append('> ✓ 抽样演员主名**全部含中文**，F02「按中文姓名检索」与 N03「中文界面」'
                 '直接成立——这是把范围收窄到华语片换来的最大收益。\n')
    elif a['person_name_cjk']:
        L.append(f'> ⚠ 仅 {a["person_name_cjk"]}/{a["person_sample"]} 的演员主名含中文，'
                 '部分人物需靠 also_known_as 别名匹配，F02 检索要同时查主名和别名。\n')
    else:
        L.append('> ⚠ 演员主名几乎不含中文，F02「按中文姓名检索」只能靠别名匹配，'
                 '需要在人物档案里回填中文名，或在范围声明里写明「以原始语言姓名为准」。\n')

    L.append('\n## 7. 抽样人物明细\n')
    L.append('| 人物 | 中文别名 | 部门 | 作品数 |\n|---|---|---|---|')
    for p in persons:
        L.append(f"| {p['name']} | {'、'.join(p['aka_cjk'][:3]) or '—'} | "
                 f"{p['department'] or '—'} | {p['n_cast_credits']}+{p['n_crew_credits']} |")
    L.append('')

    L.append('\n---\n')
    L.append('原始 JSON 快照已存于 `raw/`，可离线重放（`python probe_tmdb.py --offline`）。\n')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return out


def main():
    ap = argparse.ArgumentParser(description='影脉 TMDB 数据探针（华语片）')
    ap.add_argument('--n', type=int, default=20, help='探多少部电影（默认 20）')
    ap.add_argument('--target', type=int, default=200, help='外推的目标规模（默认 200）')
    ap.add_argument('--year-from', type=int, default=1980, help='起始年份（默认 1980）')
    ap.add_argument('--year-to', type=int, default=datetime.date.today().year,
                    help='结束年份（默认今年）')
    ap.add_argument('--min-votes', type=int, default=50,
                    help='最低评价人数，滤掉无人问津的条目（默认 50）')
    ap.add_argument('--proxy', default=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'),
                    help='代理地址，默认读 HTTPS_PROXY')
    ap.add_argument('--refresh', action='store_true', help='忽略缓存重新拉取')
    ap.add_argument('--offline', action='store_true', help='只用缓存重放，不联网')
    args = ap.parse_args()

    if not args.offline and not KEY:
        log('!! 未找到 API key。')
        log('   申请：https://www.themoviedb.org/settings/api （非商业免费）')
        log('   设置：export TMDB_API_KEY=xxx   或写入 pipeline/tmdb_apikey.txt')
        log('   也可以先只验连通性：不带 key 运行会在 401 处停下并给出诊断。')
        log('')

    sess = make_session(args.proxy)
    if not args.offline:
        precheck(sess)

    movies = probe_movies(sess, args.n, args.refresh, args.offline,
                          args.year_from, args.year_to, args.min_votes)
    if not movies:
        log('✗ 没拿到电影列表，检查网络/代理后重试')
        sys.exit(1)
    rows = probe_credits(sess, movies, args.refresh, args.offline)
    persons = probe_persons(sess, rows, args.refresh, args.offline)
    a = analyze(rows, persons, args.target)
    out = report(rows, persons, a, args.target)

    log('\n' + '=' * 56)
    log(f"样本 {a['movies']} 部 · 每部 cast 中位数 {a['cast_p50']:.0f}（最大 {a['cast_max']}）")
    log(f"去重人物：全量口径 {a['persons_all']} · 前{TOP_CAST_N}+导演口径 {a['persons_top']}")
    log(f"片对平均共同演员 {a['pair_shared_mean']} · 零共同演员片对 {a['pair_zero_pct']}%")
    log(f"演员主名含中文 {a['person_name_cjk']}/{a['person_sample']}")
    log('=' * 56)
    log(f'\n报告已写入: {out}')
    log(f'原始快照目录: {RAW_DIR}')


if __name__ == '__main__':
    main()
