# game-art

Gera assets de jogo pelo gerador de imagens do Codex CLI e os salva prontos no projeto Godot, Unity ou Unreal. Plataforma: Linux e WSL (Windows nativo está no roadmap).

## Tools

| Tool | O que faz |
| --- | --- |
| `generate_asset` | Gera um asset (ou até 3 variações), limpa e grava no projeto. Devolve o caminho, o tamanho, avisos de qualidade e um preview que o Claude vê. |
| `keep_variant` | Grava no projeto a variação escolhida de um run com várias. |
| `init_style` | Cria o guia de estilo do projeto (`.game-art/style.json`). |

## Painel `/game-art`

O painel abre sozinho quando há imagem para conferir: depois de cada geração e gravação, e no início da sessão se houver variações pendentes. Aberto assim, ele só se encaixa como barra lateral num terminal largo, a partir de 144 colunas. O comando `/game-art` abre um painel com os assets do projeto, lidos dos runs em `.game-art/runs/`. O painel sobrevive entre sessões.

- **Sem painel lateral:** onde a superfície não põe painéis, como a extensão do VS Code, o `/game-art` desenha a galeria na própria resposta, na conversa, com os mesmos botões e miniaturas.
- **Layout:** lista à esquerda e detalhe do item selecionado à direita. Abaixo de 90 colunas, os dois lados se empilham. Sem seleção, o detalhe mostra a primeira escolha pendente ou o asset mais novo.
- **Variações pendentes:** aparecem primeiro, lado a lado e com miniatura. **save x** grava a escolhida no projeto, o mesmo que o `keep_variant` faz. **open** abre em tamanho real.
- **Opções:** o detalhe de um run com variações mostra todas lado a lado. A gravada no projeto aparece como **chosen**; as outras, com **save**, que grava como outro arquivo (`nome-2.png`) sem substituir. Todas têm **open**.
- **Unchoose:** cada **chosen** mostra o arquivo que virou e tem **unchoose**, para desfazer um save feito sem querer.
  - O primeiro clique pergunta; só o **confirm** apaga.
  - Ele apaga do projeto o PNG e o arquivo que a engine criou ao lado: `.import` no Godot, `.meta` no Unity.
  - A variação volta a ser opção.
  - Só apaga arquivos que aquele run gravou.
- **Assets gravados:** vêm agrupados por tipo, do mais novo para o mais antigo. Clicar no nome mostra a miniatura no topo do painel, com **open** para o tamanho real:
  - aba do VS Code;
  - visualizador do Windows, pelo WSL;
  - `xdg-open`, no Linux.
- **Atualização:** o painel se atualiza sozinho a cada geração e gravação; **refresh** relê o disco.

A miniatura tem até 32×32 pixels.
- **No kitty e no Ghostty**, é o PNG real (elemento `Image`, protocolo gráfico do kitty).
- **Nos outros terminais**, é desenhada com células coloridas (`▀`, 2 pixels por célula), o que funciona em qualquer terminal, o do Windows incluído.
- **Na extensão do VS Code e no desktop**, é o PNG real dentro de um SVG.
- **Pixel art** pequena aparece pixel a pixel; imagens maiores são reduzidas.
- **Terminal em 256 cores:** é o caso do WSL no Windows Terminal, que não anuncia true color. Nesse modo, marrons e tons de pele saem trocados; o painel avisa e o **open** mostra a cor real. Para cores exatas, é opcional: `export COLORTERM=truecolor` no `~/.bashrc`.

## O que cada geração faz

1. **Acha o projeto** subindo a partir do diretório da sessão:
   - `project.godot` é Godot;
   - `Assets/` com `ProjectSettings/ProjectVersion.txt` é Unity;
   - `*.uproject` é Unreal.
2. **Aplica o guia de estilo**, se houver: estilo, pixel art, referências e tamanho por tipo. O que vem na chamada vence campo a campo.
3. **Gera a imagem** com `codex exec --ephemeral`, usando a ferramenta de imagem embutida do Codex (login do ChatGPT, sem API key). A geração acontece num run em `.game-art/runs/<nome>/<data-hora>/`.
   - Não fica rollout da sessão do Codex.
   - A cópia que o Codex guarda em `~/.codex/generated_images` é apagada.
   - Variações rodam no máximo 2 ao mesmo tempo.
