import { test } from 'claude-code/testing'
import { pack } from '../hooks/kit'

// #77: a picture with a clipPath or a mask in its body is packed as it is (kit's `dedupe` leaves it whole); a picture
// with neither, and with repeated shapes, is still deduped. The seam is `pack` (the markup a picture is written as).
const RECT = `<rect x='1' y='2' width='30' height='10'/>`
const USE = `<use href='#q0'/>`

test('a picture with a clipPath in its body is returned unchanged, repeated shapes and all', () => {
  const svg = `<svg><defs></defs><clipPath id='c1'><rect x='0' y='0' width='9' height='9'/></clipPath>${RECT.repeat(4)}</svg>`
  const out = pack(svg)
  if (out !== svg) throw new Error(`a clipPath picture was packed: ${out}`)
})

test('a picture with a mask in its body is returned unchanged, repeated shapes and all', () => {
  const svg = `<svg><defs></defs><mask id='m1'><rect x='0' y='0' width='9' height='9'/></mask>${RECT.repeat(4)}</svg>`
  const out = pack(svg)
  if (out !== svg) throw new Error(`a mask picture was packed: ${out}`)
})

test('a picture with neither clipPath nor mask is deduped as before', () => {
  const svg = `<svg><defs></defs>${RECT.repeat(4)}</svg>`
  const want = `<svg><defs><rect id='q0' x='1' y='2' width='30' height='10'/></defs>${USE.repeat(4)}</svg>`
  const out = pack(svg)
  if (out !== want) throw new Error(`the repeated rect was not deduped as before: ${out}`)
})
