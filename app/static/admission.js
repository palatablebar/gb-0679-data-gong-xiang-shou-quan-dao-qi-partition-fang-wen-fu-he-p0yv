(() => {
  const output = document.querySelector('#output');
  const transmit = async (path, payload, form = false) => {
    output.textContent = 'RUNNING';
    try {
      const response = await fetch(path, {
        method: 'POST',
        headers: form ? {} : { 'Content-Type': 'application/json' },
        body: form ? payload : JSON.stringify(payload),
      });
      const record = await response.json();
      if (!response.ok) throw Error(record.message);
      location.reload();
    } catch (error) {
      output.textContent = error.message;
    }
  };

  document.querySelector('#execute').addEventListener('click', () =>
    transmit('/api/admissions', { sample: document.querySelector('#input-set').value })
  );

  document.querySelector('#import').addEventListener('click', () => {
    const selected = document.querySelector('#local-csv').files[0];
    if (!selected) { output.textContent = '请选择查询文件。'; return; }
    const form = new FormData();
    form.append('file', selected);
    transmit('/api/admissions/upload', form, true);
  });

  const review = (decision) =>
    transmit(
      '/api/admissions/runs/' + admissionRecord.id + '/review',
      { version: admissionRecord.version, decision }
    );

  document.querySelector('#seal').addEventListener('click', () => {
    if (!admissionRecord.sealable) { output.textContent = '未证明未混入到期数据，暂不封存。'; return; }
    review('封存');
  });
  document.querySelector('#reject-review').addEventListener('click', () => review('退回补证'));

  document.querySelector('#grep')?.addEventListener('input', (event) => {
    for (const line of document.querySelectorAll('#query-lines tr')) {
      line.hidden = !line.textContent.includes(event.target.value);
    }
  });
})();
