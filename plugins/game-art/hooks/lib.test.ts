import { describe, expect, test } from 'claude-code/testing'

import { formatReport, generateAsset, inPool, keepVariant, MAX_PARALLEL, processArgv } from './lib/asset'
import type { AssetOutcome, AssetReport } from './lib/asset'
import type { AssetSettings } from './lib/asset'
import { buildPrompt, codexArgv, codexSaid, threadIdOf } from './lib/codex'
import { findProject } from './lib/engine'
import type { Host, RunOptions, RunResult } from './lib/host'
import { openerFor, openImage, reportBlocks } from './lib/preview'
import { parseInput, resolveRequest } from './lib/request'
import { unchoose } from './lib/gallery'
import { detailOf, relativeTo, savedByKind, summaryOf } from './lib/panel'
import { fitThumb, parseThumb, rasterOf, svgOf, svgWithPng, SVG_SOURCE_LIMIT } from './lib/thumb'
import { initStyle, loadStyle } from './lib/style'

const THREAD = '01a1262e-2137-7133-b979-7667e6d96566'
const NOW = new Date(2026, 9, 10, 11, 20, 5)
const RUN = '/games/demo/.game-art/runs/hero/20261010-112005'
const SETTINGS: AssetSettings = {
  pluginRoot: '/plugin',
  cwd: '/games/demo/scenes',
  codexHome: '/home/me/.codex',
  codexModel: '',
  generationTimeoutMs: 300000,
  now: () => NOW,
}
const CODEX_EVENTS = [
  JSON.stringify({ type: 'thread.started', thread_id: THREAD }),
  JSON.stringify({ type: 'item.completed', item: { id: 'item_0', type: 'agent_message', text: 'Saved raw.png' } }),
].join('\n')
const ok = (stdout = ''): RunResult => ({ exitCode: 0, stdout, stderr: '' })

type Call = { argv: readonly string[]; options?: RunOptions }

/** Um disco de mentira: `files` são os caminhos que existem; a listagem de um diretório sai deles. */
function fakeHost(
  files: string[],
  answer: (argv: readonly string[], files: Set<string>, options?: RunOptions) => RunResult,
  texts: Record<string, string> = {},
): Host & { calls: Call[]; written: Map<string, string> } {
  const present = new Set([...files, ...Object.keys(texts)])
  const calls: Call[] = []
  const written = new Map<string, string>()
  return {
    calls,
    written,
    writeText: async (path, text) => {
      written.set(path, text)
      present.add(path)
    },
    run: async (argv, options) => {
      calls.push({ argv, options })
      return answer(argv, present, options)
    },
    exists: async path => present.has(path),
    readText: async path => written.get(path) ?? texts[path],
    readBase64: async path => (present.has(path) ? `base64:${path}` : undefined),
    // Diretórios existem implícitos: a primeira parte de cada caminho abaixo de `dir`.
    list: async dir => [...new Set([...present].filter(path => path.startsWith(`${dir}/`)).map(path => path.slice(dir.length + 1).split('/')[0] ?? ''))],
  }
}

/** Codex grava o raw.png no run; o script de pixels responde o tamanho; o rm limpa a cópia do Codex. */
function happyPath(argv: readonly string[], files: Set<string>, options?: RunOptions): RunResult {
  if (argv[0] === 'codex') {
    files.add(`${argv[argv.indexOf('-C') + 1]}/${/as (raw-\w\.png)/.exec(options?.stdin ?? '')?.[1] ?? 'raw-a.png'}`)
    return ok(CODEX_EVENTS)
  }
  if (argv[0] === 'uv') return ok('{"width": 64, "height": 96, "warnings": []}\n')
  if (argv[0] === 'rm') return ok()
  return { exitCode: 127, stdout: '', stderr: `unexpected ${argv[0]}` }
}

/** O relatório de um asset salvo, ou undefined. */
const saved = (outcome: AssetOutcome): AssetReport | undefined => (outcome.ok && outcome.kind === 'asset' ? outcome.report : undefined)

