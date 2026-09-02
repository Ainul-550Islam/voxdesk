/**
 * The transcript viewer.
 *
 * **Every piece of text here is caller-supplied.** A caller can say anything,
 * and speech-to-text will faithfully transcribe `<img src=x onerror=...>` if
 * someone reads it aloud. So:
 *
 * - Turn text is rendered as a React child, never `dangerouslySetInnerHTML`.
 *   React escapes it. That is the whole defence and it is enough, provided
 *   nobody reaches for the escape hatch later — hence the tests that assert
 *   the string does not appear anywhere in the source tree.
 * - Search highlighting splits the string and renders the pieces as separate
 *   children rather than building an HTML string with `<mark>` in it. Building
 *   the string would be the obvious way to do this and would reintroduce the
 *   vulnerability the rest of the file avoids.
 * - `white-space: pre-wrap` preserves the shape of what was said without
 *   letting markup through.
 */
import { useMemo, useState } from 'react'

import { formatDateTime } from '../lib/format'
import { EmptyState } from './ui'

/**
 * Split `text` on `term`, case-insensitively, into plain and matched pieces.
 *
 * Returns an array of React children. Never returns a string containing
 * markup.
 */
function highlight(text, term) {
  if (!term) return text
  const needle = term.toLowerCase()
  const haystack = String(text)
  const pieces = []
  let cursor = 0

  for (;;) {
    const found = haystack.toLowerCase().indexOf(needle, cursor)
    if (found === -1) break
    if (found > cursor) pieces.push(haystack.slice(cursor, found))
    pieces.push(
      <mark key={`${found}-${pieces.length}`}>
        {haystack.slice(found, found + needle.length)}
      </mark>
    )
    cursor = found + needle.length
  }
  if (cursor === 0) return haystack
  pieces.push(haystack.slice(cursor))
  return pieces
}

const SPEAKER_LABEL = {
  user: 'Caller',
  system: 'System',
}

export default function Transcript({ turns, agentName, timezone, onCopy }) {
  const [term, setTerm] = useState('')

  const filtered = useMemo(() => {
    if (!term) return turns
    const needle = term.toLowerCase()
    return turns.filter((turn) => (turn.text ?? '').toLowerCase().includes(needle))
  }, [turns, term])

  if (!turns?.length) {
    return (
      <EmptyState
        icon="⌸"
        title="No transcript"
        description="This call has no recorded turns. Very short or failed calls often have none."
      />
    )
  }

  return (
    <div>
      <div className="toolbar">
        <div className="toolbar__grow">
          <label className="sr-only" htmlFor="transcript-search">
            Search within this transcript
          </label>
          <input
            id="transcript-search" type="search" className="input"
            placeholder="Search this transcript…"
            value={term} onChange={(event) => setTerm(event.target.value)}
          />
        </div>
        <span className="muted" style={{ fontSize: 12 }} aria-live="polite">
          {term
            ? `${filtered.length} of ${turns.length} turns match`
            : `${turns.length} turns`}
        </span>
        {onCopy && (
          <button type="button" className="btn btn--small" onClick={onCopy}>
            Copy transcript
          </button>
        )}
      </div>

      {filtered.length === 0 ? (
        <EmptyState title="Nothing in this transcript matches" />
      ) : (
        <ol className="transcript" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {filtered.map((turn, index) => {
            const speaker = String(turn.speaker ?? '').toLowerCase()
            const label =
              SPEAKER_LABEL[speaker] ?? (agentName || 'Agent')
            return (
              <li className={`turn turn--${speaker}`} key={turn.id ?? index}>
                <div className="turn__who">{label}</div>
                {/*
                  `{...}` and not `dangerouslySetInnerHTML`. This is the XSS
                  boundary for everything a caller ever said.
                */}
                <div className="turn__text">{highlight(turn.text ?? '', term)}</div>
                {turn.created_at && (
                  <time
                    className="turn__time"
                    dateTime={turn.created_at}
                  >
                    {formatDateTime(turn.created_at, timezone, {
                      year: undefined, month: undefined, day: undefined,
                      second: '2-digit',
                    })}
                  </time>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

