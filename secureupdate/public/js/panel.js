(() => {
    function syncApproval(form) {
        form.querySelector('button.btn-primary').disabled = !form.elements.confirm.checked;
    }
    function syncPage() {
        document.querySelectorAll('.gu-approval').forEach(syncApproval);
    }
    document.addEventListener('change', event => {
        const input = event.target;
        if (input.matches('.gu-approval input[name="confirm"]')) {
            syncApproval(input.form);
        } else if (input.matches('.gu-picker input[name="package_source"]')) {
            input.closest('.gu-package').querySelectorAll('.gu-approval').forEach(form => {
                form.elements.confirm.checked = false;
                syncApproval(form);
            });
        }
    });
    window.addEventListener('pageshow', syncPage);
    syncPage();
})();
