#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 端到端冒烟测试

对着运行中的服务跑一遍主要接口，覆盖 F01–F30 里已实现的部分。
用 Python 发请求而不是 curl —— Windows 的 shell 会把内联 JSON 里的中文转坏。

用法:
  python server/app.py          # 另开一个窗口
  python tests/smoke.py
  python tests/smoke.py --base http://127.0.0.1:8000 --admin testuser --password secret123
"""
import sys, io, json, argparse, datetime
import urllib.request, urllib.error, urllib.parse

if (sys.stdout.encoding or '').lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def call(base, path, method='GET', body=None, token=None, expect=200):
    url = base + path
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            code, text = r.status, r.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        code, text = e.code, e.read().decode('utf-8', 'replace')
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return code, parsed


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name:42s} {detail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:8000')
    ap.add_argument('--admin', default='testuser')
    ap.add_argument('--password', default='secret123')
    a = ap.parse_args()
    B = a.base.rstrip('/')

    print('【基础】')
    c, d = call(B, '/api/stats')
    check('GET /api/stats', c == 200 and d.get('movies', 0) > 0,
          f"{d.get('movies')} 部电影 / {d.get('graph_nodes')} 节点 / {d.get('graph_edges')} 边")
    c, d = call(B, '/api/search/person?q=' + urllib.parse.quote('梁朝伟') + '&limit=1')
    check('F02 人物检索（中文）', c == 200 and d.get('count') == 1,
          d['results'][0]['name'] if d.get('count') else '无结果')
    c, d = call(B, '/api/search/movie?q=' + urllib.parse.quote('功夫') + '&limit=3')
    check('F03 作品检索（中文）', c == 200 and d.get('count', 0) > 0, f"{d.get('count')} 条")

    print('\n【F01 账号 / F06 收藏】')
    tmp = 'smoke_' + datetime.datetime.now().strftime('%H%M%S')
    c, d = call(B, '/api/auth/register', 'POST', {'username': tmp, 'password': 'smoketest123'})
    check('注册', c == 200 and d.get('token'), f"角色 {d.get('user', {}).get('role')}")
    tok = d.get('token')
    c, d = call(B, '/api/auth/login', 'POST', {'username': tmp, 'password': 'wrongpass'})
    check('错误密码被拒', c == 401)
    c, d = call(B, '/api/favorites')
    check('未登录访问片单返回 401', c == 401)
    c, d = call(B, '/api/favorites', 'POST', {'movie_id': 9470, 'list_type': 'seen'}, tok)
    check('加入片单', c == 200 and d.get('ok'))
    c, d = call(B, '/api/favorites', token=tok)
    check('片单可读且隔离', c == 200 and d.get('count') == 1, f"{d['items'][0]['title'] if d.get('count') else ''}")

    print('\n【F17 推荐 + F18 反馈】')
    c, d = call(B, '/api/recommend/57607?limit=40', token=tok)
    base_ids = {x['id'] for x in d['results']}
    check('推荐返回候选', c == 200 and len(base_ids) > 0, f"{d['count']} 个候选")
    pick = d['results'][0]['id']
    c, d = call(B, '/api/feedback', 'POST', {'movie_id': pick, 'action': 'dislike'}, tok)
    check('标记不感兴趣', c == 200 and d.get('ok'))
    c, d2 = call(B, '/api/recommend/57607?limit=40', token=tok)
    check('不感兴趣的已从推荐排除', pick not in {x['id'] for x in d2['results']},
          f"排除计数 {d2.get('excluded_disliked')}")
    c, d = call(B, f'/api/feedback/{pick}', 'DELETE', token=tok)
    check('撤销反馈', c == 200)
    c, d3 = call(B, '/api/recommend/57607?limit=40', token=tok)
    check('撤销后又回到推荐', pick in {x['id'] for x in d3['results']})

    print('\n【F28 纠错管理】')
    c, d = call(B, '/api/auth/login', 'POST', {'username': a.admin, 'password': a.password})
    if c != 200:
        check('管理员登录', False, f'拿不到管理员账号 {a.admin}，跳过 F28/F30 的管理员部分')
        atok = None
    else:
        atok = d['token']
        check('管理员登录', True, f"角色 {d['user']['role']}")
        # 普通用户提交
        c, d = call(B, '/api/corrections', 'POST', {
            'target_type': 'movie', 'target_id': 9470, 'field': 'release_year',
            'new_value': '1900', 'reason': '冒烟测试：故意写错，随后驳回'}, tok)
        check('普通用户提交纠错', c == 200 and d.get('id'), f"编号 {d.get('id')}")
        cid = d.get('id')
        # 非管理员不能复核
        c, _ = call(B, f'/api/corrections/{cid}/review', 'POST', {'action': 'accept'}, tok)
        check('非管理员复核被拒（403）', c == 403)
        # 管理员驳回
        c, d = call(B, f'/api/corrections/{cid}/review', 'POST',
                    {'action': 'reject', 'note': '冒烟测试驳回'}, atok)
        check('管理员驳回', c == 200 and d.get('status') == 'rejected')
        c, d = call(B, '/api/movie/9470')
        check('驳回后数据未被改动', d['movie']['release_year'] == 2004,
              f"年份 = {d['movie']['release_year']}")
        # 提交并接受，验证数据真的改了
        c, d = call(B, '/api/corrections', 'POST', {
            'target_type': 'movie', 'target_id': 9470, 'field': 'title',
            'new_value': '功夫', 'reason': '冒烟测试：接受路径'}, tok)
        cid2 = d['id']
        c, d = call(B, f'/api/corrections/{cid2}/review', 'POST',
                    {'action': 'accept', 'note': '冒烟测试接受'}, atok)
        check('管理员接受', c == 200 and d.get('status') == 'accepted')
        c, d = call(B, '/api/corrections')
        check('纠错状态统计可读', c == 200 and 'by_status' in d, str(d.get('by_status')))

    print('\n【F30 备份 / F24 同步记录】')
    c, d = call(B, '/api/backups')
    check('快照列表可读', c == 200, f"{len(d.get('items', []))} 个")
    if atok:
        c, d = call(B, f'/api/backups?note={urllib.parse.quote("冒烟测试")}', 'POST', token=atok)
        check('创建快照', c == 200 and d.get('name'), d.get('name'))
        name = d.get('name')
        if name:
            c, d = call(B, f'/api/backups/{urllib.parse.quote(name)}/verify', 'POST', token=atok)
            check('校验快照', c == 200 and d.get('ok'), '行数全部一致' if d.get('ok') else '有不一致')
    c, d = call(B, '/api/syncs')
    check('同步记录可读', c == 200, f"{d.get('count', 0)} 条")

    print('\n' + '=' * 62)
    print(f'通过 {len(PASS)}　失败 {len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  ✗', f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