const input = (fields: Record<string, unknown>) => parseInput({ prompt: 'a knight', name: 'hero', kind: 'sprite', ...fields })
const request = (fields: Record<string, unknown>) => resolveRequest(input(fields), undefined, SETTINGS.cwd, '/games/demo')

describe('request', () => {
  test('fills the kind defaults', () => {
    expect(request({}).transparent).toBe(true)
    expect(request({ kind: 'texture' }).transparent).toBe(false)
    expect(request({ kind: 'texture', transparent: true }).transparent).toBe(true)
  })

  test('rejects bad names, kinds and half sizes', () => {
    expect(() => request({ name: '../hero' })).toThrow('name')
    expect(() => request({ kind: 'model' })).toThrow('kind')
    expect(() => request({ width: 64 })).toThrow('width and height')
    expect(() => request({ width: 0, height: 10 })).toThrow('width')
  })
})

describe('engine detection', () => {
  const none = () => ok()
  test('finds Godot, Unity and Unreal projects above the session directory', async () => {
    expect(await findProject(fakeHost(['/games/demo/project.godot'], none), '/games/demo/scenes')).toEqual({ engine: 'godot', root: '/games/demo' })
    expect(await findProject(fakeHost(['/games/demo/Assets', '/games/demo/ProjectSettings/ProjectVersion.txt'], none), '/games/demo/Assets/Scripts')).toEqual({
      engine: 'unity',
      root: '/games/demo',
    })
    expect(await findProject(fakeHost(['/games/demo/Demo.uproject'], none), '/games/demo/Source')).toEqual({ engine: 'unreal', root: '/games/demo' })
  })

  test('falls back to the session directory', async () => {
    expect(await findProject(fakeHost([], none), '/work/art')).toEqual({ engine: 'none', root: '/work/art' })
  })
})

describe('codex', () => {
  test('pixel art asks for a flat magenta background; other transparency asks the tool for it', () => {
    expect(buildPrompt(request({ pixelArt: true }), 'godot', 'raw-a.png')).toContain('pure magenta #FF00FF')
    expect(buildPrompt(request({}), 'godot', 'raw-a.png')).toContain('transparent_background set to true')
    expect(buildPrompt(request({ kind: 'background' }), 'unity', 'raw-a.png')).toContain('fully opaque')
    expect(buildPrompt(request({ width: 1920, height: 1080 }), 'godot', 'raw-a.png')).toContain('16:9 aspect ratio')
  })

  test('the prompt goes through stdin, before the -i references; no session rollout is kept', () => {
    expect(codexArgv(RUN, ['/ref/a.png', '/ref/b.png'], 'gpt-x')).toEqual([
      'codex', 'exec', '--skip-git-repo-check', '--ephemeral', '--json', '-s', 'workspace-write', '-C', RUN, '-m', 'gpt-x', '-', '-i', '/ref/a.png', '-i', '/ref/b.png',
    ])
  })

  test('reads the thread id only when it is a UUID, and what Codex said', () => {
    expect(threadIdOf(CODEX_EVENTS)).toBe(THREAD)
    expect(threadIdOf(JSON.stringify({ type: 'thread.started', thread_id: '../../etc' }))).toBeUndefined()
    expect(threadIdOf('not json')).toBeUndefined()
    expect(codexSaid(`${CODEX_EVENTS}\n${JSON.stringify({ type: 'error', message: 'quota exceeded' })}`)).toEqual(['Saved raw.png', 'quota exceeded'])
  })
})

describe('processing', () => {
  test('pixel art keys the chroma, trims and scales with nearest neighbour', () => {
    const argv = processArgv('/plugin', request({ pixelArt: true, width: 32, height: 48 }), '/in.png', '/out.png', '/preview.png', '/thumb.json')
    expect(argv.slice(0, 4)).toEqual(['uv', 'run', '--quiet', '/plugin/scripts/process_image.py'])
    expect(argv).toContain('--key')
    expect(argv).toContain('--trim')
    expect(argv.join(' ')).toContain('--fit 32x48 --fit-mode contain')
    expect(argv).toContain('--nearest')
  })

  test('a sized texture stretches, so cropping never breaks the seam', () => {
    const argv = processArgv('/plugin', request({ kind: 'texture', width: 256, height: 128 }), '/in.png', '/out.png', '/preview.png', '/thumb.json')
    expect(argv.join(' ')).toContain('--seamless --fit 256x128 --fit-mode stretch')
    expect(processArgv('/plugin', request({ kind: 'sprite' }), '/in.png', '/out.png', '/preview.png', '/thumb.json')).not.toContain('--seamless')
  })

  test('a texture without a size rounds to a power of two and is never keyed', () => {
    const argv = processArgv('/plugin', request({ kind: 'texture' }), '/in.png', '/out.png', '/preview.png', '/thumb.json')
    expect(argv).toContain('--pot')
    expect(argv).toContain('--seamless')
    expect(argv).not.toContain('--key')
    expect(argv).not.toContain('--trim')
  })
})

