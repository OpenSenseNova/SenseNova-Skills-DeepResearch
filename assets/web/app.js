const app = document.querySelector('#app');
const runId = location.pathname.match(/^\/runs\/([^/]+)$/)?.[1];
const forceLanding = new URLSearchParams(location.search).get('new') === '1';
let source;
let activeView = 'overview';
let currentRunTimer;

const esc = (value) => String(value ?? '').replace(
  /[&<>"']/g,
  (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character],
);

const statusName = {
  queued: '待开始', active: '进行中', partial: '部分完成', done: '已完成',
  failed: '失败', starting: '启动中', running: '进行中', completed: '已完成',
};

const reportFormatName = {
  brief: '简报', formal_report: '正式报告',
};

const time = (value) => {
  if (!value) return '旧版事件';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

const icon = (name) => {
  const paths = {
    research: '<path d="M5 19V7l7-4 7 4v12"/><path d="M8 10h8M8 14h8M10 19v-2h4v2"/>',
    tree: '<path d="M7 4v4m0 0H4v4m3-4h5m0 0v4m0-4h5v4"/><circle cx="4" cy="15" r="2"/><circle cx="12" cy="15" r="2"/><circle cx="17" cy="15" r="2"/>',
    report: '<path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4M9 11h6M9 15h6"/>',
    pulse: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    download: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M5 20h14"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
  };
  return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.tree}</svg>`;
};

function landing() {
  app.innerHTML = `
    <div class="workbench landing-workbench">
      <header class="topbar">
        <div class="brand">${icon('research')}<strong>SenseNova Workbench</strong></div>
        <span class="product-pill">Deep Research</span>
      </header>
      <main class="landing-stage">
        <form class="start-card" id="start">
          <div class="start-kicker">Research workspace</div>
          <h1>开始一次深度研究</h1>
          <p>配置研究深度和交付形式。任务启动后，可在工作台中查看真实的研究生命周期、证据指标与报告章节。</p>
          <div class="field query-field">
            <label for="query">研究问题</label>
            <textarea id="query" name="query" required placeholder="例如：分析 2026 年企业级 AI Agent 平台的竞争格局"></textarea>
          </div>
          <div class="form-grid">
            <div class="field"><label for="mode">研究模式</label><select id="mode" name="mode"><option value="heavy">Heavy</option><option value="normal">Normal</option><option value="quick">Quick</option></select></div>
            <div class="field"><label for="report-format">报告形式</label><select id="report-format" name="report_format" required><option value="" selected disabled>请选择报告形式</option><option value="brief">简报</option><option value="formal_report">正式报告</option></select></div>
            <div class="field"><label for="output-format">输出格式</label><select id="output-format" name="output_format"><option value="markdown">Markdown</option><option value="html">HTML</option><option value="pdf">PDF</option><option value="docx">Word</option></select></div>
            <div class="field"><label for="language">语言</label><input id="language" name="language" value="zh-CN"></div>
          </div>
          <p class="form-error" id="error"></p>
          <div class="start-actions"><span>任务在本机后台执行，可安全刷新或稍后恢复。</span><button class="primary-button" type="submit">${icon('plus')}开始研究</button></div>
        </form>
      </main>
    </div>`;

  document.querySelector('#start').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.target.querySelector('button');
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>正在启动';
    try {
      const body = Object.fromEntries(new FormData(event.target));
      const response = await fetch('/api/runs', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '启动失败');
      location.href = data.url;
    } catch (error) {
      document.querySelector('#error').textContent = error.message;
      button.disabled = false;
      button.innerHTML = `${icon('plus')}开始研究`;
    }
  });

  if (!forceLanding) {
    const followCurrentRun = async () => {
      try {
        const response = await fetch('/api/runs/current', { cache: 'no-store' });
        if (!response.ok) return;
        const data = await response.json();
        if (data.url) location.replace(data.url);
      } catch (_) {
        // The landing page remains usable while the local server reconnects.
      }
    };
    followCurrentRun();
    currentRunTimer = window.setInterval(followCurrentRun, 1000);
  }
}

function lifecycle(pipeline) {
  return pipeline.map((item) => `
    <li class="status-${esc(item.status)}">
      <span class="lifecycle-icon">${item.status === 'done' ? '✓' : icon('tree')}</span>
      <strong>${esc(item.label)}</strong>
      <span>${esc(statusName[item.status] || item.status)}</span>
    </li>`).join('');
}

function dimensionCards(dimensions) {
  if (!dimensions.length) {
    return `<div class="empty-state">${icon('tree')}<strong>研究维度尚未生成</strong><span>规划完成后，研究工作包会显示在这里。</span></div>`;
  }
  return `<div class="dimension-grid">${dimensions.map((item) => `
    <article class="dimension-card status-${esc(item.status)}">
      <header><div><span>${esc(item.id)}</span><h3>${esc(item.name)}</h3></div><span class="state-dot"></span></header>
      <p>${esc(item.headline || item.description || '等待研究产物')}</p>
      <dl><div><dd>${Number(item.claims) || 0}</dd><dt>证据主张</dt></div><div><dd>${Number(item.sources) || 0}</dd><dt>来源已归档</dt></div></dl>
    </article>`).join('')}</div>`;
}

function sectionRows(sections) {
  if (!sections.length) return '';
  return `
    <section class="material-group">
      <header><span class="group-icon">${icon('report')}</span><div><h3>报告章节</h3><p>按 Outline 生成的内容单元</p></div><span>${sections.filter((item) => item.status === 'done').length}/${sections.length}</span></header>
      <div>${sections.map((item, index) => `
        <div class="material-row status-${esc(item.status)}"><span class="material-index">${String(index + 1).padStart(2, '0')}</span><div><strong>${esc(item.title)}</strong><small>${esc(item.type || 'narrative')}</small></div><span>${esc(statusName[item.status] || item.status)}</span></div>`).join('')}</div>
    </section>`;
}

function activityRows(activity) {
  return `
    <section class="material-group activity-group">
      <header><span class="group-icon">${icon('pulse')}</span><div><h3>最近活动</h3><p>当前 Run 的持久化工作流事件</p></div><span>${activity.length}</span></header>
      <div>${activity.slice(0, 12).map((item) => `
        <div class="activity-row"><span class="activity-dot"></span><div><strong>${esc(item.message)}${item.scope ? ` · ${esc(item.scope)}` : ''}</strong><small>${time(item.time)}</small></div></div>`).join('') || '<div class="activity-row muted">等待第一个持久化事件</div>'}</div>
    </section>`;
}

const metricRate = (value) => value == null ? '—' : `${Number(value).toFixed(1)}%`;
const seconds = (value) => value == null ? '—' : `${Number(value).toFixed(1)}s`;

function searchObservability(search) {
  const domains = search?.domains || [];
  const sources = search?.sources || [];
  const funnel = search?.funnel || { raw: 0, unique: 0, fetched: 0, evidence: 0, rates: {} };
  if (!domains.length && !sources.length && !funnel.raw && !funnel.unique && !funnel.fetched && !funnel.evidence) return '';
  return `
    <section class="search-observability">
      <div class="materials-heading"><div><span class="eyebrow">Search observability</span><h2>检索进度与转化</h2></div><span>${Number(search.api_calls) || 0} 次 API 调用</span></div>
      <div class="funnel-grid">
        <div><strong>${Number(funnel.raw) || 0}</strong><span>Raw</span><small>Source 返回候选</small></div>
        <i>→<small>${metricRate(funnel.rates?.deduplicated)}</small></i>
        <div><strong>${Number(funnel.unique) || 0}</strong><span>Unique</span><small>规范化去重候选</small></div>
        <i>→<small>${metricRate(funnel.rates?.fetched)}</small></i>
        <div><strong>${Number(funnel.fetched) || 0}</strong><span>Fetched</span><small>已完成正文读取</small></div>
        <i>→<small>${metricRate(funnel.rates?.evidence)}</small></i>
        <div><strong>${Number(funnel.evidence) || 0}</strong><span>Evidence</span><small>进入证据产物来源</small></div>
      </div>
      ${domains.length ? `<div class="domain-progress-grid">${domains.map((item) => `
        <article class="domain-progress status-${esc(item.status)}">
          <header><div><strong>${esc(item.domain)}</strong><small>${esc((item.operations || []).join(' · ') || 'domain search')}</small></div><span>${Number(item.completed) || 0}/${Number(item.planned) || 0}</span></header>
          <div class="progress-track"><span style="width:${Math.max(0, Math.min(100, Number(item.progress_percent) || 0))}%"></span></div>
          <dl><div><dt>运行中</dt><dd>${Number(item.running) || 0}</dd></div><div><dt>API</dt><dd>${Number(item.api_calls) || 0}</dd></div><div><dt>Raw / Unique</dt><dd>${Number(item.raw) || 0} / ${Number(item.unique) || 0}</dd></div></dl>
        </article>`).join('')}</div>` : ''}
      ${sources.length ? `<details class="source-statistics" open><summary>Source 耗时与调用统计 <span>${sources.length} 个 Source</span></summary><div class="source-table"><div class="source-table-head"><span>Source</span><span>调用</span><span>平均耗时</span><span>总耗时</span><span>Raw</span><span>状态</span></div>${sources.map((item) => `
        <div class="source-table-row"><strong>${esc(item.provider)}</strong><span>${Number(item.calls) || 0}</span><span>${seconds(item.average_seconds)}</span><span>${seconds(item.total_seconds)}</span><span>${Number(item.raw) || 0}</span><span class="source-state">${item.running ? `${item.running} 运行中` : item.failed ? `${item.failed} 失败` : `${item.succeeded || 0} 成功`}</span></div>`).join('')}</div></details>` : ''}
    </section>`;
}

function overview(s, progress, dimensions, sections) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  return `
    <div class="overview-scroll">
      <section class="current-summary">
        <div><span class="eyebrow">当前进展</span><h2>${esc(progress.phase_label || '研究进行中')}</h2><p>${s.error ? `<span class="error-text">${esc(s.error)}</span>` : `工作流正在处理 ${esc(progress.phase || s.status || '当前阶段')}。`}</p></div>
        <div class="summary-progress"><div><span>总进度</span><strong>${percent}%</strong></div><div class="progress-track"><span style="width:${percent}%"></span></div><small>事件序号 ${Number(s.last_event_seq) || 0}</small></div>
      </section>
      ${searchObservability(s.search)}
      <section class="materials">
        <div class="materials-heading"><div><span class="eyebrow">研究产物</span><h2>研究材料</h2></div><span>${dimensions.length + sections.length} 份结构化产物</span></div>
        ${dimensionCards(dimensions)}
        <div class="material-groups">${sectionRows(sections)}${activityRows(s.activity || [])}</div>
      </section>
    </div>`;
}

function reportView(s) {
  if (!s.result) {
    return `<div class="report-empty">${icon('report')}<h2>最终报告尚未生成</h2><p>完成写作、审核和格式交付后，可在这里下载正式产物。</p></div>`;
  }
  const preview = s.result.source || (s.output_format === 'markdown' ? s.result : null);
  return `
    <div class="report-delivered">
      <span class="eyebrow">Research delivered</span>
      <h2>报告已生成</h2>
      <p>最终文件已从当前 Run 的正式产物导出。研究过程与证据统计仍保留在“研究概览”中。</p>
      <div class="download-card"><span class="file-icon">${icon('report')}</span><div><strong>${esc(s.result.filename)}</strong><small>${esc(String(s.output_format || 'markdown').toUpperCase())} 最终产物</small></div><a class="primary-button" href="${esc(s.result.url)}">${icon('download')}下载</a></div>
      ${s.result.source ? `<div class="download-card"><span class="file-icon secondary">${icon('report')}</span><div><strong>${esc(s.result.source.filename)}</strong><small>Markdown 源报告</small></div><a class="secondary-button" href="${esc(s.result.source.url)}">${icon('download')}下载</a></div>` : ''}
      ${preview ? '<section class="report-preview"><div class="preview-heading"><h3>报告预览</h3><span>Markdown</span></div><div id="report-preview-content" class="markdown-preview"><span class="preview-loading">正在加载报告预览…</span></div></section>' : ''}
    </div>`;
}

function inlineMarkdown(value) {
  let rendered = esc(value);
  rendered = rendered.replace(/`([^`]+)`/g, '<code>$1</code>');
  rendered = rendered.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g, '<img alt="$1" src="$2">');
  rendered = rendered.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  rendered = rendered.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  rendered = rendered.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  rendered = rendered.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  rendered = rendered.replace(/_([^_]+)_/g, '<em>$1</em>');
  return rendered;
}

function markdownPreview(markdown) {
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];
  let paragraph = [];
  let listType = null;
  let inCode = false;
  let code = [];
  const flushParagraph = () => {
    if (paragraph.length) html.push(`<p>${inlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const closeList = () => { if (listType) html.push(`</${listType}>`); listType = null; };
  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (inCode) { html.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = []; }
      inCode = !inCode;
      continue;
    }
    if (inCode) { code.push(line); continue; }
    const heading = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (!line.trim()) { flushParagraph(); closeList(); continue; }
    if (heading) { flushParagraph(); closeList(); const level = heading[1].length; html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`); continue; }
    if (unordered || ordered) {
      flushParagraph();
      const wanted = unordered ? 'ul' : 'ol';
      if (listType !== wanted) { closeList(); listType = wanted; html.push(`<${listType}>`); }
      html.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
      continue;
    }
    if (line.startsWith('>')) { flushParagraph(); closeList(); html.push(`<blockquote>${inlineMarkdown(line.replace(/^>\s?/, ''))}</blockquote>`); continue; }
    closeList(); paragraph.push(line.trim());
  }
  if (inCode) html.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  flushParagraph(); closeList();
  return html.join('');
}

