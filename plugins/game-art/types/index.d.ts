// Contrato de estado da sessão do game-art.

/** Nomes dos assets sendo gerados em segundo plano nesta sessão (um por job, repetidos se houver). */
export type GameArtJobs = string[]

/** Uma gravação de um run no projeto. */
export type GameArtSaved = { path: string; width: number; height: number; variant: 'a' | 'b' | 'c' }

/** Um run na galeria do painel. */
export type GameArtGalleryEntry = {
  name: string
  kind: 'sprite' | 'icon' | 'ui' | 'texture' | 'background' | 'concept'
  runDir: string
  /** O id do run, `YYYYMMDD-HHMMSS`: ordena e diz quando. */
  id: string
  saved: GameArtSaved[]
  /** Variações processadas no run, que dá para abrir e gravar. */
  choices: ('a' | 'b' | 'c')[]
}

/** O item em destaque no painel: um asset gravado (com `path`) ou as variações pendentes de um run (sem). */
export type GameArtSelection = { runDir: string; variant: 'a' | 'b' | 'c'; path?: string }

declare module 'claude-code' {
  interface PluginState {
    'game-art': {
      jobs: GameArtJobs
      /** A raiz do projeto de jogo da sessão; '' antes do session.start. */
      root: string
      gallery: GameArtGalleryEntry[]
      selected: GameArtSelection | null
      /** O arquivo cujo "unchoose" espera confirmação no painel; null quando nenhum. */
      confirming: string | null
      /** A resposta do último /game-art que desenha a galeria na conversa (o painel não foi posto); '' quando nenhuma. */
      inline: string
      /** Quantas respostas do /game-art desenharam a galeria na conversa nesta sessão; numera a próxima. */
      inlineReplies: number
    }
  }
}
