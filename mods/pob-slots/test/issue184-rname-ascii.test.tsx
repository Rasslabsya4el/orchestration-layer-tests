import { test } from 'claude-code/testing'
import { RNAME } from '../hooks/kit'

// #184: the PC load card names its shares in English (`tests`, `agents`, `other`, `merge`); no Russian share label.
// The seam is `RNAME`, the legend word of each share. The ticket status words («ждёт мерджа») are not here.
test('every share name of the PC load card is ASCII, and the merge share reads `merge`', () => {
  const bad = Object.entries(RNAME).filter(([, word]) => !/^[\x20-\x7e]+$/.test(word))
  if (bad.length) throw new Error(`non-ASCII share names: ${JSON.stringify(bad)}`)
  if (RNAME.merge !== 'merge') throw new Error(`RNAME.merge is ${RNAME.merge}, want merge`)
})