describe('generateAsset', () => {
  test('saves into the engine art folder and reports the size', async () => {
    const host = fakeHost(['/games/demo/project.godot'], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({ pixelArt: true }))
    expect(outcome).toEqual({
      ok: true,
      kind: 'asset',
      report: expect.objectContaining({ path: '/games/demo/assets/sprites/hero.png', width: 64, height: 96, engine: 'godot', runDir: RUN, warnings: [] }),
    })
    const codex = host.calls.find(call => call.argv[0] === 'codex')
    expect(codex?.options?.cwd).toBe(RUN)
    expect(codex?.options?.stdin).toContain('Godot game')
    expect(codex?.options?.timeoutMs).toBe(300000)
  })

  test('the run lives in the project work folder, which git and Godot ignore', async () => {
    const host = fakeHost(['/games/demo/project.godot'], happyPath)
    await generateAsset(host, SETTINGS, input({}))
    expect(host.written.get('/games/demo/.game-art/.gitignore')).toBe('runs/\n')
    expect(host.written.get('/games/demo/.game-art/.gdignore')).toBe('')
    expect(host.written.get(`${RUN}/prompt.txt`)).toContain('Subject: a knight')
    expect(host.written.get(`${RUN}/codex-a.jsonl`)).toBe(CODEX_EVENTS)
  })

  test('existing markers are kept, and a second run in the same second gets its own folder', async () => {
    const host = fakeHost(['/games/demo/project.godot', '/games/demo/.game-art/.gitignore', `${RUN}/raw-a.png`], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({}))
    expect(host.written.has('/games/demo/.game-art/.gitignore')).toBe(false)
    expect(saved(outcome)?.runDir).toBe(`${RUN}-2`)
  })

  test("Codex's own copy of the image is removed", async () => {
    const host = fakeHost(['/games/demo/project.godot'], happyPath)
    await generateAsset(host, SETTINGS, input({}))
    expect(host.calls.find(call => call.argv[0] === 'rm')?.argv).toEqual(['rm', '-rf', '--', `/home/me/.codex/generated_images/${THREAD}`])
  })

  test('warnings from processing reach the report', async () => {
    const host = fakeHost([], (argv, files, options) =>
      argv[0] === 'uv' ? ok('{"width": 8, "height": 8, "warnings": ["the background bled into the art"]}') : happyPath(argv, files, options),
    )
    const outcome = await generateAsset(host, SETTINGS, input({ pixelArt: true }))
    expect(formatReport(saved(outcome) as AssetReport)).toContain('WARNING: the background bled into the art')
  })

  test('never overwrites existing art', async () => {
    const host = fakeHost(['/games/demo/project.godot', '/games/demo/assets/sprites/hero.png', '/games/demo/assets/sprites/hero-2.png'], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({}))
    expect(saved(outcome)?.path).toBe('/games/demo/assets/sprites/hero-3.png')
  })

  test('dest overrides the folder, relative to the project root', async () => {
    const host = fakeHost(['/games/demo/Demo.uproject'], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({ dest: 'Raw/Heroes' }))
    expect(saved(outcome)?.path).toBe('/games/demo/Raw/Heroes/hero.png')
  })

  test('a missing reference stops before Codex runs', async () => {
    const host = fakeHost(['/games/demo/project.godot'], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({ references: ['seeds/hero.png'] }))
    expect(outcome).toEqual({ ok: false, error: 'reference image not found: /games/demo/scenes/seeds/hero.png' })
    expect(host.calls).toEqual([])
  })

  test('Codex that ends without the PNG is reported with what it said', async () => {
    const host = fakeHost([], argv =>
      argv[0] === 'codex' ? ok(JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text: 'Refused: content policy' } })) : ok(),
    )
    const outcome = await generateAsset(host, SETTINGS, input({}))
    expect(outcome.ok).toBe(false)
    expect(!outcome.ok && outcome.error).toContain('Refused: content policy')
  })

  test('a Codex timeout is reported, not thrown', async () => {
    const host: Host = {
      ...fakeHost([], happyPath),
      run: async () => {
        throw new Error('timed out after 300000 ms')
      },
    }
    const outcome = await generateAsset(host, SETTINGS, input({}))
    expect(!outcome.ok && outcome.error).toContain('codex exec did not finish: timed out')
  })

  test('a failed processing step keeps the raw path in the error', async () => {
    const host = fakeHost([], (argv, files, options) => (argv[0] === 'uv' ? { exitCode: 1, stdout: '', stderr: 'OSError: cannot identify image' } : happyPath(argv, files, options)))
    const outcome = await generateAsset(host, SETTINGS, input({}))
    expect(!outcome.ok && outcome.error).toContain(`/games/demo/scenes/.game-art/runs/hero/20261010-112005/raw-a.png`)
    expect(!outcome.ok && outcome.error).toContain('cannot identify image')
  })
})

