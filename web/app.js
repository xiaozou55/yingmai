/* 影脉 · 共享前端逻辑
   图表遵循的约定：细的标记、发丝级实线网格（不用虚线）、
   单系列只用一个颜色（不做「越大约深」的色阶）、选择性直接标注、
   悬停有提示、≥2 系列必带图例、每张图都有表格视图可替代。 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/** 读当前主题下的 CSS 变量，供 ECharts 使用 */
function tok(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function theme() {
  return {
    ink: tok('--text-primary'), sub: tok('--text-secondary'), muted: tok('--muted'),
    surface: tok('--surface-1'), plane: tok('--plane'),
    hair: tok('--hairline'), base: tok('--baseline'), border: tok('--border'),
    s1: tok('--series-1'), s2: tok('--series-2'), s3: tok('--series-3'),
    neutral: tok('--neutral'),
    font: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif',
  };
}

async function api(path, params) {
  const u = new URL(path, location.origin);
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, v);
  });
  const r = await fetch(u);
  if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => '')}`.slice(0, 200));
  return r.json();
}

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const poster = p => p ? `https://image.tmdb.org/t/p/w200${p}` : '';
const profile = p => p ? `https://image.tmdb.org/t/p/w185${p}` : '';

function show(node, html) { node.innerHTML = html; }
function loading(node, msg = '加载中…') { show(node, `<div class="loading">${msg}</div>`); }
function failed(node, e) { show(node, `<div class="err">出错了：${esc(e.message || e)}</div>`); }

/* ---------- ECharts 基础配置 ---------- */
function baseGrid(extra = {}) {
  const t = theme();
  return Object.assign({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: t.font, color: t.sub },
    tooltip: {
      backgroundColor: t.surface, borderColor: t.border, borderWidth: 1,
      textStyle: { color: t.ink, fontSize: 12.5, fontFamily: t.font },
      extraCssText: 'border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.10);padding:9px 12px;',
    },
    animationDuration: 320,
  }, extra);
}

/** 发丝级实线网格（不用虚线） */
function axisLine(t) { return { lineStyle: { color: t.base, width: 1 } }; }
function splitLine(t) { return { lineStyle: { color: t.hair, width: 1, type: 'solid' } }; }

/**
 * 水平条形图（排序后的量值比较）。单系列 → 全部同一颜色（slot 1），
 * 不按数值深浅上色（那是把长度重复编码成色相）。
 * highlightTop：前 n 条用主色、其余中性灰（emphasis 形式，用于「重点看这几个」）。
 * digits：数值直接标注的小数位；条形图的值就是重点，逐条标注是常规做法。
 */
function hBar(node, items, { valueKey = 'v', nameKey = 'n', highlightTop = 0,
                             unit = '', digits = 0, rowH = 22, tip = null } = {}) {
  const t = theme();
  const data = [...items].reverse();
  // 容器高度跟着行数走：行挤在一起会让直接标注互相压住
  node.style.height = Math.max(180, data.length * rowH + 48) + 'px';
  const chart = echarts.init(node, null, { renderer: 'canvas' });
  const fmt = v => Number(v).toFixed(digits);
  chart.setOption(baseGrid({
    grid: { left: 8, right: 74, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: 'value', splitLine: splitLine(t), axisLine: { show: false },
             axisTick: { show: false }, axisLabel: { color: t.muted, fontSize: 11.5 } },
    yAxis: { type: 'category', data: data.map(d => d[nameKey]),
             axisLine: axisLine(t), axisTick: { show: false },
             axisLabel: { color: t.sub, fontSize: 12 } },
    tooltip: Object.assign({ trigger: 'item' }, tip ? { formatter: tip } : {
      formatter: p => `<b>${esc(p.name)}</b><br/>${fmt(p.value)}${unit}`,
    }),
    series: [{
      type: 'bar', barMaxWidth: 15, barCategoryGap: '38%',
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: p => (highlightTop && p.dataIndex >= data.length - highlightTop)
          ? t.s1 : (highlightTop ? t.neutral : t.s1),
      },
      label: { show: true, position: 'right', color: t.muted, fontSize: 11.5,
               formatter: p => fmt(p.value) + unit },
      data: data.map(d => d[valueKey]),
    }],
  }));
  window.addEventListener('resize', () => chart.resize());
  return chart;
}

