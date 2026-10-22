// Fiação dos eventos para a lib. Só este arquivo toca no `$`.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, HookFailure, Register, RenderInput, RenderSurface } from 'claude-code'

import { generateAsset, keepVariant, outcomeText } from './lib/asset'
import type { AssetOutcome, AssetSettings } from './lib/asset'
import { findProject } from './lib/engine'
import { isPending, scanGallery, unchoose, whenOf } from './lib/gallery'
import { detailOf, fileName, inlineText, isSelected, PLACED_TEXT, relativeTo, savedByKind, summaryOf } from './lib/panel'
import type { GalleryEntry } from './lib/gallery'
import type { Host } from './lib/host'
import { KIND_SPECS, KINDS } from './lib/kinds'
import { KEEP_DESCRIPTION, KEEP_ID, KEEP_NAME, KEEP_SCHEMA, parseKeep } from './lib/keep'
import type { KeepInput } from './lib/keep'
import type { GameArtGalleryEntry, GameArtJobs, GameArtSelection } from '../types'
import { openerFor, openImage, outcomeBlocks } from './lib/preview'
import type { Opener } from './lib/preview'
import type { AssetInput } from './lib/request'
import { fitThumb, parseThumb, rasterOf, svgOf, svgWithPng, THUMB_SIDE } from './lib/thumb'
import type { Thumb } from './lib/thumb'
import { finalFile, previewFile, thumbFile } from './lib/workdir'
import type { Variant } from './lib/workdir'
import { INPUT_SCHEMA, parseInput, PLUGIN_NAME, TOOL_DESCRIPTION, TOOL_ID, TOOL_NAME } from './lib/request'
import { INIT_STYLE_DESCRIPTION, INIT_STYLE_ID, INIT_STYLE_NAME, INIT_STYLE_SCHEMA, initStyle } from './lib/style'

const DEFAULT_TIMEOUT_MS = 300000

const jobsAtom = atom({ plugin: 'game-art', key: 'jobs' } as const, [] as GameArtJobs)
const rootAtom = atom({ plugin: 'game-art', key: 'root' } as const, '')
const galleryAtom = atom({ plugin: 'game-art', key: 'gallery' } as const, [] as GameArtGalleryEntry[])
const confirmingAtom = atom({ plugin: 'game-art', key: 'confirming' } as const, null as string | null)
const selectedAtom = atom({ plugin: 'game-art', key: 'selected' } as const, null as GameArtSelection | null)
const inlineAtom = atom({ plugin: 'game-art', key: 'inline' } as const, '')
const inlineRepliesAtom = atom({ plugin: 'game-art', key: 'inlineReplies' } as const, 0)

/** Tamanho, em px de tela, de cada pixel da miniatura em SVG. */
const SVG_PIXEL = 4

const COMMAND = 'game-art'
/** Largura da galeria na conversa quando a superfície não informa a dela. */
const INLINE_COLUMNS = 100
const PANE = 'game-art-gallery'
const PANE_TITLE = 'game-art'
/** Quantos assets gravados o painel lista (os mais novos). */
const PANE_SAVED_LIMIT = 40
/** A partir desta largura o painel põe lista e detalhe lado a lado; abaixo, empilha. */
const WIDE_COLUMNS = 90
const LIST_MAX_COLUMNS = 52
const NAME_MAX_COLUMNS = 24

function hostOf($: EngineInterface): Host {
  return {
    run: (argv, options) => $.process.run(argv, options),
    exists: path => $.fs.exists(path),
    writeText: (path, text) => $.fs.write(path, text),
    readText: async path => {
      try {
        return await $.fs.read(path)
      } catch {
        return undefined
      }
    },
    readBase64: async path => {
      try {
        return (await $.fs.read(path, { as: 'bytes' })).base64
      } catch {
        return undefined
      }
    },
    list: async dir => {
      try {
        return (await $.fs.list(dir)).map(entry => entry.name)
      } catch {
        return []
      }
    },
  }
}

