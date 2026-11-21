<?php
use GlpiPlugin\Secureupdate\Panel;
use Symfony\Component\HttpFoundation\RedirectResponse;
use Symfony\Component\HttpFoundation\Response;

if (!defined('GLPI_ROOT') || !Panel::allowed() || !Plugin::isPluginActive('secureupdate')) {
    return new Response('Acesso restrito ao perfil Super-Admin.', 403);
}
global $CFG_GLPI;
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (empty($_POST) && !empty($_SERVER['CONTENT_LENGTH'])) {
        return new Response('O envio chegou sem formulário. Confira o limite post_max_size do PHP.', 413);
    }
    $token = $_POST['approval_token'] ?? null;
    if (!is_string($token) || !isset($_SESSION['secureupdate_token']) || !hash_equals($_SESSION['secureupdate_token'], $token)) {
        return new Response('O formulário expirou. Reabra o painel.', 403);
    }
    if (($_POST['op'] ?? '') !== 'bfetch') { unset($_SESSION['secureupdate_token']); }
    $upload = null;
    try {
        $op = $_POST['op'] ?? '';
        $backupOps = ['backup','bsave','btest','bdelete','brestore','bfetch','bprune','bretry'];
        if (!in_array($op, ['check','settings','download','upload','apply','rollback', ...$backupOps], true)) { throw new RuntimeException('Operação inválida.'); }
        $req = ['op'=>$op, 'actor'=>(int)Session::getLoginUserID()];
        if ($op === 'bfetch') {
            return Panel::backupDownload((string)($_POST['ref'] ?? ''), $req['actor']);
        }
        if ($op === 'bsave') {
            $rule = null;
            if (isset($_POST['enabled'])) {
                $rule = ['kind'=>$_POST['kind'] ?? '', 'at'=>$_POST['at'] ?? '', 'zone'=>$_POST['zone'] ?? ''];
                if ($rule['kind'] === 'weekly') { $rule['weekday'] = filter_var($_POST['weekday'] ?? null,FILTER_VALIDATE_INT); }
                if ($rule['kind'] === 'monthly') { $rule['day'] = filter_var($_POST['day'] ?? null,FILTER_VALIDATE_INT); }
            }
            $req['options'] = ['destination'=>$_POST['destination'] ?? '', 'rule'=>$rule,
                'keep'=>filter_var($_POST['keep'] ?? null,FILTER_VALIDATE_INT),
                'days'=>filter_var($_POST['days'] ?? null,FILTER_VALIDATE_INT),'auto'=>isset($_POST['auto'])];
            $req['confirm'] = ($_POST['confirm'] ?? '') === 'yes';
        } elseif (in_array($op, ['btest','bdelete','brestore','bprune','bretry'], true)) {
            $req['ref'] = (string)($_POST['ref'] ?? '');
            if (in_array($op,['bdelete','brestore'], true)) {
                $req['confirm'] = $req['ref'] !== '' && ($_POST['typed'] ?? '') === $req['ref'];
            } elseif ($op === 'bprune') { $req['confirm'] = ($_POST['confirm'] ?? '') === 'yes'; }
        } elseif ($op === 'backup') {
            if (($_POST['confirm'] ?? '') !== 'yes') { throw new RuntimeException('Confirme a janela de manutenção.'); }
            $req['confirm'] = true;
        } elseif ($op === 'settings') {
            $req += ['browser'=>isset($_POST['browser']),'email'=>isset($_POST['email'])];
            if (isset($_POST['backup_email'])) { $req['backup_email'] = $_POST['backup_email']; }
            $mode = $_POST['mode'] ?? 'simple';
            if ($mode === 'simple') {
                $req['hours'] = filter_var($_POST['hours'] ?? null, FILTER_VALIDATE_INT);
            } elseif ($mode === 'advanced') {
                $kind = $_POST['kind'] ?? '';
                $rule = ['kind'=>$kind, 'at'=>$_POST['at'] ?? '', 'zone'=>$_POST['zone'] ?? ''];
                if ($kind === 'weekly') { $rule['weekday'] = filter_var($_POST['weekday'] ?? null, FILTER_VALIDATE_INT); }
                if ($kind === 'monthly') { $rule['day'] = filter_var($_POST['day'] ?? null, FILTER_VALIDATE_INT); }
                $req['schedule'] = $rule;
            } else { throw new RuntimeException('Modo de agendamento inválido.'); }
        } elseif ($op === 'download' || $op === 'upload') {
            $req['ref'] = (string)($_POST['ref'] ?? '');
            if ($op === 'upload') {
                $file = $_FILES['package'] ?? [];
                if (($file['error'] ?? null) !== UPLOAD_ERR_OK
                    || !is_string($file['tmp_name'] ?? null) || !is_uploaded_file($file['tmp_name'])) {
                    throw new RuntimeException('Escolha o .tgz oficial. Confira também o limite de upload do PHP.');
                }
                $size = filesize($file['tmp_name']);
                if ($size === false || $size < 1 || $size > 536870912) {
                    throw new RuntimeException('O arquivo precisa ter entre 1 byte e 512 MiB.');
                }
                $upload = fopen($file['tmp_name'], 'rb');
                if ($upload === false) { throw new RuntimeException('Não foi possível ler o upload.'); }
                $req['size'] = $size;
            }
        } elseif ($op === 'apply' || $op === 'rollback') {
            if (($_POST['confirm'] ?? '') !== 'yes') { throw new RuntimeException('Confirme a operação selecionada.'); }
            $req['ref'] = (string)($_POST['ref'] ?? '');
        }
        Panel::call($req, $upload);
        $_SESSION['secureupdate_msg'] = 'Solicitação recebida.';
    } catch (Throwable $e) { $_SESSION['secureupdate_msg'] = $e->getMessage(); }
    finally { if (is_resource($upload)) { fclose($upload); } }
    $query = ($op ?? '') === 'settings' ? '?aba=notificacoes' : '';
    if (in_array($op ?? '', $backupOps ?? [], true)) { $query = '?aba=backups'; }
    if (in_array($op ?? '', ['download','upload'], true)) { $query = '?origem='.$op; }
    return new RedirectResponse(Panel::url().$query, 303);
}
if ($_SERVER['REQUEST_METHOD'] !== 'GET') { return new Response('', 405); }
$tab = $_GET['aba'] ?? 'atualizacao';
if ($tab === 'avisos') { $tab = 'notificacoes'; }
$tabs = ['atualizacao'=>'Atualização','saude'=>'Saúde','backups'=>'Backups','notificacoes'=>'Notificações','debug'=>'Debug'];
if (!is_string($tab) || !isset($tabs[$tab])) { return new Response('Seção desconhecida.',404); }
if ($tab === 'debug' && isset($_GET['exportar'])) {
    try { $report = Panel::call(['op'=>'debug']); }
    catch (Throwable $e) { return new Response('Diagnóstico indisponível.',503,['Cache-Control'=>'no-store']); }
    return new Response(json_encode($report, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR),
        200, ['Content-Type'=>'application/json; charset=utf-8','Content-Disposition'=>'attachment; filename="glpi-debug.json"','Cache-Control'=>'no-store','X-Content-Type-Options'=>'nosniff']);
}
$s = [];
$debug = [];
$backups = [];
$vault = [];
$offset = max(0, min(100000000, (int)($_GET['offset'] ?? 0)));
$error = '';
try {
    $s = Panel::call(['op'=>'status']);
    if ($tab === 'backups') { $vault = Panel::call(['op'=>'backups','offset'=>$offset]); $backups = $vault['items']; }
    if ($tab === 'debug') { $debug = Panel::call(['op'=>'debug']); }
} catch (Throwable $e) { $error = 'Não foi possível consultar o executor neste servidor.'; }
$plan = $s['plan'] ?? [];
$busy = !empty($s['mail_busy']) || !empty($s['pending']) || in_array($s['phase'] ?? '', ['verificando','validando pacote','atualizando','revertendo','criando backup','gerenciando backups'], true);
$_SESSION['secureupdate_token'] ??= bin2hex(random_bytes(32));
$msg = $_SESSION['secureupdate_msg'] ?? '';
unset($_SESSION['secureupdate_msg']);
$prepared = $s['prepared'] ?? [];
if (($prepared['actor'] ?? 0) !== (int)Session::getLoginUserID()) { $prepared = []; }
$canPrepare = !$busy && !empty($plan['id']) && ($plan['source'] ?? '') === GLPI_VERSION
    && time() >= ($plan['checked'] ?? 0) && time() - ($plan['checked'] ?? 0) < 86400;
