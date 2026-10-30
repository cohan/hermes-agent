import { PassThrough } from 'node:stream'

import { AlternateScreen, Box, renderSync } from '@hermes/ink'
import React from 'react'
import { expect, it, vi } from 'vitest'

import { getInputSelection } from '../app/inputSelectionStore.js'
import { TextInput } from '../components/textInput.js'

// #composer-tap-caret: a tap INSIDE the composer's input box puts the caret
// where the tap landed; a drag or a double click never turns into a selection;
// and nothing outside the box (the prompt cell / spacer row above it) may move
// the caret at all — that chrome path is the inert TextInputMouseApi.
//
// Harness notes (both load-bearing, learned the hard way):
//   - Component-level mouse handlers only run while the ink instance is in the
//     ALT SCREEN: `dispatchMouseDown/Drag/Up` in ink.tsx return early when
//     `altScreenActive` is false. That is why these mount inside
//     <AlternateScreen>. Taps in inline/native mode never reached the composer
//     handlers in the first place.
//   - <AlternateScreen> looks the instance up via `instances.get(process.stdout)`,
//     so the render has to own process.stdout (write stubbed, TTY-ish size
//     stubbed) rather than a private PassThrough — otherwise the alt screen is
//     never armed and the assertions below pass for the wrong reason.

const VALUE = 'hello world'
const CARET_AT_END = VALUE.length

const press = (button: number, col: number, row: number) => `\x1b[<${button};${col};${row}M`
const release = (button: number, col: number, row: number) => `\x1b[<${button};${col};${row}m`
const dragTo = (button: number, col: number, row: number) => `\x1b[<${button + 32};${col};${row}M`

const settle = () => new Promise(resolve => setTimeout(resolve, 60))

const mount = async () => {
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true,
    ref: () => {},
    setRawMode: () => {},
    unref: () => {}
  })
  const onChange = vi.fn()
  const out = process.stdout as unknown as NodeJS.WriteStream
  const saved = {
    columns: out.columns,
    isTTY: (out as unknown as { isTTY?: boolean }).isTTY,
    rows: out.rows,
    write: out.write
  }

  Object.assign(out, { columns: 60, isTTY: true, rows: 10 })
  out.write = () => true

  const view = renderSync(
    <AlternateScreen mouseTracking="all">
      <Box height={5} width={60}>
        <TextInput columns={40} onChange={onChange} value={VALUE} />
      </Box>
    </AlternateScreen>,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: out
    }
  )

  const restore = () => {
    view.unmount()
    view.cleanup()
    out.write = saved.write as never
    Object.assign(out, { columns: saved.columns, isTTY: saved.isTTY, rows: saved.rows })
  }

  await settle()
  await vi.waitFor(() => expect(getInputSelection()?.start).toBe(CARET_AT_END))

  return { onChange, restore, stdin }
}

it('places the caret where the tap landed inside the composer', async () => {
  const { onChange, restore, stdin } = await mount()

  try {
    // X10 mouse columns are 1-based, so escape col 4 is caret offset 3.
    stdin.write(press(0, 4, 1) + release(0, 4, 1))
    await vi.waitFor(() => expect(getInputSelection()?.start).toBe(3))
    expect(getInputSelection()?.end).toBe(3)

    // ...and the keyboard must type AT that caret, not at the end.
    stdin.write('!')
    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith('hel!lo world'))
  } finally {
    restore()
  }
})

it('swallows a drag over the composer — no selection, caret where the tap left it', async () => {
  const { restore, stdin } = await mount()

  try {
    stdin.write(press(0, 2, 1) + dragTo(0, 7, 1) + release(0, 7, 1))
    await settle()

    // The press moved the caret to offset 1 (escape col 2); the drag must not
    // extend a selection from there (a drag-select would leave start !== end).
    expect(getInputSelection()?.start).toBe(1)
    expect(getInputSelection()?.end).toBe(1)
  } finally {
    restore()
  }
})

it('does not select-all on a double click in the composer', async () => {
  const { restore, stdin } = await mount()

  try {
    stdin.write(press(0, 4, 1) + release(0, 4, 1) + press(0, 4, 1) + release(0, 4, 1))
    await settle()

    expect(getInputSelection()?.start).toBe(3)
    expect(getInputSelection()?.end).toBe(3)
  } finally {
    restore()
  }
})
