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
})();
