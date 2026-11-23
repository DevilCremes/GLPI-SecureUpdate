<?php
use GlpiPlugin\Secureupdate\Panel;

function plugin_init_secureupdate(): void {
    global $PLUGIN_HOOKS;
    if (version_compare(GLPI_VERSION, '11.0.0', '<')) {
        $PLUGIN_HOOKS['csrf_compliant']['secureupdate'] = true;
    }
    if (Panel::allowed()) {
        $PLUGIN_HOOKS['config_page']['secureupdate'] = 'front/panel.php';
        $PLUGIN_HOOKS['display_central']['secureupdate'] = [Panel::class, 'notice'];
    }
}
function plugin_version_secureupdate(): array {
    return ['name'=>'Atualizações do GLPI', 'version'=>'1.2.0', 'author'=>'SecureUpdate',
        'license'=>'GPL-3.0-or-later','requirements'=>['glpi'=>['min'=>'10.0.0','max'=>'12.1.0']]];
}