/** 把稀疏的「年份→值」补成连续年份，缺口显示为 0（否则时间轴上看不出中断） */
function fillYears(pairs) {
  if (!pairs.length) return { years: [], values: [] };
  const map = new Map(pairs);
  const lo = Math.min(...map.keys()), hi = Math.max(...map.keys());
  const years = [], values = [];
  for (let y = lo; y <= hi; y++) { years.push(y); values.push(map.get(y) || 0); }
  return { years, values };
}

/** 折线 / 柱形的时间序列。单系列不需要图例——标题就是它的名字。 */
function timeSeries(node, years, values, { unit = '部', color = null, kind = 'line',
                                           name = '' } = {}) {
  const t = theme();
  const c = color || t.s1;
  const chart = echarts.init(node, null, { renderer: 'canvas' });
  const common = {
    type: kind, name,
    itemStyle: { color: c }, symbol: 'circle', symbolSize: 7,
    lineStyle: { width: 2, color: c },
    areaStyle: kind === 'line'
      ? { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: c + '33' }, { offset: 1, color: c + '00' }]) }
      : undefined,
    barMaxWidth: 18, barCategoryGap: '42%',
    // 只标极值，不是每个点都写数字
    label: {
      show: kind === 'bar', position: 'top', color: t.muted, fontSize: 11,
      formatter: p => (p.value === Math.max(...values) ? p.value : ''),
    },
  };
  chart.setOption(baseGrid({
    grid: { left: 6, right: 16, top: 16, bottom: 6, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: t.base } },
               formatter: p => `<b>${p[0].axisValue} 年</b><br/>${p[0].value} ${unit}` },
    xAxis: { type: 'category', data: years, boundaryGap: kind === 'bar',
             axisLine: axisLine(t), axisTick: { show: false },
             axisLabel: { color: t.muted, fontSize: 11 } },
    yAxis: { type: 'value', splitLine: splitLine(t), axisLine: { show: false },
             axisTick: { show: false }, axisLabel: { color: t.muted, fontSize: 11 } },
    series: [Object.assign({}, common, { data: values })],
  }));
  window.addEventListener('resize', () => chart.resize());
  return chart;
}

/** 双系列分组条形（用于人物对比）——≥2 系列必带图例 */
function groupedBar(node, categories, series, { unit = '' } = {}) {
  const t = theme();
  const chart = echarts.init(node, null, { renderer: 'canvas' });
  const palette = [t.s1, t.s2];
  chart.setOption(baseGrid({
    grid: { left: 6, right: 16, top: 34, bottom: 6, containLabel: true },
    legend: { top: 0, textStyle: { color: t.sub, fontSize: 12 }, icon: 'roundRect',
              itemWidth: 11, itemHeight: 11 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'category', data: categories, axisLine: axisLine(t),
             axisTick: { show: false }, axisLabel: { color: t.sub, fontSize: 11.5 } },
    yAxis: { type: 'value', splitLine: splitLine(t), axisLine: { show: false },
             axisTick: { show: false }, axisLabel: { color: t.muted, fontSize: 11 } },
    series: series.map((s, i) => ({
      name: s.name, type: 'bar', data: s.data, barMaxWidth: 16, barGap: '18%',
      itemStyle: { color: palette[i % 2], borderRadius: [4, 4, 0, 0] },
    })),
  }));
  window.addEventListener('resize', () => chart.resize());
  return chart;
}

/* ---------- 登录态（F01） ---------- */
const auth = {
  get token() { return localStorage.getItem('ym-token') || ''; },
  set token(t) { t ? localStorage.setItem('ym-token', t) : localStorage.removeItem('ym-token'); },
  user: null,
};

