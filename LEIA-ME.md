# Atualizações do GLPI 1.0.8

Plugin em português para instalações Linux pelo pacote oficial .tgz do ramo GLPI 12.0.x em um único servidor com systemd e uma conexão primária MySQL ou MariaDB. O Super-Admin revisa a versão, o changelog e a urgência antes de aprovar a instalação. O serviço consulta versões diariamente por padrão. Na aba Notificações, o modo Simples permite escolher de 1 a 168 horas. O modo Avançado usa o horário e o fuso escolhidos para consultar diariamente, em um dia da semana, no primeiro ou último dia de cada mês ou em um dia do mês de 1 a 31. O painel e o e-mail podem ser habilitados de forma independente.

## Antes de instalar

- Instale e ative o [GLPI Health Check](https://github.com/JoaoPauloSCosta/glpi-health-check) compatível com GLPI 12 e com as APIs Runner e RunRepository da série 1.1. Execute um diagnóstico completo em homologação. O Health Check é um projeto de terceiros de **João Paulo S. Costa**, distribuído separadamente sob a licença GPLv3 ou posterior. O plugin do autor gera a pontuação e o histórico dos diagnósticos. O SecureUpdate usa esses resultados para detectar regressões e controlar a reabertura do GLPI. [Créditos e detalhes da integração](./README.md#créditos-ao-glpi-health-check)
- Use uma conta Unix dedicada ao PHP deste GLPI. O serviço identifica essa conta pelo socket Unix. Sites que compartilham o mesmo usuário PHP compartilham essa fronteira de confiança. O serviço confia na instalação GLPI e nos plugins que ela executa.
- Identifique a raiz da aplicação, a URL real de /status.php, todos os diretórios de dados externos e todas as unidades systemd que possam atender requisições ou gravar dados. Inclua o pool dedicado de PHP/web, timers, workers e integrações. Um cron global precisa ser separado em uma unidade dedicada ou suspenso externamente durante a janela. Um proxy pode servir uma página estática de manutenção enquanto o PHP estiver parado.
- A raiz da aplicação, o pai, o diretório plugins usado pelo instalador e o diretório da configuração de banco precisam ser protegidos contra escrita por processos web, com dono root. Preserve as permissões de leitura do PHP e de escrita dos diretórios de dados do GLPI. Confira os caminhos com namei -l. Não aplique mudanças recursivas de proprietário na instalação.
- O pacote aceita até 500.000 entradas e 4 GiB descompactados. São necessários PHP CLI compatível com o GLPI, Python 3.9+ com tarfile.data_filter, systemd, GNU tar e coreutils, rsync, curl e o par mariadb/mariadb-dump ou mysql/mysqldump. O servidor deve acessar a API e os downloads HTTPS públicos do GitHub. A escolha da versão exclui pré-lançamentos e permanece no ramo 12.0.
- Separe espaço para dump SQL, arquivo completo da instalação, dados externos, nova árvore e segunda base. O serviço estima o espaço de arquivos antes de prosseguir. Confirme também o espaço no servidor SQL. Mantenha uma cópia de segurança fora do servidor.
- O banco deve utilizar a configuração literal dbdefault gerada pelo GLPI. Réplicas, clusters, volumes montados dentro da árvore da aplicação e bancos com views, triggers, eventos ou rotinas personalizados requerem outro procedimento. Diretórios externos reais podem ser declarados com --externo.

## Como o banco muda

A migração roda **em uma cópia do banco**. Cada atualização cria um esquema vazio chamado gsu_ seguido de 16 dígitos hexadecimais, importa o dump, confere a versão e altera a conexão da nova árvore para essa cópia. O banco anterior fica preservado em manutenção. Configure monitoramento, backups e integrações para acompanhar o nome ativo em config_db.php. Integrações com o nome antigo fixado precisam ser ajustadas antes de liberar o uso.

O DBA deve permitir que a conta GLPI crie e migre somente os esquemas desse prefixo, além dos privilégios habituais no banco atual. Exemplo para uma conta e um host fictícios, a adaptar no MySQL/MariaDB conforme as regras de privilégios do servidor:

```sql
GRANT ALL PRIVILEGES ON `gsu\_%`.* TO 'glpi'@'localhost';
```

Teste a permissão em homologação. O serviço rejeita colisões e nunca remove um banco existente. Cada backup, banco anterior e árvore antiga é mantido até a limpeza pelo administrador. Preserve o último ponto de rollback, as dependências registradas em last.json e qualquer transação pendente. Backups trazem chaves, anexos e dados pessoais; o diretório de estado é privado de root.

## Instalação

Extraia o pacote em uma pasta administrativa **fora da árvore web** e entre na pasta extraída. O exemplo pressupõe um pool PHP dedicado ao GLPI e um timer dedicado aos trabalhos agendados. Substitua os caminhos, o usuário, a URL e os nomes das unidades pelos reais. Passe as unidades na ordem em que devem parar; o serviço as retoma na ordem inversa. Inclua também o serviço que o timer dispara se ele puder estar em execução. Acrescente --externo /etc/glpi e --externo /var/lib/glpi se esses forem os caminhos reais, além de outros diretórios externos usados, como /var/log/glpi.

```bash
sudo python3 instalar.py \
  --glpi /var/www/glpi \
  --usuario-php glpi-web \
  --url-status https://glpi.exemplo.com/status.php \
  --parar glpi-cron.timer \
  --parar glpi-cron.service \
  --parar php-fpm-glpi.service
```

O instalador verifica a sintaxe dos PHPs com o binário indicado por --php, copia o painel para plugins/secureupdate e instala o executor fora do site. A configuração do host fica em /etc/glpi-secureupdate/config.json. Os caminhos autorizados para escrita também constam da unidade systemd. Ajuste ambos ao mudar caminhos.

1. Em **Configurar > Plugins**, instale e ative **Atualizações do GLPI**. Abra a configuração do plugin com o perfil nativo Super-Admin ativo (ID 4 em instalações oficiais). A troca para outro perfil bloqueia acesso e aprovação.
2. No servidor, execute o diagnóstico inicial. Esse comando executa verificações e grava um diagnóstico no histórico do GLPI Health Check.

```bash
sudo python3 /usr/local/lib/glpi-secureupdate/agent.py doctor
```

3. Escolha o intervalo e os canais de aviso. A notificação no navegador aparece na página inicial ao usar o GLPI. O e-mail usa o remetente e o transporte já configurados no GLPI e o e-mail principal dos usuários ativos com o perfil nativo Super-Admin. Cada destinatário é marcado após envio aceito. Em caso de falha de SMTP, a próxima consulta tenta de novo. Uma interrupção entre o envio e o registro pode repetir o aviso.
   Ao salvar, confira a próxima consulta agendada. Se o mês não tiver o dia escolhido ou uma mudança de fuso eliminar o horário, a ocorrência é pulada. Um horário repetido ocorre uma vez. Após uma indisponibilidade, o serviço realiza uma única consulta atrasada e retoma o calendário. No modo Avançado, a consulta manual atualiza o painel e o envio de e-mail aguarda a consulta agendada. As preferências antigas em horas continuam no modo Simples. Referência de interface: [chamados recorrentes do GLPI](https://help.glpi-project.org/documentation/modules/assistance/recurrentticket).
4. Clique em **Consultar versões** ou aguarde o intervalo. Abra as notas de cada versão intermediária. A urgência usa a maior gravidade explicitamente marcada nas notas oficiais. Consulte os avisos oficiais quando a gravidade vier sem classificação.
5. Na aba **Atualização**, escolha **Baixar pelo servidor** e clique em **Baixar e verificar pacote**, ou selecione **Enviar do computador**. Neste segundo caminho, use o link da versão oficial, selecione o .tgz e clique em **Enviar e verificar arquivo**. O servidor consulta os metadados oficiais nas duas opções. Para upload, confira o espaço temporário do PHP, os limites do proxy, `upload_max_filesize` e `post_max_size` (com espaço para o formulário). O executor aceita até 512 MiB compactados.
6. Clique em **Atualizar estado** para ver o resultado. A preparação confere o tamanho e o SHA-256, sem interromper o GLPI. Ao concluir, o painel apresenta versão, origem, tamanho e resumo SHA-256. Cada nova preparação invalida a anterior. O mesmo Super-Admin pode aprovar enquanto a consulta e a verificação tiverem menos de 24 horas.
7. Durante a janela de manutenção, marque a autorização e clique em **Aprovar atualização**. O serviço revalida a release e verifica novamente os bytes já guardados no servidor antes de entrar em manutenção. Se houver falha antes da reabertura, essa autorização cobre o rollback. Após a reabertura, uma reversão exige nova aprovação.

## Atualizar o próprio SecureUpdate das versões 1.0.5 a 1.0.7 para 1.0.8

O instalador do pacote faz a instalação inicial e recusa sobrescrever uma instalação existente. Para atualizar o plugin já instalado, extraia a nova versão fora do site. Em homologação, confira os PHPs com o mesmo PHP CLI da instalação e execute os testes.

1. Abra o painel e confirme que o executor está ocioso e que não há transação pendente. Reserve uma janela sem agendamentos e sem outras aprovações. Desative o SecureUpdate no GLPI e pare a unidade glpi-secureupdate. Mantenha o GLPI Health Check instalado.
2. Guarde as versões atuais das duas pastas de código em um diretório privado fora da árvore web. Prepare a nova pasta `secureupdate/` para o caminho `<GLPI>/plugins/secureupdate/` e `service/` para `/usr/local/lib/glpi-secureupdate/`. Use arquivos de código de propriedade de root, diretórios 0755 e arquivos 0644. Troque as duas pastas na mesma janela. Preserve integralmente `/etc/glpi-secureupdate/`, `/var/lib/glpi-secureupdate/` e a configuração existente da unidade.
3. Inicie a unidade, conclua a atualização do plugin em **Configurar > Plugins** se o GLPI solicitar e reative-o. Confira a versão do executor em **Debug**, abra o catálogo de Backups e teste o destino. Em caso de falha durante essa troca de código, pare o serviço, reponha as duas pastas de código guardadas e reinicie-o antes de retomar os agendamentos.

## E-mail sobre o resultado dos backups

Em **Notificações → E-mail sobre backups**, escolha **Somente erros e avisos** ou **Todos os resultados, incluindo sucessos** e clique em **Salvar preferências**. O padrão é **Desativado**, inclusive ao atualizar uma instalação anterior. Essa escolha é independente do e-mail de novas versões e do seu calendário de consultas.

Os Super-Admins ativos com e-mail principal cadastrado recebem o tipo de operação, o resultado, a data e a referência do trabalho. A cobertura inclui backups manuais e agendados, operações do catálogo, cópias anteriores à atualização ou ao rollback, janela de backup perdida, envio ao destino pendente e falha de limpeza após uma cópia válida. O aviso de um ponto de segurança se refere à cópia, mesmo se uma etapa posterior da atualização falhar. Um trabalho do catálogo interrompido é informado como resultado a verificar. O conteúdo do backup, as credenciais e os logs ficam fora do e-mail.

A aba mostra quantos avisos estão pendentes. O executor usa o remetente e o transporte configurados no GLPI e retenta falhas de entrega após 15 minutos, quando estiver disponível. Reiniciar o serviço preserva a fila. Destinatários já confirmados em uma tentativa parcial são pulados na próxima. Uma interrupção entre o aceite do transporte e a gravação da confirmação pode repetir um e-mail. O aceite do transporte não confirma a chegada à caixa de entrada. A troca para **Somente erros e avisos** descarta sucessos ainda na fila. **Desativado** cancela a fila. Um envio já iniciado pode terminar. O resultado do backup permanece disponível no catálogo mesmo se o SMTP estiver fora do ar.

## Backups, agendamento e retenção

O Super-Admin abre **Backups**, escolhe o destino, confirma a política e clica em **Salvar política**. **Testar conexão** verifica escrita, leitura e remoção de um arquivo temporário. **Criar backup agora** pede confirmação da janela de manutenção. Para uma cópia recorrente, ative o agendamento e escolha frequência diária, semanal, dia fixo, primeiro ou último dia do mês, horário e fuso. Uma ocorrência com atraso de dez minutos ou mais é pulada para evitar interromper o atendimento fora da janela. A autorização do responsável é conferida novamente no banco a cada execução. Agendar backups só autoriza essas cópias.

A cópia reúne banco, instalação, configurações, anexos e diretórios externos declarados. A coleta ocorre em manutenção. O serviço retoma o estado anterior antes de enviar a cópia ao destino escolhido. O catálogo fica em `/var/lib/glpi-secureupdate/backups/manuais/<id>/`. Ele inclui cópias manuais e agendadas. O ponto de rollback gerado durante uma **atualização** continua em `/var/lib/glpi-secureupdate/backups/`, em diretório próprio.

| Destino | Preparação no Linux |
| --- | --- |
| Neste servidor | Disponível ao instalar. O conteúdo permanece junto ao catálogo privado de root. |
| Outro diretório local | Instale rclone. Crie um diretório exclusivo, fora do GLPI e do estado do serviço, de propriedade de root e modo 0700. |
| NAS | Instale rclone e configure um perfil para o compartilhamento SMB ou SFTP, limitado à pasta de backup. Para SFTP, configure a verificação da chave do servidor com known_hosts_file. |
| Nuvem | Instale rclone e configure um perfil do provedor, por exemplo S3 ou armazenamento compatível, com um prefixo dedicado. Conceda acesso para listar, ler, escrever e remover nesse prefixo. |

O administrador do Linux provisiona os perfis. O painel oferece ao Super-Admin os destinos pelo nome. Crie `/etc/glpi-secureupdate/destinations.json` com aliases exclusivos. O exemplo apresenta três destinos opcionais.

```json
{
  "disco": {"type":"local","label":"Disco de backup","target":"/srv/glpi-backups"},
  "nas-ti": {"type":"nas","label":"NAS da TI","target":"nas-ti:compartilhamento/glpi"},
  "s3-ti": {"type":"cloud","label":"S3 da TI","target":"s3-ti:meu-bucket/glpi"}
}
```

Configure os perfis com `sudo rclone config --config /etc/glpi-secureupdate/rclone.conf`. Use dono root e modo 0600 para os dois arquivos. Para um destino local externo, acrescente `ReadWritePaths=/srv/glpi-backups` na seção `[Service]` de um override criado por `systemctl edit glpi-secureupdate`. Recarregue o systemd e reinicie o executor em uma janela sem operações pendentes. Se o destino depender de uma montagem, declare também `RequiresMountsFor=/srv/glpi-backups` na seção `[Unit]`. Para provedores com renovação de tokens, disponibilize credenciais que funcionem sob as restrições de escrita da unidade e valide a renovação em homologação. Mantenha cada alias, perfil e prefixo associado ao mesmo repositório durante toda a vida de suas cópias.

O envio verifica o conteúdo no destino por leitura. Somente depois libera o espaço local do SQL e do arquivo de dados. Uma falha deixa a cópia local disponível e o painel oferece **Reenviar ao destino**. O catálogo, o resumo da instalação e os hashes continuam locais. O TAR baixado reúne `database.sql`, `files.tar`, `SHA256SUMS` e `resumo.json`. Reserve espaço temporário para materializar backups remotos e produzir o TAR. Ajuste os timeouts do PHP e do proxy para o tamanho dos backups. O arquivo inclui chaves, dados pessoais e credenciais. Proteja-o com acesso restrito, transporte protegido e criptografia no armazenamento, por exemplo um perfil rclone crypt sobre o provedor. Guarde em outro local protegido uma cópia do catálogo com os hashes de confiança e da configuração do executor, incluindo as chaves de criptografia. A recuperação após perda total do servidor requer também esses itens ou um ensaio independente de recuperação do SQL e dos arquivos.

**Restaurar.** Expanda um backup e digite o identificador completo. O executor compara os hashes com o manifesto local, exige a mesma versão do GLPI e o mesmo layout de diretórios, e prepara uma nova base SQL. Em manutenção, salva o estado atual em `/var/lib/glpi-secureupdate/restore-points/<trabalho>/`. Depois troca os arquivos e a conexão, verifica o banco e o healthcheck. Após a reabertura testa o acesso HTTP. Uma falha nesse último teste aparece na aba Saúde e exige revisão para preservar gravações novas. Se houver falha durante a troca antes de reabrir, tenta repor o estado anterior. Se a própria recuperação falhar, o registro da transação permanece para diagnóstico e retomada. O ponto anterior inclui chamados criados após a cópia restaurada. Restaurar com sucesso encerra a validade de um rollback de atualização associado à base anterior.

**Excluir e aplicar retenção.** A exclusão individual pede o identificador completo. A política preserva as N cópias verificadas mais recentes do destino e também todas as cópias dentro dos últimos D dias. Com D igual a zero, vale o limite por quantidade. Exemplo: com 7 cópias e 30 dias, só são candidatas as mais antigas que 30 dias e fora das últimas 7. A prévia mostra a quantidade e os IDs. A confirmação é recusada se a lista mudar. A limpeza automática começa desativada e, se habilitada, é executada após um novo backup bem-sucedido naquele destino. Envios pendentes, pontos de rollback de atualizações e checkpoints de restauração ficam fora da retenção. Políticas do provedor, como versionamento, lixeira e bloqueio de objetos, podem reter os bytes ou impedir a exclusão. Uma exclusão interrompida pode ser retomada pelo catálogo.

Enquanto o serviço web estiver parado, consulte o log do trabalho no Linux. Em caso de interrupção, siga as instruções de recuperação abaixo.

## O que acontece durante a atualização

1. Verificação de versão e esquema, diagnóstico completo de referência, consulta ao endpoint público e nova conferência de SHA-256 do pacote aprovado.
2. Modo de manutenção, parada do ingresso e dos gravadores, dump do banco e backup de arquivos, chaves, plugins e dados externos. A lista de arquivos e os hashes são verificados. A importação do dump em um banco vazio fornece um teste de restauração.
3. Troca de árvore, migração na cópia do banco e conferência de esquema e versão. O executor usa a identidade Unix do PHP para comandos GLPI, acesso ao banco e diagnósticos.
4. Healthcheck nativo e diagnóstico completo do GLPI Health Check. O painel informa pontuação e ID do diagnóstico, regressões, cobertura perdida e verificações ignoradas. Um erro grave novo, uma verificação antes executada que deixou de funcionar ou uma falha obrigatória de banco/filesystem bloqueia a reabertura. Alertas preexistentes são identificados pela chave da verificação.
5. Reabertura e teste da URL real /status.php. O teste exige HTTP 200 e JSON válido com banco e filesystem saudáveis. Uma página HTML de login ou erro não passa. Se a manutenção já estava ativa, ela é preservada.

O diagnóstico CLI não cobre todos os fluxos do navegador. Após a reabertura, valide login com um usuário de teste e os fluxos relevantes ao seu negócio, como abertura de chamado, leitura de anexo e entrega de e-mail. Um resultado positivo descreve a cobertura executada. O plugin não cria chamados nem envia notificações de teste em nome de usuários.

## Rollback e interrupções

- **Falha antes da reabertura**. O serviço repõe a árvore anterior e os dados externos do snapshot e usa o banco original. Valida o esquema e, se houve troca, executa outro diagnóstico antes de reabrir. Falhas de recuperação deixam o registro pendente para atendimento no servidor.
- **Falha após permitir gravações**. O painel mostra o erro do endpoint. Só uma nova aprovação de **Rollback** inicia a reversão. Antes de voltar ao estado anterior, o serviço cria uma cópia do banco e dos arquivos atuais, inclusive dos chamados recebidos após a atualização. Esses chamados permanecem na cópia, mas deixam de estar visíveis no GLPI restaurado. Uma eventual reconciliação deve ser feita pelo administrador.
- **Queda de energia ou processo interrompido**. O registro durável bloqueia novos trabalhos. Pela console do servidor, confira o estado e as unidades de entrada/escrita antes de retomar. Com a transação pendente e a configuração de host original, o comando recover retoma a reabertura já iniciada ou restaura o estado anterior. Ele também funciona quando a interface está fora do ar.

```bash
sudo systemctl status glpi-secureupdate.service
sudo journalctl -u glpi-secureupdate.service -n 100
sudo cat /var/lib/glpi-secureupdate/transaction.json
sudo python3 /usr/local/lib/glpi-secureupdate/agent.py recover
```

Os logs detalhados de cada trabalho ficam em /var/lib/glpi-secureupdate/job-*.log. Os backups e os diagnósticos JSON de referência, pós-migração e rollback ficam em /var/lib/glpi-secureupdate/backups/. O diretório anterior também é mantido em uma pasta privada ao lado da aplicação. A interface só exibe um resumo, sem credenciais.

## Debug

Abra **Configurar > Plugins > Atualizações do GLPI > Debug** com o perfil Super-Admin ativo. A aba mostra o estado do executor, o ID do trabalho, a etapa da transação, os horários das consultas em UTC e a disponibilidade de rollback. **Exportar JSON** baixa esses mesmos campos para suporte. O ID corresponde a `job-<id>.log` no servidor. O relatório exclui credenciais, endereços, variáveis de ambiente, mensagens de exceção e conteúdo dos chamados. A consulta e a exportação são somente leitura.

## Validação

No diretório extraído do pacote:

```bash
python3 -m unittest discover -s tests -v
php -n tests/test_panel.php
```

A suíte Python usa transações e arquivos reais em diretórios temporários, com serviços GLPI, GitHub, banco e systemd simulados. Ela cobre integridade do pacote, aprovação, concorrência, rollback por falha na migração ou no healthcheck, rollback pós-reabertura e preservação de chamados no backup. A suíte também cobre a integridade dos dados externos usados pelo rollback, a preservação do ponto anterior, a recusa de estados desconhecidos, a aprovação de uso único, as permissões das credenciais em memória e os campos de debug. O teste PHP verifica sessão, perfil ativo e escape de HTML. Antes de usar em produção, execute o ciclo completo em uma cópia restaurada do seu ambiente, incluindo a aprovação de rollback e a conferência dos anexos.

[Atualização oficial do GLPI](https://glpi-install.readthedocs.io/en/latest/update.html) · [Status do GLPI](https://help.glpi-project.org/documentation/advanced/status) · [GLPI Health Check](https://github.com/JoaoPauloSCosta/glpi-health-check)
