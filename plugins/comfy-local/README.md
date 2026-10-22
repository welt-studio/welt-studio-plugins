# comfy-local

Plugin de Claude Code que dirige um ComfyUI local por MCP.

Empacota um servidor MCP com 42 ferramentas mais uma skill que ensina o
fluxo de trabalho. A maior parte é wrapper sobre o `comfy-cli`, que emite
um envelope JSON estável em todo comando, incluindo um campo `hint` nos
erros dizendo como corrigir; a biblioteca de workflows fala HTTP direto com
o ComfyUI, porque é a rota `/userdata` que alimenta a barra lateral da
interface.

O que ele tem e o `comfy-mcp` oficial da Comfy Org não: **edição de grafo nos
dois sentidos**. Ler o workflow que está aberto no canvas, montá-lo node a node,
ligar saída em entrada, e ver cada mudança aparecer na tela — sem o usuário
precisar salvar nada. Os verbos de edição existem no `comfy-cli` e nenhum outro
servidor MCP os expõe.

## Pré requisitos

1. **Python 3.10 ou superior.** O plugin cria o próprio venv isolado no
   diretório de dados dele, então não suja o seu ambiente.

2. **comfy-cli 1.20.0 ou superior**, de preferência num venv separado. É o
   piso que as ferramentas de edição de grafo, fragmento e receita exigem;
   `comfy_server_info` confere e avisa no bloco `cli`:

   ```
   python -m venv C:\comfy\env
   C:\comfy\env\Scripts\pip.exe install comfy-cli
   C:\comfy\env\Scripts\comfy.exe --workspace=C:\comfy\ComfyUI install
   ```

   Mantenha o ComfyUI num venv próprio, não no venv do plugin. As
   dependências do ComfyUI são pesadas e não devem se misturar.

3. **ComfyUI rodando.** O plugin sobe com `comfy_launch_server` se
   precisar, mas o workspace precisa existir antes.

## Instalação

Num terminal, no prompt do Claude Code:

```
/plugin install comfy-local --marketplace welt-studio/welt-studio-plugins
```

Na primeira vez ele pergunta se adiciona o marketplace (`y`) e depois o escopo (Enter escolhe o de usuário).

O plugin instala desabilitado de propósito, porque conecta a um serviço
externo. Habilite e responda as três perguntas de configuração:

| Campo | Valor |
|---|---|
| Interpretador Python | `python`, ou caminho absoluto |
| Executável comfy | `C:\comfy\env\Scripts\comfy.exe` |
| Endereço do ComfyUI | vazio para o padrão `127.0.0.1:8188` |

Na primeira execução o servidor monta o venv dele e instala o SDK do MCP.
Leva alguns segundos e acontece uma vez só. O progresso sai no stderr,
visível com `claude --debug`.

Essa instalação acontece dentro do tempo de conexão do servidor MCP. Numa
máquina lenta ou numa rede ruim ela pode estourar esse limite, e o servidor
aparece como falha na aba **Errors** do `/plugin`. Na sessão seguinte ele
sobe normal, porque o ambiente já está pronto. Para evitar de vez, suba a
variável `MCP_TIMEOUT` antes de abrir o Claude Code.

## Ferramentas

| Grupo | Ferramentas |
|---|---|
| Canvas aberto | `read_canvas` |
| Edição de grafo | `edit_graph`, `graph_recipe` |
| Biblioteca | `workflow_library` |
| Estado | `server_info`, `launch_server`, `stop_server` |
| Execução | `validate_workflow`, `run_workflow`, `job_status`, `job_wait`, `job_list`, `job_watch`, `job_cancel`, `fetch_outputs` |
| Diagnóstico | `system_stats`, `free_memory`, `server_logs` |
| Introspecção | `search_nodes`, `show_node`, `list_nodes`, `node_neighbors`, `node_path`, `search_models`, `show_model` |
| Construção | `list_templates`, `show_template`, `check_template`, `fetch_template`, `workflow_slots`, `set_workflow_slots`, `workflow_notes`, `compose_workflow`, `decompose_workflow`, `vary_workflow`, `list_fragments` |
| Dependências | `download_model`, `download_status`, `install_node` |
| Arquivos | `upload_files`, `preview_media` |
| Metodologia | `read_methodology` |

Todas com o prefixo `comfy_`.

`comfy_edit_graph` e `comfy_graph_recipe` agrupam vários verbos num campo
`action`, em vez de virarem doze ferramentas soltas — cada ferramenta custa
contexto em toda sessão, e estas compartilham o mesmo arquivo de trabalho.

| Ferramenta | `action` |
|---|---|
| `comfy_edit_graph` | `add_node`, `connect`, `set_widget`, `delete_nodes`, `clear`, `reset_doc`, `ls_nodes`, `print` |
| `comfy_graph_recipe` | `apply`, `capture`, `foreach` |
| `comfy_workflow_library` | `list`, `get`, `save`, `delete` |

`comfy_read_canvas` não tem `action`: lê o grafo aberto na aba e grava num
arquivo, que é o começo do ciclo.

Duas mudam a instalação e pedem confirmação do usuário antes:
`comfy_download_model` grava gigabytes em disco e `comfy_install_node` instala
código de terceiro no interpretador do ComfyUI. Uma terceira não mexe na
instalação mas também destrói trabalho: `comfy_job_cancel` descarta o que um
job em andamento já produziu.

## Variáveis de ambiente

Além das três da configuração do plugin, o servidor lê:

