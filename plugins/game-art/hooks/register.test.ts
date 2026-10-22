import type { On, ProcessRunResult } from 'claude-code'
import { describe, expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

import { PLUGIN_NAME, TOOL_ID, TOOL_NAME } from './lib/request'
import { KEEP_ID, KEEP_NAME } from './lib/keep'
import { INIT_STYLE_ID, INIT_STYLE_NAME } from './lib/style'

const ROOT = '/games/demo'
/** Um PNG 1x1 completo: o engine confere o cabeçalho antes de desenhar um Image. */
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
const THREAD = '01a1262e-2137-7133-b979-7667e6d96566'
const exited = (exitCode: number, stdout = '', stderr = ''): ProcessRunResult => ({
  exitCode,
  stdout,
  stderr,
  isStdoutTruncated: false,
  isStderrTruncated: false,
})

type World = {
  env: Record<string, string>
  submitted: string[]
  appended: string[]
  notified: string[]
  statuses: (string | undefined)[]
  files: Set<string>
  written: Map<string, string>
  registered: string[]
  runs: { argv: readonly string[]; stdin?: string; timeoutMs?: number }[]
}

/** O mundo abaixo do plugin: um projeto Godot em /games/demo, Codex e uv respondidos pela fixture. */
function world(on: On): World {
  const state: World = {
    env: { HOME: '/home/me' },
    submitted: [],
    appended: [],
    notified: [],
    statuses: [],
    files: new Set([`${ROOT}/project.godot`]),
    written: new Map(),
    registered: [],
    runs: [],
  }
  on('session.append', ($, e, next) => {
    state.appended.push(e.message.content.map(block => ('text' in block ? block.text : '')).join(''))
    return next(e)
  })
  on('prompt.submit', ($, e) => {
    state.submitted.push(e.text)
    return { text: e.text }
  })
  on('ui.notify', ($, e) => {
    state.notified.push(e.text)
    return { value: { isSent: true, channel: 'terminal_bell' } }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: ROOT }))
  on('tool.register', ($, e) => {
    state.registered.push(e.name)
    return { value: { tool: `mcp__${PLUGIN_NAME}__${e.name}` } }
  })
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.status', ($, e) => {
    state.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('fs.exists', ($, e) => ({ value: state.files.has(e.path) }))
  // Diretórios existem implícitos: a primeira parte de cada caminho conhecido abaixo do pedido.
  on('fs.list', ($, e) => {
    const known = [...state.files, ...state.written.keys()].filter(path => path.startsWith(`${e.path}/`))
    const names = [...new Set(known.map(path => path.slice(e.path.length + 1).split('/')[0] ?? ''))]
    return { value: names.map(name => ({ name, kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false })) }
  })
  on('fs.write', ($, e) => {
    state.written.set(e.path, e.text)
    state.files.add(e.path)
    return { value: undefined }
  })
  on('env.get', ($, e) => ({ value: state.env[e.name] }))
  // Texto gravado volta como texto (run.json, style.json); outro arquivo presente volta como bytes de PNG.
  on('fs.read', ($, e) => {
    const text = state.written.get(e.path)
    if (text !== undefined) return { value: text }
    return state.files.has(e.path) ? { value: { base64: PNG } } : { deny: `ENOENT ${e.path}` }
  })
  on('process.run', ($, e) => {
    state.runs.push({ argv: e.argv, stdin: e.init?.stdin, timeoutMs: e.init?.timeoutMs })
    if (e.argv[0] === 'codex') {
      const output = /as (raw-\w\.png)/.exec(e.init?.stdin ?? '')?.[1] ?? 'missing'
      state.files.add(`${e.argv[e.argv.indexOf('-C') + 1]}/${output}`)
      return { value: exited(0, JSON.stringify({ type: 'thread.started', thread_id: THREAD })) }
    }
    if (e.argv[0] === 'uv') {
      state.files.add(e.argv[e.argv.indexOf('--preview') + 1] ?? '')
      return { value: exited(0, '{"width": 32, "height": 32, "warnings": []}') }
    }
    if (e.argv[0] === 'rm') {
      for (const path of e.argv.slice(e.argv.indexOf('--') + 1)) state.files.delete(path)
      return { value: exited(0) }
    }
    return { deny: `unexpected command ${e.argv.join(' ')}` }
  })
  return state
}