$rule = $s['schedule'] ?? [];
$zoneName = $rule['zone'] ?? date_default_timezone_get();
$nextCheck = '';
if ($tab === 'notificacoes' && !empty($s['next'])) {
    $nextCheck = (new DateTimeImmutable('@'.(int)$s['next']))
        ->setTimezone(new DateTimeZone($zoneName))->format('d/m/Y H:i');
}
$nav = [];
foreach ($tabs as $id=>$label) { $nav[] = ['id'=>$id,'label'=>$label,'url'=>Panel::url().'?aba='.$id]; }
Html::header('Atualizações do GLPI', Panel::url(), 'config', 'plugins');
\Glpi\Application\View\TemplateRenderer::getInstance()->display('@secureupdate/panel.html.twig', [
    'tab'=>$tab,'nav'=>$nav,'panel_url'=>Panel::url(),'css_url'=>$CFG_GLPI['root_doc'].'/plugins/secureupdate/css/panel.css',
    'js_url'=>$CFG_GLPI['root_doc'].'/plugins/secureupdate/js/panel.js',
    'selected_source'=>$_GET['origem'] ?? ($prepared['source'] ?? 'download'),
    'installed'=>GLPI_VERSION,'phase'=>!empty($s['mail_busy']) ? 'enviando e-mail de backup' : (($s['step'] ?? '') ?: ($s['phase'] ?? '')),'error'=>$error ?: ($s['error'] ?? ''),'msg'=>$msg,
    'connected'=>(bool)$s,'available'=>!empty($plan['id']),'target'=>$plan['version'] ?? '',
    'urgency'=>$plan['urgency'] ?? '', 'notes'=>$plan['notes'] ?? [],'busy'=>$busy,
    'can_prepare'=>$canPrepare,'prepared'=>$prepared,'asset_url'=>$plan['url'] ?? '',
    'can_apply'=>$canPrepare && !empty($prepared['id']),
    'ref'=>$plan['id'] ?? '', 'token'=>$_SESSION['secureupdate_token'],'restore'=>$s['restore'] ?? '',
    'checked'=>empty($plan['checked']) ? '' : date('d/m/Y H:i T',(int)$plan['checked']),
    'backup_time'=>empty($s['backup_time']) ? '' : date('d/m/Y H:i T',(int)$s['backup_time']),
    'backups'=>array_map(static fn($b)=>array_merge($b,['date'=>date('d/m/Y H:i T',(int)$b['time']),
        'size'=>number_format($b['size']/1048576,1,',','.') . ' MiB']),$backups),
    'backup_kinds'=>[['id'=>'daily','label'=>'Diariamente'],['id'=>'weekly','label'=>'Semanalmente'],['id'=>'monthly','label'=>'Dia do mês'],['id'=>'first','label'=>'Primeiro dia do mês'],['id'=>'last','label'=>'Último dia do mês']],
    'backup_result'=>$s['backup_result'] ?? [],
    'backup_actions'=>[['id'=>'bfetch','label'=>'Baixar arquivo TAR'],['id'=>'bretry','label'=>'Reenviar ao destino']],
    'vault'=>$vault,'policy'=>$vault['policy'] ?? [],'brule'=>$vault['policy']['rule'] ?? [],
    'backup_zone'=>$vault['policy']['rule']['zone'] ?? date_default_timezone_get(),
    'backup_next'=>empty($vault['policy']['next']) ? '' : (new DateTimeImmutable('@'.(int)$vault['policy']['next']))
        ->setTimezone(new DateTimeZone($vault['policy']['rule']['zone']))->format('d/m/Y H:i T'),
    'offset'=>$offset,'prev_offset'=>max(0,$offset-25),'next_offset'=>$offset+25,
    'health_rows'=>array_map(static fn($key,$value)=>['name'=>$key,'value'=>$value],array_keys($s['health'] ?? []),array_values($s['health'] ?? [])),
    'debug_rows'=>array_map(static fn($key,$value)=>['name'=>$key,'value'=>$value],array_keys($debug),array_values($debug)),
    'advanced'=>!empty($rule),'kind'=>$rule['kind'] ?? 'weekly','at'=>$rule['at'] ?? '09:00',
    'weekday'=>$rule['weekday'] ?? 1,'day'=>$rule['day'] ?? 7,'zone'=>$zoneName,'next_check'=>$nextCheck,
    'zones'=>in_array($tab,['notificacoes','backups'],true) ? array_unique([$zoneName, ...DateTimeZone::listIdentifiers()]) : [],
    'weekdays'=>['Segunda-feira','Terça-feira','Quarta-feira','Quinta-feira','Sexta-feira','Sábado','Domingo'],
    'month_days'=>range(1,31),'hour_options'=>range(1,168),
    'backup_email'=>$s['backup_email'] ?? 'off','backup_mail_failed'=>$s['backup_mail_failed'] ?? false,
    'backup_mail_pending'=>$s['backup_mail_pending'] ?? 0,
    'hours'=>$s['hours'] ?? 24,'browser'=>$s['browser'] ?? false,'email'=>$s['email'] ?? false,
    'export_url'=>Panel::url().'?aba=debug&exportar=1'
]);
Html::footer();
