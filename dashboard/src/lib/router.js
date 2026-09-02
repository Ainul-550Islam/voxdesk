/**
 * A hash router.
 *
 * Hash rather than the History API deliberately: the dashboard is served as
 * static files behind the same origin as the API, and `pushState` routing
 * needs a server rewrite so a refresh on `/calls/abc` does not 404. A hash
 * needs nothing, works from `file://`, and the URL is still shareable — which
 * is the actual requirement (the audit found no URL for a call at all).
 */
import { useEffect, useState } from 'react'

export function currentPath() {
  const hash = window.location.hash.replace(/^#/, '')
  return hash || '/'
}

export function navigate(path) {
  if (currentPath() === path) return
  window.location.hash = path
}

export function useRoute() {
  const [path, setPath] = useState(currentPath)

  useEffect(() => {
    const onChange = () => setPath(currentPath())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  return path
}

/**
 * Match `/calls/:id` style patterns.
 *
 * Returns `null` on no match, or an object of captured params.
 */
export function match(pattern, path) {
  const patternParts = pattern.split('/').filter(Boolean)
  const pathParts = path.split('/').filter(Boolean)
  if (patternParts.length !== pathParts.length) return null

  const params = {}
  for (let index = 0; index < patternParts.length; index += 1) {
    const expected = patternParts[index]
    const actual = pathParts[index]
    if (expected.startsWith(':')) {
      params[expected.slice(1)] = decodeURIComponent(actual)
    } else if (expected !== actual) {
      return null
    }
  }
  return params
}

