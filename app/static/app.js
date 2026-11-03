'use strict';
const $ = id => document.getElementById(id);
let forced = false, timer, browsePath = '', lastLog = 0, busy = false, lastJob = '';
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(timer); timer = setTimeout(() => $('toast').hidden = true, 5500); }
async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {method, headers: {'Content-Type': 'application/json', 'X-Requested-With': 'adname-ui'}, ...(body !== undefined ? {body: JSON.stringify(body)} : {})});
  let data; try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') showLogin();
    throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리할 수 없습니다. 입력값을 확인하세요.');
  }
  return data;
}
function handle(fn) { return async event => { event?.preventDefault(); try { await fn(event); } catch (error) { toast(error.message); } }; }
function showLogin() { $('loginView').hidden = false; $('mainView').hidden = true; $('initialSetupView').hidden = true; $('topActions').hidden = true; forced = false; for (const d of document.querySelectorAll('dialog[open]')) d.close(); }
async function signedIn() {
  const me = await api('/me'); forced = me.must_change;
  $('loginView').hidden = true; $('topActions').hidden = forced; $('mainView').hidden = forced;
  $('initialSetupView').hidden = !forced;
  $('userLabel').textContent = me.username;
  if (forced) $('initialUser').value = me.username; else await poll();
}
$('loginForm').onsubmit = handle(async () => {
  await api('/login', 'POST', {username: $('loginUser').value, password: $('loginPass').value, code: $('loginCode').value.trim()});
  $('loginPass').value = ''; $('loginCode').value = ''; await signedIn();
});
$('logout').onclick = handle(async () => { await api('/logout', 'POST'); showLogin(); });
$('initialSetupForm').onsubmit = handle(async () => {
  if ($('initialPass').value !== $('initialConfirm').value) throw new Error('새 비밀번호 확인이 일치하지 않습니다.');
  await api('/account/setup', 'POST', {username: $('initialUser').value, password: $('initialPass').value});
  $('initialSetupForm').reset(); toast('관리자 계정을 저장했습니다.'); await signedIn();
});
async function openSettings() {
  const s = await api('/settings'); $('settingsForm').reset();
  resetTelegramChats();
  $('settingUser').value = s.username; $('totpSetup').hidden = true;
  $('totpStatus').textContent = s.totp_enabled ? '사용 중' : '사용 안 함';
  $('setupTotp').hidden = s.totp_enabled; $('disableTotp').hidden = !s.totp_enabled;
  $('webhookStatus').textContent = s.webhook_configured ? '웹후크 저장됨 · 비워 두면 기존 주소 유지' : '저장된 웹후크 없음';
  $('telegramStatus').textContent = s.telegram_configured ? 'Telegram 설정 저장됨 · 비워 두면 기존 값 유지' : '저장된 Telegram 설정 없음';
  $('telegramCommands').checked = s.telegram_commands;
  for (const e of document.querySelectorAll('[name=event]')) e.checked = s.events.includes(e.value);
  if (!$('settingsDialog').open) $('settingsDialog').showModal();
}
$('settingsOpen').onclick = handle(openSettings);
for (const close of document.querySelectorAll('.close')) close.onclick = () => close.closest('dialog').close();
for (const dialog of document.querySelectorAll('dialog')) {
  let startedOutside = false;
  const outside = event => {
    const r = dialog.getBoundingClientRect();
    return event.target === dialog && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom);
  };
  dialog.addEventListener('pointerdown', event => { startedOutside = outside(event); });
  dialog.addEventListener('click', event => { if (startedOutside && outside(event)) dialog.close(); startedOutside = false; });
}
$('settingsDialog').addEventListener('close', () => {
  $('settingsForm').reset(); $('totpKey').value = ''; $('qr').removeAttribute('src'); $('totpSetup').hidden = true;
  resetTelegramChats();
});
$('settingsForm').onsubmit = handle(async () => {
  if ($('newPass').value !== $('confirmPass').value) throw new Error('새 비밀번호 확인이 일치하지 않습니다.');
  await api('/settings', 'PUT', {username: $('settingUser').value, password: $('newPass').value,
    webhook: $('clearWebhook').checked ? '' : ($('webhook').value.trim() || null),
    telegram_token: $('clearTelegram').checked ? '' : ($('telegramToken').value.trim() || null),
    telegram_chat_id: $('clearTelegram').checked ? '' : ($('telegramChatId').value.trim() || null),
    telegram_commands: !$('clearTelegram').checked && $('telegramCommands').checked,
    events: [...document.querySelectorAll('[name=event]:checked')].map(e => e.value)});
  $('settingsDialog').close(); toast('설정을 저장했습니다.'); await signedIn();
});
$('setupTotp').onclick = handle(async () => { const s = await api('/totp/setup', 'POST'); $('qr').src = s.qr; $('totpKey').value = s.key; $('totpSetup').hidden = false; });
$('copyKey').onclick = handle(async () => {
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText($('totpKey').value);
  else { $('totpKey').select(); if (!document.execCommand('copy')) throw new Error('키를 선택했습니다. 직접 복사하세요.'); }
  toast('등록 키를 복사했습니다.');
});
$('enableTotp').onclick = handle(async () => {
  await api('/totp/enable', 'POST', {code: $('setupCode').value.trim()});
  $('settingsDialog').close(); toast('TOTP를 활성화했습니다. 이후 로그인 시 인증 코드가 필요합니다.');
});
$('disableTotp').onclick = handle(async () => {
  await api('/totp/disable', 'POST');
  $('settingsDialog').close(); toast('TOTP를 해제했습니다.');
});
$('testWebhook').onclick = handle(async () => {
  $('testWebhook').disabled = true;
  try { await api('/webhook/test', 'POST', {webhook: $('webhook').value.trim() || null}); toast('테스트 알림을 전송했습니다. Discord를 확인하세요.'); }
  finally { $('testWebhook').disabled = false; }
});
$('testTelegram').onclick = handle(async () => {
  $('testTelegram').disabled = true;
  try {
    await api('/telegram/test', 'POST', {telegram_token: $('telegramToken').value.trim() || null,
      telegram_chat_id: $('telegramChatId').value.trim() || null});
    toast('테스트 알림을 전송했습니다. Telegram을 확인하세요.');
  } finally { $('testTelegram').disabled = false; }
});
let telegramLookup = 0;
function resetTelegramChats() {
  telegramLookup++;
  $('telegramChatsPanel').hidden = true;
  $('telegramChatChoices').replaceChildren();
  $('telegramChatChoices').closest('label').hidden = true;
  $('telegramDiscoveryStatus').textContent = '';
  $('findTelegramChats').disabled = false;
}
$('telegramToken').addEventListener('input', resetTelegramChats);
$('findTelegramChats').onclick = handle(async () => {
  resetTelegramChats();
  const lookup = telegramLookup;
  $('findTelegramChats').disabled = true;
  try {
    const data = await api('/telegram/chats', 'POST', {telegram_token: $('telegramToken').value.trim() || null});
    if (lookup !== telegramLookup || !$('settingsDialog').open) return;
    $('telegramChatsPanel').hidden = false;
    const bot = data.bot_username ? `@${data.bot_username}` : '해당 봇';
    $('telegramDiscoveryStatus').textContent = data.chats.length
      ? `${bot}에서 확인한 수신 대화를 선택하세요. 선택 후 연결 테스트와 설정 저장이 필요합니다.`
      : `${bot}에게 지금 /start를 보내고 채팅 ID 찾기를 다시 누르세요. 응답이 없어도 정상입니다. 그룹은 /start@봇아이디, 채널은 새 게시글을 작성하세요. 다른 프로그램이 메시지를 처리하는 봇이라면 전용 봇을 사용하거나 숫자 ID를 직접 입력하세요.`;
    const select = $('telegramChatChoices');
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '알림을 받을 대화 선택';
    select.append(placeholder);
    const types = {private: '개인', group: '그룹', supergroup: '그룹', channel: '채널'};
    for (const chat of data.chats) {
      const option = document.createElement('option'); option.value = chat.id;
      option.textContent = `${chat.name} · ${types[chat.type]} · ${chat.id}`; select.append(option);
    }
    select.closest('label').hidden = !data.chats.length;
  } finally { if (lookup === telegramLookup) $('findTelegramChats').disabled = false; }
});
$('telegramChatChoices').onchange = () => {
  if ($('telegramChatChoices').value) {
    $('telegramChatId').value = $('telegramChatChoices').value;
    $('clearTelegram').checked = false;
    toast('채팅 ID를 입력했습니다. 연결 테스트 후 설정을 저장하세요.');
  }
};
async function loadFolders(path) {
  const data = await api('/folders?path=' + encodeURIComponent(path)); browsePath = data.path;
  $('browsePath').textContent = '/' + browsePath; $('parentFolder').disabled = !browsePath;
  $('folderList').replaceChildren();
  if (!data.folders.length) $('folderList').textContent = '하위 폴더가 없습니다.';
  for (const name of data.folders) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = '▱  ' + name;
    b.onclick = handle(() => loadFolders([browsePath, name].filter(Boolean).join('/'))); $('folderList').append(b);
  }
}
$('browse').onclick = handle(async () => { await loadFolders($('folder').value); $('folderDialog').showModal(); });
$('parentFolder').onclick = handle(() => loadFolders(browsePath.split('/').slice(0, -1).join('/')));
$('selectFolder').onclick = () => { $('folder').value = '/' + browsePath; $('folderDialog').close(); };
const cleanRule = value => value.replace(/^[\u200b\ufeff\u2060]+|[\u200b\ufeff\u2060]+$/gu, '');
for (const id of ['find', 'replace']) {
  $(id).addEventListener('change', () => { $(id).value = cleanRule($(id).value); });
  $(id).addEventListener('input', updateExample);
}
function updateExample() {
  const f = cleanRule($('find').value), r = cleanRule($('replace').value);
  const example = f ? (f.startsWith('#') ? 'anime-1' + f + '.mkv' : f + 'movie1.mp4') : 'abcd.com@movie1.mp4';
  $('example').textContent = example + ' → ' + example.split(f || 'abcd.com@').join(r);
}
$('renameForm').onsubmit = handle(async () => {
  for (const id of ['find', 'replace']) $(id).value = cleanRule($(id).value);
  updateExample();
  if (!$('find').value) throw new Error('찾을 문구를 입력하세요.');
  $('start').disabled = true;
  try { renderJob(await api('/jobs', 'POST', {folder: $('folder').value, find: $('find').value, replace: $('replace').value})); }
  catch (error) { $('start').disabled = false; throw error; }
});
$('stop').onclick = handle(async () => renderJob(await api('/jobs/stop', 'POST')));
function renderJob(j) {
  busy = ['scanning', 'running', 'stopping'].includes(j.state);
  const scanning = j.state === 'scanning';
  $('start').disabled = busy; $('stop').disabled = !busy || j.state === 'stopping';
  for (const id of ['browse', 'find', 'replace']) $(id).disabled = busy;
  $('spinner').hidden = !scanning && j.state !== 'stopping'; $('statusIcon').hidden = scanning || j.state === 'stopping';
  const names = {idle:'대기', scanning:'탐색 중', running:'진행 중', stopping:'중단 중', stopped:'중단됨', completed:'완료', no_work:'작업 없음', error:'오류'};
  $('statusBadge').textContent = names[j.state] || j.state;
  $('statusText').textContent = ({idle:'작업을 시작할 준비가 되었습니다',scanning:'탐색 중',running:'파일명을 변경하고 있습니다',stopping:'작업을 중단하고 있습니다',stopped:'작업을 중단했습니다',completed:'작업이 완료되었습니다',no_work:'수행할 작업이 없습니다',error:'작업 중 오류가 발생했습니다'})[j.state];
  $('statusDetail').textContent = scanning ? `${j.scanned.toLocaleString()}개 파일 확인 · 변경 대상 수집 중` :
    j.current || (j.state === 'idle' ? '폴더와 변경 규칙을 지정해 주세요.' : j.errors ? '일부 파일에 오류가 있습니다. 관리자 로그를 확인하세요.' : j.total === 0 ? '변경할 파일이 없습니다.' : '자세한 변경 내역은 관리자 로그에서 확인하세요.');
  $('progressArea').hidden = scanning || ['idle','stopping','no_work'].includes(j.state);
  $('percent').textContent = j.percent + '%'; $('progress').value = j.percent;
  $('count').textContent = scanning ? '— / —' : `${j.processed.toLocaleString()} / ${j.total.toLocaleString()}`;
  $('renamed').textContent = j.renamed.toLocaleString(); $('errors').textContent = j.errors.toLocaleString();
  lastJob = j.id;
}
async function poll() {
  if ($('mainView').hidden) return;
  try { renderJob(await api('/jobs')); }
  catch (error) { if (!$('mainView').hidden) { $('statusBadge').textContent = '연결 끊김'; $('statusDetail').textContent = '서버 연결을 확인하세요. 상태를 다시 확인하고 있습니다.'; } }
}
setInterval(poll, 1000);
async function loadLogs(append = false) {
  const rows = await api('/logs?before=' + (append ? lastLog : 0));
  if (!append) $('logsList').replaceChildren();
  if (!rows.length && !append) $('logsList').textContent = '기록된 로그가 없습니다.';
  for (const row of rows) {
    const div = document.createElement('div'); div.className = 'log-item' + (row.level === 'error' ? ' log-error' : '');
    const time = document.createElement('time'); time.textContent = `${row.time} · ${row.level}`;
    const message = document.createElement('div'); message.textContent = row.message; div.append(time, message); $('logsList').append(div); lastLog = row.id;
  }
  $('olderLogs').disabled = rows.length < 200;
}
$('openLogs').onclick = handle(async () => { await loadLogs(); $('logsDialog').showModal(); });
$('refreshLogs').onclick = handle(() => loadLogs()); $('olderLogs').onclick = handle(() => loadLogs(true));
signedIn().catch(() => showLogin());

