/* 公共前端工具：统一请求、加载态、空态、错误态 */
const $ = (id) => document.getElementById(id);

/* 统一请求：401 跳登录，非 2xx 抛出可读错误 */
async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    throw new Error('无法连接服务，请确认平台正在运行');
  }
  if (res.status === 401) {
    location.href = '/login';
    throw new Error('会话已过期，正在跳转登录页');
  }
  if (!res.ok) {
    let d = {};
    try { d = await res.json(); } catch (_) {}
    throw new Error(d.detail || ('请求失败（HTTP ' + res.status + '）'));
  }
  return res.json();
}

function setState(el, kind, text, onRetry) {
  if (!el) return;
  const icon = kind === 'loading' ? '加载中' : kind === 'empty' ? '无数据' : '出错';
  const cls = kind === 'error' ? 'state err' : 'state muted';
  el.className = cls;
  el.textContent = (kind === 'loading' ? '' : icon + '：') + text;
  if (kind === 'error' && onRetry) {
    const b = document.createElement('button');
    b.textContent = '重试';
    b.style.marginLeft = '10px';
    b.onclick = onRetry;
    el.appendChild(b);
  }
}

const setLoading = (el, t) => setState(el, 'loading', t || '加载中…');
const setEmpty = (el, t) => setState(el, 'empty', t || '暂无数据');
const setError = (el, msg, retry) => setState(el, 'error', msg, retry);

const fmtPct = (v) => ((v || 0) * 100).toFixed(2) + '%';
const fmtNum = (n) => (n || 0).toLocaleString();
const rateClass = (r) => (r >= 0.15 ? 'bad' : r >= 0.09 ? 'warn' : '');
const lvlTag = (l) => (l === 'P0' ? 'bad' : l === 'P1' ? 'warn' : '');
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
));

/* 包装异步页面初始化：统一捕获并显示错误条 */
function guard(fn) {
  return async function (...args) {
    const bar = $('globalError');
    try {
      if (bar) { bar.style.display = 'none'; bar.textContent = ''; }
      return await fn.apply(this, args);
    } catch (e) {
      if (bar) {
        bar.style.display = 'block';
        bar.textContent = e.message || '页面加载失败';
        const b = document.createElement('button');
        b.textContent = '重新加载';
        b.style.marginLeft = '12px';
        b.onclick = () => location.reload();
        bar.appendChild(b);
      }
      console.error(e);
    }
  };
}

/* 批量取数：任一失败不阻塞其余，失败项返回 null */
async function apiAll(pairs) {
  const entries = Object.entries(pairs);
  const results = await Promise.all(
    entries.map(([k, p]) =>
      (Array.isArray(p) ? api(p[0], p[1]) : api(p)).then(
        (v) => [k, v, null],
        (e) => [k, null, e.message]
      )
    )
  );
  const data = {}, errors = {};
  for (const [k, v, err] of results) {
    data[k] = v;
    if (err) errors[k] = err;
  }
  return { data, errors };
}

/* 退出登录 */
async function logout() {
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
}
