# spec-gate

Você descreve o que quer. O Claude escreve a spec te entrevistando, escreve os testes a partir dela, implementa até passar, audita o resultado e commita.

**Em quatro momentos ele para e te pergunta** — e não avança sem sua resposta. Você é o Product Owner: decide o que é o produto; o Claude decide como construir.

---

## Instalação

Num terminal, no prompt do Claude Code:

```
/plugin install spec-gate --marketplace welt-studio/welt-studio-plugins
```

Na primeira vez ele pergunta se adiciona o marketplace (`y`) e depois o escopo (Enter escolhe o de usuário).

Precisa de Python 3 no PATH. Funciona em Linux, macOS e WSL2.

> O plugin fica **inerte** até você criar um `.specgate.json` no projeto. Sem ele, nada dispara — instalar não muda nada nos seus outros projetos.

---

## Começando

Abra o Claude Code na pasta do seu projeto e rode:

```
/spec-gate conversor de unidades de comprimento na CLI
```

A partir daí é conversa. O que vai acontecer, na ordem:

### 1. Ele te entrevista

Uma pergunta fechada por vez, com opções concretas:

> *Unidade inválida: erro em stderr com exit 1, ou mensagem em stdout com exit 0?*

Responda. Ele pergunta a próxima. **Ele nunca chuta** — se você não decidiu, ele pergunta de novo ou registra como pendência.

No fim, escreve os itens de backlog em `docs/backlog/01-nome.md`, `02-outro.md`… Cada um é um PBI: uma fatia pequena e entregável, com cada comportamento marcado por um ID estável (`[C1]`, `[E1]`) que acompanha aquele requisito até o teste e o commit.

Antes de te mostrar o resultado, três subagents releem o backlog inteiro em paralelo, cada um com uma lente diferente: valores concretos, casos de borda e erro, e contradições entre PBIs. O que os três acharem chega até você deduplicado, numa lista só.

### 2. 🛑 Ele para e pede sua aprovação do backlog

Você recebe as ambiguidades que sobraram e, se algum PBI ficou grande demais, uma proposta de quebrá-lo em menores.

Responda tudo de uma sentada. **Este é o ponto mais barato de corrigir rumo** — daqui pra frente cada erro custa testes e código.

### 3. Ele escreve os testes — de todos os PBIs de uma vez

Um subagent por PBI, em paralelo, cada um lendo **só a sua spec**. Todos ficam mecanicamente impedidos de ler seu código-fonte durante essa fase, então os testes não conseguem espelhar a implementação — eles testam o que você pediu, não o que o código faz.

### 4. 🛑 Ele para e pede sua aprovação dos testes

*Os testes capturam mesmo o que você quis dizer?*

Chega tudo junto: um gate por PBI, abertos de uma vez, para você responder a fila inteira numa sentada em vez de ser interrompido item a item. Aprove uns, reprove outros — os aprovados seguem sozinhos.

Vale ler com atenção: os testes viram o contrato. Se algo aqui está errado, é agora que sai barato.

### 5. Ele implementa e audita

Antes de escrever a primeira linha, a suíte roda e **precisa falhar**. Teste que já passa sem implementação não está testando nada — ou a asserção é vazia, ou o comportamento já existe. Se ela passar, ele não decide sozinho: **para e te pergunta**.

Daí implementa até a suíte inteira passar. As tentativas são contadas pelo hook, não por ele: dá para ver `tentativa 3/5` no board, e no estouro ele fica impedido de continuar editando código — o que resta é te reportar onde travou.

Se travar no meio do caminho, ele troca de olhos: passada a metade das tentativas, o subagent atual é encerrado e outro assume em contexto limpo, recebendo só o diagnóstico. Insistir no mesmo contexto poluído é o que costuma transformar um PBI difícil num PBI falhado.

Depois um segundo subagent, em contexto limpo, audita spec contra código — sem receber o relatório de quem implementou, então ele forma a própria opinião. E cada divergência que ele aponta passa por um terceiro, que tenta **refutá-la**: só as que sobrevivem devolvem o trabalho para a implementação. As refutadas viram observação no seu aceite, para você decidir.

### 6. 🛑 Ele para e pede seu aceite

Você aceita ou rejeita a entrega. Rejeitou? Volta para implementação e tenta de novo.

### 7. Ele commita

Antes do commit, a suíte inteira roda mais uma vez. Falhou, o commit não sai.

