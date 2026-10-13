"""docs/의 Markdown 문서를 목차·검색이 있는 HTML 파일 하나로 묶는다 (릴리스 첨부용, 오프라인에서 열림).

사용:
    pip install markdown pygments
    python tools/build_docs_html.py                       # dist/quickortho-sdk-docs-<버전>.html
    python tools/build_docs_html.py -o docs.html --strict # 깨진 내부 링크가 있으면 실패 (CI)

- 원본은 docs/*.md 그대로임. 이 스크립트는 읽기만 함
- 문서 사이 링크(`concepts.md#제목`)는 한 파일 안의 앵커로 바꿈. 앵커는 GitHub와 같은 규칙으로 만들므로
  GitHub에서 동작하는 링크는 여기서도 동작함. docs 밖(`../examples/` 등)은 해당 버전 태그의 GitHub 주소로 바꿈
- 외부 글꼴·스크립트를 쓰지 않음 (인터넷 없이 열림). 코드 강조는 빌드할 때 Pygments로 넣음
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import posixpath
import re
import sys
from pathlib import Path

import markdown
import markdown.extensions.toc as md_toc
from pygments.formatters import HtmlFormatter

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REPO = "https://github.com/SeaViewer91/quickortho-sdk"

# 사이드바 묶음과 순서 (docs/README.md의 구성과 같음)
GROUPS: list[tuple[str, list[str]]] = [
    ("처음 쓰는 경우", ["installation.md", "quickstart.md", "concepts.md"]),
    ("개발할 때", ["api-reference.md", "outputs.md", "events-and-cancel.md", "refinement.md", "report.md",
                 "cli.md", "performance.md"]),
    ("문제가 생겼을 때", ["troubleshooting.md"]),
    ("SDK 자체를 고칠 때", ["development.md"]),
]


# ── 앵커: GitHub 규칙 ───────────────────────────────────────────────
def gh_slug(value: str, separator: str = "-") -> str:
    """GitHub 제목 앵커: 소문자, 글자·숫자·밑줄·하이픈·공백만 남기고, 공백을 하이픈으로 (연속 하이픈을 합치지 않음)."""
    s = html.unescape(value).strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def _gh_unique(id_: str, ids) -> str:
    """같은 제목이 여러 번 나오면 GitHub처럼 -1, -2를 붙임."""
    base, n = id_, 0
    while id_ in ids or not id_:
        n += 1
        id_ = f"{base}-{n}"
    ids.add(id_)
    return id_


md_toc.unique = _gh_unique


def version() -> str:
    text = (ROOT / "src/quickortho/_version.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "(.+)"', text)[1]


def page_key(rel: str) -> str:
    """docs 기준 경로 → 페이지 앵커 (`concepts.md` → `concepts`, `release-notes/v0.4.1.md` → `release-v0.4.1`)."""
    rel = rel.removesuffix(".md")
    if rel == "README":
        return "home"
    if rel.startswith("release-notes/"):
        return "release-" + rel.split("/", 1)[1]
    return rel


def _vkey(p: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", p.stem))


# ── 변환 ───────────────────────────────────────────────────────────
class Page:
    def __init__(self, rel: str, ver: str):
        self.rel = rel
        self.key = page_key(rel)
        src = (DOCS / rel).read_text(encoding="utf-8")
        md = markdown.Markdown(
            extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists"],
            extension_configs={
                "codehilite": {"css_class": "hl", "guess_lang": False},
                "toc": {"slugify": gh_slug},
            },
        )
        body = md.convert(src)
        toc = md.toc_tokens
        h1 = toc[0] if toc and toc[0]["level"] == 1 else None
        self.title = html.unescape(h1["name"]) if h1 else rel
        self.sections = [(f"{self.key}--{t['id']}", html.unescape(t["name"]))
                         for t in (h1["children"] if h1 else toc) if t["level"] == 2]
        self.html = self._rewrite(body, ver)

    def _rewrite(self, body: str, ver: str) -> str:
        key, base = self.key, posixpath.dirname(self.rel)
        body = re.sub(r'<h1 id="[^"]*">', f'<h1 id="{key}">', body, count=1)
        body = re.sub(r'(<h[2-6] id=")([^"]+)"', lambda m: f'{m[1]}{key}--{m[2]}"', body)

        def href(m: re.Match) -> str:
            url = html.unescape(m[1])
            if re.match(r"^[a-z]+:", url):
                return f'href="{html.escape(url)}" target="_blank" rel="noopener"'
            path, _, frag = url.partition("#")
            if not path:
                return f'href="#{key}--{frag}"'
            target = posixpath.normpath(posixpath.join(base, path))
            if target.endswith(".md") and not target.startswith(".."):
                k = page_key(target)
                return f'href="#{k}--{frag}"' if frag else f'href="#{k}"'
            # docs 밖: 이 버전 태그의 GitHub 주소
            repo_path = posixpath.normpath(posixpath.join("docs", target))
            kind = "tree" if url.endswith("/") else "blob"
            gh = f"{REPO}/{kind}/v{ver}/{repo_path}" + (f"#{frag}" if frag else "")
            return f'href="{html.escape(gh)}" target="_blank" rel="noopener"'

        return re.sub(r'href="([^"]*)"', href, body)


def build(ver: str) -> tuple[str, list[str]]:
    home = Page("README.md", ver)
    groups = [(name, [Page(f, ver) for f in files]) for name, files in GROUPS]
    notes = sorted((DOCS / "release-notes").glob("v*.md"), key=_vkey, reverse=True)
    rel_pages = [Page(f"release-notes/{p.name}", ver) for p in notes]
    all_pages = [home] + [p for _, ps in groups for p in ps] + rel_pages

    # 내부 링크 검사
    ids = set()
    for p in all_pages:
        ids.add(p.key)
        ids.update(re.findall(r'<h[1-6] id="([^"]+)"', p.html))
    broken = []
    for p in all_pages:
        for frag in re.findall(r'href="#([^"]+)"', p.html):
            if html.unescape(frag) not in ids:
                broken.append(f"{p.rel}: #{html.unescape(frag)}")

    nav = [_nav_group(None, [home])]
    nav += [_nav_group(name, ps) for name, ps in groups]
    nav.append(_nav_releases(rel_pages))
    content = "\n".join(
        f'<section class="doc" data-page="{p.key}" data-title="{html.escape(p.title)}">{p.html}</section>'
        for p in all_pages
    )
    return _page(ver, "\n".join(nav), content), broken


def _nav_group(name: str | None, pages: list[Page]) -> str:
    items = []
    for p in pages:
        sub = "" if p.key == "home" else "".join(
            f'<li><a href="#{sid}">{html.escape(t)}</a></li>' for sid, t in p.sections)
        title = "처음 화면" if p.key == "home" else p.title
        items.append(f'<li class="page" data-page="{p.key}"><a class="page-link" href="#{p.key}">'
                     f'{html.escape(title)}</a>' + (f'<ul class="sub">{sub}</ul>' if sub else "") + "</li>")
    head = f'<p class="group">{html.escape(name)}</p>' if name else ""
    return f'<div class="nav-group">{head}<ul>{"".join(items)}</ul></div>'


def _nav_releases(pages: list[Page]) -> str:
    items = "".join(f'<li class="page" data-page="{p.key}"><a class="page-link" href="#{p.key}">'
                    f'{html.escape(p.title.replace("QuickOrtho SDK ", ""))}</a></li>' for p in pages)
    return f'<div class="nav-group"><p class="group">릴리스 노트</p><ul class="releases">{items}</ul></div>'


# ── 페이지 틀 ───────────────────────────────────────────────────────
def _pygments_css() -> str:
    light = HtmlFormatter(style="friendly").get_style_defs(".hl")
    dark = HtmlFormatter(style="github-dark").get_style_defs(".hl")

    def scope(css: str, prefix: str) -> str:
        out = []
        for line in css.splitlines():
            if line.startswith(".hl") and "background" not in line.split("{", 1)[1].split(";")[0]:
                out.append(f"{prefix} {line}")
            elif line.startswith(".hl"):
                sel, rule = line.split("{", 1)
                rule = re.sub(r"background(-color)?:[^;}]*;?", "", rule)
                out.append(f"{prefix} {sel}{{{rule}")
        return "\n".join(out)

    return (scope(light, ":root") + "\n" + scope(dark, ':root[data-theme="dark"]') + "\n@media (prefers-color-scheme: dark) {\n"
            + scope(dark, ':root:not([data-theme="light"])') + "\n}")


CSS = r"""
:root {
  --paper: #fbfcfa; --panel: #f1f4f1; --ink: #1d2622; --muted: #5b6862; --rule: #d8dfda;
  --accent: #0d6b62; --accent-soft: #e0efec; --warn: #9a5b1c; --code-bg: #f3f5f2; --mark: #ffe8a3;
  --grid: rgba(13, 107, 98, .09);
  color-scheme: light;
}
:root[data-theme="dark"] {
  --paper: #121715; --panel: #18201d; --ink: #dde5e1; --muted: #93a19a; --rule: #2a3531;
  --accent: #5cc4b5; --accent-soft: #1d3330; --warn: #e0a463; --code-bg: #1a221f; --mark: #5a4a12;
  --grid: rgba(92, 196, 181, .08);
  color-scheme: dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #121715; --panel: #18201d; --ink: #dde5e1; --muted: #93a19a; --rule: #2a3531;
    --accent: #5cc4b5; --accent-soft: #1d3330; --warn: #e0a463; --code-bg: #1a221f; --mark: #5a4a12;
    --grid: rgba(92, 196, 181, .08);
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html { scroll-padding-top: 1.25rem; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.75 "Pretendard", "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", system-ui, sans-serif;
  word-break: keep-all; overflow-wrap: break-word;
}
code, pre, kbd { font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, "D2Coding", monospace; font-size: .86em; }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* 사이드바 */
.side {
  position: fixed; inset: 0 auto 0 0; width: 300px; background: var(--panel); border-right: 1px solid var(--rule);
  display: flex; flex-direction: column; z-index: 10;
}
.brand { padding: 1.1rem 1.25rem .4rem; }
.brand b { display: block; font-size: 1.05rem; letter-spacing: -.01em; }
.brand span { color: var(--muted); font-size: .85rem; }
.search { padding: .5rem 1rem .75rem; position: relative; }
.search input {
  width: 100%; padding: .55rem .75rem; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--paper); color: var(--ink); font: inherit; font-size: .92rem;
}
.search input:not(:placeholder-shown) + kbd { display: none; }
.search kbd { position: absolute; right: 1.6rem; top: 1.05rem; color: var(--muted); font-size: .75rem;
  border: 1px solid var(--rule); border-radius: 4px; padding: 0 .35rem; }
