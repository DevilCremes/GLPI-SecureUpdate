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
echo "Testes PHP do painel aprovados.\n";
