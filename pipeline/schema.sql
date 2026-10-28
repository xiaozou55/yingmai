-- 影脉 数据模型 v0.1
-- 设计原则（对应需求报告 3.1 业务规则）：
--   1. 人物和电影按 TMDB 源 ID 关联，姓名只作展示与检索，不因重名合并
--   2. 演职员明细**全量保留**，同一人担任多个职务不合并
--   3. 合作关系由明细推导，每对人物每部电影只计一次（推导在 build 阶段，不在这里）
--   4. 每条记录挂来源与抓取时间，保证可追溯（F12 关系证据）
--   5. 采集 / 存储 / 图计算分离（N04），本模型只负责存储

DROP VIEW  IF EXISTS v_credit_person;
DROP TABLE IF EXISTS credit;
DROP TABLE IF EXISTS movie;
DROP TABLE IF EXISTS person;
DROP TABLE IF EXISTS seed;
DROP TABLE IF EXISTS source;

-- 数据来源登记（F19 采集范围配置）
CREATE TABLE source (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,          -- TMDB
    base_url    TEXT,
    license     TEXT,
    attribution TEXT,                   -- 署名要求原文
    note        TEXT
);

-- 种子人物 —— 本项目的采集起点。
-- 实测（2026-10-14）：以 Top-N 热门片为种子会得到 93.7% 断开的网络，
-- 改以人物为种子后 18/18 全连通。
CREATE TABLE seed (
    person_id   INTEGER PRIMARY KEY,
    query_name  TEXT,                   -- 我们用来搜的中文名
    name        TEXT,                   -- TMDB 主名
    department  TEXT,
    note        TEXT
);

-- 人物（F04 人物档案）
CREATE TABLE person (
    id                   INTEGER PRIMARY KEY,   -- TMDB person id
    name                 TEXT NOT NULL,
    known_for_department TEXT,
    gender               INTEGER,
    birthday             TEXT,
    deathday             TEXT,
    place_of_birth       TEXT,
    popularity           REAL,
    profile_path         TEXT,
    also_known_as        TEXT,          -- JSON 数组，F02 别名检索用
    detail_fetched       INTEGER DEFAULT 0,   -- 是否已拉过 /person/{id} 详情
    source_id            INTEGER REFERENCES source(id),
    fetched_at           TEXT
);

-- 电影（F05 作品详情）
CREATE TABLE movie (
    id                INTEGER PRIMARY KEY,   -- TMDB movie id
    title             TEXT,                  -- zh-CN 标题
    original_title    TEXT,
    original_language TEXT,                  -- 实测：zh=国语片，cn=粤语片
    origin_country    TEXT,                  -- JSON 数组
    release_date      TEXT,
    release_year      INTEGER,               -- 冗余列，F11 年份筛选用
    runtime           INTEGER,
    genres            TEXT,                  -- JSON 数组
    vote_count        INTEGER,
    vote_average      REAL,
    popularity        REAL,
    overview          TEXT,
    poster_path       TEXT,
    seed_hits         INTEGER,               -- 有几粒种子出演，选片依据
    source_id         INTEGER REFERENCES source(id),
    fetched_at        TEXT
);

-- 演职员明细（F22 演职员采集）
-- 同一人在同一部片担任多个职务时保留多行；合作边在关系构建阶段按
-- (movie_id, 人物对) 去重，因此这里**不能**提前合并。
CREATE TABLE credit (
    id             INTEGER PRIMARY KEY,
    movie_id       INTEGER NOT NULL REFERENCES movie(id),
    person_id      INTEGER NOT NULL REFERENCES person(id),
    credit_type    TEXT NOT NULL,        -- cast / crew
    department     TEXT,
    job            TEXT,                 -- Actor / Director / ...
    character_name TEXT,
    billing_order  INTEGER,              -- cast 的 order，用于「取前 N 位主演」
    source_id      INTEGER REFERENCES source(id),
    fetched_at     TEXT,
    UNIQUE (movie_id, person_id, credit_type, job, character_name)
);

CREATE INDEX idx_credit_movie  ON credit(movie_id);
CREATE INDEX idx_credit_person ON credit(person_id);
CREATE INDEX idx_movie_year    ON movie(release_year);
CREATE INDEX idx_movie_seed    ON movie(seed_hits);

-- 演职员明细的可读视图（F12 关系证据直接查它）
CREATE VIEW v_credit_person AS
SELECT c.movie_id, m.title AS movie_title, m.release_year,
       c.person_id, p.name AS person_name,
       c.credit_type, c.department, c.job, c.character_name, c.billing_order
FROM credit c
JOIN movie  m ON m.id = c.movie_id
JOIN person p ON p.id = c.person_id;