E então começa o próximo PBI.

---

## E se ele travar numa dúvida no meio?

Acontece: uma ambiguidade que ninguém viu aparece só na hora de implementar.

Nesse caso ele **estaciona o PBI** — guarda o trabalho parcial numa branch `parked/01-nome`, deixa a pasta limpa, e abre mais uma parada: a pergunta chega até você.

Quando você responder, ele retoma de onde parou, com o trabalho intacto.

> Estacionar preserva o trabalho **e não trava a fila**: o PBI parado espera você, os outros seguem. O princípio continua o mesmo — nada avança *naquilo* que você não decidiu.

---

## E se você mudar de ideia depois de entregue?

Acontece mais do que se admite: o PBI foi entregue, você usou, e o limite de 50 devia ser 100.

Peça a mudança. Ele abre uma parada para você confirmar exatamente o que muda — citando o requisito pelo ID, `[C3]` —, atualiza a spec com a sua decisão, e **refaz o ciclo daquele PBI**: testes novos, prova de vermelho, implementação, aceite. Nada de emendar a spec e deixar o código como estava.

É isto que faz a spec ser a fonte da verdade e não um documento de arranque: enquanto o produto existir, o que está escrito na spec é o que está no código.

## Retomando depois

Fechou o terminal? A sessão compactou? Rode `/spec-gate` de novo, sem argumento:

```
/spec-gate
```

Ele lê o estado do disco e te diz onde parou e o que está pendente. Você nunca precisa lembrar em que ponto estava.

---

## Acompanhando o progresso

### 📊 O board no navegador

Um painel estilo Kanban que se atualiza sozinho a cada 2 segundos, sem você precisar recarregar nada:

```bash
<plugin-dir>/scripts/dashboard.sh /caminho/do/projeto
```

Sobe um servidor local (porta 8437, só em `127.0.0.1`) e abre o navegador — no WSL2 abre no Windows automaticamente. O que você vê:

- **Barra de progresso** do lote inteiro
- **Cada PBI** com status colorido: `pending`, `running`, `delivered`, `skipped`, `failed` — mais tentativas e hash do commit
- **🛑 Gates aguardando você**, com o PBI e a rodada atual
- **❓ Perguntas acumuladas**, prontas para você responder de uma vez

Deixe aberto num segundo monitor e você acompanha o lote inteiro sem tocar no terminal.

> Adicione `.specgate-dashboard.html` ao `.gitignore` junto com `.specgate/`.

### 🧩 O mesmo board dentro do VS Code

Um painel dockável ao lado do editor, sem servidor e sem polling — ele vigia o arquivo de estado e atualiza na hora.

Isto é uma **extensão do VS Code, não parte do plugin**: os arquivos vêm junto quando você adiciona o marketplace, mas instalar é um passo à parte.

```bash
npm install -g @vscode/vsce
cd ~/.claude/plugins/marketplaces/welt-studio-plugins/vscode-spec-gate-board
vsce package
code --install-extension spec-gate-board-0.1.0.vsix
```

Depois é automático: sempre que o projeto aberto tiver `.specgate.json`, o painel abre sozinho. Para chamar à mão, `Ctrl+Shift+P` → *"spec-gate: Show Board"*.

> A API de plugins do Claude Code não tem superfície de UI — por isso janela fixa só existe por fora, como extensão.

### No terminal

O mesmo quadro, em ANSI, direto na conversa:

```
/spec-gate:board
```

Funciona até fora do Claude Code — é só um script lendo o estado.

### Na barra do Claude Code

Uma linha de resumo permanente. No `.claude/settings.json` do projeto:

```json
{"statusLine": {"type": "command", "command": "bash <plugin-dir>/scripts/statusline.sh"}}
```

Mostra `spec-gate 3/7 ok · 1 pulados · 0 falhas`, e acrescenta `· 1 gate(s) aberto(s)` quando algo espera por você.

> `<plugin-dir>` é a pasta onde o plugin foi instalado.

---

## Configuração

Crie `.specgate.json` na raiz do projeto — ou deixe o `/spec-gate` criar na primeira vez:

```json
{
  "test_command": "pytest -q",
  "source_paths": ["src"]
}
```

Só esses dois importam para começar. Os demais têm padrão razoável:

