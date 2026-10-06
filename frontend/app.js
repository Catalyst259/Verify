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
    const messages = {
      no_claims: '材料中未提取到明确主张，未进行核验。请补充包含具体说法的文字、图片或链接。',
      not_implemented: `已提取 ${data.claims.length} 条主张，核验功能尚未实现。`,
      completed: `核验流程已完成，共 ${data.claims.length} 条主张。`,
      partial: '核验未全部完成，请查看各项结果。',
      failed: '核验失败，已保留提取的主张，请查看结果。',
    };
    status.className = ['partial', 'failed'].includes(data.status) ? 'error' : '';
    status.textContent = messages[data.status] || '核验流程已结束，请查看结果。';
  } catch (error) {
    status.className = 'error';
    status.textContent = error.message;
  } finally {
    evaluating = false;
    fields.disabled = false;
    renderUploads();
  }
});
