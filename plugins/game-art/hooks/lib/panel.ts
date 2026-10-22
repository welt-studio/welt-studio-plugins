// O que o painel mostra, sem desenhar: o item em destaque, as seções da lista e o resumo do cabeçalho.

import type { GameArtSelection } from '../../types'
import { isPending } from './gallery'
import type { GalleryEntry, Saved } from './gallery'
import { KINDS } from './kinds'
import type { Kind } from './kinds'

export type Selection = GameArtSelection

/** A resposta do /game-art quando o painel foi posto. */
export const PLACED_TEXT = 'game-art panel opened.'

/**
 * A resposta do /game-art quando o painel não foi posto, com o motivo do engine. O número da resposta na sessão a faz
 * única: só a mais recente vira galeria, e as anteriores ficam como texto.
 */
export function inlineText(reason: string, reply: number): string {
  return `game-art gallery #${reply}: the panel was not placed (${reason}), so it is drawn in this reply instead.`
}

/** O que o lado de detalhe mostra: a comparação de variações de um run, ou um asset gravado. */
export type Detail = { kind: 'pending'; entry: GalleryEntry } | { kind: 'saved'; entry: GalleryEntry; item: Saved }

export type SavedRow = { entry: GalleryEntry; item: Saved }

/** A seleção, quando ainda existe; sem ela, a primeira escolha pendente, senão o asset mais novo. */
export function detailOf(gallery: readonly GalleryEntry[], selected: Selection | null): Detail | undefined {
  if (selected) {
    const entry = gallery.find(candidate => candidate.runDir === selected.runDir)
    const item = selected.path === undefined ? undefined : entry?.saved.find(saved => saved.path === selected.path)
    if (entry && item) return { kind: 'saved', entry, item }
    if (entry && selected.path === undefined && isPending(entry)) return { kind: 'pending', entry }
  }
  const pending = gallery.find(isPending)
  if (pending) return { kind: 'pending', entry: pending }
  const newest = gallery.find(entry => entry.saved.length > 0)
  const item = newest?.saved.at(-1)
  return newest && item ? { kind: 'saved', entry: newest, item } : undefined
}

/** Os assets gravados por tipo, na ordem dos tipos, cada tipo do mais novo para o mais antigo; tipos vazios ficam de fora. */
export function savedByKind(gallery: readonly GalleryEntry[], limit: number): { kind: Kind; rows: SavedRow[] }[] {
  const rows = gallery.flatMap(entry => [...entry.saved].reverse().map(item => ({ entry, item }))).slice(0, limit)
  return KINDS.map(kind => ({ kind, rows: rows.filter(row => row.entry.kind === kind) })).filter(section => section.rows.length > 0)
}

export function summaryOf(gallery: readonly GalleryEntry[]): string {
  const assets = gallery.reduce((total, entry) => total + entry.saved.length, 0)
  const pending = gallery.filter(isPending).length
  return [`${assets} asset${assets === 1 ? '' : 's'}`, pending > 0 ? `${pending} to choose` : ''].filter(Boolean).join(' · ')
}

export function fileName(path: string): string {
  return path.split('/').pop() ?? path
}

/** O caminho relativo à raiz do projeto, quando está dentro dela. */
export function relativeTo(root: string, path: string): string {
  return root !== '' && path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path
}

export function isSelected(detail: Detail | undefined, entry: GalleryEntry, item?: Saved): boolean {
  if (detail === undefined || detail.entry.runDir !== entry.runDir) return false
  return item === undefined ? detail.kind === 'pending' : detail.kind === 'saved' && detail.item.path === item.path
}