| Chave | Padrão | Para quê |
|---|---|---|
| `test_command` | — | Como rodar sua suíte |
| `source_paths` | `["src"]` | Onde vive seu código (fica bloqueado na fase de testes) |
| `max_fix_attempts` | `5` | Tentativas de correção antes de parar e te reportar (contadas pelo hook) |
| `require_red` | `true` | Exigir suíte vermelha antes de começar a implementar |
| `require_trace` | `true` | Exigir rastro requisito → teste antes de implementar |
| `traceability_path` | `docs/traceability.json` | Onde vive a matriz de rastreabilidade |
| `test_timeout_seconds` | `600` | Teto de tempo da suíte |
| `max_behaviors_per_pbi` | `7` | Acima disso, ele propõe quebrar o PBI |
| `max_public_interfaces_per_pbi` | `1` | Idem, contando interfaces públicas |
| `spec_paths` | `["docs/backlog"]` | O que o congelamento de spec protege |
| `block_destructive` | `true` | Bloqueio de `rm -rf`, `reset --hard` etc. |
| `project_name` | — | Nome exibido no dashboard |

Adicione `.specgate/` ao `.gitignore` — é estado de runtime.

**Para não ser interrompido por permissão a cada comando**, libere no `.claude/settings.json` do projeto só o que o fluxo precisa:

```json
{
  "permissions": {
    "allow": [
      "Bash(python3 -m pytest*)",
      "Bash(git add*)", "Bash(git commit*)", "Bash(git status*)",
      "Bash(git diff*)", "Bash(git log*)", "Bash(git checkout -- *)",
      "Bash(mkdir -p .specgate*)", "Bash(printf*)"
    ]
  }
}
```

Ative o `acceptEdits` com **shift+tab** e evite `--dangerously-skip-permissions`: com as regras acima, o fluxo roda sem abrir mão das proteções.

---

## Referência

<details>
<summary><b>O caminho completo de um PBI</b></summary>

```
você descreve o que quer
         ▼
CONCEPÇÃO                               subagent: spec-analyst
  entrevista, uma pergunta fechada por vez
  → escreve docs/backlog/NN-nome.md, requisitos com ID [C1]/[E1]
         ▼
REFINAMENTO                             3× spec-analyst em paralelo
  lentes: valores · borda-erro · coerência-lote
  caça ambiguidade · avalia granularidade · dedup
         ▼
    🛑 GATE DE BACKLOG
         ▼
TESTES DO LOTE                          1 blackbox-tester por PBI, em paralelo
  🔒 leitura de source_paths bloqueada
  → escreve docs/traceability.json (requisito → teste)
         ▼
    🛑 GATE DE TESTES  (um por PBI, todos de uma vez)
         ▼
   ────── daqui pra baixo, um PBI de cada vez ──────
         ▼
  🔒 RASTREABILIDADE — todo requisito tem entrada, todo teste existe
  🔒 PROVA DE RED — a suíte tem que falhar antes de implementar
     já passa? 🛑 GATE DE RED, o PO decide
         ▼
IMPLEMENTAÇÃO                           subagent: implementer
  🔒 spec congelada · teto de tentativas contado pelo hook
  na metade do teto: troca para um implementer novo
         ▼
CONFORMIDADE                            subagent: spec-reviewer
  auditoria em contexto limpo
  cada divergência crítica → subagent: divergence-verifier
  só as confirmadas voltam para implementação
         ▼
    🛑 GATE DE ACEITE
         ▼
COMMIT
  🔒 suíte completa roda antes de deixar passar
  → carimba o commit em docs/traceability.json
  → registra decisões transversais em docs/DECISIONS.md
         ▼
   próximo PBI

TRANSVERSAIS
  🔒 bloqueio de operações destrutivas
  🛑 GATE DE AMBIGUIDADE — estaciona o PBI e pergunta (a fila segue)
  🛑 GATE DE EMENDA — requisito muda num PBI entregue
     → spec atualizada → o PBI refaz o ciclo inteiro
```

Dois tipos de gate, com comportamentos diferentes:

| | Quais | O que faz |
|---|---|---|
| 🔒 **Mecânico** | Black-box, prova de RED, teto de tentativas, regressão, destrutivo, congelamento de spec | Age sozinho — bloqueia ou libera, sem perguntar |
| 🛑 **De PO** | Backlog, testes, aceite, ambiguidade, RED, emenda | Para o PBI (backlog para o lote) e espera **você** |

</details>

<details>
<summary><b>Os gates mecânicos, em detalhe</b></summary>