/** 带 Bearer token 的请求；错误信息取后端返回的 detail */
async function apiAuth(path, { method = 'GET', body = null } = {}) {
  const h = { 'Content-Type': 'application/json' };
  if (auth.token) h['Authorization'] = 'Bearer ' + auth.token;
  const r = await fetch(path, { method, headers: h, body: body ? JSON.stringify(body) : null });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON */ }
  if (!r.ok) throw new Error((data && data.detail) || text || String(r.status));
  return data;
}

async function refreshAuth() {
  const slot = $('#authSlot');
  if (!slot) return;
  try {
    const me = await apiAuth('/api/me');
    auth.user = me.logged_in ? me.user : null;
    if (me.logged_in) {
      slot.innerHTML = `<a href="favorites.html">我的片单 <b>${me.favorites}</b></a>
                        <a href="#" id="logout">${esc(me.user.username)} · 退出</a>`;
      $('#logout').onclick = async e => {
        e.preventDefault();
        try { await apiAuth('/api/auth/logout', { method: 'POST' }); } catch { /* 忽略 */ }
        auth.token = ''; location.href = 'index.html';
      };
    } else {
      slot.innerHTML = `<a href="login.html">登录 / 注册</a>`;
    }
  } catch { slot.innerHTML = `<a href="login.html">登录 / 注册</a>`; }
}

/* ---------- 主题切换 ---------- */
function initTheme() {
  // ?theme=light / dark 可强制指定（截图、演示用）
  const url = new URLSearchParams(location.search).get('theme');
  const saved = url || localStorage.getItem('ym-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  const btn = $('#themeBtn');
  if (!btn) return;
  btn.onclick = () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('ym-theme', next);
    location.reload();   // 图表要按新主题重画
  };
}

/* ---------- 顶栏 ---------- */
function topbar(active) {
  const tabs = [['index.html', '概览'], ['graph.html', '合作图谱'], ['person.html', '人物档案'],
                ['movie.html', '作品'], ['analysis.html', '分析发现'],
                ['admin.html', '管理'], ['about.html', '关于']];
  return `<header class="top">
    <div class="brand"><a href="index.html">影脉</a><span>影视人物合作关系与作品发现</span></div>
    <nav class="tabs">
      ${tabs.map(([h, n]) => `<a href="${h}" class="${h === active ? 'on' : ''}">${n}</a>`).join('')}
      <span id="authSlot" class="authslot"></span>
      <a href="#" id="themeBtn" title="切换深浅色">◐</a>
    </nav>
  </header>`;
}

/** 页面初始化：注入顶栏 + 主题按钮 + 登录态 */
function boot(active) {
  const h = document.createElement('div');
  h.innerHTML = topbar(active);
  document.body.insertBefore(h.firstElementChild, document.body.firstChild);
  initTheme();
  window.authReady = refreshAuth();
}

/**
 * 收藏按钮（F06）。已收藏时高亮；未登录时点了会跳到登录页。
 * 返回一个可插入的 HTML 片段，绑定用 bindFav()。
 */
function favButtons(movieId, cur) {
  const b = (t, label) => `<button class="fav ${cur === t ? 'on' : ''}" data-m="${movieId}" data-t="${t}">${label}</button>`;
  return `<div class="favrow">${b('want', '想看的')}${b('seen', '看过的')}</div>`;
}

/** 已登录时把已收藏的按钮点亮 */
async function markFavs(root) {
  if (!auth.user) return;
  try {
    const d = await apiAuth('/api/favorites');
    const set = new Set(d.items.map(x => x.movie_id + ':' + x.list_type));
    $$('.fav', root).forEach(b => b.classList.toggle('on', set.has(b.dataset.m + ':' + b.dataset.t)));
  } catch { /* 未登录或接口异常时保持未选中 */ }
}

