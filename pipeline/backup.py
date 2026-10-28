#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影脉 备份与恢复（F30）

快照 = 数据库副本 + 一份清单（行数、校验和、创建时间、备注）。
校验不只看文件能不能打开，还要比对各表行数与清单是否一致 —— 文件在但内容缺了才最危险。

用法:
  python backup.py create [--note "说明"]   创建快照
  python backup.py list                     列出所有快照
  python backup.py verify <名称>            校验快照（含行数比对）
  python backup.py restore <名称> [--yes]   从快照恢复（会先自动备份当前库）
  python backup.py prune --keep 10          只保留最近 N 个
"""
import os, sys, io, json, shutil, sqlite3, hashlib, argparse, datetime

if (sys.stdout.encoding or '').lower().replace('-', '') != 'utf8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, 'yingmai.db')
DIR = os.path.join(ROOT, 'backups')
STAMP = lambda: datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
log = lambda *a: print(*a, flush=True)

# 清单里要记行数的表（推导表也算，恢复后能立刻判断是否需要重建）
COUNTED = ['source', 'seed', 'person', 'movie', 'credit',
           'graph_node', 'relation', 'relation_evidence',
           'app_user', 'favorite', 'audit_log', 'correction', 'feedback']


def file_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def counts(path):
    """各表行数；表不存在就记 None（老快照可能没有后加的表）。"""
    c = sqlite3.connect(path)
    out = {}
    for t in COUNTED:
        try:
            out[t] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    try:
        ok = c.execute('PRAGMA integrity_check').fetchone()[0]
    except sqlite3.DatabaseError as e:
        ok = f'error: {e}'
    c.close()
    return out, ok


def manifest_path(name):
    return os.path.join(DIR, name.replace('.db', '') + '.json')


def cmd_create(note=''):
    if not os.path.exists(DB):
        log(f'数据库不存在：{DB}'); return
    os.makedirs(DIR, exist_ok=True)
    name = f'yingmai-{STAMP()}.db'
    dst = os.path.join(DIR, name)
    # 用 SQLite 的在线备份 API，比直接复制文件安全（不怕写入中途复制到半个事务）
    src = sqlite3.connect(DB)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close(); src.close()

    cnt, integ = counts(dst)
    man = {'name': name, 'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
           'note': note, 'bytes': os.path.getsize(dst), 'sha256': file_hash(dst),
           'integrity_check': integ, 'counts': cnt}
    with open(manifest_path(name), 'w', encoding='utf-8') as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    log(f'✓ 快照已创建 {name}  ({man["bytes"]//1024} KB)')
    log(f'  完整性检查 {integ}　电影 {cnt.get("movie")} 部　人物 {cnt.get("person")} 人　'
        f'合作边 {cnt.get("relation")} 条')
    return name


def load_man(name):
    p = manifest_path(name)
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding='utf-8'))


def all_snapshots():
    if not os.path.isdir(DIR):
        return []
    out = []
    for f in sorted(os.listdir(DIR)):
        if f.endswith('.db'):
            m = load_man(f) or {}
            out.append({'name': f, 'size': os.path.getsize(os.path.join(DIR, f)),
                        'created_at': m.get('created_at', '—'),
                        'note': m.get('note', ''), 'has_manifest': bool(m)})
    return sorted(out, key=lambda x: x['created_at'], reverse=True)


def cmd_list():
    ss = all_snapshots()
    if not ss:
        log('还没有快照。用 python backup.py create 创建一个。'); return
    log(f'{"快照":34s} {"大小":>9s}  创建时间             备注')
    for s in ss:
        log(f'{s["name"]:34s} {s["size"]//1024:>7d} KB  {s["created_at"]:19s} {s["note"]}')


def cmd_verify(name):
    path = os.path.join(DIR, name if name.endswith('.db') else name + '.db')
    if not os.path.exists(path):
        log(f'✗ 快照不存在：{path}'); return False
    man = load_man(os.path.basename(path))
    ok = True
    log(f'校验 {os.path.basename(path)}')
    cnt, integ = counts(path)
    log(f'  {"✓" if integ == "ok" else "✗"} 完整性检查        {integ}')
    ok &= (integ == 'ok')
    if not man:
        log('  ! 没有清单文件，只能做完整性检查，无法比对行数')
        return ok
    h = file_hash(path)
    log(f'  {"✓" if h == man["sha256"] else "✗"} 校验和             {h[:16]}… '
        f'(清单 {man["sha256"][:16]}…)')
    ok &= (h == man['sha256'])
    bad = []
    for t, v in man['counts'].items():
        cur = cnt.get(t)
        if v is None and cur is None:
            continue
        mark = '✓' if cur == v else '✗'
        if cur != v:
            bad.append(t)
        log(f'  {mark} {t:20s} {cur} (清单 {v})')
    if bad:
        log(f'  ✗ 行数不一致：{bad}')
        ok = False
    log('通过' if ok else '未通过')
    return ok


def cmd_restore(name, yes=False):
    path = os.path.join(DIR, name if name.endswith('.db') else name + '.db')
    if not os.path.exists(path):
        log(f'✗ 快照不存在：{path}'); return
    if not cmd_verify(os.path.basename(path)):
        if not yes:
            log('\n快照校验未通过，已中止。确实要用就加 --yes。'); return
        log('\n警告：快照校验未通过，仍按 --yes 继续。')
    if not yes:
        log(f'\n将用 {os.path.basename(path)} 覆盖当前数据库 {DB}。')
        log('当前库会先自动备份一份。确认请输入 yes：')
        if input().strip().lower() != 'yes':
            log('已取消'); return
    # 先给当前库留一份，避免恢复错了没有退路
    if os.path.exists(DB):
        safety = cmd_create(note=f'恢复 {os.path.basename(path)} 前的自动备份')
        log(f'当前库已备份为 {safety}')
    shutil.copy2(path, DB)
    cnt, integ = counts(DB)
    log(f'✓ 已恢复。完整性 {integ}　电影 {cnt.get("movie")} 部　合作边 {cnt.get("relation")} 条')
    log('提示：如果恢复的是采集快照，关系表可能需要在 pipeline 下重跑 build.py relations。')


def cmd_prune(keep):
    ss = all_snapshots()
    if len(ss) <= keep:
        log(f'共 {len(ss)} 个快照，无需清理'); return
    for s in ss[keep:]:
        os.remove(os.path.join(DIR, s['name']))
        mp = manifest_path(s['name'])
        if os.path.exists(mp):
            os.remove(mp)
        log(f'已删除 {s["name"]}')
    log(f'保留最近 {keep} 个')


def main():
    ap = argparse.ArgumentParser(description='影脉 备份与恢复')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('create'); c.add_argument('--note', default='')
    sub.add_parser('list')
    v = sub.add_parser('verify'); v.add_argument('name')
    r = sub.add_parser('restore'); r.add_argument('name'); r.add_argument('--yes', action='store_true')
    p = sub.add_parser('prune'); p.add_argument('--keep', type=int, default=10)
    a = ap.parse_args()
    {'create': lambda: cmd_create(a.note),
     'list': cmd_list,
     'verify': lambda: cmd_verify(a.name),
     'restore': lambda: cmd_restore(a.name, a.yes),
     'prune': lambda: cmd_prune(a.keep)}[a.cmd]()


if __name__ == '__main__':
    main()