| Variável | Padrão | Efeito |
|---|---|---|
| `COMFY_LOCAL_URL` | `127.0.0.1:8188` | endereço padrão do ComfyUI |
| `COMFY_MCP_TIMEOUT` | 300 | timeout em segundos das chamadas normais |
| `COMFY_MCP_LONG_TIMEOUT` | 1800 | timeout de operações longas |
| `COMFY_MCP_MAX_CHARS` | 24000 | teto de caracteres por resposta |
| `COMFY_MCP_LIVE` | 1 | publicar cada edição no canvas aberto; `0` desliga |

`COMFY_LOCAL_URL` é o que a pergunta "Endereço do ComfyUI" preenche. Aceita
`host:porta` ou `http://host:porta`. As ferramentas que expõem `host` e
`port` sobrescrevem esse padrão chamada a chamada; um valor sem forma de
endereço é ignorado em silêncio e a CLI usa os defaults dela.

Quinze das trinta e oito ferramentas endereçam o servidor por `--host` e
`--port`. As outras vinte e três não: seis resolvem o servidor pelo workspace
ativo — `comfy_upload_files`, `comfy_fetch_outputs`, `comfy_search_models`,
`comfy_show_model`, `comfy_system_stats` e `comfy_free_memory` —, outras falam
com a galeria de templates, e as demais mexem em arquivo local ou no próprio
workspace: `comfy_server_info`, `comfy_launch_server` e `comfy_stop_server`
agem sobre o ComfyUI do workspace ativo, e `comfy_download_model`,
`comfy_download_status` e `comfy_install_node` gravam dentro dele.

Se o seu ComfyUI não está no endereço padrão, aponte o workspace com
`comfy set-default`. Sem isso, tudo que resolve pelo workspace mira no lugar
errado enquanto as quinze roteadas acertam, que é o tipo de falha que custa a
ser entendida.

`comfy_server_logs` é um caso à parte: o comando não aceita `--host`, mas o
`--port` seleciona de qual instância ler o log. Ele respeita a porta de
`COMFY_LOCAL_URL` quando você não passa uma — sem isso, um ComfyUI fora da
8188 teria o log lido da instância errada bem na hora de diagnosticar
uma falha.

## O ciclo de mão dupla

As ferramentas de edição trabalham num arquivo. A extensão que vem junto liga
esse arquivo ao ComfyUI que você tem aberto, nos dois sentidos:

```
comfy_read_canvas   →  o grafo da sua aba vira um arquivo
comfy_edit_graph    →  o agente edita esse arquivo
                    →  cada edição volta para a sua tela
```

Sem ela, o agente só alcança o que já foi salvo em disco ou na biblioteca. Com
ela, ele edita o que você está vendo, e você vê acontecer. Instale assim:

```
cp -r plugins/comfy-local/comfyui-extension /caminho/do/ComfyUI/custom_nodes/comfyui-welt-live
```

Reinicie o ComfyUI. A partir daí, toda edição de `comfy_edit_graph` aparece no
canvas na hora, e o envelope traz um bloco `live` dizendo quantas abas foram
alcançadas.

Com mais de uma aba aberta, a primeira que responder ao pedido de leitura é a que
vale — a publicação, essa vai para todas.

Isto precisa ser uma extensão porque não há como comandar a aba aberta de fora:
o handler de `/ws` do ComfyUI aceita do cliente apenas mensagens `feature_flags`
e descarta o resto, e nenhuma rota faz o servidor transmitir um evento arbitrário
aos frontends. `PromptServer.send_sync` é o único caminho, e só existe dentro do
processo do ComfyUI. A leitura tem o mesmo motivo pelo avesso: o grafo aberto vive
no navegador, não no processo do ComfyUI, então o jeito é pedir pelo websocket e
esperar a aba devolver por HTTP.

A leitura vale-se da mesma guarda: ao responder, a aba registra aquele estado como
conhecido, então a edição que o agente devolve em cima dele entra sem pedir
confirmação. Se você mexer no canvas nesse meio tempo, a guarda volta a valer.

**A extensão não sobrescreve trabalho não salvo.** Aplicar troca o canvas
inteiro, então se você mexeu no grafo depois da última vez que o agente o
conheceu, a publicação é recusada em vez de recarregar por cima — e nada é
perguntado a você. Quem conserta é o agente: ele relê com `comfy_read_canvas`,
refaz a edição sobre o estado atual e publica de novo, e aí ela entra sozinha.

Nas configurações do ComfyUI, em *Welt Live*, dá para desligar o espelhamento
com **Aplicar edições automaticamente**. Desligado, ele para de vez: não há
aplicação manual, as publicações são descartadas e só aparece um aviso no
console do navegador. Religue a opção para o canvas voltar a acompanhar.

Uma aba que não está em foco é adormecida pelo navegador, então a leitura tem 25
segundos de prazo e uma segunda tentativa automática. Se ainda assim falhar, traga
a janela do ComfyUI para a frente.

Sem a extensão nada quebra: a leitura devolve `extensao_ausente`, a edição grava
normalmente e o bloco `live` diz que o canvas não acompanhou. Nesse caso, publique com `comfy_workflow_library`
(`action: "save"`) e abra pela barra lateral de Workflows — que é o caminho que
funciona em qualquer ComfyUI, com ou sem extensão.

## Notas

O `comfy-cli` já traz skills próprias, instaláveis com `comfy skills
install`, que ensinam o agente a usar a CLI direto pelo shell. Este plugin
resolve outro problema: expor a mesma capacidade por MCP, para superfícies
que não têm shell no seu Windows, como o Claude Desktop e o Cowork. Em
Claude Code as duas abordagens funcionam e você pode escolher.

A metodologia completa do Comfy continua acessível pela ferramenta
`comfy_read_methodology`, que imprime as skills oficiais sob demanda em vez
de carregar milhares de linhas em toda sessão.