nav.toc { overflow-y: auto; padding: 0 .5rem 2rem; flex: 1; font-size: .92rem; }
.nav-group { margin-top: .9rem; }
.group { margin: 0 .75rem .25rem; color: var(--muted); font-size: .8rem; font-weight: 600; }
nav ul { list-style: none; margin: 0; padding: 0; }
.page-link, .sub a { display: block; padding: .28rem .75rem; border-radius: 5px; color: var(--ink); text-decoration: none; line-height: 1.4; }
.page-link:hover, .sub a:hover { background: var(--accent-soft); }
.sub { display: none; margin: .1rem 0 .4rem .75rem; border-left: 1px solid var(--rule); }
.sub a { font-size: .86rem; color: var(--muted); padding-left: .8rem; }
.page.open > .sub { display: block; }
.page.current > .page-link { color: var(--accent); font-weight: 600; }
.sub a.current { color: var(--accent); }
.releases { display: grid; grid-template-columns: 1fr 1fr; }
.side-foot { border-top: 1px solid var(--rule); padding: .6rem 1rem; display: flex; gap: .5rem; align-items: center;
  font-size: .82rem; color: var(--muted); }
.side-foot button { margin-left: auto; }
button { font: inherit; color: var(--ink); background: var(--paper); border: 1px solid var(--rule); border-radius: 5px;
  padding: .25rem .6rem; cursor: pointer; }

