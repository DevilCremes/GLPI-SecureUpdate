(() => {
    function syncApproval(form) {
        form.querySelector('button.btn-primary').disabled = !form.elements.confirm.checked;
    }

    function syncSections(ledger) {
        const panes = Array.from(ledger.querySelectorAll('.gu-pane'));
        const index = ledger.querySelector('.gu-index');
        if (!panes.length || !index) return;
        if (!index.children.length) {
            for (const pane of panes) {
                const link = document.createElement('a');
                link.href = '#' + pane.id;
                link.textContent = pane.dataset.label;
                link.setAttribute('aria-controls', pane.id);
                index.append(link);
            }
        }
        const selected = panes.find(pane => '#' + pane.id === location.hash) || panes[0];
        panes.forEach(pane => { pane.hidden = pane !== selected; });
        for (const link of index.children) {
            if (link.hash === '#' + selected.id) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
        }
        index.hidden = panes.length < 2;
        ledger.classList.toggle('gu-indexed', !index.hidden);
    }

    function syncPage() {
        document.querySelectorAll('.gu-approval').forEach(syncApproval);
        document.querySelectorAll('.gu-ledger').forEach(syncSections);
    }
    function followOperation(monitor) {
        const details = monitor.querySelector('details');
        const list = monitor.querySelector('ol');
        const heading = monitor.querySelector('strong');
        const bar = monitor.querySelector('progress');
        const meter = monitor.querySelector('.gu-meter-text');
        const connection = monitor.querySelector('.gu-connection');
        const preference = 'secureupdate.console.open';
        try { details.open = sessionStorage.getItem(preference) !== 'no'; } catch {}
        details.addEventListener('toggle', () => {
            try { sessionStorage.setItem(preference, details.open ? 'yes' : 'no'); } catch {}
        });

        let rows = [];
        let lastJob;
        let delay = 2000;
        let tracking = !monitor.hidden;
        let unconfirmedJob = null;
        const feedback = monitor.querySelector('.gu-request-feedback');

        function show(activity) {
            if (heading.textContent !== activity.title) heading.textContent = activity.title;
            monitor.classList.toggle('gu-attention', activity.attention);
            const transfer = activity.transfer;
            bar.hidden = !activity.busy;
            if (transfer && transfer[1] > 0) {
                bar.max = transfer[1];
                bar.value = transfer[0];
                meter.textContent = Math.floor(100 * transfer[0] / transfer[1]) + '% do pacote';
            } else {
                bar.removeAttribute('value');
                meter.textContent = '';
            }
            const events = activity.events.slice();
            if (!events.length || events[events.length - 1][1] !== activity.title) {
                events.push([null, activity.title]);
            }
            if (lastJob !== activity.job || rows.length > events.length ||
                rows.some((entry, i) => entry[0] !== events[i][0] || entry[1] !== events[i][1])) {
                list.replaceChildren();
                rows = [];
            }
            const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 12;
            for (const [stamp, label] of events.slice(rows.length)) {
                const row = document.createElement('li');
                if (stamp) {
                    const time = document.createElement('time');
                    const date = new Date(stamp * 1000);
                    time.dateTime = date.toISOString();
                    time.textContent = date.toLocaleTimeString('pt-BR');
                    row.append(time);
                }
                row.append(document.createTextNode(label));
                list.append(row);
            }
            if (follow && details.open) list.scrollTop = list.scrollHeight;
            lastJob = activity.job;
            rows = events;
        }

        async function get(url) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await fetch(url, {cache:'no-store', credentials:'same-origin',
                    signal:controller.signal, redirect:'error'});
                if (!response.ok) throw new Error('unavailable');
                return await response.text();
            } finally { clearTimeout(timer); }
        }

        async function update() {
            try {
                const activity = JSON.parse(await get(monitor.dataset.url));
                if (typeof activity.busy !== 'boolean' || !Array.isArray(activity.events)) throw new Error('activity');
                show(activity);
                if (!activity.busy) {
                    const current = document.querySelector('.gu-results');
                    const url = new URL(location.href);
                    url.searchParams.set('conteudo', '1');
                    const fragment = new DOMParser().parseFromString(await get(url), 'text/html');
                    const next = fragment.querySelector('.gu-results');
                    if (!next || next.dataset.tab !== current.dataset.tab) throw new Error('fragment');
                    current.replaceWith(document.importNode(next, true));
                    syncPage();
                    if (next.dataset.busy !== '1' && next.dataset.job === activity.job) {
                        connection.hidden = true;
                        tracking = false;
                        if (!activity.job && feedback.hidden) monitor.hidden = true;
                        if (unconfirmedJob === activity.job) heading.textContent = 'Solicitação sem confirmação';
                        return;
                    }
                }
                connection.hidden = true;
                delay = 2000;
            } catch {
                connection.hidden = false;
                connection.textContent = 'Sem resposta do painel. Tentando reconectar automaticamente…';
                delay = Math.min(delay * 2, 15000);
            }
            setTimeout(update, delay);
        }
        document.addEventListener('submit', event => {
            const form = event.target;
            const current = form.closest('.gu-results');
            if (!current || form.method.toLowerCase() !== 'post') return;
            const data = new FormData(form);
            const button = event.submitter;
            if (button && button.name) data.set(button.name, button.value);
            if (!data.get('op') || data.get('op') === 'bfetch') return;
            event.preventDefault();
            if (tracking) return;
            tracking = true;
            delay = 2000;
            current.inert = true;
            unconfirmedJob = null;
            monitor.hidden = false;
            feedback.hidden = connection.hidden = true;
            rows = [];
            list.replaceChildren();
            bar.hidden = false;
            bar.removeAttribute('value');
            meter.textContent = '';
            monitor.classList.remove('gu-attention');
            heading.textContent = 'Enviando solicitação ao executor';
            monitor.scrollIntoView({block:'nearest'});
            const xhr = new XMLHttpRequest();
            xhr.open('POST', form.action || location.href);
            xhr.setRequestHeader('X-SecureUpdate-Panel', '1');
            xhr.responseType = 'json';
            if (data.get('op') === 'upload') {
                xhr.upload.onprogress = progress => {
                    heading.textContent = 'Enviando arquivo do computador';
                    if (progress.lengthComputable) {
                        bar.max = progress.total;
                        bar.value = progress.loaded;
                        meter.textContent = Math.floor(100 * progress.loaded / progress.total) + '% enviado';
                    }
                };
                xhr.upload.onload = () => {
                    heading.textContent = 'Aguardando o executor verificar o pacote';
                    bar.removeAttribute('value');
                    meter.textContent = '';
                };
            }
            xhr.onloadend = () => {
                const reply = xhr.response;
                if (xhr.status === 200 && reply && typeof reply.accepted === 'boolean') {
                    const next = new URL(reply.location, location.href);
                    if (next.origin === location.origin) history.replaceState(null, '', next);
                    if (!reply.accepted) {
                        unconfirmedJob = current.dataset.job;
                        feedback.textContent = reply.message;
                        feedback.hidden = false;
                    }
                } else {
                    unconfirmedJob = current.dataset.job;
                    feedback.textContent = 'Não foi possível confirmar o recebimento. Confira o resultado e o identificador do trabalho antes de repetir.';
                    feedback.hidden = false;
                }
                update();
            };
            xhr.send(data);
        });
        if (tracking) update();
    }

    document.addEventListener('change', event => {
        const input = event.target;
        if (input.matches('.gu-approval input[name="confirm"]')) syncApproval(input.form);
        else if (input.matches('.gu-picker input[name="package_source"]')) {
            input.closest('.gu-package').querySelectorAll('.gu-approval').forEach(form => {
                form.elements.confirm.checked = false;
                syncApproval(form);
            });
        }
    });
    window.addEventListener('hashchange', syncPage);
    window.addEventListener('pageshow', syncPage);
    syncPage();
    document.querySelectorAll('.gu-monitor').forEach(followOperation);
})();
