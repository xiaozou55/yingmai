# 影脉 —— 影视人物合作关系与作品发现平台

把公开的电影元数据与演职员记录整理成「人物—作品」网络，据此分析共同参与关系，
提供人物检索、关系探索与作品发现。

> **平台中的「合作」仅指共同参与作品**，不代表私人友谊、直接对手戏或实际交流。
> 每条关系都可追溯到具体作品与来源。

## 目录结构

```
影视/
├── pipeline/            数据管线
│   ├── probe_tmdb.py    数据探针（体检报告、原始 JSON 缓存）
│   ├── seeds.py         种子人物筛选
│   ├── schema.sql       数据模型（数据字典）
│   ├── collect.py       采集器：init / plan / fetch / enrich / update / syncs / status
│   ├── build.py         关系构建 + 质量检测 + 导出
│   └── backup.py        备份与恢复：create / list / verify / restore / prune
├── server/app.py        后端服务（FastAPI；读接口匿名可用，写接口需登录）
├── tests/smoke.py       端到端冒烟测试（对着运行中的服务跑一遍）
├── web/                 前端页面（无需构建）
│   ├── index.html       概览与检索
│   ├── graph.html       合作图谱 + 关系路径 + 关系证据
│   ├── person.html      人物档案 + 时间线 + 推荐与反馈 + 纠错入口
│   ├── movie.html       作品详情 + 演职员 + 收藏 + 纠错入口
│   ├── analysis.html    连接人物 / 合作群体 / 人物对比
│   ├── login.html       登录 / 注册
│   ├── favorites.html   我的片单 + 操作记录
│   ├── admin.html       纠错复核 / 备份 / 同步记录
│   └── about.html       数据来源、范围声明、计算规则
├── raw/                 接口原始 JSON 快照（可离线重放，勿提交）
├── backups/             数据库快照（勿提交）
├── _work/               实测报告与文档脚本
└── yingmai.db           SQLite 数据库
```

## 运行

### 0. 前置：网络与凭据

TMDB 的接口域名 `api.themoviedb.org` 在中国大陆被 DNS 污染，**必须经代理访问**。
启动代理客户端后，用 `--proxy` 或环境变量 `HTTPS_PROXY` 指定（本机代理端口为 7897）：

```bash
export HTTPS_PROXY=http://127.0.0.1:7897
curl -s -o /dev/null -w "%{http_code}\n" https://api.themoviedb.org/3/configuration
# 返回 401 表示通了（只是没带 key）
```

API key 从 <https://www.themoviedb.org/settings/api> 申请（非商业免费），
写入 `pipeline/tmdb_apikey.txt`（已在 `.gitignore` 中排除）。

### 1. 采集数据

```bash
cd pipeline
python collect.py init    --proxy http://127.0.0.1:7897   # 建库、登记 8 位种子
python collect.py plan    --proxy http://127.0.0.1:7897   # 选 200 部片
python collect.py fetch   --proxy http://127.0.0.1:7897   # 拉电影详情与演职员
python collect.py enrich  --proxy http://127.0.0.1:7897   # 补人物生日/别名
python collect.py status                                  # 查看进度
```

接口响应会缓存到 `raw/`，重跑不重复请求；`--offline` 可完全离线重放。

### 2. 构建关系

```bash
python build.py relations   # 生成合作边与关系证据
python build.py quality     # 质量检测
python build.py stats       # 图的规模与合作最密的组合
python build.py export      # 导出 web/graph.json
```

### 3. 日常维护

```bash
python collect.py update --proxy http://127.0.0.1:7897   # F24 增量更新
python collect.py syncs                                  # 看同步历史
python backup.py create --note "说明"                     # F30 建快照
python backup.py verify <快照名>                          # 校验（含行数比对）
python backup.py restore <快照名>                         # 恢复（会先自动备份当前库）
```