describe('preview', () => {
  const report = {
    path: '/games/demo/assets/sprites/hero.png',
    width: 64,
    height: 96,
    engine: 'godot' as const,
    projectRoot: '/games/demo',
    runDir: RUN,
    preview: `${RUN}/preview-a.png`,
    warnings: [],
    note: 'note',
  }

  test('the answer carries the preview image when it can be read, and text alone otherwise', async () => {
    const withPreview = await reportBlocks(fakeHost([`${RUN}/preview-a.png`], () => ok()), report)
    expect(withPreview).toEqual([
      { type: 'text', text: expect.stringContaining('Saved /games/demo/assets/sprites/hero.png') },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: `base64:${RUN}/preview-a.png` } },
    ])
    expect(await reportBlocks(fakeHost([], () => ok()), report)).toHaveLength(1)
  })

  test('picks VS Code, then WSL, then xdg-open', () => {
    expect(openerFor(undefined, 'vscode', 'Linux version 6.18 (microsoft-standard-WSL2)')).toBe('vscode')
    // A extensão do VS Code não define TERM_PROGRAM: a superfície decide.
    expect(openerFor('vscode', undefined, 'Linux version 6.18 (microsoft-standard-WSL2)')).toBe('vscode')
    expect(openerFor('terminal', undefined, 'Linux version 6.18.40.1-microsoft-standard-WSL2')).toBe('wsl')
    expect(openerFor(undefined, 'xterm', 'Linux version 6.8.0-generic')).toBe('xdg')
  })

  test('on WSL the path is converted and handed to explorer.exe, whatever its exit code', async () => {
    const host = fakeHost([], argv => (argv[0] === 'wslpath' ? ok('\\\\wsl.localhost\\Ubuntu\\hero.png\n') : { exitCode: 1, stdout: '', stderr: '' }))
    expect(await openImage(host, 'wsl', '/home/me/hero.png')).toBeUndefined()
    expect(host.calls.map(call => call.argv)).toEqual([
      ['wslpath', '-w', '/home/me/hero.png'],
      ['explorer.exe', '\\\\wsl.localhost\\Ubuntu\\hero.png'],
    ])
  })

  test('a viewer that fails becomes a warning, never an error', async () => {
    const host: Host = { ...fakeHost([], () => ok()), run: async () => { throw new Error('ENOENT code') } }
    expect(await openImage(host, 'vscode', '/x.png')).toContain('ENOENT code')
  })
})