/* 검색 결과 */
.results { display: none; overflow-y: auto; flex: 1; padding: 0 .5rem 2rem; font-size: .9rem; }
.side.searching .results { display: block; }
.side.searching nav.toc { display: none; }
.results a { display: block; padding: .5rem .75rem; border-radius: 6px; color: var(--ink); text-decoration: none; }
.results a:hover, .results a.sel { background: var(--accent-soft); }
.results .where { color: var(--muted); font-size: .78rem; }
.results .title { font-weight: 600; }
.results .snip { color: var(--muted); font-size: .84rem; line-height: 1.5; }
.results mark { background: var(--mark); color: inherit; border-radius: 2px; }
.results .none { color: var(--muted); padding: .75rem; }

/* 본문 */
main { margin-left: 300px; padding: 0 3rem 6rem; }
.cover {
  margin: 0 -3rem 1rem; padding: 4.5rem 3rem 2.5rem; border-bottom: 1px solid var(--rule);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 32px 32px; background-position: -1px -1px;
}
.cover h1 { font-size: 2.6rem; line-height: 1.15; margin: 0 0 .6rem; letter-spacing: -.02em; }
.cover p { margin: 0; color: var(--muted); max-width: 46rem; }
.cover .meta { margin-top: 1.25rem; font-size: .9rem; }
.doc { max-width: 52rem; padding-top: 2.5rem; }
.doc + .doc { border-top: 1px solid var(--rule); margin-top: 3rem; }
.doc[data-page="home"] > h1 { display: none; }
h1 { font-size: 2rem; line-height: 1.25; letter-spacing: -.015em; margin: 0 0 1rem; }
h2 { font-size: 1.45rem; line-height: 1.35; margin: 2.6rem 0 .8rem; padding-top: .4rem; }
h3 { font-size: 1.15rem; margin: 2rem 0 .5rem; }
h4 { font-size: 1rem; margin: 1.6rem 0 .4rem; }
h2 code, h3 code, h4 code { font-size: .9em; }
h1, h2, h3, h4 { position: relative; }
.anchor { position: absolute; left: -1.2em; color: var(--muted); text-decoration: none; opacity: 0; font-weight: 400; }
h1:hover .anchor, h2:hover .anchor, h3:hover .anchor, h4:hover .anchor, .anchor:focus { opacity: 1; }
p, li { max-width: 46rem; }
:not(pre) > code { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 4px; padding: .05em .3em; }
.hl { position: relative; margin: 1rem 0; }
.hl pre { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 6px; padding: .9rem 1rem;
  overflow-x: auto; line-height: 1.55; margin: 0; }