**Black-box.** Durante a fase de Testes, leitura de `source_paths` é bloqueada (Read, Grep, Glob e comandos Bash de leitura). O agente é instruído a registrar a lacuna da spec em vez de espiar o código.

**Rastreabilidade.** Cada comportamento e caso de erro da spec tem um ID estável (`[C1]`, `[E1]`), e `docs/traceability.json` — versionado junto com o código — diz qual teste cobre cada um. Na entrada da implementação o hook confere que todo requisito tem entrada e que todo teste citado existe mesmo no arquivo indicado. Requisito que você decidiu deixar sem teste passa, desde que declarado (`"status": "uncovered"` com o motivo): o que o gate proíbe é o silêncio sobre o requisito. Desligue com `"require_trace": false`; specs sem IDs (formato antigo) não disparam nada.

**Prova de RED.** Na transição para a implementação, o `test_command` roda e a suíte precisa estar **vermelha**. Se já passa, o fluxo para e abre o gate de RED para você decidir: o comportamento já existia, ou os testes não capturam a spec? Aprovado o gate, a transição libera sozinha. Desligue com `"require_red": false`.

> Vale por rodada do gate de testes, não por transição: uma vez provado o vermelho, voltar para a implementação (depois de uma conformidade reprovada, por exemplo) não roda a suíte de novo — nesse ponto o código já existe, e exigir vermelho seria pedir o impossível. Testes reprovados e reescritos abrem rodada nova, e aí o vermelho é exigido outra vez.
>
> O vermelho é provado por PBI, via código de saída da suíte — não requisito por requisito. Ler nomes de teste de um runner qualquer (pytest, jest, go test) seria frágil demais para virar gate.

**Teto de tentativas.** Cada execução da suíte durante a implementação é contada pelo hook em `.specgate/attempts.json` — não pelo relato do agente. No estouro de `max_fix_attempts`, editar `source_paths` é bloqueado; rodar a suíte continua liberado, para que o relatório de onde travou saia com o estado real. O contador zera quando um gate daquele PBI ganha rodada nova, o que exige uma fala sua.

**Regressão.** Intercepta `git commit` e `git merge`, roda o `test_command` e bloqueia se a suíte quebrar. *Exceção:* commits numa branch `parked/*` não rodam a suíte — trabalho estacionado é incompleto por definição. O merge de volta passa pelo gate normal.

**Destrutivo.** Bloqueia `git reset --hard`, `git clean -f`, `git push --force`, `git branch -D`, `git checkout .`, `git restore .` e `rm -rf`. Para liberar, o agente precisa explicar o que seria perdido e obter sua confirmação. `git branch -d` minúsculo passa livre.

**Congelamento de spec.** Depois que você aprova o backlog, `docs/backlog/` fica congelado. A promessa exata é *"o agente não altera o contrato por conta própria"* — existe uma janela estreita, só para ele transcrever uma decisão **sua**, exigindo que sua fala tenha acontecido antes. Ela abre em dois casos, e só neles: a retomada de um PBI estacionado (você respondeu a ambiguidade) e a emenda de um PBI entregue (você pediu a mudança). Vale para um arquivo só, some quando a fase avança, e fecha quando o gate ganha rodada nova.

**Gates de PO.** Toda transição de fase passa por `.specgate/phase`; com gate aberto, escrever nele é bloqueado — **para aquele PBI**. Gate de backlog trava o lote inteiro (é o contrato de todos); gate de um PBI trava só ele. Se o guard não consegue identificar de quem é a transição (fase sem PBI, escrita por Bash) ou de quem é o gate (entrada sem `pbi`), vale o bloqueio amplo: ignorância nunca destrava mais que conhecimento.

E registrar uma decisão em `gate.json` exige um turno seu posterior à abertura do gate — o contador `.specgate/seq` só avança quando você fala de verdade.

Todos os guards **falham abertos**: erro interno libera a ação, para nunca travar sua sessão. A exceção é o gate de regressão — se a suíte não *puder ser executada*, o commit é bloqueado, porque não conseguir verificar não é o mesmo que estar tudo bem.

</details>

<details>
<summary><b>Estado em <code>.specgate/</code></b></summary>

Tudo aqui é gitignored — estado de runtime, não histórico.

