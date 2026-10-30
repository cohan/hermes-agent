import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import { stripAnsi } from '@hermes/shared/ansi'
import React from 'react'
import { afterEach, expect, it } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

type StatusRuleProps = React.ComponentProps<typeof StatusRule>

// The live-bar swap: a named session's title owns the LEFT slot (where the busy
// ticker / idle status text normally sit) and, while the turn runs, the ticker
// takes the RIGHT edge in the title's old place.  These mount the real rule
// through Ink — `StatusRule(...)` called as a plain function never runs
// FaceTicker, so a tree-only assertion cannot see which side the ticker landed.
//
// `compacting` is used wherever the ticker's CONTENT is asserted: its verb is
// frozen ("compacting…") instead of rotating, so the string is deterministic.

const T0 = 1_800_000_000_000
const mounted: Array<() => void> = []

const FLUSH_MS = 60

const mount = (props: StatusRuleProps) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: props.cols ?? 100, isTTY: false, rows: 20 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', (chunk: Buffer) => {
    output += chunk.toString()
  })

  const instance = renderSync(<StatusRule {...props} />, {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  mounted.push(() => {
    instance.unmount()
    instance.cleanup()
  })

  return () => {
    const lines = stripAnsi(output)
      .split('\n')
      .filter(line => line.trim())

    return lines.at(-1) ?? ''
  }
}

const bar = async (props: Partial<StatusRuleProps> = {}) => {
  const read = mount({ ...baseProps, ...props })

  await new Promise(resolve => setTimeout(resolve, FLUSH_MS))

  return read()
}

const baseProps: StatusRuleProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/repo',
  indicatorStyle: 'kaomoji',
  liveSessionCount: 0,
  model: 'opus-4.8',
  sessionStartedAt: null,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: { context_max: 200_000, context_percent: 25, context_used: 50_000, total: 50_000 },
  voiceLabel: ''
}

afterEach(() => {
  while (mounted.length > 0) {
    mounted.pop()!()
  }
})

it('puts a named session left of the model and hands the right edge back to the cwd', async () => {
  const rendered = await bar({ sessionTitle: 'weekly-digest' })

  expect(rendered).toContain('weekly-digest')
  expect(rendered.indexOf('weekly-digest')).toBeLessThan(rendered.indexOf('opus 4.8'))
  expect(rendered).toContain('~/repo')
})

it('keeps the upstream layout when the session has no title', async () => {
  const rendered = await bar()

  expect(rendered).toContain('ready')
  expect(rendered.indexOf('ready')).toBeLessThan(rendered.indexOf('opus 4.8'))
  expect(rendered).toContain('~/repo')
})

it('moves the busy ticker to the right edge while a titled session works', async () => {
  const rendered = await bar({ busy: true, compacting: true, sessionTitle: 'weekly-digest', turnStartedAt: T0 - 30_000 })

  expect(rendered).toContain('weekly-digest')
  expect(rendered).toContain('compacting…')
  expect(rendered.indexOf('compacting…')).toBeGreaterThan(rendered.indexOf('opus 4.8'))
  // The ticker occupies the edge the cwd would otherwise hold.
  expect(rendered).not.toContain('~/repo')
})

it('leaves the ticker on the left when there is no title', async () => {
  const rendered = await bar({ busy: true, compacting: true, turnStartedAt: T0 - 30_000 })

  expect(rendered).toContain('compacting…')
  expect(rendered.indexOf('compacting…')).toBeLessThan(rendered.indexOf('opus 4.8'))
  expect(rendered).toContain('~/repo')
})

it('drops the title from the left slot when the fields filter omits it', async () => {
  const rendered = await bar({
    sessionTitle: 'weekly-digest',
    statusBarFields: new Set(['model', 'context_pct'])
  })

  expect(rendered).not.toContain('weekly-digest')
  expect(rendered).toContain('~/repo')
})
