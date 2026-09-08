import type { ReactNode } from "react";

/**
 * Dependency-free Markdown renderer for assistant replies.
 * Blocks: fenced code, headings, bullet/numbered lists, pipe tables, horizontal rules, paragraphs.
 * Inline: `code`, **bold**, *italic*, [text](https://url), bare URLs. Only http(s) links are rendered.
 */

const INLINE_RE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)|https?:\/\/[^\s<>)]+)/g;

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const s = m[0];
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const k = `${keyBase}-${i++}`;
    if (s.startsWith("`")) out.push(<code key={k}>{s.slice(1, -1)}</code>);
    else if (s.startsWith("**") || s.startsWith("__")) out.push(<strong key={k}>{s.slice(2, -2)}</strong>);
    else if (s.startsWith("[")) {
      const t = s.slice(1, s.indexOf("]("));
      const href = s.slice(s.indexOf("](") + 2, -1);
      out.push(<a key={k} href={href} target="_blank" rel="noreferrer noopener">{t}</a>);
    } else if (s.startsWith("http")) out.push(<a key={k} href={s} target="_blank" rel="noreferrer noopener">{s}</a>);
    else out.push(<em key={k}>{s.slice(1, -1)}</em>);
    last = at + s.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function lines(text: string, keyBase: string): ReactNode[] {
  const parts = text.split("\n");
  return parts.flatMap((l, i) => (i < parts.length - 1 ? [...inline(l, `${keyBase}-${i}`), <br key={`${keyBase}-br${i}`} />] : inline(l, `${keyBase}-${i}`)));
}

function table(rows: string[], key: string): ReactNode {
  const cells = (r: string) => r.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  return (
    <div className="md-table" key={key}>
      <table>
        <thead>
          <tr>{head.map((h, i) => <th key={i}>{inline(h, `${key}-h${i}`)}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}>{inline(c, `${key}-${i}-${j}`)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function renderMarkdown(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const src = text.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  let k = 0;
  const para: string[] = [];
  const flush = () => {
    if (para.length) {
      out.push(<p key={`p${k++}`}>{lines(para.join("\n"), `p${k}`)}</p>);
      para.length = 0;
    }
  };
  while (i < src.length) {
    const line = src[i];
    if (line.startsWith("```")) {
      flush();
      const lang = line.slice(3).trim();
      const buf: string[] = [];
      i++;
      while (i < src.length && !src[i].startsWith("```")) buf.push(src[i++]);
      i++;
      out.push(
        <pre key={`c${k++}`} data-lang={lang || undefined}>
          {buf.join("\n")}
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      const level = h[1].length;
      const content = inline(h[2], `h${k}`);
      out.push(level === 1 ? <h3 key={`h${k++}`}>{content}</h3> : level === 2 ? <h4 key={`h${k++}`}>{content}</h4> : <h5 key={`h${k++}`}>{content}</h5>);
      i++;
      continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s-*_]*$/.test(line)) {
      flush();
      out.push(<hr key={`hr${k++}`} />);
      i++;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < src.length && /^\s*\|?\s*:?-{2,}/.test(src[i + 1])) {
      flush();
      const rows: string[] = [];
      while (i < src.length && /^\s*\|.*\|\s*$/.test(src[i])) rows.push(src[i++].trim());
      out.push(table(rows, `t${k++}`));
      continue;
    }
    const li = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      const items: string[] = [];
      while (i < src.length) {
        const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(src[i]);
        if (m) {
          items.push(m[3]);
          i++;
        } else if (src[i].startsWith("  ") && items.length) {
          items[items.length - 1] += "\n" + src[i].trim();
          i++;
        } else break;
      }
      const els = items.map((it, j) => <li key={j}>{lines(it, `li${k}-${j}`)}</li>);
      out.push(ordered ? <ol key={`l${k++}`}>{els}</ol> : <ul key={`l${k++}`}>{els}</ul>);
      continue;
    }
    if (line.trim() === "") {
      flush();
      i++;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out;
}

export default function Markdown({ text }: { text: string }) {
  return <div className="md">{renderMarkdown(text)}</div>;
}
