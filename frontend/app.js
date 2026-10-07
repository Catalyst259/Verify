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
  // 四类子图的 UNVERIFIED 语义一致，证据不足的统计不限于事实核验。
  const findings = Object.values(results).flatMap(result => result.findings || []);
  const insufficient = new Set(findings.filter(item => item.assessment?.evidence_sufficient === false)
    .map(item => item.claim_id));
  const unverified = new Set(findings.filter(item => item.assessment?.verdict === 'UNVERIFIED' &&
    item.assessment.evidence_sufficient !== false).map(item => item.claim_id));
  if (insufficient.size) messages.push(`有 ${insufficient.size} 条主张证据不足，未能确认；这不表示这些说法是假的。`);
  if (unverified.size) messages.push(`有 ${unverified.size} 条主张未能确认，请查看判定理由。`);
  return {
    message: messages.join(''),
    attention: Boolean(partial || failed || insufficient.size || unverified.size ||
      data.status === 'failed' || (data.status === 'partial' && !Object.keys(results).length)),
  };
}

// 来源原文和模型理由均作为文本展示，不能作为 HTML 或脚本执行。
function textElement(tag, text, className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
}

// 四类子图各有自己的判定词表：同一个词在不同类别里含义不同，套用同一套标签会把
// 「数值冲突」「存在明显分化」显示成事实判定的措辞，等于丢掉后端语义。
const VERDICT_LABELS = {
  fact: {
    SUPPORTED: '有证据支持', CONTRADICTED: '与证据矛盾',
    CONDITIONAL: '有条件成立', UNVERIFIED: '证据不足，未能确认',
  },
  route: {
    MATCHED: '数值吻合', MISMATCHED: '数值冲突',
    CONDITION_MISMATCH: '条件不符', UNVERIFIED: '证据不足，未能确认',
  },
  crowd: {
    SUPPORTED: '当前证据支持', NOT_SUPPORTED: '当前证据不支持',
    SCENARIO_ONLY: '仅特定场景成立', UNVERIFIED: '证据不足，未能确认',
  },
  experience: {
    CONSISTENT: '体验较一致', DIVERGENT: '存在明显分化',
    SCENARIO_DEPENDENT: '高度依赖场景', UNVERIFIED: '证据不足，未能确认',
  },
};
const KIND_NAMES = { fact: '事实', route: '路线', crowd: '人流', experience: '体验' };

// 判定词表里没有卡片的分类，用判定自身的 kind 判别；旧数据没有 kind 即事实判定。
function assessmentKind(finding, fallback) {
  return finding?.assessment?.kind || fallback;
}

function durationText(seconds) {
  if (seconds == null) return '未知';
  const minutes = seconds / 60;
  return Number.isInteger(minutes) ? `${minutes} 分钟` : `${minutes.toFixed(1)} 分钟`;
}

function distanceText(meters) {
  return meters == null ? '未知' : `${(meters / 1000).toFixed(1)} 公里`;
}