/** Onde o Codex guarda o estado dele: `CODEX_HOME`, ou `~/.codex`. */
async function codexHomeOf($: EngineInterface): Promise<string> {
  const configured = await $.env.get('CODEX_HOME')
  if (configured) return configured
  const home = await $.env.get('HOME')
  return home ? `${home}/.codex` : ''
}

/** `surface`: a de quem desenhou o clique; sem ela (uma tool), só o ambiente diz. */
async function openerOf($: EngineInterface, surface?: RenderSurface): Promise<Opener> {
  const procVersion = await $.fs.read('/proc/version').catch(() => undefined)
  return openerFor(surface, await $.env.get('TERM_PROGRAM'), procVersion)
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

type Configured = Pick<AssetSettings, 'codexModel' | 'generationTimeoutMs'>

async function settingsOf($: EngineInterface, configured: Configured): Promise<AssetSettings> {
  return { ...configured, pluginRoot: $.plugin.root, cwd: await $.session.cwd(), codexHome: await codexHomeOf($), now: () => new Date() }
}

/** Relê os runs do projeto para o painel; a escrita no estado redesenha quem lê. */
async function refreshGallery($: EngineInterface): Promise<void> {
  const host = hostOf($)
  const root = (await findProject(host, await $.session.cwd())).root
  await update($, rootAtom, () => root)
  const entries = await scanGallery(host, root)
  await update($, galleryAtom, () => entries)
}

/**
 * Mostra o painel quando há imagem para conferir. Aberto sem o usuário pedir, o engine só o encaixa como barra
 * lateral num terminal largo; num estreito ele espera, e o /game-art abre na hora.
 */
function showPane($: EngineInterface): void {
  void $.ui.open({ id: PANE, title: PANE_TITLE }).catch(() => undefined)
}

/**
 * Sem true color o Claude Code reduz cada cor à mais próxima de 256, e marrons e tons de pele não existem lá. Testado:
 * nem a cor mais próxima por percepção nem dithering salvam pixel art de 32 px, então o painel só avisa.
 */
async function isTrueColor($: EngineInterface): Promise<boolean> {
  const colorterm = (await $.env.get('COLORTERM'))?.toLowerCase()
  return colorterm === 'truecolor' || colorterm === '24bit'
}

/** O que o painel desenha de uma variação: os pixels da miniatura e, fora do terminal, o PNG do preview. */
type Picture = { thumb: Thumb | undefined; png: string | undefined }
const NO_PICTURE: Picture = { thumb: undefined, png: undefined }

/**
 * O `Image` mostra o PNG de verdade só onde o terminal fala o protocolo gráfico do kitty (kitty, Ghostty); nos outros
 * desenha apenas o texto alternativo, então o painel só o usa quando reconhece um desses e fica no Raster no resto.
 */
async function hasKittyGraphics($: EngineInterface): Promise<boolean> {
  if (await $.env.get('KITTY_WINDOW_ID')) return true
  if ((await $.env.get('TERM')) === 'xterm-kitty') return true
  return (await $.env.get('TERM_PROGRAM'))?.toLowerCase() === 'ghostty'
}

async function loadPicture($: EngineInterface, runDir: string, variant: Variant, withPng: boolean): Promise<Picture> {
  const host = hostOf($)
  const thumb = parseThumb(await host.readText(`${runDir}/${thumbFile(variant)}`))
  return { thumb, png: withPng && thumb ? await host.readBase64(`${runDir}/${previewFile(variant)}`) : undefined }
}

/** Clique no painel: abre a imagem no visualizador (aba do VS Code, Windows pelo WSL, ou xdg-open). */
async function openFromPane($: EngineInterface, surface: RenderSurface, path: string): Promise<void> {
  const failed = await openImage(hostOf($), await openerOf($, surface), path)
  if (failed) $.ui.toast(`game-art: ${failed}`)
}

/** Clique em "gravar" no painel: o mesmo keep_variant que o Claude chama. */
async function keepFromPane($: EngineInterface, configured: Configured, runDir: string, variant: Variant): Promise<void> {
  const outcome = await keepVariant(hostOf($), await settingsOf($, configured), runDir, variant, undefined)
  if (!outcome.ok) {
    $.ui.toast(`game-art: ${outcome.error}`)
    return
  }
  await settle($, outcome, false)
  if (outcome.kind === 'asset') {
    const { path } = outcome.report
    await update($, selectedAtom, () => ({ runDir, variant, path }))
  }
}

/** Segundo clique do "unchoose" no painel: tira o arquivo do projeto e a variação volta a ser opção. */
async function unchooseFromPane($: EngineInterface, runDir: string, path: string): Promise<void> {
  await update($, confirmingAtom, () => null)
  const outcome = await unchoose(hostOf($), runDir, path)
  $.ui.toast(outcome.ok ? `game-art: removed ${outcome.removed.join(', ') || path}` : `game-art: ${outcome.error}`)
  await refreshGallery($)
}

/** O que vem depois de uma geração que saiu: abre o PNG salvo se pedido, avisa e atualiza o painel. */
async function settle($: EngineInterface, outcome: AssetOutcome, open: boolean): Promise<void> {
  if (!outcome.ok) return
  await refreshGallery($)
  showPane($)
  if (outcome.kind === 'asset') {
    if (open) {
      const failed = await openImage(hostOf($), await openerOf($), outcome.report.path)
      if (failed) outcome.report.warnings.push(failed)
    }
    $.ui.toast(`game-art: ${outcome.report.path}`)
  } else {
    $.ui.toast(`game-art: ${outcome.report.results.filter(result => result.ok).length} variants ready to choose`)
  }
}

/** A resposta de generate_asset e keep_variant: os blocos com o relatório e o preview. */
async function answer($: EngineInterface, outcome: AssetOutcome, open: boolean) {
  if (!outcome.ok) return { deny: `game-art: ${outcome.error}` }
  await settle($, outcome, open)
  return { result: await outcomeBlocks(hostOf($), outcome) }
}

function showJobs($: EngineInterface, jobs: GameArtJobs): void {
  $.ui.status(jobs.length > 0 ? `game-art: ${jobs.length} generating in the background (${jobs.join(', ')})` : undefined)
}

function withoutOne(jobs: GameArtJobs, name: string): GameArtJobs {
  const index = jobs.indexOf(name)
  return index < 0 ? jobs : [...jobs.slice(0, index), ...jobs.slice(index + 1)]
}

/**
 * Um reload do mod (ou do worker) descarta o ambiente que acompanhava os jobs, e o Codex deles morre junto. O `$.state`
 * fica: os nomes que sobraram ali no `session.start` são de jobs que não vão terminar, e a conversa precisa saber.
 */
async function reportInterruptedJobs($: EngineInterface): Promise<void> {
  const orphans = await read($, jobsAtom)
  if (orphans.length === 0) return
  showJobs($, await update($, jobsAtom, () => []))
  const names = orphans.map(name => `"${name}"`).join(', ')
  const text = `game-art background job${orphans.length === 1 ? '' : 's'} ${names} stopped before finishing (the mod reloaded); nothing was saved into the project. Run generate_asset again for ${orphans.length === 1 ? 'it' : 'them'}.`
  await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
  $.ui.toast(`game-art: ${orphans.length} background job${orphans.length === 1 ? '' : 's'} interrupted`)
}

/**
 * Uma geração que segue depois que a tool respondeu. Ao terminar, o resultado vira um prompt do plugin: um turno
 * próprio assim que a sessão estiver ociosa (depois do turno em andamento, se houver), para o Claude seguir com o
 * asset sem esperar o usuário. O usuário também recebe toast e notificação.
 */
async function runInBackground($: EngineInterface, settings: AssetSettings, request: AssetInput): Promise<void> {
  try {
    showJobs($, await update($, jobsAtom, jobs => [...jobs, request.name]))
    let text: string
    try {
      const outcome = await generateAsset(hostOf($), settings, request)
      await settle($, outcome, request.open)
      text = outcomeText(outcome)
    } catch (error) {
      text = `Failed internally: ${messageOf(error)}`
    }
    showJobs($, await update($, jobsAtom, jobs => withoutOne(jobs, request.name)))
    const headline = `game-art background job "${request.name}" finished`
    await $.ui.notify(headline, { title: 'game-art' })
    await $.prompt.submit({ text: `${headline}.\n${text}` })
  } catch {
    // O ambiente deste job acabou (reload, fim da sessão): o `$` recusa tudo. O nome que ficou no estado é
    // o aviso: o session.start do próximo carregamento o reporta como interrompido.
  }
}

/**
 * A galeria: o painel lateral a desenha e, onde a superfície não põe painéis (a extensão do VS Code), a resposta do
 * /game-art na conversa.
 */
async function galleryTree($: EngineInterface, e: RenderInput<'Pane' | 'CommandOutput'>, configured: Configured, available: number) {
  const elements = $.ui.resolve(e)
  const { Box, Button, Text } = elements
  const root = await read($, rootAtom)
  const gallery = await read($, galleryAtom)
  const jobs = await read($, jobsAtom)
  const detail = detailOf(gallery, await read($, selectedAtom))
  const confirming = await read($, confirmingAtom)
  const pending = gallery.filter(isPending)
  const sections = savedByKind(gallery, PANE_SAVED_LIMIT)
  const columns = Math.max(24, available)
  const isWide = columns >= WIDE_COLUMNS
  const listWidth = isWide ? Math.min(LIST_MAX_COLUMNS, Math.floor(columns * 0.4)) : columns
  const detailWidth = isWide ? columns - listWidth - 1 : columns
  // Largura útil de dentro de uma caixa com borda e uma coluna de respiro de cada lado.
  const inner = (width: number) => Math.max(8, width - 4)

  const kittyGraphics = e.surface === 'terminal' && (await hasKittyGraphics($))
  const withPng = e.surface !== 'terminal' || kittyGraphics
  const approximate = e.surface === 'terminal' && !kittyGraphics && !(await isTrueColor($))
  // Uma miniatura desenhada: Image no kitty/Ghostty, Raster nos outros terminais, SVG fora do terminal.
  const view = ({ thumb, png }: Picture, side: number, key: string, alt: string) => {
    if (thumb === undefined) return <Text dimColor>(no preview)</Text>
    const fitted = fitThumb(thumb, side)
    if (e.surface === 'terminal') {
      const { Image, Raster } = $.ui.resolve(e as typeof e & { surface: 'terminal' })
      const cells = rasterOf(fitted)
      if (kittyGraphics && png) return <Image key={key} source={{ png }} columns={cells.columns} rows={cells.rows} alt={alt} />
      return <Raster key={key} {...cells} />
    }
    if (!('Svg' in elements)) return <Text dimColor>{alt}</Text>
    // O PNG real quando cabe no Svg; senão, os pixels da miniatura.
    const source = (png && svgWithPng(png, fitted, SVG_PIXEL)) || svgOf(fitted, SVG_PIXEL)
    return <elements.Svg source={source} alt={alt} />
  }
  // As variações que o detalhe mostra: todas as opções do run, ou só a do asset quando o run teve uma.
  const shown = detail === undefined ? [] : detail.entry.choices.length > 1 ? detail.entry.choices : detail.kind === 'saved' ? [detail.item.variant] : []
  const pictures = await Promise.all(shown.map(variant => loadPicture($, detail!.entry.runDir, variant, withPng)))
  const nameWidth = Math.min(
    NAME_MAX_COLUMNS,
    Math.max(8, ...pending.map(entry => entry.name.length), ...sections.flatMap(section => section.rows.map(row => fileName(row.item.path).length))),
  )
  const marker = (isOn: boolean) => (isOn ? '▸ ' : '  ')
  // Marcador, nome, tamanho (9) e data (11): a data só entra se a linha inteira couber, para nunca quebrar.
  const showDate = inner(listWidth) >= 2 + nameWidth + 1 + 9 + 11
  const when = (id: string) => (showDate ? ` ${whenOf(id)}` : '')

  // Uma opção de um run: miniatura e, embaixo, "chosen" (com o arquivo e o unchoose) quando já está no projeto, ou
  // "save" quando não; open nas duas. O unchoose apaga um arquivo do projeto, então pede um segundo clique.
  const option = (entry: GalleryEntry, variant: Variant, picture: Picture, side: number) => {
    const chosen = entry.saved.find(item => item.variant === variant)
    const isConfirming = chosen !== undefined && confirming === chosen.path
    return (
      <Box key={`choice-${entry.runDir}-${variant}`} flexDirection="column">
        {view(picture, side, `thumb-${entry.runDir}-${variant}`, `${entry.name} ${variant}`)}
        <Box flexDirection="row" columnGap={1}>
          {chosen ? (
            <Text>
              <Text bold color="green">{`chosen ${variant}`}</Text>
              <Text dimColor> · {fileName(chosen.path)}</Text>
            </Text>
          ) : (
            <Button key={`keep-${entry.runDir}-${variant}`} variant="primary" onPress={() => void keepFromPane($, configured, entry.runDir, variant)}>
              {`save ${variant}`}
            </Button>
          )}
          <Button key={`view-${entry.runDir}-${variant}`} plain onPress={() => void openFromPane($, e.surface, chosen?.path ?? `${entry.runDir}/${finalFile(variant)}`)}>
            open
          </Button>
        </Box>
        {chosen && !isConfirming && (
          <Button key={`unchoose-${entry.runDir}-${variant}`} plain onPress={() => void update($, confirmingAtom, () => chosen.path)}>
            unchoose
          </Button>
        )}
        {chosen && isConfirming && (
          <Box flexDirection="row" columnGap={1}>
            <Text color="red">{`delete ${fileName(chosen.path)}?`}</Text>
            <Button key={`confirm-${entry.runDir}-${variant}`} onPress={() => void unchooseFromPane($, entry.runDir, chosen.path)}>
              confirm
            </Button>
            <Button key={`cancel-${entry.runDir}-${variant}`} plain onPress={() => void update($, confirmingAtom, () => null)}>
              cancel
            </Button>
          </Box>
        )}
      </Box>
    )
  }
  const options = (entry: GalleryEntry) => (
    <Box flexDirection="column" marginTop={1}>
      {shown.length > 1 && <Text bold>options</Text>}
      <Box flexDirection="row" flexWrap="wrap" columnGap={2} marginTop={1}>
        {shown.map((variant, slot) => option(entry, variant, pictures[slot] ?? NO_PICTURE, sideFor(shown.length)))}
      </Box>
    </Box>
  )
  const sideFor = (count: number) => Math.max(8, Math.min(THUMB_SIDE, Math.floor((inner(detailWidth) - 2 * (count - 1)) / count)))

  const list = (
    <Box key="list" flexDirection="column" width={listWidth} borderStyle="round" borderDimColor paddingX={1}>
      {pending.length > 0 && <Text bold color="yellow">choose a variant</Text>}
      {pending.map(entry => {
        const isOn = isSelected(detail, entry)
        return (
          <Button key={`pending-${entry.runDir}`} plain onPress={() => void update($, selectedAtom, () => ({ runDir: entry.runDir, variant: 'a' as const }))}>
            <Text bold={isOn}>{marker(isOn)}{entry.name.padEnd(nameWidth)}</Text> <Text dimColor>{`${entry.choices.length} options`.padEnd(9)}{when(entry.id)}</Text>
          </Button>
        )
      })}
      {sections.map(section => (
        <Box key={`kind-${section.kind}`} flexDirection="column" marginTop={pending.length > 0 || section !== sections[0] ? 1 : 0}>
          <Text bold>{KIND_SPECS[section.kind].folder} <Text dimColor>({section.rows.length})</Text></Text>
          {section.rows.map(({ entry, item }) => {
            const isOn = isSelected(detail, entry, item)
            return (
              <Button
                key={`open-${item.path}`}
                plain
                onPress={() => void update($, selectedAtom, () => ({ runDir: entry.runDir, variant: item.variant, path: item.path }))}
              >
                <Text bold={isOn}>{marker(isOn)}{fileName(item.path).padEnd(nameWidth)}</Text> <Text dimColor>{`${item.width}x${item.height}`.padEnd(9)}{when(entry.id)}</Text>
              </Button>
            )
          })}
        </Box>
      ))}
      {gallery.length === 0 && <Text dimColor>Nothing generated in this project yet.</Text>}
    </Box>
  )

  const card = (
    <Box key="detail" flexDirection="column" width={detailWidth} borderStyle="round" borderDimColor paddingX={1}>
      {detail === undefined && <Text dimColor>Ask Claude for an asset; it shows up here.</Text>}
      {detail?.kind === 'saved' && (
        <Box flexDirection="column">
          <Box flexDirection="row" columnGap={1}>
            <Text bold>{fileName(detail.item.path)}</Text>
            <Button key="open-selected" variant="primary" onPress={() => void openFromPane($, e.surface, detail.item.path)}>open</Button>
          </Box>
          <Text dimColor>
            {detail.entry.kind} · {detail.item.width}x{detail.item.height} · {whenOf(detail.entry.id)} · {relativeTo(root, detail.item.path)}
          </Text>
          {options(detail.entry)}
          {shown.length > 1 && <Text dimColor>saving another option adds a file next to it; nothing is replaced</Text>}
        </Box>
      )}
      {detail?.kind === 'pending' && (
        <Box flexDirection="column">
          <Text bold>
            {detail.entry.name} <Text dimColor>· choose one to save into the project</Text>
          </Text>
          {options(detail.entry)}
        </Box>
      )}
    </Box>
  )

  return (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between">
        <Text>
          <Text bold>game-art</Text>
          <Text dimColor> · {root === '' ? 'no project' : fileName(root)} · {summaryOf(gallery)}</Text>
        </Text>
        <Button key="refresh" plain onPress={() => void refreshGallery($)}>refresh</Button>
      </Box>
      {jobs.length > 0 && <Text color="yellow">generating: {jobs.join(', ')}</Text>}
      <Box flexDirection={isWide ? 'row' : 'column-reverse'} columnGap={1} marginTop={1}>
        {list}
        {card}
      </Box>
      {approximate && detail !== undefined && (
        <Text dimColor>approximate colours (terminal in 256 colours): open shows the real ones</Text>
      )}
    </Box>
  )
}

/** O `.catch` das tools: um erro interno vira recusa com o motivo, nunca uma exceção solta. */
function internalError($: unknown, e: unknown, next: { error: HookFailure }) {
  return { deny: `game-art failed internally (${next.error.kind}): ${next.error.message ?? 'no detail'}` }
}

export const register: Register = (on, options) => {
  const configured: Configured = {
    codexModel: typeof options.codexModel === 'string' ? options.codexModel.trim() : '',
    generationTimeoutMs: typeof options.generationTimeoutMs === 'number' ? options.generationTimeoutMs : DEFAULT_TIMEOUT_MS,
  }

  on('session.start', async ($, e, next) => {
    const tools = [
      { name: TOOL_NAME, id: TOOL_ID, description: TOOL_DESCRIPTION, inputSchema: INPUT_SCHEMA },
      { name: KEEP_NAME, id: KEEP_ID, description: KEEP_DESCRIPTION, inputSchema: KEEP_SCHEMA },
      { name: INIT_STYLE_NAME, id: INIT_STYLE_ID, description: INIT_STYLE_DESCRIPTION, inputSchema: INIT_STYLE_SCHEMA },
    ]
    for (const { id, ...spec } of tools) {
      const { tool } = await $.tool.register({ ...spec, isDeferred: false })
      // Os matchers do tool.call usam ids montados com PLUGIN_NAME; um plugin.json com outro nome deixaria a tool sem resposta.
      if (tool !== id) throw new Error(`registered as ${tool}, expected ${id}: plugin.json name must be ${PLUGIN_NAME}`)
    }
    await reportInterruptedJobs($)
    await $.command.register({ name: COMMAND, description: 'Open the game-art panel: the generated assets of this project, to view and to pick variants' })
    await refreshGallery($)
    if ((await read($, galleryAtom)).some(isPending)) showPane($)
    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    await refreshGallery($)
    const opened = await $.ui.open({ id: PANE, title: PANE_TITLE })
    // O painel que não foi posto fica aberto esperando, como o engine pede: uma superfície que põe painéis o desenha
    // quando chegar. Até lá a galeria vai na resposta do comando (o render de CommandOutput a desenha).
    const text = opened.isPlaced ? PLACED_TEXT : inlineText(opened.reason, await update($, inlineRepliesAtom, count => count + 1))
    await update($, inlineAtom, () => (opened.isPlaced ? '' : text))
    return { text }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => galleryTree($, e, configured, e.props.bodyColumns))

  // Só a resposta mais recente do /game-art sem painel vira a galeria; as anteriores, e qualquer uma depois que o
  // painel foi posto, ficam como o texto que são. A largura é a do viewport: o CommandOutput não informa a da coluna.
  on('ui.render', { component: 'CommandOutput', props: { command: COMMAND } }, async ($, e, next) =>
    e.props.text === (await read($, inlineAtom)) ? galleryTree($, e, configured, e.viewport?.columns ?? INLINE_COLUMNS) : next(e),
  )

  on('tool.call', { tool: INIT_STYLE_ID }, async ($, e) => {
    const { tool, tool_use_id, ...input } = e
    const outcome = await initStyle(hostOf($), await $.session.cwd(), input)
    if (!outcome.ok) return { deny: `game-art: ${outcome.error}` }
    return { result: `Wrote ${outcome.path}:\n${outcome.text}` }
  }).catch(internalError)

  on('tool.call', { tool: TOOL_ID }, async ($, e) => {
    const { tool, tool_use_id, ...input } = e
    let request: AssetInput
    try {
      request = parseInput(input)
    } catch (error) {
      return { deny: `game-art: ${messageOf(error)}` }
    }
    const settings = await settingsOf($, configured)
    if (request.background) {
      void runInBackground($, settings, request)
      return { result: `Started generating "${request.name}" in the background. The result arrives as a message when it finishes; the user also gets a notification. Do not wait or poll for it.` }
    }
    const plural = request.variants > 1 ? ` (${request.variants} variants)` : ''
    $.ui.status(`game-art: generating ${request.name}${plural} with Codex`)
    try {
      return await answer($, await generateAsset(hostOf($), settings, request), request.open)
    } finally {
      $.ui.status(undefined)
    }
  }).catch(internalError)

  on('tool.call', { tool: KEEP_ID }, async ($, e) => {
    const { tool, tool_use_id, ...input } = e
    let keep: KeepInput
    try {
      keep = parseKeep(input)
    } catch (error) {
      return { deny: `game-art: ${messageOf(error)}` }
    }
    return answer($, await keepVariant(hostOf($), await settingsOf($, configured), keep.run, keep.variant, keep.dest), keep.open)
  }).catch(internalError)
}
