import { cloneElement, useEffect, useState } from 'react'
import Icon from './Icon'

/**
 * Markdown rendering for model replies.
 *
 * The model writes Markdown whether asked to or not -- bold labels, lists,
 * tables, code -- and showing raw asterisks makes a correct answer look broken.
 * This covers what actually appears in replies: headings, paragraphs, lists,
 * fenced code, tables, quotes, rules, links and citation markers.
 *
 * It builds React elements directly rather than generating HTML, so there is
 * no `dangerouslySetInnerHTML` anywhere and model output can never inject
 * markup. That matters more than usual here: answers quote text from
 * documents and web pages, neither of which is trusted input.
 */

// Links, bold, strikethrough, inline code, italic, and [1] / [1, 2] markers.
const INLINE =
  /(\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|`[^`\n]+`|\*[^*\n]+\*|\[\d+(?:\s*,\s*\d+)*\])/g
const LINK = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/

function renderInline(text, keyPrefix, onCite) {
  const parts = text.split(INLINE)
  return parts.map((part, i) => {
    const key = `${keyPrefix}-${i}`
    if (!part) return null

    const link = LINK.exec(part)
    if (link) {
      // Only http(s) can match, so a javascript: URL can never become a link.
      return (
        <a key={key} href={link[2]} target="_blank" rel="noreferrer noopener">
          {link[1]}
        </a>
      )
    }
    if ((part.startsWith('**') || part.startsWith('__')) && part.length > 4) {
      return <strong key={key}>{renderInline(part.slice(2, -2), key, onCite)}</strong>
    }
    if (part.startsWith('~~') && part.length > 4) {
      return <del key={key}>{part.slice(2, -2)}</del>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      return <em key={key}>{part.slice(1, -1)}</em>
    }
    // Citation markers pointing at the Sources section of the message.
    if (/^\[\d+(?:\s*,\s*\d+)*\]$/.test(part)) {
      return part
        .slice(1, -1)
        .split(',')
        .map((n) => Number(n.trim()))
        .map((n, j) =>
          onCite ? (
            <button
              key={`${key}-${j}`}
              className="cite"
              onClick={() => onCite(n)}
              aria-label={`Show source ${n}`}
            >
              {n}
            </button>
          ) : (
            <sup key={`${key}-${j}`} className="cite">
              {n}
            </sup>
          ),
        )
    }
    return <span key={key}>{part}</span>
  })
}

function CodeBlock({ lang, code }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(false), 1400)
    return () => clearTimeout(t)
  }, [copied])

  return (
    <div className="rt__code">
      <div className="rt__codehead">
        <span>{lang || 'code'}</span>
        <button
          onClick={() => navigator.clipboard?.writeText(code).then(() => setCopied(true))}
          aria-label="Copy code"
        >
          <Icon name={copied ? 'check' : 'copy'} size={13} />
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

const FENCE = /^\s*```\s*([\w+#.-]*)\s*$/
const BULLET = /^\s*[-*•+]\s+(.*)$/
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/
const HEADING = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/
const QUOTE = /^\s*>\s?(.*)$/
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/
const TABLE_ROW = /^\s*\|.*\|\s*$/
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

const splitRow = (line) =>
  line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim())

export default function RichText({ text, onCite, trailing }) {
  if (!text) return trailing ?? null

  const lines = text.split('\n')
  const blocks = []
  let list = null // { ordered, start, items }
  let paragraph = []
  let quote = []

  const inline = (s, key) => renderInline(s, key, onCite)

  const flushParagraph = () => {
    if (!paragraph.length) return
    const key = `p-${blocks.length}`
    blocks.push(
      <p key={key} className="rt__p">
        {inline(paragraph.join('\n'), key)}
      </p>,
    )
    paragraph = []
  }

  const flushList = () => {
    if (!list) return
    const key = `l-${blocks.length}`
    const Tag = list.ordered ? 'ol' : 'ul'
    blocks.push(
      <Tag key={key} className="rt__list" start={list.ordered ? list.start : undefined}>
        {list.items.map((item, i) => (
          <li key={`${key}-${i}`}>{inline(item, `${key}-${i}`)}</li>
        ))}
      </Tag>,
    )
    list = null
  }

  const flushQuote = () => {
    if (!quote.length) return
    const key = `q-${blocks.length}`
    blocks.push(
      <blockquote key={key} className="rt__quote">
        {inline(quote.join('\n'), key)}
      </blockquote>,
    )
    quote = []
  }

  const flushAll = () => {
    flushParagraph()
    flushList()
    flushQuote()
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Fenced code runs to the closing fence, or to the end of the text while
    // a reply is still streaming and the fence has not been closed yet.
    const fence = FENCE.exec(line)
    if (fence) {
      flushAll()
      const body = []
      i++
      while (i < lines.length && !FENCE.test(lines[i])) body.push(lines[i++])
      blocks.push(<CodeBlock key={`c-${blocks.length}`} lang={fence[1]} code={body.join('\n')} />)
      continue
    }

    // A table needs a header row followed by a |---|---| separator.
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      flushAll()
      const header = splitRow(line)
      const rows = []
      i += 2
      while (i < lines.length && TABLE_ROW.test(lines[i])) rows.push(splitRow(lines[i++]))
      i--
      const key = `t-${blocks.length}`
      blocks.push(
        <div key={key} className="rt__tablewrap">
          <table className="rt__table">
            <thead>
              <tr>
                {header.map((h, c) => (
                  <th key={c}>{inline(h, `${key}-h${c}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {header.map((_, c) => (
                    <td key={c}>{inline(row[c] ?? '', `${key}-${r}-${c}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    if (RULE.test(line)) {
      flushAll()
      blocks.push(<hr key={`r-${blocks.length}`} className="rt__rule" />)
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      flushAll()
      const level = Math.min(heading[1].length, 4)
      const key = `h-${blocks.length}`
      blocks.push(
        <p key={key} className={`rt__h rt__h${level}`} role="heading" aria-level={level + 1}>
          {inline(heading[2], key)}
        </p>,
      )
      continue
    }

    const q = QUOTE.exec(line)
    if (q) {
      flushParagraph()
      flushList()
      quote.push(q[1])
      continue
    }
    flushQuote()

    const bullet = BULLET.exec(line)
    const numbered = NUMBERED.exec(line)
    if (bullet || numbered) {
      flushParagraph()
      const ordered = Boolean(numbered)
      const content = bullet ? bullet[1] : numbered[2]
      // A change of list type starts a new list rather than mixing them.
      if (!list || list.ordered !== ordered) {
        flushList()
        list = { ordered, start: numbered ? Number(numbered[1]) : 1, items: [] }
      }
      list.items.push(content)
      continue
    }

    if (!line.trim()) {
      flushAll()
      continue
    }

    flushList()
    paragraph.push(line)
  }

  flushAll()

  // Put a trailing node (the streaming caret) inside the last text block, so
  // it sits at the end of the final line rather than on a line of its own.
  if (trailing) {
    const last = blocks[blocks.length - 1]
    if (last && (last.type === 'p' || last.type === 'blockquote')) {
      blocks[blocks.length - 1] = cloneElement(last, {}, last.props.children, trailing)
    } else if (last && (last.type === 'ul' || last.type === 'ol')) {
      const items = last.props.children
      const tail = items[items.length - 1]
      blocks[blocks.length - 1] = cloneElement(
        last,
        {},
        [...items.slice(0, -1), cloneElement(tail, {}, tail.props.children, trailing)],
      )
    } else {
      blocks.push(<span key="trailing">{trailing}</span>)
    }
  }

  return <div className="rt">{blocks}</div>
}
