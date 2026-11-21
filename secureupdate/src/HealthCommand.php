<?php
namespace GlpiPlugin\Secureupdate;

use Glpi\Console\AbstractCommand;
use GlpiPlugin\Healthcheck\Runner;
use GlpiPlugin\Healthcheck\RunRepository;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

class HealthCommand extends AbstractCommand {
    protected function configure(): void {
        parent::configure();
        $this->setName('plugins:secureupdate:health');
        $this->setDescription('Executa o diagnóstico completo do GLPI Health Check.');
    }
    protected function execute(InputInterface $input, OutputInterface $output): int {
        if (!\Plugin::isPluginActive('healthcheck') || !class_exists(Runner::class)) {
            throw new \RuntimeException('Instale e ative o plugin GLPI Health Check antes de atualizar.');
        }
        $id = (new Runner())->run(Runner::MODE_FULL, Runner::TRIGGER_MANUAL, 0);
        $row = RunRepository::getRun($id);
        $issues = [];
        foreach (RunRepository::getResults($id) as $key=>$r) { $issues[(string)$key] = (string)$r['status']; }
        if (!$row || !$issues) { throw new \RuntimeException('Diagnóstico incompleto.'); }
        $output->writeln(json_encode(['id'=>$id,'score'=>$row['score'],'checks'=>$issues], JSON_THROW_ON_ERROR));
        return 0;
    }
}
