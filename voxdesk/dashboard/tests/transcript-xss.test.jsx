/**
 * The transcript is the XSS boundary.
 *
 * Every word in it is caller-supplied: speech-to-text will faithfully
 * transcribe `<img src=x onerror=...>` if someone reads it aloud. Finding F16
 * said React's default escaping already made this safe and was worth a
 * regression test rather than a fix. This is that test.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import Transcript from '../src/components/Transcript'

const HOSTILE = '<img src=x onerror="window.__pwned=1"> <script>alert(1)</script>'

const TURNS = [
  { id: 't1', speaker: 'user', text: HOSTILE, created_at: '2026-08-14T18:30:05Z' },
  { id: 't2', speaker: 'assistant', text: 'Understood.',
    created_at: '2026-08-14T18:30:09Z' },
]

describe('transcript rendering', () => {
  it('renders hostile caller text as literal text, not markup', () => {
    const { container } = render(
      <Transcript turns={TURNS} agentName="Riya" timezone="UTC" />
    )

    // Visible as characters...
    expect(screen.getByText(/onerror/)).toBeInTheDocument()
    // ...but never parsed into elements.
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(window.__pwned).toBeUndefined()
  })

  it('keeps search highlighting safe', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <Transcript turns={TURNS} agentName="Riya" timezone="UTC" />
    )

    // Highlighting is the obvious place someone would build an HTML string
    // with <mark> in it and reintroduce the hole.
    await user.type(screen.getByLabelText(/search within this transcript/i), 'img')

    expect(container.querySelectorAll('mark').length).toBeGreaterThan(0)
    expect(container.querySelector('img')).toBeNull()
    expect(window.__pwned).toBeUndefined()
  })

  it('never uses dangerouslySetInnerHTML anywhere in the source tree', async () => {
    const { readFileSync, readdirSync, statSync } = await import('node:fs')
    const { join, resolve } = await import('node:path')

    const offenders = []
    const walk = (dir) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry)
        if (statSync(full).isDirectory()) { walk(full); continue }
        if (!/\.(jsx?|tsx?)$/.test(entry)) continue
        const source = readFileSync(full, 'utf8')
        // Ignore the comments that explain why it is absent.
        const code = source
          .replace(/\/\*[\s\S]*?\*\//g, '')
          .replace(/^\s*\/\/.*$/gm, '')
        if (code.includes('dangerouslySetInnerHTML')) offenders.push(full)
      }
    }
    walk(resolve(process.cwd(), 'src'))

    expect(offenders).toEqual([])
  })
})

describe('speaker labels', () => {
  it("uses the tenant's configured agent name", () => {
    render(<Transcript turns={TURNS} agentName="Riya" timezone="UTC" />)

    expect(screen.getByText('Riya')).toBeInTheDocument()
    expect(screen.getByText('Caller')).toBeInTheDocument()
  })

  it('falls back to a neutral label rather than a hard-coded name', () => {
    render(<Transcript turns={TURNS} agentName={undefined} timezone="UTC" />)

    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.queryByText('Alex')).not.toBeInTheDocument()
  })

  it('never hard-codes "Alex" in the active dashboard source', async () => {
    const { readFileSync, readdirSync, statSync } = await import('node:fs')
    const { join, resolve } = await import('node:path')

    const offenders = []
    const walk = (dir) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry)
        if (statSync(full).isDirectory()) { walk(full); continue }
        if (!/\.jsx?$/.test(entry)) continue
        const code = readFileSync(full, 'utf8')
          .replace(/\/\*[\s\S]*?\*\//g, '')
          .replace(/^\s*\/\/.*$/gm, '')
        if (/['"`]Alex['"`]/.test(code)) offenders.push(full)
      }
    }
    walk(resolve(process.cwd(), 'src'))

    expect(offenders).toEqual([])
  })
})