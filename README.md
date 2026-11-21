# GLPI-SecureUpdate
Plugin para GLPI 12.0.x em Linux que reúne atualizações aprovadas pelo Super-Admin, verificação SHA-256 do pacote oficial, healthcheck e rollback. Permite agendar backups locais, em NAS ou na nuvem, gerenciar a retenção e receber resultados por e-mail.

O SecureUpdate é um plugin em português para administrar atualizações e backups do **GLPI 12.0.x em Linux**. No próprio GLPI, o Super-Admin pode consultar o que mudou em uma versão, verificar o pacote oficial e aprovar a instalação. A consulta por novas versões pode ser agendada. **Toda atualização exige aprovação explícita.**

[Baixar a prévia interativa do painel](https://raw.githubusercontent.com/DevilCremes/GLPI-SecureUpdate/main/docs/previa-secureupdate.html)

## Como funciona uma atualização

1. **Revisão da versão.** O painel compara a instalação com as versões estáveis do mesmo ramo e reúne o changelog das versões intermediárias. Quando as notas oficiais indicam gravidade, o painel apresenta a urgência correspondente.
2. **Verificação do pacote.** O Super-Admin escolhe entre baixar o arquivo oficial no servidor ou enviar o `.tgz` do computador. Nos dois casos, o executor confere o tamanho e o SHA-256 com os metadados da release antes de liberar a aprovação.
3. **Manutenção e backup.** Após a aprovação, o serviço coloca o GLPI em manutenção, pausa os serviços de acesso e gravação configurados e copia o banco, a aplicação, os anexos e os diretórios externos declarados. A migração ocorre em uma cópia do banco, preservando a base anterior.
4. **Verificação e reabertura.** O executor compara os diagnósticos de antes e depois da migração e verifica o banco e o sistema de arquivos. Depois de reabrir o acesso, testa o endpoint público `/status.php`.

Se a migração ou o diagnóstico falhar antes da reabertura, o executor tenta restaurar o estado anterior. Depois que o GLPI volta a aceitar gravações, o rollback exige nova aprovação. Antes de reverter, o serviço copia o estado atual. Chamados criados nesse intervalo ficam preservados nessa cópia e podem exigir reconciliação pelo administrador.

## Backups independentes das atualizações

- **Criação e agendamento.** Execute uma cópia sob demanda ou programe uma janela diária, semanal ou mensal, com horário e fuso. É possível escolher, por exemplo, toda segunda-feira, todo dia 7 ou o último dia do mês.
- **Destinos.** Salve no servidor, em outro disco, em um NAS ou na nuvem. O administrador Linux configura os destinos adicionais com rclone. Pelo painel, o Super-Admin escolhe e testa a conexão. Se a transferência falhar, a cópia local fica disponível para reenvio.
- **Catálogo e restauração.** Consulte, baixe, restaure ou exclua cópias. A restauração verifica os hashes, exige a mesma versão e estrutura de diretórios do GLPI e cria um ponto de segurança com os dados em uso antes da troca.
- **Retenção.** Combine quantidade e idade. Por exemplo, preserve as sete cópias mais recentes e todas as dos últimos 30 dias. O painel mostra os candidatos à exclusão. A limpeza automática é opcional e preserva os pontos de rollback das atualizações.

## Notificações e diagnóstico

Os avisos de **novas versões** podem aparecer no painel durante o uso do GLPI ou chegar por e-mail. A consulta aceita um intervalo simples em horas ou uma regra de calendário.

Os **e-mails de backup** têm configuração própria. O Super-Admin pode desativá-los, receber somente erros e avisos ou receber todos os resultados. A mensagem identifica a operação e informa, por exemplo, se a cópia local foi criada, mas o envio ao NAS ficou pendente. Uma falha de e-mail mantém o aviso na fila para nova tentativa.

A aba **Saúde** apresenta os resultados do diagnóstico e as regressões detectadas. A aba **Debug** mostra a etapa em andamento e o identificador que permite localizar o log no servidor. O resumo pode ser exportado em JSON sem incluir credenciais ou conteúdo dos chamados.

## Instalação e compatibilidade

O projeto foi desenvolvido para instalações pelo pacote oficial do GLPI em **um servidor Linux com systemd**, PHP CLI e um banco primário MySQL ou MariaDB. A interface PHP segue o tema do GLPI. Um serviço Python instalado fora da árvore web executa as operações autorizadas. O diagnóstico integrado requer o [GLPI Health Check](https://github.com/JoaoPauloSCosta/glpi-health-check).

O guia do pacote detalha as dependências, as permissões do banco, a configuração das unidades que devem parar durante a manutenção e o procedimento de recuperação pelo terminal. [Guia de instalação e operação](./LEIA-ME.md)

## Validação

Na versão **1.0.7**, passaram **107 testes automatizados**. A suíte cobre aprovação, integridade dos arquivos, restauração, rollback, retenção e novas tentativas de envio de e-mail. Os testes usam arquivos locais e simulam serviços externos. A homologação de ponta a ponta com o GLPI, o banco, o destino remoto e o SMTP da instalação continua necessária.

## Licença

Distribuído sob a GNU General Public License, versão 3 ou posterior (`GPL-3.0-or-later`). [Texto da licença](./COPYING)
