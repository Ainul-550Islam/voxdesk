/**
 * Data-fetching hooks.
 *
 * One place for loading, error, retry and cancellation. The audit found this
 * inlined in `App.jsx`; with thirteen pages that would have become thirteen
 * copies, each with its own bugs.
 *
 * Every fetch is guarded against setting state after unmount, which is what
 * produces the "update on an unmounted component" warnings that people learn
 * to ignore and then miss a real one behind.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from './api'

/**
 * Run `fetcher` when `deps` change.
 *
 * Returns `{ data, error, loading, reload }`. `error` is always an `ApiError`,
 * so a page can branch on `.status` for a permission-denied state without
 * knowing anything about fetch.
 */
export function useApi(fetcher, deps = [], { skip = false } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: !skip })
  const alive = useRef(true)
  const requestId = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const run = useCallback(async () => {
    if (skip) {
      setState({ data: null, error: null, loading: false })
      return
    }
    const id = ++requestId.current
    setState((previous) => ({ ...previous, loading: true, error: null }))
    try {
      const data = await fetcher()
      // A slow first request must not overwrite a fast second one.
      if (alive.current && id === requestId.current) {
        setState({ data, error: null, loading: false })
      }
    } catch (error) {
      if (alive.current && id === requestId.current) {
        setState({
          data: null,
          error: error instanceof ApiError
            ? error
            : new ApiError(0, 'Something went wrong. Please try again.'),
          loading: false,
        })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skip, ...deps])

  useEffect(() => { run() }, [run])

  return { ...state, reload: run }
}

/**
 * An action with its own pending and error state.
 *
 * Separate from `useApi` because a mutation must not clear the page's data
 * while it runs — a table that empties itself when you click "cancel" looks
 * broken.
 */
export function useAction(action, { onSuccess } = {}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const run = useCallback(async (...args) => {
    setPending(true)
    setError(null)
    try {
      const result = await action(...args)
      if (alive.current) setPending(false)
      onSuccess?.(result)
      return result
    } catch (caught) {
      if (alive.current) {
        setError(caught instanceof ApiError
          ? caught
          : new ApiError(0, 'Something went wrong. Please try again.'))
        setPending(false)
      }
      return null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action, onSuccess])

  return { run, pending, error, clearError: () => setError(null) }
}

/**
 * Backend pagination state.
 *
 * Requirement 23: page, size, filters and search survive navigation within
 * the page. Changing a filter resets to page 1 — staying on page 7 of a
 * result set that now has two pages shows an empty table and looks like a bug.
 */
export function usePagination(pageSize = 25) {
  const [offset, setOffset] = useState(0)
  const [limit, setLimit] = useState(pageSize)

  return useMemo(() => ({
    offset,
    limit,
    page: Math.floor(offset / limit) + 1,
    setPage: (page) => setOffset(Math.max(0, (page - 1) * limit)),
    setLimit: (next) => { setLimit(next); setOffset(0) },
    reset: () => setOffset(0),
    pageCount: (total) => Math.max(1, Math.ceil((total || 0) / limit)),
  }), [offset, limit])
}

/** Debounce a value, so a search box does not fire a request per keystroke. */
export function useDebounced(value, delay = 350) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/** Escape and outside-click handling for dialogs and menus. */
export function useDismiss(active, onDismiss) {
  const ref = useRef(null)
  useEffect(() => {
    if (!active) return undefined
    const onKey = (event) => { if (event.key === 'Escape') onDismiss() }
    const onClick = (event) => {
      if (ref.current && !ref.current.contains(event.target)) onDismiss()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onClick)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onClick)
    }
  }, [active, onDismiss])
  return ref
}

/** Track a media query, for layout that genuinely differs on small screens. */
export function useMediaQuery(queryString) {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia(queryString).matches
      : false
  )
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined
    const list = window.matchMedia(queryString)
    const onChange = (event) => setMatches(event.matches)
    list.addEventListener('change', onChange)
    setMatches(list.matches)
    return () => list.removeEventListener('change', onChange)
  }, [queryString])
  return matches
}