async function start($: Engine): Promise<void> {
  await $.session.start({ cwd: ROOT, surface: null, isInteractive: false })
}

describe('generate_asset tool', () => {
  test('is registered at session start', async ($, on) => {
    const fixture = world(on)
    await start($)
    expect(fixture.registered).toEqual([TOOL_NAME, KEEP_NAME, INIT_STYLE_NAME])
  })

  test('init_style writes the guide into the project work folder', async ($, on) => {
    const fixture = world(on)
    await start($)
    const ran = await $.tool.call({ tool: INIT_STYLE_ID, style: 'muted 16-bit', pixelArt: true })
    expect(ran.deny).toBeUndefined()
    expect(ran.result).toContain(`Wrote ${ROOT}/.game-art/style.json`)
    expect(JSON.parse(fixture.written.get(`${ROOT}/.game-art/style.json`) ?? '')).toEqual({ style: 'muted 16-bit', pixelArt: true })
  })

  test('a call generates, processes and answers the saved path', async ($, on) => {
    const fixture = world(on)
    await start($)
    const ran = await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'icon', pixelArt: true })
    expect(ran.deny).toBeUndefined()
    // Tool registrada responde texto ou blocos: um objeto é recusado pelo engine (o kit não confere o formato).
    expect(ran.result).toEqual([
      { type: 'text', text: expect.stringContaining(`Saved ${ROOT}/assets/icons/potion.png (32x32 PNG).`) },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } },
    ])
    expect(fixture.runs.map(run => run.argv[0])).toEqual(['codex', 'rm', 'uv'])
    expect(fixture.runs[1]?.argv).toContain(`/home/me/.codex/generated_images/${THREAD}`)
    expect([...fixture.written.keys()]).toContain(`${ROOT}/.game-art/.gitignore`)
    expect(fixture.runs[0]?.timeoutMs).toBe(300000)
  })

  test('the configured model and timeout reach codex exec', { options: { codexModel: 'gpt-x', generationTimeoutMs: 900000 } }, async ($, on) => {
    const fixture = world(on)
    await start($)
    await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'icon' })
    const codex = fixture.runs.find(run => run.argv[0] === 'codex')
    expect(codex?.argv).toContain('gpt-x')
    expect(codex?.timeoutMs).toBe(600000)
  })

  test('variants stay in the run until keep_variant saves the chosen one', async ($, on) => {
    const fixture = world(on)
    await start($)
    const ran = await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'icon', variants: 2 })
    expect(ran.deny).toBeUndefined()
    const blocks = ran.result as { type: string; text?: string }[]
    expect(blocks[0]?.text).toContain('Generated 2 of 2 variants')
    expect(blocks.filter(block => block.type === 'image')).toHaveLength(2)
    expect(fixture.runs.filter(run => run.argv[0] === 'codex')).toHaveLength(2)
    const outputs = fixture.runs.filter(run => run.argv[0] === 'uv').map(run => run.argv[run.argv.indexOf('--out') + 1])
    expect(outputs.every(path => path?.includes('/.game-art/runs/potion/'))).toBe(true)

    const runDir = /in (\S+); nothing was saved/.exec(blocks[0]?.text ?? '')?.[1] ?? ''
    const kept = await $.tool.call({ tool: KEEP_ID, run: runDir, variant: 'b' })
    expect(kept.deny).toBeUndefined()
    const keep = fixture.runs.at(-1)
    expect(keep?.argv).toContain(`${runDir}/raw-b.png`)
    expect(keep?.argv).toContain(`${ROOT}/assets/icons/potion.png`)
  })

  test('background answers at once and wakes the session with the result when it finishes', async ($, on) => {
    const fixture = world(on)
    await start($)
    const ran = await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'icon', background: true })
    expect(ran.result).toContain('Started generating "potion" in the background')
    // O job segue depois da resposta: espera ele chegar na conversa.
    for (let tick = 0; tick < 5000 && fixture.submitted.length === 0; tick++) await Promise.resolve()
    expect(fixture.submitted[0]).toContain('game-art background job "potion" finished')
    expect(fixture.submitted[0]).toContain(`Saved ${ROOT}/assets/icons/potion.png`)
    expect(fixture.appended).toEqual([])
    expect(fixture.notified).toEqual(['game-art background job "potion" finished'])
    expect(fixture.statuses).toContain('game-art: 1 generating in the background (potion)')
    expect(fixture.statuses.at(-1)).toBeUndefined()
  })

  test('a background job still listed when the module starts again is reported as interrupted', async ($, on) => {
    const fixture = world(on)
    // O Codex deste job nunca responde: é o job que um reload deixaria pela metade.
    on('process.run', { argv: ['codex'] } as never, () => new Promise(() => {}))
    await start($)
    await $.tool.call({ tool: TOOL_ID, prompt: 'a shield', name: 'shield', kind: 'icon', background: true })
    for (let tick = 0; tick < 500 && !fixture.statuses.some(text => text?.includes('(shield)')); tick++) await Promise.resolve()
    expect(fixture.statuses).toContain('game-art: 1 generating in the background (shield)')

    // O reload roda o register de novo e dispara session.start; o $.state continua.
    await start($)
    expect(fixture.appended.at(-1)).toContain('game-art background job "shield" stopped before finishing (the mod reloaded)')
    expect(fixture.statuses.at(-1)).toBeUndefined()
    await start($)
    expect(fixture.appended.filter(text => text.includes('stopped before finishing'))).toHaveLength(1)
  })

  test('invalid input is refused before anything runs', async ($, on) => {
    const fixture = world(on)
    await start($)
    const ran = await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'mesh' })
    expect(ran.deny).toContain('kind must be one of')
    expect(fixture.runs).toEqual([])
  })
})

