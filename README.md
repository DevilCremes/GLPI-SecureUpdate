# GLPI-SecureUpdate
Plugin para GLPI 10.0.x, 11.0.x e 12.0.x em Linux que reúne atualizações aprovadas pelo Super-Admin, verificação SHA-256 do pacote oficial, integração com o GLPI Health Check de João Paulo S. Costa e rollback. Permite agendar backups locais, em NAS ou na nuvem, gerenciar a retenção e receber resultados por e-mail.

## Baixar e instalar no Linux

Para a **primeira instalação** do SecureUpdate, execute o bloco abaixo no servidor do GLPI. Ele baixa o pacote deste repositório para uma pasta privada no seu diretório pessoal e inicia o instalador automático. São necessários `curl`, `tar`, `python3` e acesso a `sudo`.

```bash
revisao=a01357a1cb34c7d11bf041fd43ef24434df22577
pasta=$(mktemp -d "$HOME/glpi-secureupdate.XXXXXX") &&
cd "$pasta" &&
curl --fail --show-error --location \
  "https://github.com/DevilCremes/GLPI-SecureUpdate/archive/$revisao.tar.gz" \
  --output secureupdate.tar.gz &&
tar -xzf secureupdate.tar.gz --strip-components=1 &&
sudo python3 instalar.py
```

Se o instalador encontrar permissões incompatíveis na raiz do GLPI ou em `plugins/`, confira o caminho e o ajuste proposto antes de confirmar com `s`, `sim`, `y` ou `yes`. Enter recusa a alteração. A alteração é limitada ao próprio diretório.

Confira a conta PHP e a lista de serviços sugeridas pelo instalador. Ao concluir, entre no GLPI com o perfil **Super-Admin** e abra **Configurar → Plugins → Atualizações do GLPI**.

