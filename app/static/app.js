const STORAGE_KEY = 'fund-lens-watchlist';
const DEFAULT_FUNDS = ['090007', '006122', '090010'];

const state = {
  funds: JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') || DEFAULT_FUNDS,
  results: new Map(),
  refreshing: false,
};

const list = document.querySelector('#fund-list');
const input = document.querySelector('#fund-input');
const message = document.querySelector('#form-message');
const template = document.querySelector('#fund-card-template');

function save() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.funds));
}

function signed(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? '+' : ''}${number.toFixed(2)}%`;
}

function directionClass(value) {
  if (value == null) return '';
  return Number(value) >= 0 ? 'up' : 'down';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}

function createCard(code) {
  const card = template.content.firstElementChild.cloneNode(true);
  card.dataset.code = code;
  card.querySelector('.fund-code').textContent = code;
  const summary = card.querySelector('.card-summary');
  summary.addEventListener('click', () => {
    const expanded = card.classList.toggle('expanded');
    summary.setAttribute('aria-expanded', String(expanded));
  });
  card.querySelector('.remove-button').addEventListener('click', event => {
    event.stopPropagation();
    removeFund(code);
  });
  list.append(card);
  return card;
}

function renderResult(card, data) {
  card.classList.remove('loading', 'failed');
  card.querySelector('.fund-name').textContent = data.name;
  card.querySelector('.estimated-nav').textContent = Number(data.estimatedNav).toFixed(4);
  const estimatedChange = card.querySelector('.estimated-change');
  estimatedChange.textContent = signed(data.estimatedChangePct);
  estimatedChange.className = `estimated-change ${directionClass(data.estimatedChangePct)}`;
  card.querySelector('.official-nav').textContent = Number(data.officialNav).toFixed(4);
  const officialChange = card.querySelector('.official-change');
  officialChange.textContent = signed(data.officialChangePct);
  officialChange.className = `official-change ${directionClass(data.officialChangePct)}`;
  card.querySelector('.official-date').textContent = data.navDate;
  card.querySelector('.facts').innerHTML = [
    `股票仓位 ${data.stockPositionPct}%`,
    `前十大占比 ${data.disclosedWeightPct}%`,
    `持仓期 ${data.holdingDate || '未知'}`,
    data.marketStatus,
  ].map(item => `<span class="fact">${item}</span>`).join('');

  card.querySelector('.holdings').innerHTML = data.holdings.map(item => `
    <div class="holding">
      <b>${escapeHtml(item.name)}</b><strong class="${directionClass(item.changePct)}">${item.changePct == null ? '—' : signed(item.changePct)}</strong>
      <small>${escapeHtml(item.code)} · 权重 ${item.weightPct}%</small><small>${item.price == null ? '' : item.price.toFixed(2)}</small>
    </div>
  `).join('') || '<div class="holding"><span>暂无股票持仓数据</span></div>';
}

function renderError(card, error) {
  card.classList.remove('loading');
  card.classList.add('failed');
  card.querySelector('.fund-name').textContent = '暂时无法估值';
  card.querySelector('.card-error').textContent = error;
}

async function loadFund(code) {
  const card = list.querySelector(`[data-code="${code}"]`) || createCard(code);
  card.classList.add('loading');
  try {
    const response = await fetch(`/api/funds/${code}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '数据读取失败');
    state.results.set(code, body);
    renderResult(card, body);
  } catch (error) {
    state.results.set(code, { error: error.message });
    renderError(card, error.message);
  }
}

async function refreshAll() {
  if (state.refreshing || state.funds.length === 0) return;
  state.refreshing = true;
  state.funds.forEach(code => {
    const card = list.querySelector(`[data-code="${code}"]`) || createCard(code);
    card.classList.add('loading');
  });
  try {
    const response = await fetch(`/api/funds?codes=${encodeURIComponent(state.funds.join(','))}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '数据读取失败');
    body.funds.forEach(data => {
      const card = list.querySelector(`[data-code="${data.code}"]`);
      if (data.error) {
        state.results.set(data.code, { error: data.error });
        renderError(card, data.error);
      } else {
        state.results.set(data.code, data);
        renderResult(card, data);
      }
    });
  } catch (error) {
    state.funds.forEach(code => {
      const card = list.querySelector(`[data-code="${code}"]`);
      renderError(card, error.message);
    });
  } finally {
    state.refreshing = false;
    document.querySelector('#last-refresh').textContent = `最后刷新 ${new Date().toLocaleTimeString('zh-CN')}`;
  }
}

function addFund() {
  const code = input.value.trim();
  message.classList.remove('error');
  if (!/^\d{6}$/.test(code)) {
    message.textContent = '请输入正确的 6 位基金代码。';
    message.classList.add('error');
    return;
  }
  if (state.funds.includes(code)) {
    message.textContent = '这只基金已经在观察列表中。';
    return;
  }
  state.funds.unshift(code);
  save();
  input.value = '';
  message.textContent = `已加入 ${code}，正在估值。`;
  createCard(code);
  loadFund(code);
}

function removeFund(code) {
  state.funds = state.funds.filter(item => item !== code);
  state.results.delete(code);
  save();
  list.querySelector(`[data-code="${code}"]`)?.remove();
}

document.querySelector('#add-button').addEventListener('click', addFund);
document.querySelector('#refresh-button').addEventListener('click', refreshAll);
input.addEventListener('keydown', event => { if (event.key === 'Enter') addFund(); });

state.funds.forEach(createCard);
refreshAll();
setInterval(refreshAll, 60_000);