describe('gallery pane', () => {
  const RUNS = `${ROOT}/.game-art/runs`
  const SAVED_RUN = `${RUNS}/barrel/20261010-151126`
  const PENDING_RUN = `${RUNS}/cobblestone/20261010-113451`
  const record = (kind: string, variants: string[]) => JSON.stringify({ request: { kind, name: 'x', references: [] }, engine: 'godot', projectRoot: ROOT, variants })

  function withRuns(fixture: World): void {
    fixture.written.set(`${SAVED_RUN}/run.json`, record('sprite', ['a']))
    fixture.written.set(`${SAVED_RUN}/saved.json`, JSON.stringify([{ path: `${ROOT}/assets/sprites/barrel.png`, width: 48, height: 48, variant: 'a' }]))
    fixture.written.set(`${PENDING_RUN}/run.json`, JSON.stringify({ ...JSON.parse(record('texture', ['a', 'b'])), request: { kind: 'texture', name: 'cobblestone', references: [], transparent: false, pixelArt: true } }))
    for (const file of ['raw-a.png', 'raw-b.png', 'final-a.png', 'final-b.png', 'preview-a.png', 'preview-b.png']) fixture.files.add(`${PENDING_RUN}/${file}`)
    for (const thumb of [`${SAVED_RUN}/thumb-a.json`, `${PENDING_RUN}/thumb-a.json`, `${PENDING_RUN}/thumb-b.json`]) fixture.written.set(thumb, THUMB)
  }
  // 2x2: vermelho, transparente / azul, verde.
  const THUMB = JSON.stringify({ width: 2, height: 2, rgba: btoa(String.fromCharCode(255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 255, 255, 0, 255, 0, 255)) })

  const PANE = { component: 'Pane', requestId: 'game-art-gallery', props: { title: 'game-art', isFocused: true, bodyColumns: 120 } as never } as const

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`shows the pending variants first, and the clicked asset in the detail (${surface})`, async ($, on) => {
      const fixture = world(on)
      withRuns(fixture)
      await start($)
      const ui = await $.ui.mount({ plugin: PLUGIN_NAME, surface, ...PANE })
      expect(await ui.find({ type: 'Text', text: /choose a variant/ })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'sprites' })).toBeDefined()
      const picture = surface === 'terminal' ? 'Raster' : 'Svg'
      expect(await ui.findAll({ type: picture })).toHaveLength(2)
      // Sem COLORTERM o terminal está em 256 cores: o painel avisa que a miniatura é aproximada.
      const warning = await ui.find({ type: 'Text', text: /approximate colours/ })
      expect(warning === undefined).toBe(surface !== 'terminal')
      // Fora do terminal a miniatura é o PNG real do preview, dentro do SVG.
      if (surface === 'desktop') expect(JSON.stringify(await ui.drawn())).toContain(`data:image/png;base64,${PNG}`)

      expect(await ui.find({ type: 'Text', text: /choose one to save/ })).toBeDefined()
      await ui.press({ key: `open-${ROOT}/assets/sprites/barrel.png` })
      expect(await ui.findAll({ type: picture })).toHaveLength(1)
      expect(await ui.find({ type: 'Text', text: 'assets/sprites/barrel.png' })).toBeDefined()
      // Voltar para a escolha pendente pela lista.
      await ui.press({ key: `pending-${PENDING_RUN}` })
      expect(await ui.findAll({ type: picture })).toHaveLength(2)
      await ui.press({ key: `open-${ROOT}/assets/sprites/barrel.png` })
      await ui.press({ key: 'open-selected' })
      expect(fixture.runs.at(-1)?.argv).toEqual(['xdg-open', `${ROOT}/assets/sprites/barrel.png`])
      await ui.press({ key: `pending-${PENDING_RUN}` })
      await ui.press({ key: `view-${PENDING_RUN}-b` })
      expect(fixture.runs.at(-1)?.argv).toEqual(['xdg-open', `${PENDING_RUN}/final-b.png`])
      await ui.unmount()
    })
  }

  test('in kitty or Ghostty the pane shows the real PNG instead of cells', async ($, on) => {
    const fixture = world(on)
    withRuns(fixture)
    fixture.env.TERM_PROGRAM = 'ghostty'
    await start($)
    const ui = await $.ui.mount({ plugin: PLUGIN_NAME, surface: 'terminal', ...PANE })
    expect(await ui.findAll({ type: 'Image' })).toHaveLength(2)
    expect(await ui.findAll({ type: 'Raster' })).toHaveLength(0)
    expect(await ui.find({ type: 'Text', text: /approximate colours/ })).toBeUndefined()
  })

  test('saving a variant from the pane writes it into the project and the list follows', async ($, on) => {
    const fixture = world(on)
    withRuns(fixture)
    await start($)
    const ui = await $.ui.mount({ plugin: PLUGIN_NAME, surface: 'terminal', ...PANE })
    await ui.press({ key: `keep-${PENDING_RUN}-b` })
    const keep = fixture.runs.find(run => run.argv[0] === 'uv')
    expect(keep?.argv).toContain(`${PENDING_RUN}/raw-b.png`)
    expect(keep?.argv).toContain(`${ROOT}/assets/textures/cobblestone.png`)
    expect(JSON.parse(fixture.written.get(`${PENDING_RUN}/saved.json`) ?? '[]')[0]).toMatchObject({ variant: 'b' })
    expect(await ui.find({ type: 'Text', text: /choose a variant/ })).toBeUndefined()
    // As opções do run continuam no detalhe: a gravada marcada "chosen", a outra ainda com "save".
    expect(await ui.find({ type: 'Text', text: 'options' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'chosen b' })).toBeDefined()
    expect(await ui.find({ key: `keep-${PENDING_RUN}-a` })).toBeDefined()
    expect(await ui.find({ key: `keep-${PENDING_RUN}-b` })).toBeUndefined()
    // O open da escolhida abre o arquivo do projeto, não o do run.
    await ui.press({ key: `view-${PENDING_RUN}-b` })
    expect(fixture.runs.at(-1)?.argv).toEqual(['xdg-open', `${ROOT}/assets/textures/cobblestone.png`])
    expect(await ui.find({ type: 'Text', text: 'textures' })).toBeDefined()
  })

  test('the pane opens by itself when there is something to check', async ($, on) => {
    const fixture = world(on)
    const opened: string[] = []
    on('ui.open', ($, e) => {
      opened.push(e.id)
      return { value: { isPlaced: true } }
    })
    await start($)
    expect(opened).toEqual([])
    await $.tool.call({ tool: TOOL_ID, prompt: 'a red potion', name: 'potion', kind: 'icon' })
    expect(opened).toEqual(['game-art-gallery'])

    // Variações pendentes de outra sessão: o painel abre já no início.
    withRuns(fixture)
    opened.length = 0
    await start($)
    expect(opened).toEqual(['game-art-gallery'])
  })

  test('unchoose asks first, then takes the file (and the engine sidecar) out of the project', async ($, on) => {
    const fixture = world(on)
    withRuns(fixture)
    await start($)
    const ui = await $.ui.mount({ plugin: PLUGIN_NAME, surface: 'terminal', ...PANE })
    await ui.press({ key: `keep-${PENDING_RUN}-b` })
    const saved = `${ROOT}/assets/textures/cobblestone.png`
    fixture.files.add(saved)
    fixture.files.add(`${saved}.import`)
    expect(await ui.find({ type: 'Text', text: /cobblestone\.png/ })).toBeDefined()

    // Primeiro clique só pergunta; cancelar volta sem apagar nada.
    await ui.press({ key: `unchoose-${PENDING_RUN}-b` })
    expect(await ui.find({ type: 'Text', text: 'delete cobblestone.png?' })).toBeDefined()
    await ui.press({ key: `cancel-${PENDING_RUN}-b` })
    expect(fixture.files.has(saved)).toBe(true)
    expect(fixture.runs.some(run => run.argv[0] === 'rm' && run.argv.includes(saved))).toBe(false)

    await ui.press({ key: `unchoose-${PENDING_RUN}-b` })
    await ui.press({ key: `confirm-${PENDING_RUN}-b` })
    expect(fixture.runs.at(-1)?.argv).toEqual(['rm', '-f', '--', saved, `${saved}.import`])
    expect(JSON.parse(fixture.written.get(`${PENDING_RUN}/saved.json`) ?? '[]')).toEqual([])
    // A variação volta a ser escolha pendente, com "save" de novo.
    expect(await ui.find({ key: `keep-${PENDING_RUN}-b` })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /choose a variant/ })).toBeDefined()
  })

  test('where no pane can be placed (VS Code extension), /game-art draws the gallery in its own reply', async ($, on) => {
    const fixture = world(on)
    withRuns(fixture)
    on('ui.open', () => ({ value: { isPlaced: false, reason: 'the attached surfaces place no panes' } }))
    await start($)
    const ran = await $.command.run({ command: 'game-art', args: '' } as never)
    const text = (ran as { text?: string }).text ?? ''
    expect(text).toContain('this surface has no side panel')

    const row = await $.ui.mount({
      plugin: PLUGIN_NAME,
      surface: 'vscode',
      component: 'CommandOutput',
      props: { command: 'game-art', args: '', text, isErrored: false } as never,
    })
    expect(await row.find({ type: 'Text', text: /choose one to save/ })).toBeDefined()
    expect(await row.findAll({ type: 'Svg' })).toHaveLength(2)
    await row.press({ key: `view-${PENDING_RUN}-a` })
    expect(fixture.runs.at(-1)?.argv[0]).toBe('xdg-open')
  })

  test('/game-art opens the pane', async ($, on) => {
    world(on)
    const opened: string[] = []
    on('ui.open', ($, e) => {
      opened.push(e.id)
      return { value: { isPlaced: true } }
    })
    await start($)
    const ran = await $.command.run({ command: 'game-art', args: '' } as never)
    expect(opened).toEqual(['game-art-gallery'])
    expect(JSON.stringify(ran)).toContain('panel opened')
  })
})
