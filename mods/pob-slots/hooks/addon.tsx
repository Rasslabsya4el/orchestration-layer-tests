// This project's own add-on of the monitor pane: none yet. The pane (register.tsx) draws only its generic cards
// until this file lists cards and tabs of its own. The file is the project's own: `core update` never rewrites it.
import type { Addon } from './register'

export const ADDON: Addon = { cards: [], tabs: [] }