describe('style guide', () => {
  const GUIDE = '/games/demo/.game-art/style.json'
  const godot = (texts: Record<string, string>) => fakeHost(['/games/demo/project.godot'], happyPath, texts)

  test('fills style, pixel art, references and the size per kind; the call wins field by field', async () => {
    const guide = { style: 'muted 16-bit', pixelArt: true, references: ['art/seed.png'], sizes: { sprite: [64, 64] } }
    const host = godot({ [GUIDE]: JSON.stringify(guide), '/games/demo/art/seed.png': '' })
    const outcome = await generateAsset(host, SETTINGS, input({ style: 'neon' }))
    const codex = host.calls.find(call => call.argv[0] === 'codex')
    expect(codex?.options?.stdin).toContain('Style: neon; crisp pixel art')
    expect(codex?.argv).toContain('/games/demo/art/seed.png')
    expect(host.calls.find(call => call.argv[0] === 'uv')?.argv.join(' ')).toContain('--fit 64x64')
    expect(formatReport(saved(outcome) as AssetReport)).toContain(`Style guide applied: ${GUIDE}`)
  })

  test('call references replace the guide ones and are relative to the session', () => {
    const resolved = resolveRequest(input({ references: ['seed.png'] }), { references: ['art/a.png'] }, '/games/demo/scenes', '/games/demo')
    expect(resolved.references).toEqual(['/games/demo/scenes/seed.png'])
  })

  test('a broken guide stops the generation with the reason', async () => {
    const broken = godot({ [GUIDE]: '{ "style": "x", "pixelart": true }' })
    const outcome = await generateAsset(broken, SETTINGS, input({}))
    expect(!outcome.ok && outcome.error).toContain('unknown field pixelart')
    expect(broken.calls).toEqual([])
    const invalid = await generateAsset(godot({ [GUIDE]: '{ nope' }), SETTINGS, input({}))
    expect(!invalid.ok && invalid.error).toContain('is not valid JSON')
    const badSize = await loadStyle(godot({ [GUIDE]: '{ "sizes": { "mesh": [1, 1] } }' }), '/games/demo').catch((error: Error) => error.message)
    expect(badSize).toContain('sizes.mesh is not a kind')
  })

  test('init_style writes the guide once and refuses to replace it silently', async () => {
    const host = godot({ '/games/demo/art/seed.png': '' })
    const first = await initStyle(host, '/games/demo/scenes', { style: 'muted 16-bit', pixelArt: true, references: ['art/seed.png'], sizes: { icon: [32, 32] } })
    expect(first.ok && first.path).toBe(GUIDE)
    expect(JSON.parse(host.written.get(GUIDE) ?? '')).toEqual({ style: 'muted 16-bit', pixelArt: true, references: ['art/seed.png'], sizes: { icon: [32, 32] } })
    const again = await initStyle(host, '/games/demo/scenes', { style: 'other' })
    expect(!again.ok && again.error).toContain('pass overwrite: true')
    expect((await initStyle(host, '/games/demo/scenes', { style: 'other', overwrite: true })).ok).toBe(true)
    expect(await loadStyle(host, '/games/demo')).toEqual(expect.objectContaining({ style: 'other' }))
  })

  test('init_style checks the references exist', async () => {
    const outcome = await initStyle(godot({}), '/games/demo', { references: ['missing.png'] })
    expect(!outcome.ok && outcome.error).toBe('reference image not found: /games/demo/missing.png')
  })
})