async /** 推荐反馈按钮（F18）：喜欢 / 不感兴趣。未登录时跳登录页。 */
async function bindFeedback(root) {
  if (window.authReady) await window.authReady;
  const btns = $$('.fb', root);
  if (!btns.length) return;
  let mine = {};
  if (auth.user) {
    try {
      const d = await apiAuth('/api/feedback');
      mine = Object.fromEntries(d.items.map(x => [x.movie_id, x.action]));
    } catch { /* 未登录或接口异常 */ }
  }
  btns.forEach(b => {
    const id = +b.dataset.m;
    b.classList.toggle('on', mine[id] === b.dataset.a);
    b.onclick = async () => {
      if (!auth.user) { location.href = 'login.html?next=' + encodeURIComponent(location.pathname + location.search); return; }
      const on = b.classList.contains('on');
      b.disabled = true;
      try {
        if (on) await apiAuth('/api/feedback/' + id, { method: 'DELETE' });
        else await apiAuth('/api/feedback', { method: 'POST', body: { movie_id: id, action: b.dataset.a } });
        // 同一条的两个按钮互斥，重画整块
        $$('.fb', root).filter(x => +x.dataset.m === id)
          .forEach(x => x.classList.toggle('on', !on && x === b));
        refreshAuth();
      } catch (e) { alert('操作失败：' + e.message); }
      b.disabled = false;
    };
  });
}

/** 纠错提交按钮（F28）。target 形如 'movie:9470'，field 用下拉选。 */
function correctionEntry(targetType, targetId, fields, current) {
  return `<details class="tv"><summary>发现数据有误？提交纠错</summary>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:8px">
      <div class="field"><label>字段</label>
        <select class="cf">${fields.map(f => `<option value="${f}">${f}</option>`).join('')}</select></div>
      <div class="field" style="flex:1;min-width:180px"><label>更正为</label>
        <input class="cv" placeholder="填写正确的值"></div>
      <div class="field" style="flex:1;min-width:180px"><label>来源链接（便于核对）</label>
        <input class="cu" placeholder="https://…"></div>
      <div class="field" style="flex:1;min-width:180px"><label>说明</label>
        <input class="cr" placeholder="为什么认为有误"></div>
      <button class="primary cgo" data-tt="${targetType}" data-ti="${targetId}">提交</button>
    </div>
    <div class="cmsg hint"></div></details>`;
}

function bindCorrection(root) {
  $$('.cgo', root).forEach(btn => {
    btn.onclick = async () => {
      const box = btn.closest('details');
      const msg = $('.cmsg', box);
      if (!auth.user) { location.href = 'login.html?next=' + encodeURIComponent(location.pathname + location.search); return; }
      const body = {
        target_type: btn.dataset.tt, target_id: +btn.dataset.ti,
        field: $('.cf', box).value, new_value: $('.cv', box).value.trim(),
        source_url: $('.cu', box).value.trim(), reason: $('.cr', box).value.trim(),
      };
      if (!body.new_value) { msg.className = 'cmsg err'; msg.textContent = '请填写更正后的值'; return; }
      btn.disabled = true;
      try {
        const d = await apiAuth('/api/corrections', { method: 'POST', body });
        msg.className = 'cmsg ok';
        msg.textContent = `已提交（编号 ${d.id}），等待管理员复核。可在「管理」页查看。`;
      } catch (e) { msg.className = 'cmsg err'; msg.textContent = '提交失败：' + e.message; }
      btn.disabled = false;
    };
  });
}

async function bindFav(root, onChange) {
  if (window.authReady) await window.authReady;
  await markFavs(root);
  $$('.fav', root).forEach(btn => {
    btn.onclick = async e => {
      e.preventDefault(); e.stopPropagation();
      if (!auth.user) { location.href = 'login.html?next=' + encodeURIComponent(location.pathname + location.search); return; }
      const on = btn.classList.contains('on');
      btn.disabled = true;
      try {
        if (on) await apiAuth('/api/favorites/' + btn.dataset.m + '?list_type=' + btn.dataset.t, { method: 'DELETE' });
        else await apiAuth('/api/favorites', { method: 'POST', body: { movie_id: +btn.dataset.m, list_type: btn.dataset.t } });
        btn.classList.toggle('on');
        refreshAuth();
        if (onChange) onChange();
      } catch (err) { alert('操作失败：' + err.message); }
      btn.disabled = false;
    };
  });
}
