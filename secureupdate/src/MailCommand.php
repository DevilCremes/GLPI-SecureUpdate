<?php
namespace GlpiPlugin\Secureupdate;

use Glpi\Console\AbstractCommand;
use Symfony\Component\Console\Input\InputArgument;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

class MailCommand extends AbstractCommand {
    protected function configure(): void {
        parent::configure();
        $this->setName('plugins:secureupdate:notify');
        $this->addArgument('version', InputArgument::REQUIRED, 'Versão do GLPI ou backup para um resultado recebido pela entrada padrão.');
        $this->setDescription('Envia aos Super-Admins ativos avisos de versão ou resultados de backups.');
    }

    private function backupMessage(): array {
        $event = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR);
        $levels = ['success'=>'Sucesso', 'warning'=>'Atenção', 'error'=>'Erro'];
        if (!is_array($event) || !isset($levels[$event['level'] ?? ''])
            || !preg_match('/^[0-9a-f]{16}$/D', $event['id'] ?? '')
            || !is_string($event['operation'] ?? null) || !is_string($event['message'] ?? null)
            || !is_array($event['acked'] ?? null)) {
            throw new \InvalidArgumentException('Resultado de backup inválido.');
        }
        $when = (new \DateTimeImmutable($event['time']))->setTimezone(new \DateTimeZone(date_default_timezone_get()));
        $subject = '[GLPI] '.$levels[$event['level']].' · '.$event['operation'];
        $body = $event['message']."\n\nOperação: ".$event['operation']."\nRegistrado em: ".$when->format('d/m/Y H:i:s T')
            ."\nReferência: ".$event['id']."\n\nConfira o resultado em SecureUpdate > Backups.";
        return [$subject, $body, array_fill_keys($event['acked'], true)];
    }

    protected function execute(InputInterface $input, OutputInterface $out): int {
        global $DB;
        $version = $input->getArgument('version');
        $backup = $version === 'backup';
        $acked = [];
        if ($backup) {
            [$subject, $body, $acked] = $this->backupMessage();
        } else {
            if (!preg_match('/^(?:10|11|12)\.0\.(?:0|[1-9][0-9]{0,4})$/D', $version)) { throw new \InvalidArgumentException('Versão inválida.'); }
            $subject = '[GLPI] Versão '.$version.' disponível';
            $body = "A versão ".$version." está disponível. Acesse Atualizações do GLPI com o perfil Super-Admin para revisar o changelog. A instalação exige sua aprovação.";
        }
        $sender = \Config::getEmailSender();
        $seen = [];
        $sent = 0;
        $failed = false;
        $users = $DB->request(['SELECT'=>['users_id'],'FROM'=>'glpi_profiles_users','WHERE'=>['profiles_id'=>4]]);
        foreach ($users as $r) {
            $id = (int)$r['users_id'];
            if (isset($seen[$id])) { continue; }
            $seen[$id] = true;
            $u = new \User();
            if (!$u->getFromDB($id) || !$u->fields['is_active'] || $u->fields['is_deleted']) { continue; }
            $now = date('Y-m-d H:i:s');
            if ((!empty($u->fields['begin_date']) && $u->fields['begin_date'] > $now)
                || (!empty($u->fields['end_date']) && $u->fields['end_date'] < $now)) { continue; }
            $email = $u->getDefaultEmail();
            if (!$email) { continue; }
            $key = 'notified_'.$id;
            if ($backup) {
                if (isset($acked[$id])) { $sent++; continue; }
            } else {
                $state = \Config::getConfigurationValues('plugin:secureupdate', [$key]);
                if (($state[$key] ?? '') === $version) { $sent++; continue; }
            }
            try {
                $mailer = new \GLPIMailer();
                if (version_compare(GLPI_VERSION, '11.0.0', '<')) {
                    if (!$mailer->setFrom($sender['email'], $sender['name'] ?? '') || !$mailer->addAddress($email)) {
                        throw new \RuntimeException('Endereço de e-mail inválido.');
                    }
                    $mailer->isHTML(false);
                    $mailer->Subject = $subject;
                    $mailer->Body = $body;
                } else {
                    $mailer->getEmail()->from($sender['email'])->to($email)->subject($subject)->text($body);
                }
                if (!$mailer->send()) { throw new \RuntimeException('Falha ao enviar aviso.'); }
            } catch (\Throwable $e) {
                $failed = true;
                continue;
            }
            if ($backup) {
                $out->writeln('SU_ACK '.$id);
            } else {
                \Config::setConfigurationValues('plugin:secureupdate', [$key=>$version]);
            }
            $sent++;
        }
        if ($failed) { return 1; }
        if (!$sent) {
            if ($backup) { $out->writeln('SU_TERMINAL no_recipients'); return 2; }
            throw new \RuntimeException('Nenhum Super-Admin ativo com e-mail configurado.');
        }
        if ($backup) { $out->writeln('SU_DONE'); }
        return 0;
    }
}
