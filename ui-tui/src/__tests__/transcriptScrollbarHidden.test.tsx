import { PassThrough } from 'stream'

import { Box, type ScrollBoxHandle, Text, renderSync } from '@hermes/ink'
import { stripAnsi } from '@hermes/shared/ansi'
import React, { type RefObject } from 'react'
import { afterEach, expect, it } from 'vitest'

import { TranscriptScrollbar } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

// #no-transcript-scrollbar (local patch): the transcript's right-hand gutter
// must stay free of drawn glyphs, because a terminal-native drag-select over
// more than one line copies that column along with the text (│ / ┃ land in
// whatever gets pasted — a pasted shell command is then unusable).
//
// Two harness facts this test depends on:
//   - a sibling "marker" Text is rendered ALONGSIDE the gutter, and a short
//     flush is awaited before reading the frame. A component that draws nothing
//     produces an empty frame, so asserting only "no glyph" is vacuously true
//     and passes even with the bar drawn; the marker proves the frame was
//     captured, and the flush (Ink writes frames asynchronously) proves it was
//     captured late enough to contain the gutter's cells.
//   - it asserts the RENDERED frame, not the component tree: a tree-only check
//     would pass for a component that still paints into the reserved Box.
//
// The gutter itself stays reserved (a width-1 column), so the transcript never
// reflows when it outgrows the viewport and the composer keeps its scrollbar
// allowance.

const mounted: Array<() => void> = []
const BARS = ['│', '┃', '┆', '┇']
const FLUSH_MS = 80

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const frame = async (height: number) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 40, isTTY: false, rows: height })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', (chunk: Buffer) => {
    output += chunk.toString()
  })

  const scrollRef = { current: null } as RefObject<null | ScrollBoxHandle>
  const instance = renderSync(
    <Box flexDirection="row">
      <Text>marker</Text>
      <TranscriptScrollbar scrollRef={scrollRef} t={DEFAULT_THEME} />
    </Box>,
    {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    }
  )

  mounted.push(() => {
    instance.unmount()
    instance.cleanup()
  })

  await wait(FLUSH_MS)

  return stripAnsi(output)
}

afterEach(() => {
  while (mounted.length) {
    mounted.pop()?.()
  }
})

it('draws no scrollbar glyphs in the transcript gutter', async () => {
  const out = await frame(12)

  // The frame really was captured (guards against a vacuous pass).
  expect(out).toContain('marker')

  for (const bar of BARS) {
    expect(out).not.toContain(bar)
  }
})
