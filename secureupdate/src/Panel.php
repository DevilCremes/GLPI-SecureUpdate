<?php
namespace GlpiPlugin\Secureupdate;

use Plugin;
use Session;

final class Panel {
    public static function allowed(): bool {
        $user = filter_var(Session::getLoginUserID(), FILTER_VALIDATE_INT, ['options'=>['min_range'=>1]]);
        return $user !== false && in_array($_SESSION['glpiactiveprofile']['id'] ?? null, [4, '4'], true);
    }
    public static function escape($s): string {
        return htmlspecialchars((string)$s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    }
    public static function url(): string {
        global $CFG_GLPI;
        return $CFG_GLPI['root_doc'] . '/plugins/secureupdate/front/panel.php';
    }
    public static function call(array $request, $upload = null): array {
        $fp = @stream_socket_client('unix:///run/glpi-secureupdate/control.sock', $errno, $errstr, 2);
        if (!$fp) { throw new \RuntimeException('Serviço local de atualização indisponível.'); }
        try {
            stream_set_timeout($fp, 4);
            $line = json_encode($request, JSON_THROW_ON_ERROR) . "\n";
            if (fwrite($fp, $line) !== strlen($line)) { throw new \RuntimeException('Envio incompleto.'); }
            $data = fgets($fp, 4194304);
            if ($data === false) { throw new \RuntimeException('Sem resposta do serviço.'); }
            $r = json_decode($data, true, 32, JSON_THROW_ON_ERROR);
            if (empty($r['ok'])) { throw new \RuntimeException($r['error'] ?? 'Falha na operação.'); }
            if ($upload !== null) {
                stream_set_timeout($fp, 90);
                $sent = stream_copy_to_stream($upload, $fp, $request['size']);
                if ($sent !== $request['size'] || !stream_socket_shutdown($fp, STREAM_SHUT_WR)) {
                    throw new \RuntimeException('Transferência interrompida. Consulte o estado da verificação.');
                }
            }
            return $r['data'];
        } finally { fclose($fp); }
    }
    public static function response($body, int $status, array $headers = []) {
        if (version_compare(GLPI_VERSION, '11.0.0', '>=')) {
            if ($body instanceof \Closure) {
                return new \Symfony\Component\HttpFoundation\StreamedResponse($body, $status, $headers);
            }
            return new \Symfony\Component\HttpFoundation\Response($body, $status, $headers);
        }
        http_response_code($status);
        foreach ($headers as $name => $value) { header($name . ': ' . $value); }
        if ($body instanceof \Closure) { $body(); } else { echo $body; }
        return null;
    }
    public static function backupDownload(string $id, int $actor) {
        if (!preg_match('/^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/D', $id)) {
            throw new \RuntimeException('Identificador inválido.');
        }
        $fp = @stream_socket_client('unix:///run/glpi-secureupdate/control.sock', $errno, $errstr, 3);
        if (!$fp) { throw new \RuntimeException('Serviço de backups indisponível.'); }
        try {
            stream_set_timeout($fp, 900);
            $request = json_encode(['op'=>'bfetch','actor'=>$actor,'ref'=>$id], JSON_THROW_ON_ERROR)."\n";
            if (fwrite($fp,$request) !== strlen($request)) { throw new \RuntimeException('Envio incompleto.'); }
            $header = json_decode(fgets($fp,4096) ?: '{}', true, 8, JSON_THROW_ON_ERROR);
            if (empty($header['ok']) || !is_int($header['size'] ?? null) || $header['size'] <= 0) {
                throw new \RuntimeException('Download indisponível. Consulte o trabalho no servidor.');
            }
            return self::response(static function () use ($fp) {
                try { fpassthru($fp); } finally { fclose($fp); }
            }, 200, ['Content-Type'=>'application/x-tar','Cache-Control'=>'private, no-store',
                     'Content-Length'=>(string)$header['size'],'X-Content-Type-Options'=>'nosniff',
                     'Content-Disposition'=>'attachment; filename="glpi-backup-'.$id.'.tar"']);
        } catch (\Throwable $error) { fclose($fp); throw $error; }
    }

    public static function notice(): void {
        if (!self::allowed() || !Plugin::isPluginActive('secureupdate')) { return; }
        try { $s = self::call(['op'=>'notice']); } catch (\Throwable $e) { return; }
        if (empty($s['browser']) || empty($s['plan']['id'])) { return; }
        echo '<tr><td><div class="alert alert-info">GLPI ' . self::escape($s['plan']['version'])
            . ' disponível. Urgência: ' . self::escape($s['plan']['urgency'])
            . ' · <a href="' . self::escape(self::url()) . '">Revisar atualização</a></div></td></tr>';
    }
}