async function loadReportPreview(url) {
  const target = document.querySelector('#report-preview-content');
  if (!target) return;
  try {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error('报告读取失败');
    target.innerHTML = markdownPreview(await response.text());
  } catch (error) {
    target.innerHTML = `<span class="preview-error">${esc(error.message)}</span>`;
  }
}

function dashboard(s, connected = false) {
  const progress = s.progress || {
    percent: s.status === 'completed' ? 100 : 0,
    phase_label: statusName[s.status] || '处理中',
  };
  const dimensions = s.dimensions || [];
  const sections = s.sections || [];
  const pipeline = s.pipeline || [{ id: 'planning', label: '研究规划', status: s.status === 'starting' ? 'active' : 'queued' }];
  const metrics = s.metrics || { sources: 0, claims: 0, counter_claims: 0, quality: { primary: 0 } };
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  if (activeView === 'report' && !s.result) activeView = 'overview';

  app.innerHTML = `
    <div class="workbench research-workbench">
      <header class="topbar">
        <div class="brand">${icon('research')}<strong>SenseNova Workbench</strong></div>
        <div class="run-identity"><span>${esc(s.run_id)}</span><i></i><span>${esc(String(s.mode || 'heavy').toUpperCase())}</span></div>
        <div class="topbar-actions"><span class="connection ${connected ? 'live' : ''}"><i></i>${connected ? '实时同步' : '正在连接'}</span><a class="icon-button" href="/?new=1" title="新建研究">${icon('plus')}</a></div>
      </header>
      <div class="dashboard-shell">
        <aside class="status-sidebar">
          <header><span class="eyebrow">研究状态</span><h1>${esc(s.query || '正在读取研究任务')}</h1></header>
          <div class="status-content">
            <div class="status-meta"><span>研究模式</span><strong>${esc(String(s.mode || 'heavy').toUpperCase())}</strong></div>
            <div class="status-meta"><span>报告形式</span><strong>${esc(reportFormatName[s.report_format] || s.report_format || '正式报告')}</strong></div>
            <div class="sidebar-progress"><div><span>工作流完成度</span><strong>${percent}%</strong></div><div class="progress-track"><span style="width:${percent}%"></span></div></div>
            <dl class="sidebar-metrics"><div><dt>唯一来源</dt><dd>${Number(metrics.sources) || 0}</dd></div><div><dt>证据主张</dt><dd>${Number(metrics.claims) || 0}</dd></div><div><dt>API 调用</dt><dd>${Number(s.search?.api_calls) || 0}</dd></div></dl>
            <section class="lifecycle-section"><h2>深度研究生命周期</h2><ol class="lifecycle">${lifecycle(pipeline)}</ol></section>
          </div>
        </aside>
        <main class="dashboard-main">
          <nav class="dashboard-tabs" aria-label="深度研究视图">
            <button data-view="overview" class="${activeView === 'overview' ? 'active' : ''}">${icon('tree')}研究概览</button>
            <button data-view="report" class="${activeView === 'report' ? 'active' : ''}" ${s.result ? '' : 'disabled'}>${icon('report')}最终报告</button>
            <span>${esc(String(s.output_format || 'markdown').toUpperCase())}</span>
          </nav>
          ${activeView === 'report' ? reportView(s) : overview(s, progress, dimensions, sections)}
        </main>
      </div>
    </div>`;

  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => {
    activeView = button.dataset.view;
    dashboard(s, connected);
  }));
  const previewUrl = s.result?.source?.url || (s.output_format === 'markdown' ? s.result?.url : null);
  if (activeView === 'report' && previewUrl) loadReportPreview(previewUrl);
}

async function load() {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '无法读取运行');
  dashboard(data, false);
  connect();
}

function connect() {
  source?.close();
  source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  source.addEventListener('snapshot', (event) => dashboard(JSON.parse(event.data), true));
  source.onerror = () => {
    const connection = document.querySelector('.connection');
    if (connection) {
      connection.classList.remove('live');
      connection.innerHTML = '<i></i>连接中断';
    }
  };
}

if (runId) {
  load().catch((error) => {
    app.innerHTML = `<main class="error-page"><div><h1>无法打开运行</h1><p>${esc(error.message)}</p><a href="/">返回首页</a></div></main>`;
  });
} else {
  landing();
}