function renderVerification(data) {
  const states = {
    not_implemented: '此类核验尚未实现', skipped: '本次未执行此类核验',
    failed: '核验失败，尚未获得结论', partial: '核验未全部完成，尚未获得结论',
  };
  const graphs = Object.values(data.subgraph_results || {});
  const cards = [];
  const list = (parent, title, items) => {
    if (!items.length) return;
    parent.append(textElement('h4', title));
    const entries = document.createElement('ul');
    entries.append(...items.map(item => textElement('li', item)));
    parent.append(entries);
  };
  for (const claim of data.claims) {
    const findings = graphs.flatMap(graph => (graph.findings || []).map(finding => ({ graph, finding })))
      .filter(({ finding }) => finding.claim_id === claim.claim_id);
    const entries = findings.length ? findings : [{
      graph: (data.subgraph_results || {})[claim.type.toLowerCase()], finding: null,
    }];
    for (const { graph, finding } of entries) {
      const card = textElement('article', '', 'claim-result');
      const assessment = finding?.assessment;
      const kind = assessmentKind(finding, graph?.graph_name || claim.type.toLowerCase());
      card.append(textElement('p', `${KIND_NAMES[kind] || kind}核验`, 'kind'));
      card.append(textElement('h3', claim.content));
      const labels = VERDICT_LABELS[kind] || {};
      const verdict = textElement('p', labels[assessment?.verdict] || states[graph?.status] ||
        assessment?.verdict || '尚未获得核验结论', 'verdict');
      if (assessment) verdict.dataset.verdict = assessment.verdict;
      card.append(verdict);
      if (assessment && ['partial', 'failed'].includes(graph?.status)) {
        card.append(textElement('p', `${graph.status === 'failed' ? '本项核验失败' : '本项核验仅完成一部分'}；当前展示已返回的判定。`, 'error'));
      }
      // 数值对比是路线结论本身：主张值与实测值并列，用户不读理由也能看出冲突。
      if (assessment?.kind === 'route') {
        card.append(textElement('p', [
          `主张 ${durationText(assessment.claimed_seconds)} · 实测 ${durationText(assessment.measured_seconds)}`,
          `交通方式 ${assessment.transport_mode} · 距离 ${distanceText(assessment.distance_meters)}` +
            ` · 容差 ${durationText(assessment.tolerance_seconds)}`,
        ].join('\n'), 'route-compare'));
      }
      if (assessment?.kind === 'crowd') {
        card.append(textElement('p', `场景条件：${assessment.scenario}` + (assessment.evidence_time_coverage
          ? `\n证据时间覆盖：${assessment.evidence_time_coverage}` : ''), 'scenario'));
      }
      if (assessment?.kind === 'experience') {
        card.append(textElement('p', `来源一致度：${assessment.source_agreement == null ? '未知' :
          `${Math.round(assessment.source_agreement * 100)}%`}`, 'agreement'));
      }
      const evidence = finding?.evidence || [];
      let reason = assessment?.reason || finding?.summary;
      // 结论里的内部证据 ID 换成与下方材料对应的编号，原响应仍完整保留。
      evidence.forEach((item, index) => {
        if (reason && item.evidence_id) {
          const label = `【证据 ${index + 1}】`;
          reason = reason.replaceAll(`证据${item.evidence_id}`, label).replaceAll(item.evidence_id, label);
        }
      });
      if (reason) card.append(textElement('p', reason, 'reason'));
      if (assessment) {
        card.append(textElement('p', `核验范围：${assessment.target}\n时间范围：${assessment.time_scope}`, 'scope'));
        list(card, '成立条件或例外', assessment.conditions || []);
        list(card, '仍待核实', (assessment.remaining_gaps || []).map(gap => `${gap.question} — ${gap.reason}`));
      }
      const error = finding?.error || graph?.error;
      if (error) card.append(textElement('p', `未完成项：${error}`, 'error'));
      const roles = [
        ['supporting_evidence', '支持证据'], ['counter_evidence', '反证'], ['context_evidence', '背景证据'],
      ];
      const roleFor = item => roles.filter(([field]) => item.evidence_id &&
        (assessment?.[field] || []).includes(item.evidence_id)).map(([, label]) => label);
      const cited = evidence.filter(item => roleFor(item).length).length;
      card.append(textElement('p', `采集 ${evidence.length} 份材料；判定引用 ${cited} 份。`, 'evidence-count'));
      for (const item of [...evidence].sort((a, b) => Boolean(roleFor(b).length) - Boolean(roleFor(a).length))) {
        const detail = textElement('details', '', 'evidence');
        const labels = roleFor(item);
        detail.open = labels.length > 0;
        detail.append(textElement('summary', `证据 ${evidence.indexOf(item) + 1} · ${labels.join('、') || '未被本次判定引用'} · ${item.source}`));
        if (item.url) {
          try {
            const url = new URL(item.url);
            if (['http:', 'https:'].includes(url.protocol)) {
              const link = textElement('a', '打开来源');
              link.href = url.href;
              link.target = '_blank';
              link.rel = 'noopener noreferrer';
              detail.append(link);
            }
          } catch { /* 无法解析的来源地址保留在原始响应中，不生成可点击链接。 */ }
        }
        detail.append(textElement('p', `发布时间：${item.published_at || '未知'}\n读取时间：${item.retrieved_at || '未知'}`, 'evidence-time'));
        const original = document.createElement('details');
        original.append(textElement('summary', '查看证据原文'), textElement('p', item.content, 'evidence-content'));
        detail.append(original);
        card.append(detail);
      }
      cards.push(card);
    }
  }
  // 冲突只陈述矛盾，不替用户裁决：两侧各自的判定词与依据都要能直接看到。
  // 无冲突时不渲染任何内容，干净的一次核验不该出现冲突区块。
  const conflicts = (data.conflicts || []).map(conflict => {
    const card = textElement('article', '', 'claim-conflict');
    const claim = data.claims.find(item => item.claim_id === conflict.claim_id);
    card.append(textElement('h3', '同一条说法的结论互相矛盾'));
    if (claim) card.append(textElement('p', claim.content));
    card.append(textElement('p', conflict.detail, 'conflict-detail'));
    const sides = document.createElement('ul');
    sides.append(...Object.entries(conflict.by_graph).map(([name, item]) => {
      const kind = assessmentKind(item, name);
      const label = (VERDICT_LABELS[kind] || {})[item.assessment?.verdict] || item.assessment?.verdict;
      return textElement('li', `${KIND_NAMES[kind] || name}核验：${label || '尚未获得核验结论'}`
        + ` —— ${item.summary}`);
    }));
    card.append(sides);
    return card;
  });
  document.querySelector('#conflicts').replaceChildren(...conflicts);
  document.querySelector('#conflicts').hidden = !conflicts.length;
  document.querySelector('#findings').replaceChildren(...cards);
  document.querySelector('#claims').textContent = JSON.stringify(data, null, 2);
  document.querySelector('#raw-result').open = false;
  result.hidden = false;
  const summary = verificationStatus(data);
  status.className = summary.attention ? 'error' : '';
  status.textContent = summary.message;
  result.focus({ preventScroll: true });
  result.scrollIntoView({ block: 'start' });
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
    renderVerification(data);
  } catch (error) {
    status.className = 'error';
    status.textContent = error.message;
  } finally {
    evaluating = false;
    fields.disabled = false;
    renderUploads();
  }
});
