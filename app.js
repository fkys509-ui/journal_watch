const reportList = document.getElementById('reportList');
const reportContent = document.getElementById('reportContent');
const latestName = document.getElementById('latestName');
const latestTime = document.getElementById('latestTime');
const reportCount = document.getElementById('reportCount');
const statusBadge = document.getElementById('statusBadge');
const latestLink = document.getElementById('latestLink');
const openRawLink = document.getElementById('openRawLink');
const refreshButton = document.getElementById('refreshButton');

let reports = [];
let currentReport = null;

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function markdownToHtml(markdown) {
  const escaped = escapeHtml(markdown);
  return escaped
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^\- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, (block) => `<ul>${block}</ul>`)
    .replace(/\[([^\]]+)\]\(([^\)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/\n\n+/g, '</p><p>')
    .replace(/^(.+)$/s, '<p>$1</p>')
    .replace(/<p><h/g, '<h')
    .replace(/<\/h([1-3])><\/p>/g, '</h$1>')
    .replace(/<p><ul>/g, '<ul>')
    .replace(/<\/ul><\/p>/g, '</ul>')
    .replace(/<p><\/p>/g, '');
}

function formatTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

async function loadIndex() {
  statusBadge.textContent = '加载中';
  const response = await fetch('reports-index.json?ts=' + Date.now(), { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  reports = data.reports || [];
  currentReport = data.latest || reports[0] || null;

  latestName.textContent = currentReport ? currentReport.name : '暂无报告';
  latestTime.textContent = currentReport ? formatTime(currentReport.mtime) : '-';
  reportCount.textContent = String(reports.length);
  statusBadge.textContent = reports.length ? '可用' : '空';

  renderReportList();
  if (currentReport) {
    latestLink.href = currentReport.url;
    openRawLink.href = currentReport.url;
    await loadReport(currentReport.url);
  } else {
    reportContent.innerHTML = '<p>当前还没有生成报告。</p>';
  }
}

async function loadReport(url) {
  const response = await fetch(url + '?ts=' + Date.now(), { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const markdown = await response.text();
  reportContent.innerHTML = markdownToHtml(markdown);
}

function renderReportList() {
  reportList.innerHTML = '';
  reports.forEach((report) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'report-item' + (currentReport && currentReport.name === report.name ? ' active' : '');
    item.innerHTML = `<strong>${report.name}</strong><br><small>${formatTime(report.mtime)}</small>`;
    item.addEventListener('click', async () => {
      currentReport = report;
      latestName.textContent = report.name;
      latestTime.textContent = formatTime(report.mtime);
      latestLink.href = report.url;
      openRawLink.href = report.url;
      renderReportList();
      try {
        await loadReport(report.url);
      } catch (error) {
        reportContent.innerHTML = `<p>加载失败：${error.message}</p>`;
      }
    });
    reportList.appendChild(item);
  });
}

refreshButton.addEventListener('click', async () => {
  try {
    await loadIndex();
  } catch (error) {
    statusBadge.textContent = '加载失败';
    reportContent.innerHTML = `<p>刷新失败：${error.message}</p>`;
  }
});

loadIndex().catch((error) => {
  statusBadge.textContent = '加载失败';
  reportContent.innerHTML = `<p>无法加载报告索引：${error.message}</p>`;
});