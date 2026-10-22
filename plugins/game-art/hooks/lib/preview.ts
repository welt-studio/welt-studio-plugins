// O preview: a miniatura que volta para o modelo e o arquivo aberto no visualizador de quem pediu.

import type { AssetOutcome, AssetReport, VariantsReport } from './asset'
import { formatReport, formatVariant, formatVariants } from './asset'
import type { Host } from './host'

/**
 * Por onde abrir uma imagem: VS Code (a extensão, ou o terminal integrado, inclusive Remote-WSL), o Windows a partir do
 * WSL, ou o Linux. A extensão não define `TERM_PROGRAM`, então a superfície que desenhou o clique decide quando há uma.
 */
export const OPENERS = ['vscode', 'wsl', 'xdg'] as const
export type Opener = (typeof OPENERS)[number]

export function openerFor(surface: string | undefined, termProgram: string | undefined, procVersion: string | undefined): Opener {
  if (surface === 'vscode' || termProgram === 'vscode') return 'vscode'
  return /microsoft/i.test(procVersion ?? '') ? 'wsl' : 'xdg'
}

/** Abre o PNG; devolve um aviso quando não deu, nunca lança. */
export async function openImage(host: Host, opener: Opener, path: string): Promise<string | undefined> {
  try {
    if (opener === 'vscode') {
      const opened = await host.run(['code', path])
      return opened.exitCode === 0 ? undefined : `could not open the image in VS Code: ${opened.stderr.trim()}`
    }
    if (opener === 'wsl') {
      const converted = await host.run(['wslpath', '-w', path])
      if (converted.exitCode !== 0) return `could not convert the path for Windows: ${converted.stderr.trim()}`
      // O explorer.exe sai com 1 mesmo quando abre: o código de saída não diz nada.
      await host.run(['explorer.exe', converted.stdout.trim()])
      return undefined
    }
    const opened = await host.run(['xdg-open', path])
    return opened.exitCode === 0 ? undefined : `could not open the image with xdg-open: ${opened.stderr.trim()}`
  } catch (error) {
    return `could not open the image: ${error instanceof Error ? error.message : String(error)}`
  }
}

/** Blocos como a Messages API os escreve: o engine repassa a resposta da tool assim para o modelo. */
export type ContentBlock =
  | { type: 'text'; text: string }
  | { type: 'image'; source: { type: 'base64'; media_type: 'image/png'; data: string } }

async function imageBlock(host: Host, path: string): Promise<ContentBlock[]> {
  const data = await host.readBase64(path)
  return data === undefined ? [] : [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data } }]
}

/** A resposta da tool: o relatório e, quando o preview pôde ser lido, a imagem para o modelo ver. */
export async function reportBlocks(host: Host, report: AssetReport): Promise<ContentBlock[]> {
  return [{ type: 'text', text: formatReport(report) }, ...(await imageBlock(host, report.preview))]
}

/** Com variações: o resumo e, para cada uma, a legenda seguida da imagem. */
export async function variantsBlocks(host: Host, report: VariantsReport): Promise<ContentBlock[]> {
  const blocks: ContentBlock[] = [{ type: 'text', text: formatVariants(report) }]
  for (const result of report.results) {
    blocks.push({ type: 'text', text: formatVariant(result) })
    if (result.ok) blocks.push(...(await imageBlock(host, result.preview)))
  }
  return blocks
}

export async function outcomeBlocks(host: Host, outcome: AssetOutcome & { ok: true }): Promise<ContentBlock[]> {
  return outcome.kind === 'asset' ? reportBlocks(host, outcome.report) : variantsBlocks(host, outcome.report)
}