let lastChange = 0;
function fileSize(bytes) {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toLocaleString(undefined, {maximumFractionDigits: 2})} ${units[index]}`;
}
async function loadChanges(append = false) {
  const data = await api('/changes?all_jobs=' + ($('changesScope').value === 'all') + '&before=' + (append ? lastChange : 0));
  if (!append) { $('changesList').replaceChildren(); lastChange = 0; }
  $('changesSummary').textContent = `${$('changesScope').value === 'all' ? '전체' : '최근 작업'} · 보관된 변경 ${data.total.toLocaleString()}건` + (data.job && $('changesScope').value !== 'all' ? ` · 대상 /${data.job.folder}` : '');
  for (const row of data.rows) {
    const tr = document.createElement('tr');
    for (const value of [row.path, row.old_name, row.new_name]) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    const size = document.createElement('td'); size.textContent = fileSize(row.size_bytes);
    const exact = document.createElement('small'); exact.textContent = row.size_bytes.toLocaleString() + ' bytes'; size.append(exact); tr.append(size);
    $('changesList').append(tr); lastChange = row.id;
  }
  $('changesEmpty').hidden = data.total !== 0; $('olderChanges').disabled = data.rows.length < 200;
}
$('openChanges').onclick = handle(async () => { await loadChanges(); $('changesDialog').showModal(); });
$('refreshChanges').onclick = handle(() => loadChanges());
$('changesScope').onchange = handle(() => loadChanges());
$('olderChanges').onclick = handle(() => loadChanges(true));