| Arquivo | Conteúdo |
|---|---|
| `phase` | Fase ativa (`testing`, `implementing:<spec>`, ou vazio) |
| `gate.json` | Os gates, com `checkpoint`, `pbi`, `rodada`, `status`, `opened_at_seq` |
| `batch.json` | Fila de PBIs, com `backlog_aprovado` e status por item |
| `seq` | Contador de turnos seus — só o hook de eventos escreve nele |
| `red.json` | Prova de que a suíte estava vermelha antes de cada PBI — só o hook escreve |
| `attempts.json` | Tentativas gastas no PBI ativo — só o hook escreve |
| `events.jsonl` | Log da sessão, consumido pelo painel do VS Code |

**Rodada:** cada tentativa de decisão de um gate é uma rodada. Reprovou? A próxima tentativa abre a rodada seguinte, e a anterior fica no histórico. É o que permite rework sem apagar o registro do que foi decidido antes.

</details>

<details>
<summary><b>Honestidade sobre o que os guards são</b></summary>

Nenhum estado em arquivo é uma barreira real contra o Bash do próprio agente — formas contorcidas (`dd`, `eval`, heredoc, indireção por variável) contornam qualquer parsing de comando. Todo guard baseado em inspecionar a chamada é **fricção**, que encarece o desvio *casual* — a ameaça real, um modelo racionalizando um atalho. Não é uma sandbox e não se vende como tal.

A propriedade genuinamente dura é outra: o Claude **não fabrica um turno seu**. O contador `.specgate/seq` só avança dentro do hook de eventos, no `UserPromptSubmit` — nunca por uma ação do agente. Por isso o caminho honesto (esperar você falar) é sempre o de **menor resistência**.

A prova de RED e o teto de tentativas ficam do mesmo lado dessa linha, e é o que os separa de uma regra escrita no prompt: **quem executa a suíte e quem conta as tentativas é o hook**, não o agente que está sendo avaliado. Um modelo pode escrever no relatório que rodou os testes, que eles falhavam antes, que gastou duas tentativas — nada disso muda o que está em `red.json` e `attempts.json`, porque ele não escreve nesses arquivos. A fricção continua sendo fricção; a medição é medição.

</details>

---

## Limites conhecidos

- **Não existe modo automático.** Um gate que exige sua fala não funciona sem você. Se quiser trabalho não supervisionado, este plugin não é a ferramenta.
- **A memória entre PBIs é curta de propósito.** `docs/DECISIONS.md` guarda até três linhas por PBI, só o que é transversal. Não é um registro de arquitetura — é o bilhete que o próximo subagent, em contexto limpo, precisa ler para não reinventar o que o lote já decidiu.
- **Bloqueio por parsing é fricção, não sandbox.** Um agente disposto a burlar de propósito consegue. O alvo é o desvio acidental.
- **O gate não lê semântica.** Com dois gates abertos, uma resposta que trata de um satisfaz mecanicamente os dois — distinguir é instrução, não mecânica.
- **A contagem de granularidade é julgamento**, não medição: o limite é comparado mecanicamente, mas quem conta os comportamentos é o modelo.
- **Suítes lentas doem**, porque o gate de regressão roda síncrono — e a prova de RED acrescenta mais uma execução por PBI, na entrada da implementação. Ajuste `test_timeout_seconds` ou aponte `test_command` para um subconjunto rápido.
- **A prova de RED é por PBI, não por requisito.** Um PBI com cinco comportamentos em que só um teste falha já conta como vermelho.
- **A contagem de tentativas é otimista por um.** O hook conta antes de o comando rodar, então uma execução que nem chega a iniciar (typo, dependência faltando) já entrou na conta.

---

## Para quem quer o porquê

O design completo, com as decisões e os trade-offs assumidos, está em dois documentos:
[o fluxo gateado pelo PO (0.2.0)](../../docs/superpowers/specs/2026-07-24-spec-gate-po-gated-flow-design.md)
e [a engenharia dos loops (0.3.0)](../../docs/superpowers/specs/2026-07-31-spec-gate-0.3.0-loops.md),
que trouxe a prova de RED, o teto mecânico, a rastreabilidade por requisito, o chokepoint
por PBI e a emenda de spec.

O plugin destila ideias do [maul-team](https://github.com/sohei56/maul-team) (framework Scrum de agentes), mantendo o PO gateado em pontos discretos em vez de interrompido difusamente — e cobrindo uma lacuna que o original deixa aberta: como um épico vira PBIs pequenos.