describe('variants', () => {
  test('never runs more than MAX_PARALLEL at once and keeps the order', async () => {
    let running = 0
    let peak = 0
    const results = await inPool([1, 2, 3], MAX_PARALLEL, async item => {
      running++
      peak = Math.max(peak, running)
      await Promise.resolve()
      running--
      return item * 10
    })
    expect(results).toEqual([10, 20, 30])
    expect(peak).toBe(MAX_PARALLEL)
  })

  test('three variants: three Codex runs, each processed inside the run, none in the project', async () => {
    const host = fakeHost(['/games/demo/project.godot'], happyPath)
    const outcome = await generateAsset(host, SETTINGS, input({ variants: 3 }))
    expect(outcome.ok && outcome.kind).toBe('variants')
    const outs = host.calls.filter(call => call.argv[0] === 'uv').map(call => call.argv[call.argv.indexOf('--out') + 1])
    expect(outs).toEqual([`${RUN}/final-a.png`, `${RUN}/final-b.png`, `${RUN}/final-c.png`])
    expect(JSON.parse(host.written.get(`${RUN}/run.json`) ?? '{}').variants).toEqual(['a', 'b', 'c'])
  })

  test('one failed variant does not sink the others', async () => {
    const host = fakeHost(['/games/demo/project.godot'], (argv, files, options) =>
      argv[0] === 'codex' && options?.stdin?.includes('raw-b.png') ? ok('') : happyPath(argv, files, options),
    )
    const outcome = await generateAsset(host, SETTINGS, input({ variants: 2 }))
    if (!outcome.ok || outcome.kind !== 'variants') throw new Error('expected variants')
    expect(outcome.report.results.map(result => result.ok)).toEqual([true, false])
  })

  test('keep_variant refuses a folder that is not a run, and a variant the run does not have', async () => {
    const host = fakeHost([], happyPath, { [`${RUN}/run.json`]: JSON.stringify({ variants: ['a', 'b'] }) })
    expect(await keepVariant(host, SETTINGS, '/elsewhere', 'a', undefined)).toEqual({ ok: false, error: '/elsewhere is not a game-art run (no run.json)' })
    const wrong = await keepVariant(host, SETTINGS, RUN, 'c', undefined)
    expect(!wrong.ok && wrong.error).toBe('this run has variants a, b, not c')
    const missing = await keepVariant(host, SETTINGS, RUN, 'b', undefined)
    expect(!missing.ok && missing.error).toContain('did not come out')
  })
})

describe('thumbnail', () => {
  // 2x3: vermelho, transparente / azul, verde / branco, preto.
  const pixels = [255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 255, 255, 0, 255, 0, 255, 255, 255, 255, 255, 0, 0, 0, 255]
  const thumb = parseThumb(JSON.stringify({ width: 2, height: 3, rgba: btoa(String.fromCharCode(...pixels)) }))

  test('reads the pixels and refuses a file that does not add up', () => {
    expect(thumb?.width).toBe(2)
    expect(parseThumb(JSON.stringify({ width: 4, height: 4, rgba: btoa('xx') }))).toBeUndefined()
    expect(parseThumb('not json')).toBeUndefined()
    expect(parseThumb(undefined)).toBeUndefined()
  })

  test('each terminal cell shows two pixels; transparency is the terminal background, never its text colour', () => {
    const raster = rasterOf(thumb!)
    expect([raster.columns, raster.rows]).toEqual([2, 2])
    const binary = atob(raster.cells)
    const words = new Uint32Array(Uint8Array.from(binary, char => char.charCodeAt(0)).buffer)
    expect([...words]).toEqual([
      0x2580, 0xff0000, 0x0000ff, // vermelho sobre azul
      0x2584, 0x00ff00, 0x01000000, // transparente sobre verde: meio bloco de baixo, fundo do terminal
      0x2580, 0xffffff, 0x01000000, // branco; a linha ímpar de baixo fica com o fundo do terminal
      0x2580, 0x000000, 0x01000000,
    ])
  })

  test('a cell with no visible pixel is a blank', () => {
    const empty = parseThumb(JSON.stringify({ width: 1, height: 2, rgba: btoa(String.fromCharCode(0, 0, 0, 0, 0, 0, 0, 0)) }))
    const words = new Uint32Array(Uint8Array.from(atob(rasterOf(empty!).cells), char => char.charCodeAt(0)).buffer)
    expect([...words]).toEqual([0x20, 0x01000000, 0x01000000])
  })

  test('shrinks with nearest neighbour and never enlarges', () => {
    expect(fitThumb(thumb!, 32)).toBe(thumb)
    const half = fitThumb(thumb!, 1)
    expect([half.width, half.height]).toEqual([1, 1])
    expect([...half.rgba]).toEqual([255, 0, 0, 255])
  })

  test('the real PNG goes inside the SVG when it fits, and nothing when it does not', () => {
    const svg = svgWithPng('iVBORw0KGgo=', thumb!, 4)
    expect(svg).toContain('href="data:image/png;base64,iVBORw0KGgo="')
    expect(svg).toContain('width="8" height="12"')
    expect(svg).toContain('image-rendering:pixelated')
    expect(svgWithPng('A'.repeat(SVG_SOURCE_LIMIT), thumb!, 4)).toBeUndefined()
  })

  test('the SVG has one crisp square per visible pixel', () => {
    const svg = svgOf(thumb!, 4)
    expect(svg).toContain('width="8" height="12"')
    expect(svg).toContain('shape-rendering="crispEdges"')
    expect(svg.match(/<rect/g)).toHaveLength(5)
    expect(svg).toContain('fill="#ff0000"')
  })
})

