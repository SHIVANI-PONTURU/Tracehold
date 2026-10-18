// Picks the guide entry for a free-text question. Whole-string fuzzy search missed most natural phrasings
// ("what does the tracer do") and matched filler words ("what is taint" found "What is TraceHold?"), so this
// scores content words instead: stop words dropped, light stemming, rarer words weigh more (IDF).

import type { GuideEntry } from './guide'

const STOP = new Set(('a an the is are was were be been am do does did doing done how what why who whom which when where ' +
  'i me my we our you your it its this that these those there here of to in on at for from by with about into than then ' +
  'and or but not no so if can could should would will shall may might must has have had having get got please tell ' +
  'explain show mean means meaning work works working use used using much many some any all each one thing things ' +
  'happen happens kind sort really just also very more most only s t').split(' '))

export function stem(w: string): string {
  if (w.length > 4 && w.endsWith('ies')) w = w.slice(0, -3) + 'y'
  else if (w.length > 5 && w.endsWith('ing')) w = w.slice(0, -3)
  else if (w.length > 4 && w.endsWith('ed')) w = w.slice(0, -2)
  else if (w.length > 4 && /(ss|x|ch|sh)es$/.test(w)) w = w.slice(0, -2)
  else if (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) w = w.slice(0, -1)
  // "approve" and "approved" both end as "approv"
  return w.length > 4 && w.endsWith('e') ? w.slice(0, -1) : w
}

const SYNONYM: Record<string, string> = { color: 'colour', frozen: 'freez', hitl: 'gate', approval: 'approv' }

export function tokens(text: string): string[] {
  return text.toLowerCase().replace(/[’']/g, '').split(/[^a-z0-9]+/).filter(w => w && !STOP.has(w))
    .map(w => { const s = stem(w); return SYNONYM[s] ?? s })
}

// Weights: a word in the entry's first question (its title) counts most, then the other questions and keywords,
// then the answer text.
const W_TITLE = 2, W_TERM = 1, W_ANSWER = 0.4
// A question whose words are exactly those of one of the entry's listed questions (the suggestion chips, for one).
const PHRASE_BONUS = 3
const MIN_SCORE = 1.2, MIN_COVER = 0.5

export interface Matcher { best: (q: string) => GuideEntry | null; scored: (q: string) => { id: string; score: number; cover: number }[] }

export function makeMatcher(guide: GuideEntry[]): Matcher {
  const index = guide.map(e => {
    const w = new Map<string, number>()
    const add = (text: string, weight: number) => { for (const t of tokens(text)) w.set(t, Math.max(w.get(t) ?? 0, weight)) }
    add(e.a, W_ANSWER)
    add([...e.q.slice(1), e.keys].join(' '), W_TERM)
    add(e.q[0], W_TITLE)
    const phrases = e.q.map(x => [...new Set(tokens(x))].sort().join(' ')).filter(Boolean)
    return { e, w, phrases }
  })
  const df = new Map<string, number>()
  for (const { w } of index) for (const t of w.keys()) df.set(t, (df.get(t) ?? 0) + 1)
  const idf = (t: string) => Math.log(1 + guide.length / (df.get(t) ?? guide.length))

  const scored = (q: string) => {
    const qt = [...new Set(tokens(q))]
    const key = [...qt].sort().join(' ')
    // A word no entry knows still counts against coverage, so an off-topic question is not answered from one stray word.
    const mass = qt.reduce((s, t) => s + (df.has(t) ? idf(t) : 1), 0)
    return index.map(({ e, w, phrases }) => {
      let score = key && phrases.includes(key) ? PHRASE_BONUS : 0, hit = 0
      for (const t of qt) { const x = w.get(t); if (x) { score += x * idf(t); hit += idf(t) } }
      return { id: e.id, score, cover: mass ? hit / mass : 0 }
    }).filter(r => r.score > 0).sort((a, b) => b.score - a.score || b.cover - a.cover)
  }
  const best = (q: string) => {
    const top = scored(q)[0]
    return top && top.score >= MIN_SCORE && top.cover >= MIN_COVER ? guide.find(e => e.id === top.id)! : null
  }
  return { best, scored }
}
