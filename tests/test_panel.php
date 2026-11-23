<?php
// Executável com php -n tests/test_panel.php, sem banco ou sessão do GLPI.
require dirname(__DIR__) . '/secureupdate/src/Panel.php';
class Session {
    public static int $id = 0;
    public static function getLoginUserID(): int { return self::$id; }
}
class Plugin {
    public static function isPluginActive(string $name): bool { return true; }
}
function check(bool $result, string $message): void {
    if (!$result) { throw new RuntimeException($message); }
}
use GlpiPlugin\Secureupdate\Panel;
$_SESSION = [];
check(!Panel::allowed(),'Sessão anônima');
Session::$id = 10;
$_SESSION['glpiactiveprofile'] = ['id'=>3,'name'=>'Super-Admin'];
check(!Panel::allowed(),'Perfil diferente com o mesmo nome');
$_SESSION['glpiactiveprofile'] = ['id'=>4,'name'=>'Administrador global'];
check(Panel::allowed(),'Super-Admin ativo renomeado');
$_SESSION['glpiactiveprofile']['id'] = 2;
check(!Panel::allowed(),'Troca de perfil');
foreach (['04', '4x', true, 4.0, []] as $bad) {
    $_SESSION['glpiactiveprofile']['id'] = $bad;
    check(!Panel::allowed(), 'Formato de perfil inválido');
}
$_SESSION['glpiactiveprofile']['id'] = '4';
check(Panel::allowed(), 'Identificador do banco como texto');
Session::$id = 0;
check(!Panel::allowed(), 'Perfil residual em sessão encerrada');
check(Panel::escape('<script>') === '&lt;script&gt;', 'Escape de conteúdo externo');
// GLPI 10 routes directly to PHP, without Symfony HttpFoundation.
define('GLPI_VERSION', '10.0.28');
require dirname(__DIR__) . '/secureupdate/setup.php';
plugin_init_secureupdate();
check($PLUGIN_HOOKS['csrf_compliant']['secureupdate'] === true, 'CSRF nativo no GLPI 10');
$range = plugin_version_secureupdate()['requirements']['glpi'];
foreach (['10.0.28','11.0.13','12.0.2'] as $version) {
    check(version_compare($version, $range['min'], '>=') && version_compare($version, $range['max'], '<'), 'Ramo suportado');
}
ob_start();
Panel::response('Perfil restrito', 403);
check(ob_get_clean() === 'Perfil restrito' && http_response_code() === 403, 'Resposta HTTP no GLPI 10');
ob_start();
Panel::response(static function () { echo 'TAR'; }, 200);
check(ob_get_clean() === 'TAR' && http_response_code() === 200, 'Streaming no GLPI 10');
echo "Testes PHP do painel aprovados.\n";
