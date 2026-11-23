<?php
namespace GlpiPlugin\Secureupdate;

use Glpi\Console\AbstractCommand;
use GlpiPlugin\Healthcheck\Runner;
use GlpiPlugin\Healthcheck\RunRepository;
use Symfony\Component\Console\Input\InputArgument;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

class HealthCommand extends AbstractCommand {
    protected function configure(): void {
        parent::configure();
        $this->setName('plugins:secureupdate:health');
        $this->addArgument('target', InputArgument::OPTIONAL, 'Versão principal que será instalada.');
        $this->setDescription('Executa o diagnóstico completo do GLPI Health Check.');
    }
    protected function execute(InputInterface $input, OutputInterface $output): int {
        if (!\Plugin::isPluginActive('healthcheck') || !class_exists(Runner::class)) {
            throw new \RuntimeException('Instale e ative o plugin GLPI Health Check antes de atualizar.');
        }
        if (!\Update::isDbUpToDate()) {
            throw new \RuntimeException('O esquema do banco não corresponde ao núcleo instalado.');
        }
        $plugins = array_values(\Plugin::getPlugins());
        $target = $input->getArgument('target');
        if ($target !== null) {
            foreach ($plugins as $plugin) {
                $bounds = \Plugin::getInfo($plugin)['requirements']['glpi'] ?? [];
                if ((isset($bounds['max']) && version_compare($target, $bounds['max'], '>='))
                    || (isset($bounds['min']) && version_compare($target, $bounds['min'], '<'))) {
                    throw new \RuntimeException("Compatibilidade com GLPI $target não confirmada para o plugin $plugin.");
                }
            }
        }
        $id = (new Runner())->run(Runner::MODE_FULL, Runner::TRIGGER_MANUAL, 0);
        $row = RunRepository::getRun($id);
        $issues = [];
        foreach (RunRepository::getResults($id) as $key=>$r) { $issues[(string)$key] = (string)$r['status']; }
        if (!$row || !$issues) { throw new \RuntimeException('Diagnóstico incompleto.'); }
        $output->writeln(json_encode(['id'=>$id,'plugins'=>$plugins,'schema_current'=>true,'score'=>$row['score'],'checks'=>$issues], JSON_THROW_ON_ERROR));
        return 0;
    }
}
