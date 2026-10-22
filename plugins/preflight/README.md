# preflight

Mod que pega na hora da escrita o que o `/code-review` pegaria depois.

Nasceu da análise de cerca de 700 findings do `/code-review` em 9 projetos. Cerca de 40% eram mecânicos:
- sobras de rename, remoção ou extração (docstring citando tipo apagado, import órfão, doc descrevendo função que não existe mais);
- falha de lint ou format que o pre-commit reprovaria;
- edição em massa por `sed`/heredoc deixando lixo para trás.

O resto pedia julgamento: estados de loading, erro e vazio, invalidação de cache, caminho irmão, atomicidade. O review continua existindo; o objetivo é ele chegar com menos para achar.

## O que faz

| Camada | Quando | Efeito |
|---|---|---|
| Trava de escrita por Bash | antes de cada `Bash` | Nega `sed -i`, `perl -i`, `awk -i inplace`, redirect (`>`, `>>`, `tee`), cópia e movimentação (`cp`, `mv`, `install`, `rsync`, `dd of=`) e script inline (`python - <<EOF`, `node -e`) que grava arquivo dentro de **qualquer** repositório git, não só o da sessão. Também segue laço, `find -exec`, `xargs` e `bash -c`. Manda usar Edit/Write. Resolve variáveis atribuídas no próprio comando (`S=/tmp/x; cat > $S/a`) e o `cd` antes do alvo; fora de repositório (`/tmp`, `$TMPDIR`, rascunho) passa. Alvo que não dá para resolver (variável de fora do comando, `~`, script sem caminho literal fora de um diretório conhecido) é barrado. |
| Pre-commit por arquivo | depois de cada Edit/Write/NotebookEdit | Roda `pre-commit run --files <arquivo>` do próprio repo, pulando os hooks de projeto inteiro (`pass_filenames: false` ou `always_run: true`, como tsc e pyright). A falha volta ao modelo no mesmo passo. Quando o fixer reescreve o arquivo e passa, só pede re-leitura. |
| Varredura de símbolo removido | depois de cada Edit/Write | Identificadores compostos (camelCase, PascalCase, snake_case) que saíram do arquivo são procurados com `git grep -w` no repo inteiro. Cada ocorrência restante (doc, comentário, import, teste) volta ao modelo. |
| Gate de fim de turno | `Stop` | Roda o pre-commit **completo** nos arquivos tocados desde o último prompt. Se reprovar, bloqueia o fim do turno com a saída. No máximo 2 bloqueios por prompt, para não prender a sessão. |
| Ledger de findings | `ReportFindings` + system prompt | Grava cada finding do `/code-review` por repo (os últimos 150). Uma seção no system prompt traz o checklist de julgamento e os findings recentes do repo. |

O pre-commit só roda em repo que tem `.pre-commit-config.yaml`. Sem ele, valem a trava de Bash, a varredura e o checklist.

`/preflight` mostra o que está ativo no repo e o que o ledger guardou.

## Limites

- **Só Linux e macOS.** Os caminhos são tratados como POSIX; no Windows (`C:\...`) as checagens não reconhecem o repositório e ficam desligadas.
- **Script inline que mistura leitura e escrita.** A trava julga os caminhos literais do script, sem saber qual é lido e qual é gravado. Um script que cita um caminho fora de repositório e grava num caminho calculado (`sys.argv[1]`, variável) dentro de um repositório passa. Um script que lê um arquivo do repositório e grava em `/tmp` é barrado.
- **`rm`, `ln`, `touch` e `git` não contam como escrita.** A trava mira edição de conteúdo; apagar e criar arquivo vazio ficam com o review.

## Configuração

Em `/config` ou em `pluginConfigs.preflight.options` no `settings.json`:

| Campo | Padrão | Para quê |
|---|---|---|
| `allowBashWrites` | `false` | Desliga a trava de escrita por Bash. |
| `precommitTimeoutMs` | `120000` | Teto de uma rodada do pre-commit. |

## Instalação

Num terminal, no prompt do Claude Code:

```
/plugin install preflight --marketplace welt-studio/welt-studio-plugins
```

Na primeira vez ele pergunta se adiciona o marketplace (`y`) e depois o escopo (Enter escolhe o de usuário, que vale para todas as sessões). Em seguida abre a tela das opções (`allowBashWrites`, `precommitTimeoutMs`).

## Desenvolvimento

Mod de function hooks em TypeScript. Só `hooks/register.ts` toca no `$`; a lógica fica em `hooks/lib/`, atrás da interface `Host` (`hooks/lib/host.ts`).

```
claude plugin validate plugins/preflight
tsc -p plugins/preflight          # depois que o engine carregou o mod uma vez e gerou .claude-plugin/types/
claude plugin test plugins/preflight
claude --plugin-dir plugins/preflight   # sessão com o mod carregado do disco
```

A API de mods é early access: revalidar e rodar os testes a cada release do Claude Code.
