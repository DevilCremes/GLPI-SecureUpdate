<?php
use Glpi\Plugin\Hooks;
use GlpiPlugin\Secureupdate\Panel;

function plugin_init_secureupdate(): void {
    global $PLUGIN_HOOKS;
    if (Panel::allowed()) {
        $PLUGIN_HOOKS[Hooks::CONFIG_PAGE]['secureupdate'] = 'front/panel.php';
        $PLUGIN_HOOKS[Hooks::DISPLAY_CENTRAL]['secureupdate'] = [Panel::class, 'notice'];
    }
}
function plugin_version_secureupdate(): array {
    return ['name'=>'Atualizações do GLPI', 'version'=>'1.0.7', 'author'=>'SecureUpdate',
        'license'=>'GPL-3.0-or-later','requirements'=>['glpi'=>['min'=>'12.0.0','max'=>'12.1.0']]];
}