4. **Limpa a imagem** com `scripts/process_image.py`, via `uv`, com o Pillow numa venv isolada:
   - recorta o fundo magenta da pixel art;
   - apara a borda vazia;
   - funde as bordas das texturas para repetirem;
   - encaixa no tamanho pedido (nearest em pixel art);
   - arredonda texturas para potência de 2.

   Também avisa sem bloquear quando:
   - o fundo não foi recortado;
   - o tom do chroma vazou para a arte;
   - a transparência pedida não veio.
5. **Grava o PNG** em `<pasta de arte da engine>/<tipo>/<nome>.png`, sem nunca sobrescrever: o nome ganha `-2`, `-3`... Com variações, nada entra no projeto até o `keep_variant`.
6. **Opcionais:**
   - `open: true` abre o PNG na hora, do mesmo jeito que o painel abre. Só quando você pede; o normal é escolher no painel.
   - `background: true` responde na hora. Ao terminar, o resultado entra como mensagem do plugin e abre um turno assim que a sessão estiver ociosa, para o Claude seguir sem esperar você. Há também notificação, e a status line mostra quantas gerações ainda rodam.

| Engine | Pasta de arte | Depois |
| --- | --- | --- |
| Godot | `assets/` | o editor cria o `.import` |
| Unity | `Assets/Art/` | o editor cria o `.meta` |
| Unreal | `SourceArt/` | importar pelo Content Browser |
| nenhuma | `art/` (no diretório da sessão) | — |

| Tipo | Subpasta | Fundo padrão | Pós-processamento |
| --- | --- | --- | --- |
| `sprite` | `sprites` | transparente | apara a borda |
| `icon` | `icons` | transparente | apara a borda |
| `ui` | `ui` | transparente | apara a borda |
| `texture` | `textures` | opaco | emenda seamless; potência de 2 sem tamanho pedido |
| `background` | `backgrounds` | opaco | — |
| `concept` | `concepts` | opaco | — |

## Pasta de trabalho `.game-art/`

```
.game-art/
  style.json   guia de estilo (versionado)
  .gitignore   ignora runs/
  .gdignore    o Godot não importa nada daqui
  runs/<nome>/<data-hora>/   run.json, prompt.txt, raw-a.png, codex-a.jsonl, preview-a.png, saved.json (e final-x.png com variações)
```

O Unity e o Unreal também não importam a pasta, porque ela fica fora de `Assets/` e de `Content/`.

Exemplo de `style.json`:

```json
{
  "style": "16-bit pixel art, 1px dark outline, muted earthy palette",
  "pixelArt": true,
  "references": ["art/seeds/hero.png"],
  "sizes": { "sprite": [64, 64], "icon": [32, 32] }
}
```

Campo desconhecido ou valor inválido é erro com o motivo, nunca ignorado. As referências contam muito: uma referência de cor forte puxa a paleta de tudo que for gerado com ela.

## Requisitos

- Codex CLI logado: `codex login status`.
- [uv](https://docs.astral.sh/uv/).

## Configuração

- `codexModel`: modelo passado a `codex exec -m`. Vazio usa o do `~/.codex/config.toml`.
- `generationTimeoutMs`: tempo máximo de uma geração. O padrão é 300000 e o teto é 600000.

## Limitações conhecidas

- **Geração em background e reload.** Uma geração em background não sobrevive a um reload do mod no meio dela, o que só acontece durante o desenvolvimento com hot reload. No carregamento seguinte, o mod avisa na conversa quais jobs pararam, para gerar de novo.

## Desenvolvimento

```bash
claude plugin validate <pasta do mod>
claude plugin test <pasta do mod>          # hooks e lib (TypeScript)
uv run scripts/test_process_image.py       # script de pixels (unittest), na pasta do mod
```