Para **apenas conferir a detecção**, substitua a última linha do bloco por `sudo python3 instalar.py --detectar`. Se o SecureUpdate já estiver instalado, siga o [procedimento para atualizar o próprio plugin](./LEIA-ME.md#atualizar-o-próprio-secureupdate).

O SecureUpdate é um plugin em português para administrar atualizações e backups do **GLPI 10.0.x, 11.0.x e 12.0.x em Linux**. No próprio GLPI, o Super-Admin pode consultar o que mudou em uma versão, verificar o pacote oficial e aprovar a instalação. A consulta por novas versões pode ser agendada. **Toda atualização exige aprovação explícita.**

Na versão 1.4.0, a aba **Atualização** permite escolher a manutenção no ramo instalado ou uma migração para o próximo ramo (10 → 11 ou 11 → 12). As consultas agendadas continuam no ramo instalado. A passagem de 10 para 12 exige duas operações, cada uma com pacote verificado, aprovação, backup e diagnóstico próprios.


[Baixar a prévia interativa do painel](https://raw.githubusercontent.com/DevilCremes/GLPI-SecureUpdate/main/docs/previa-secureupdate.html)

## Créditos ao GLPI Health Check

**GLPI Health Check** é um plugin independente de **João Paulo S. Costa** ([@JoaoPauloSCosta](https://github.com/JoaoPauloSCosta)). O autor desenvolveu o mecanismo que analisa a instalação do GLPI, atribui uma pontuação de saúde e registra os achados de cada execução. O projeto original declara **Copyright © 2026 João Paulo S. Costa** e adota a licença **GNU GPL, versão 3 ou posterior** (`GPL-3.0-or-later`). [Documentação do autor em português](https://github.com/JoaoPauloSCosta/glpi-health-check/blob/main/README.pt_BR.md) · [Declaração de autoria](https://github.com/JoaoPauloSCosta/glpi-health-check/blob/c08d653fddc0c01d35137d87bfa0e10a47dfe520/setup.php) · [Licença do projeto original](https://github.com/JoaoPauloSCosta/glpi-health-check/blob/c08d653fddc0c01d35137d87bfa0e10a47dfe520/LICENSE)

O Health Check é instalado e atualizado separadamente a partir do [repositório original](https://github.com/JoaoPauloSCosta/glpi-health-check). O pacote do SecureUpdate contém o código de integração. Para executar o diagnóstico completo, ele chama o `Runner` do Health Check e lê a pontuação, o identificador da execução e o resultado de cada verificação por meio de `RunRepository`.

O **SecureUpdate** compara esses resultados antes e depois da migração. Também consulta o estado nativo do GLPI, verifica o esquema do banco e testa o acesso HTTP após a reabertura. O gerenciamento do backup, da janela de manutenção e do rollback pertence ao SecureUpdate. Assim, na aba **Saúde**, a pontuação do diagnóstico é creditada ao Health Check de João Paulo S. Costa. A indicação de regressão decorre da comparação feita pelo SecureUpdate.

## Interface

O painel usa a navegação em abas e cartões do tema do GLPI. Nas abas **Atualização** e **Backups**, a barra acompanha o envio de pacotes e o console recolhível registra as etapas. O resultado aparece automaticamente quando o executor termina, inclusive após a retomada do Apache. Em telas pequenas, as abas podem ser roladas.

## Como funciona uma atualização

1. **Revisão da versão.** O painel compara a instalação com as versões estáveis do mesmo ramo e reúne o changelog das versões intermediárias. Quando as notas oficiais indicam gravidade, o painel apresenta a urgência correspondente.
2. **Verificação do pacote.** O Super-Admin escolhe entre baixar o arquivo oficial no servidor ou enviar o `.tgz` do computador. Nos dois casos, o executor confere o tamanho e o SHA-256 com os metadados da release antes de liberar a aprovação.
3. **Manutenção e backup.** Após a aprovação, o serviço coloca o GLPI em manutenção, pausa os serviços de acesso e gravação configurados e copia o banco, a aplicação, os anexos e os diretórios externos declarados. A migração ocorre em uma cópia do banco, preservando a base anterior.
4. **Verificação e reabertura.** O executor compara os diagnósticos de antes e depois da migração e verifica o banco e o sistema de arquivos. Depois de reabrir o acesso, testa o endpoint público `/status.php`.

Se a migração ou o diagnóstico falhar antes da reabertura, o executor tenta restaurar o estado anterior. Depois que o GLPI volta a aceitar gravações, o rollback exige nova aprovação. Antes de reverter, o serviço copia o estado atual. Chamados criados nesse intervalo ficam preservados nessa cópia e podem exigir reconciliação pelo administrador.

## Backups independentes das atualizações

- **Criação e agendamento.** Execute uma cópia sob demanda ou programe uma janela diária, semanal ou mensal, com horário e fuso. É possível escolher, por exemplo, toda segunda-feira, todo dia 7 ou o último dia do mês.
- **Destinos.** Salve no servidor, em outro disco, em um NAS ou na nuvem. Em **Backups → Destinos de backup**, o Super-Admin cadastra uma pasta local, um NAS SMB ou um bucket S3 e escolhe a subpasta de cada destino. O executor testa a conexão antes de liberar o destino na política. Perfis rclone provisionados no Linux também são aceitos. Se a transferência falhar, a cópia local fica disponível para reenvio.
- **Catálogo e restauração.** Consulte, baixe, restaure ou exclua cópias. A restauração verifica os hashes, exige a mesma versão e estrutura de diretórios do GLPI e cria um ponto de segurança com os dados em uso antes da troca.
- **Retenção.** Combine quantidade e idade. Por exemplo, preserve as sete cópias mais recentes e todas as dos últimos 30 dias. O painel mostra os candidatos à exclusão. A limpeza automática é opcional e preserva os pontos de rollback das atualizações.

## Notificações e diagnóstico

Os avisos de **novas versões** podem aparecer no painel durante o uso do GLPI ou chegar por e-mail. A consulta aceita um intervalo simples em horas ou uma regra de calendário.

Os **e-mails de backup** têm configuração própria. O Super-Admin pode desativá-los, receber somente erros e avisos ou receber todos os resultados. A mensagem identifica a operação e informa, por exemplo, se a cópia local foi criada, mas o envio ao NAS ficou pendente. Falhas temporárias são retentadas até cinco vezes. Sem destinatários ativos, o erro é sinalizado e removido da fila.

A aba **Saúde** apresenta os resultados do diagnóstico e as regressões detectadas. A aba **Debug** mostra a etapa em andamento e o identificador que permite localizar o log no servidor. O resumo pode ser exportado em JSON sem incluir credenciais ou conteúdo dos chamados.

## Instalação e compatibilidade

O projeto foi desenvolvido para instalações pelo pacote oficial do GLPI em **um servidor Linux com systemd**, PHP CLI e um banco primário MySQL ou MariaDB. A interface PHP segue o tema do GLPI. Um serviço Python instalado fora da árvore web executa as operações autorizadas. O diagnóstico integrado requer o [GLPI Health Check](https://github.com/JoaoPauloSCosta/glpi-health-check).

O guia do pacote detalha as dependências, as permissões do banco, a configuração das unidades que devem parar durante a manutenção e o procedimento de recuperação pelo terminal. [Guia de instalação e operação](./LEIA-ME.md)

## Validação

Na versão **1.4.0**, passaram **167 testes Python e 3 testes JavaScript**. A suíte cobre aprovação, integridade dos arquivos, restauração entre bancos renomeados, compatibilidade de plugins, downloads auditados, execução de tarefas, barra de progresso e tentativas limitadas de e-mail. Os testes usam arquivos locais e simulam serviços externos. A homologação de ponta a ponta com o GLPI, o banco, o destino remoto e o SMTP da instalação continua necessária.

## Licença

Distribuído sob a GNU General Public License, versão 3 ou posterior (`GPL-3.0-or-later`). [Texto da licença](./COPYING)