pre:not(.hl pre) { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 6px; padding: .9rem 1rem; overflow-x: auto; }
.copy { position: absolute; top: .45rem; right: .45rem; font-size: .75rem; padding: .1rem .5rem; opacity: 0; }
.hl:hover .copy, .copy:focus { opacity: 1; }
.table { overflow-x: auto; margin: 1rem 0; }
table { border-collapse: collapse; font-size: .9rem; line-height: 1.55; min-width: 60%; }
th, td { border-bottom: 1px solid var(--rule); padding: .45rem .7rem; text-align: left; vertical-align: top; }
th { background: var(--panel); font-weight: 600; border-top: 1px solid var(--rule); }
td code { white-space: nowrap; }
blockquote { margin: 1rem 0; padding: .6rem 1rem; border-left: 3px solid var(--warn); background: var(--panel); border-radius: 0 6px 6px 0; }
blockquote p { margin: .2rem 0; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2rem 0; }
.flash { animation: flash 1.2s ease-out; }
@keyframes flash { from { background: var(--mark); } to { background: transparent; } }

.menu { display: none; }
@media (max-width: 900px) {
  .side { transform: translateX(-100%); transition: transform .2s; width: min(320px, 88vw); }
  .side.show { transform: none; box-shadow: 0 0 0 100vmax rgba(0,0,0,.35); }
  main { margin-left: 0; padding: 0 16px 5rem; }
  .cover { margin: 0 -16px 1rem; padding: 4rem 16px 2rem; }
  .cover h1 { font-size: 2rem; }
  .menu { display: block; position: fixed; top: .7rem; right: .7rem; z-index: 11; background: var(--panel); }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
@media print {
  .side, .menu, .copy, .anchor { display: none !important; }
  main { margin: 0; padding: 0; }
  .doc { max-width: none; break-before: page; border: 0 !important; }
  .cover { background: none; }
  a { color: inherit; }
}
"""

JS = r"""
(() => {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const side = $('.side'), input = $('#q'), results = $('.results');
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
                  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

  // 테마
  const root = document.documentElement, themeBtn = $('#theme');
  const applyTheme = t => { if (t) root.dataset.theme = t; else delete root.dataset.theme;
    themeBtn.textContent = { light: '밝게', dark: '어둡게' }[t] || '시스템'; };
  applyTheme(store.get('qo-theme'));
  themeBtn.addEventListener('click', () => {
    const order = [null, 'light', 'dark'], cur = root.dataset.theme || null;
    const next = order[(order.indexOf(cur) + 1) % 3]; applyTheme(next); store.set('qo-theme', next || '');
  });

  // 제목 옆 링크, 표 스크롤, 코드 복사
  $$('main h1[id], main h2[id], main h3[id], main h4[id]').forEach(h => {
    const a = document.createElement('a'); a.className = 'anchor'; a.href = '#' + h.id; a.textContent = '#';
    a.setAttribute('aria-label', '이 제목의 링크'); h.prepend(a);
  });
  $$('main table').forEach(t => { const w = document.createElement('div'); w.className = 'table'; t.before(w); w.append(t); });
  $$('.hl').forEach(b => {
    const btn = document.createElement('button'); btn.className = 'copy'; btn.textContent = '복사';
    btn.addEventListener('click', async () => {
      const text = b.querySelector('pre').innerText;
      try { await navigator.clipboard.writeText(text); }
      catch (e) { const r = document.createRange(); r.selectNodeContents(b.querySelector('pre'));
        const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand('copy'); s.removeAllRanges(); }
      btn.textContent = '복사됨'; setTimeout(() => btn.textContent = '복사', 1200);
    });
    b.append(btn);
  });

  // 현재 위치 표시
  const pageItems = Object.fromEntries($$('nav.toc li.page').map(li => [li.dataset.page, li]));
  const heads = $$('main .doc > h1[id], main .doc > h2[id]');
  let current = null;
  const mark = h => {
    const page = h.closest('.doc').dataset.page;
    if (current !== page) {
      Object.values(pageItems).forEach(li => li.classList.remove('current', 'open'));
      const li = pageItems[page]; if (li) { li.classList.add('current', 'open'); if (!side.matches(':hover')) li.scrollIntoView({ block: 'nearest' }); }
      current = page;
    }
    $$('.sub a.current').forEach(a => a.classList.remove('current'));
    const a = $(`nav.toc a[href="#${CSS.escape(h.id)}"]`); if (a && a.parentElement.closest('.sub')) a.classList.add('current');
  };
  const onScroll = () => {
    let best = heads[0];
    for (const h of heads) { if (h.getBoundingClientRect().top < 120) best = h; else break; }
    if (best) mark(best);
  };
  let ticking = false;
  addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { onScroll(); ticking = false; }); } }, { passive: true });
  onScroll();

  // 이동하면 제목을 잠깐 강조, 모바일은 목차 닫기
  const flash = () => { const t = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (t) { t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash'); } };
  addEventListener('hashchange', () => { flash(); side.classList.remove('show'); });
  $('.menu').addEventListener('click', e => { e.stopPropagation(); side.classList.toggle('show'); });
  document.addEventListener('click', e => { if (side.classList.contains('show') && !side.contains(e.target)) side.classList.remove('show'); });

  // 검색: 제목 단위 구간의 본문을 부분 문자열로 찾음 (한글 조사가 붙어도 찾음)
  const index = [];
  $$('main .doc').forEach(doc => {
    let cur = null;
    const pageTitle = doc.dataset.title;
    for (const el of doc.children) {
      if (/^H[1-4]$/.test(el.tagName) && el.id) {
        cur = { id: el.id, page: pageTitle, title: el.textContent.replace(/^#/, '').trim(), text: '' }; index.push(cur);
      } else if (cur) cur.text += ' ' + el.textContent;
    }
  });
  index.forEach(s => { s.text = s.text.replace(/\s+/g, ' ').trim(); s.lt = s.title.toLowerCase(); s.lx = s.text.toLowerCase(); });
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const hi = (s, terms) => { let out = esc(s); terms.forEach(t => { if (!t) return;
    out = out.replace(new RegExp(esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`); }); return out; };
  let sel = -1;
  const search = () => {
    const q = input.value.trim().toLowerCase();
    side.classList.toggle('searching', !!q);
    if (!q) { results.innerHTML = ''; return; }
    const terms = q.split(/\s+/).filter(Boolean);
    const hits = [];
    for (const s of index) {
      if (!terms.every(t => s.lt.includes(t) || s.lx.includes(t))) continue;
      let score = 0; terms.forEach(t => { if (s.lt.includes(t)) score += 10; if (s.lt.startsWith(t)) score += 5; });
      hits.push([score, s]);
    }
    hits.sort((a, b) => b[0] - a[0]);
    sel = -1;
    if (!hits.length) { results.innerHTML = `<p class="none">"${esc(input.value.trim())}"이(가) 들어간 곳이 없음. 다른 낱말이나 영문 이름(예: make_indices)으로 찾아봄</p>`; return; }
    results.innerHTML = hits.slice(0, 60).map(([, s]) => {
      const i = s.lx.indexOf(terms[0]); const from = Math.max(0, i - 40);
      const snip = i >= 0 ? (from ? '…' : '') + s.text.slice(from, from + 140) + '…' : s.text.slice(0, 140);
      return `<a href="#${esc(s.id)}"><div class="where">${esc(s.page)}</div><div class="title">${hi(s.title, terms)}</div>`
           + `<div class="snip">${hi(snip, terms)}</div></a>`;
    }).join('');
  };
  input.addEventListener('input', search);
  input.addEventListener('keydown', e => {
    const links = $$('a', results);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); if (!links.length) return;
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links.forEach((a, i) => a.classList.toggle('sel', i === sel)); links[sel].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') { (links[sel] || links[0])?.click(); }
    else if (e.key === 'Escape') { input.value = ''; search(); input.blur(); }
  });
  results.addEventListener('click', e => { if (e.target.closest('a')) { input.value = ''; search(); } });
  addEventListener('keydown', e => {
    if ((e.key === '/' && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) || (e.key === 'k' && (e.metaKey || e.ctrlKey))) {
      e.preventDefault(); side.classList.add('show'); input.focus(); input.select();
    }
  });
  if (location.hash) setTimeout(flash, 50);
})();
"""


def _page(ver: str, nav: str, content: str) -> str:
    today = dt.date.today().isoformat()
    cover = (
        '<header class="cover">'
        "<h1>QuickOrtho SDK 사용 설명서</h1>"
        "<p>드론 영상으로 정사 모자이크·DSM·다중분광 반사율을 만드는 Python SDK의 설치, 개념, API, 명령줄, 문제 해결을 한 파일에 담음. "
        "인터넷 없이 열림.</p>"
        f'<p class="meta">버전 {ver} · 문서 생성 {today} · '
        f'<a href="{REPO}/releases/tag/v{ver}" target="_blank" rel="noopener">이 버전의 릴리스</a></p>'
        "</header>"
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QuickOrtho SDK {ver} 문서</title>
<meta name="description" content="QuickOrtho SDK {ver} 사용 설명서 (오프라인 HTML)">
<style>{CSS}
{_pygments_css()}</style>
</head>
<body>
<button class="menu" aria-label="목차 열기">목차</button>
<aside class="side" aria-label="문서 목차">
  <div class="brand"><b>QuickOrtho SDK</b><span>버전 {ver} 문서</span></div>
  <div class="search"><input id="q" type="search" placeholder="문서 검색" aria-label="문서 검색" autocomplete="off"><kbd>/</kbd></div>
  <div class="results" role="listbox" aria-label="검색 결과"></div>
  <nav class="toc">{nav}</nav>
  <div class="side-foot"><a href="{REPO}" target="_blank" rel="noopener">GitHub</a><button id="theme" type="button" title="화면 테마 바꾸기">시스템</button></div>
</aside>
<main>
{cover}
{content}
</main>
<script>{JS}</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, help="결과 HTML (기본 dist/quickortho-sdk-docs-<버전>.html)")
    ap.add_argument("--strict", action="store_true", help="깨진 내부 링크가 있으면 종료 코드 1")
    args = ap.parse_args()
    ver = version()
    out = args.output or ROOT / "dist" / f"quickortho-sdk-docs-{ver}.html"
    doc, broken = build(ver)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"{out} ({out.stat().st_size / 1024:.0f} KB)")
    for b in broken:
        print(f"깨진 링크: {b}", file=sys.stderr)
    if broken:
        print(f"깨진 내부 링크 {len(broken)}개", file=sys.stderr)
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