describe('panel', () => {
  const saved = (path: string) => ({ path, width: 32, height: 32, variant: 'a' as const })
  const barrel = { name: 'barrel', kind: 'sprite' as const, runDir: '/r/barrel/2', id: '20261010-151126', saved: [saved('/p/assets/sprites/barrel.png')], choices: [] }
  const coin = { name: 'coin', kind: 'icon' as const, runDir: '/r/coin/1', id: '20261010-112527', saved: [saved('/p/assets/icons/coin.png')], choices: [] }
  const chest = { name: 'chest', kind: 'icon' as const, runDir: '/r/chest/3', id: '20261010-152618', saved: [], choices: ['a' as const, 'b' as const] }

  test('with nothing selected: the first pending choice, else the newest asset', () => {
    expect(detailOf([chest, barrel, coin], null)).toEqual({ kind: 'pending', entry: chest })
    expect(detailOf([barrel, coin], null)).toEqual({ kind: 'saved', entry: barrel, item: barrel.saved[0] })
    expect(detailOf([], null)).toBeUndefined()
  })

  test('a selection that no longer exists falls back to the default', () => {
    expect(detailOf([chest, coin], { runDir: '/r/coin/1', variant: 'a', path: '/p/assets/icons/coin.png' })).toEqual({ kind: 'saved', entry: coin, item: coin.saved[0] })
    expect(detailOf([chest, coin], { runDir: '/gone', variant: 'a', path: '/gone.png' })).toEqual({ kind: 'pending', entry: chest })
  })

  test('saved assets come by kind, in the kinds order', () => {
    expect(savedByKind([chest, barrel, coin], 40).map(section => [section.kind, section.rows.map(row => row.entry.name)])).toEqual([
      ['sprite', ['barrel']],
      ['icon', ['coin']],
    ])
    expect(summaryOf([chest, barrel, coin])).toBe('2 assets · 1 to choose')
    expect(relativeTo('/p', '/p/assets/icons/coin.png')).toBe('assets/icons/coin.png')
    expect(relativeTo('/p', '/elsewhere/x.png')).toBe('/elsewhere/x.png')
  })
})

describe('unchoose', () => {
  const SAVED_JSON = `${RUN}/saved.json`
  const record = JSON.stringify([{ path: '/games/demo/assets/icons/hero.png', width: 32, height: 32, variant: 'a' }])

  test('removes only what this run saved, with the engine sidecars that exist', async () => {
    const host = fakeHost(['/games/demo/assets/icons/hero.png', '/games/demo/assets/icons/hero.png.meta'], () => ok(), { [SAVED_JSON]: record })
    const outcome = await unchoose(host, RUN, '/games/demo/assets/icons/hero.png')
    expect(outcome).toEqual({ ok: true, removed: ['/games/demo/assets/icons/hero.png', '/games/demo/assets/icons/hero.png.meta'] })
    expect(host.calls[0]?.argv).toEqual(['rm', '-f', '--', '/games/demo/assets/icons/hero.png', '/games/demo/assets/icons/hero.png.meta'])
    expect(JSON.parse(host.written.get(SAVED_JSON) ?? '')).toEqual([])
  })

  test('never deletes a path the run did not save', async () => {
    const host = fakeHost(['/games/demo/project.godot'], () => ok(), { [SAVED_JSON]: record })
    const outcome = await unchoose(host, RUN, '/games/demo/project.godot')
    expect(outcome.ok).toBe(false)
    expect(host.calls).toEqual([])
  })
})
