const form = document.querySelector('#form');
const fields = document.querySelector('#fields');
const dropzone = document.querySelector('#dropzone');
const picker = document.querySelector('#images');
const submit = document.querySelector('#submit');
const status = document.querySelector('#status');
const result = document.querySelector('#result');
const uploads = [];
let evaluating = false;

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map(item => item.msg).join('；') : data.detail;
    throw new Error(detail || '请求失败，请重试');
  }
  return data;
}

function renderUploads() {
  document.querySelector('#uploads').replaceChildren(...uploads.map(item => {
    const row = document.createElement('li');
    const preview = document.createElement('img');
    preview.src = item.preview;
    preview.alt = item.file.name;
    const label = document.createElement('span');
    label.textContent = `${item.file.name} · ${item.error || item.code || '上传中…'}`;
    if (item.error) label.className = 'error';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '移除';
    remove.disabled = item.pending || evaluating;
    remove.onclick = () => {
      uploads.splice(uploads.indexOf(item), 1);
      URL.revokeObjectURL(item.preview);
      renderUploads();
    };
    row.append(preview, label, remove);
    return row;
  }));
  submit.disabled = evaluating || uploads.some(item => item.pending || item.error);
}

function uploadFiles(files) {
  if (evaluating) return;
  result.hidden = true;
  for (const file of files) {
    const item = { file, preview: URL.createObjectURL(file), pending: true };
    uploads.push(item); // 先记录拖入顺序，上传完成顺序不会改变提交顺序。
    const body = new FormData();
    body.append('file', file);
    request('/api/files', { method: 'POST', body })
      .then(data => { item.code = data.file_code; })
      .catch(error => { item.error = error.message; })
      .finally(() => { item.pending = false; renderUploads(); });
  }
  renderUploads();
}

picker.addEventListener('change', () => {
  uploadFiles(picker.files);
  picker.value = '';
});
for (const event of ['dragenter', 'dragover', 'dragleave', 'drop']) {
  dropzone.addEventListener(event, e => {
    e.preventDefault();
    dropzone.classList.toggle('dragover', event === 'dragenter' || event === 'dragover');
    if (event === 'drop') uploadFiles(e.dataTransfer.files);
  });
}

function verificationStatus(data) {
  if (data.status === 'no_claims') {
    return { message: '材料中未提取到明确主张，未进行核验。请补充包含具体说法的文字、图片或链接。', attention: false };
  }
  const results = data.subgraph_results || {};
  const labels = { fact: '事实', route: '路线', crowd: '人流', experience: '体验' };
  const names = state => Object.entries(results)
    .filter(([, result]) => result.status === state)
    .map(([name]) => labels[name] || name).join('、');
  const completed = names('completed');
  const partial = names('partial');
  const failed = names('failed');
  const skipped = names('skipped');
  const unavailable = names('not_implemented');
  const messages = [`已提取 ${data.claims.length} 条主张。`];
  if (completed) messages.push(`${completed}核验流程已完成。`);
  if (partial) messages.push(`${partial}核验仅完成一部分，请查看已返回的结果和未完成项。`);
  if (failed) messages.push(`${failed}核验失败，已保留已有材料，请查看错误说明。`);
  if (skipped) messages.push(`本次未执行${skipped}核验，请查看各项结果中的说明。`);
  if (unavailable) messages.push(`${unavailable}核验尚未实现，本次未核验这些内容。`);
  if (messages.length === 1) {
    const fallback = {
      not_implemented: '本次核验功能尚未实现，尚未对这些说法作出核验结论。',
      completed: '核验流程已完成。',
      partial: '仍有核验步骤未完成，请查看各项结果。',
      failed: '核验失败，已保留提取的主张，请查看错误说明。',
    };
    messages.push(fallback[data.status] || '核验流程已结束，请查看结果。');
  }
  // 执行完成与主张是否得到证据支持是两种结果，证据不足不能显示为核验失败。
  const findings = results.fact?.findings || [];
  const insufficient = new Set(findings.filter(item => item.assessment?.evidence_sufficient === false)
    .map(item => item.claim_id));
  const unverified = new Set(findings.filter(item => item.assessment?.verdict === 'UNVERIFIED' &&
    item.assessment.evidence_sufficient !== false).map(item => item.claim_id));
  if (insufficient.size) messages.push(`有 ${insufficient.size} 条事实主张证据不足，未能确认；这不表示这些说法是假的。`);
  if (unverified.size) messages.push(`有 ${unverified.size} 条事实主张未能确认，请查看判定理由。`);
  return {
    message: messages.join(''),
    attention: Boolean(partial || failed || insufficient.size || unverified.size ||
      data.status === 'failed' || (data.status === 'partial' && !Object.keys(results).length)),
  };
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (evaluating || uploads.some(item => item.pending || item.error)) return;
  const targetPlace = document.querySelector('#place').value.trim();
  if (!targetPlace) return;
  const payload = {
    target_place: targetPlace,
    text: document.querySelector('#description').value,
    link: document.querySelector('#links').value.split(/\r?\n/).map(link => link.trim()).filter(Boolean),
    image: uploads.map(item => item.code),
  };
  if (!payload.text.trim() && !payload.link.length && !payload.image.length) {
    result.hidden = true;
    status.className = 'error';
    status.textContent = '请至少提供一种材料：文字、图片或链接。';
    return;
  }
  evaluating = true;
  fields.disabled = true;
  renderUploads();
  result.hidden = true;
  status.className = '';
  status.textContent = '正在读取材料并执行核验，请稍候…';
  try {
    const data = await request('/api/verifications', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    document.querySelector('#claims').textContent = JSON.stringify(data, null, 2);
    result.hidden = false;
    const summary = verificationStatus(data);
    status.className = summary.attention ? 'error' : '';
    status.textContent = summary.message;
  } catch (error) {
    status.className = 'error';
    status.textContent = error.message;
  } finally {
    evaluating = false;
    fields.disabled = false;
    renderUploads();
  }
});