`update` 走 TMDB 的 `/movie/changes` 找出源站变动过的条目，只重取它们再重算关系。
**网络不通时它会报错并拒绝写入同步记录**，不会把"没连上"记成"源站没变动"。

### 4. 起服务

```bash
cd ..
python server/app.py        # 或 python -m uvicorn server.app:app --reload
```

打开 <http://127.0.0.1:8000>。跑一遍端到端自检：

```bash
python tests/smoke.py
```

## 账号与收藏（F01 / F06）

- 注册 / 登录：`POST /api/auth/register`、`POST /api/auth/login`，返回不透明 token，
  前端存在 `localStorage`，通过 `Authorization: Bearer <token>` 发送。
- 密码经 **PBKDF2-SHA256 加盐哈希**（20 万轮）存储，比较用定长比较；
  收藏按用户隔离，未登录访问返回 401。
- 片单分「想看的」「看过的」两类。**标记为「看过的」会从作品推荐中排除**，
  这是 F17 的验收要求。
- 所有写操作记入 `audit_log`，`GET /api/auth/log` 可查本人操作记录（F29 的一部分）。

## 推荐反馈（F18）

对推荐结果点「喜欢 / 不感兴趣」：

- 「不感兴趣」的作品**从后续推荐中排除**；
- 「喜欢」的作品的**类型**成为加分项（同类型的候选排得更前）。

## 纠错与运维（F28 / F30 / F24）

**纠错**：作品页和人物页都有「发现数据有误？」入口。提交时填更正后的值、来源链接和说明；
管理员在「管理」页复核，接受则写回数据并保留原值作为版本记录，驳回则只留痕。
可纠正的字段有白名单限制（`movie.title / original_title / release_year`、`person.name`），
不是任意字段都能改。

**权限**：第一个注册的账号自动成为管理员（否则没人能复核）。之后注册的都是普通用户。
手工调整角色：

```bash
python -c "import sqlite3;c=sqlite3.connect('yingmai.db');c.execute(\"UPDATE app_user SET role='admin' WHERE username='某人'\");c.commit()"
```

**备份**：快照 = 数据库副本 + 清单（行数、SHA-256、完整性检查结果）。
校验会逐表比对行数——文件在但内容缺了才是最难发现的情况。
**恢复只走命令行**，不在 Web 上做：服务本身占着数据库，边跑边覆盖自己不安全。

## 数据来源与范围

- 数据源：**TMDB** 公开接口（非商业使用）。*This product uses the TMDB API but is not
  endorsed or certified by TMDB.*
- 仅电影，不含电视剧、综艺。
- 仅华语片：`original_language` 为 `zh`（国语）或 `cn`（粤语）。**TMDB 用非标准的 `cn`
  标注粤语片**，只筛 `zh` 会漏掉约一半华语条目。
- 种子：八位跨代际演员（成龙、洪金宝、刘德华、梁朝伟、张曼玉、周星驰、吴京、易烊千玺），
  取其作品全集后按「稠密优先 + 热度补足」选 200 部。
- 图谱节点：每部电影前 10 位主演 + 导演；其余演职员保留明细但不参与关系计算。

## 计算规则

- 人物与电影按 TMDB 源 ID 关联，姓名只作展示与检索，**不因重名合并**。
- 同一人同片担任多个职务时保留明细，但**每对人物每部电影只计一次**。
- 合作边为无向边，频次为共同出演的电影数，排除自连接。
- 时间线按**发行年**统计，不代表实际拍摄时间。
- 推荐基于合作者与类型偏好生成，**不生成事实关系**。

## 已知限制

- 样本限定在 200 部片、八位种子的作品范围内，**网络指标不代表行业地位**。
- 路径「未找到」只表示当前样本与条件下没找到，不代表现实中无关联。
- 语料偏向港片（粤语 139 部 / 国语 61 部），因为八粒种子里六粒是港星。
- 存在同名不同 ID 的人物，按 ID 关联不合并，属于 TMDB 源数据问题。
