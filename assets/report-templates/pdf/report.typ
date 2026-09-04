#let navy = rgb("17324d")
#let blue = rgb("2563a8")
#let cyan = rgb("0e7490")
#let ink = rgb("243447")
#let muted = rgb("667085")
#let line-color = rgb("d8e0e8")
#let paper-blue = rgb("f3f7fb")
#let row-blue = rgb("f7fafc")

$if(title)$
#set document(title: [$title$])
$endif$

#set page(
  paper: "a4",
  margin: (top: 21mm, bottom: 21mm, left: 18mm, right: 18mm),
  header: context {
    if counter(page).get().first() > 2 {
      set text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 7.5pt, fill: muted)
      [DEEP RESEARCH]
      h(1fr)
      [$if(title)$$title$$else$研究报告$endif$]
      v(2.5pt)
      line(length: 100%, stroke: 0.45pt + line-color)
    }
  },
  footer: context {
    if counter(page).get().first() > 1 {
      set text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 7.5pt, fill: muted)
      [研究报告]
      h(1fr)
      [#counter(page).display("1")]
    }
  },
)

#set text(
  font: ("Heiti SC", "STHeiti", "Arial Unicode MS"),
  size: 9.8pt,
  lang: "zh",
  fill: ink,
)
#set par(justify: true, leading: 0.86em, spacing: 1.28em, linebreaks: "optimized")
#set smartquote(enabled: true)
#set heading(numbering: none)
#set list(indent: 1.4em, body-indent: 0.6em, spacing: 0.72em)
#set enum(indent: 1.4em, body-indent: 0.6em, spacing: 0.72em)
#show terms: it => it.children.map(child => [
  #strong[#child.term]
  #block(inset: (left: 1.5em))[#child.description]
]).join()
#set quote(block: true)
#show quote: it => block(
  width: 100%,
  inset: (x: 12pt, y: 8pt),
  fill: paper-blue,
  stroke: (left: 2.5pt + blue),
  radius: 2pt,
)[#it]

#show heading.where(level: 1): it => block(
  above: 2.15em,
  below: 1.05em,
  breakable: false,
  width: 100%,
)[
  #grid(columns: (4pt, 1fr), column-gutter: 9pt,
    rect(width: 4pt, height: 1.25em, fill: cyan, radius: 2pt),
    text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 16.5pt, weight: "bold", fill: navy)[#it.body],
  )
  #v(5pt)
  #line(length: 100%, stroke: 0.6pt + line-color)
]

#show heading.where(level: 2): it => block(
  above: 1.85em,
  below: 0.82em,
  breakable: false,
)[
  #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 13.2pt, weight: "semibold", fill: blue)[#it.body]
]

#show heading.where(level: 3): it => block(
  above: 1.45em,
  below: 0.62em,
  breakable: false,
)[
  #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 11pt, weight: "semibold", fill: navy)[#it.body]
]

#set table(
  inset: (x: 3.5pt, y: 4.3pt),
  stroke: (x: 0.35pt + line-color, y: 0.35pt + line-color),
  fill: (x, y) => if y == 0 { navy } else if calc.even(y) { row-blue } else { white },
)
#show table.cell.where(y: 0): set text(
  font: ("Heiti SC", "STHeiti", "Arial Unicode MS"),
  weight: "semibold",
  fill: white,
)
#show figure.where(kind: table): set block(
  width: 100%, above: 1.15em, below: 1.35em, breakable: true,
)
#show figure.where(kind: table): set text(
  font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 7.6pt,
)
#show figure.where(kind: table): set par(justify: false, leading: 0.63em, spacing: 0.2em)

#show figure.where(kind: image): set block(
  width: 100%, above: 1.45em, below: 1.55em, breakable: false,
)
#show figure.where(kind: image): set align(center)
#show figure.caption: set align(left)
#show figure.caption: set text(
  font: ("Heiti SC", "STHeiti", "Arial Unicode MS"),
  size: 8.3pt,
  fill: muted,
)

#show link: set text(fill: blue)
#show raw: set text(font: ("JetBrains Mono", "SF Mono", "Menlo"), size: 8.2pt, ligatures: true)
#show raw.where(block: true): it => block(
  width: 100%,
  inset: 10pt,
  above: 1.25em,
  below: 1.35em,
  fill: rgb("f6f8fa"),
  stroke: 0.45pt + line-color,
  radius: 3pt,
)[
  #set par(justify: false, leading: 0.7em, spacing: 0pt)
  #it
]

$if(highlighting-definitions)$
$highlighting-definitions$
$endif$

#let horizontalrule = line(length: 100%, stroke: 0.6pt + line-color)

$if(title)$
#page[
  #v(7mm)
  #grid(columns: (auto, 1fr, auto), align: (left, center, right),
    [#rect(width: 5pt, height: 11pt, fill: cyan, radius: 2pt)],
    [#text(size: 8.5pt, weight: "bold", tracking: 0.13em, fill: navy)[DEEP RESEARCH]],
    [#text(size: 8pt, fill: muted)[专题研究报告]],
  )
  #v(17mm)
  #block(
    width: 100%,
    inset: (x: 18pt, y: 22pt),
    fill: gradient.linear(navy, rgb("11546b"), angle: 18deg),
    radius: 6pt,
  )[
    #rect(width: 27mm, height: 3pt, fill: rgb("67e8f9"), radius: 2pt)
    #v(15pt)
    #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 25pt, weight: "bold", fill: white, hyphenate: false)[$title$]
  ]
$if(subtitle)$
  #v(9mm)
  #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 12pt, weight: "semibold", fill: navy)[$subtitle$]
$endif$
  #v(18mm)
  #line(length: 100%, stroke: 0.6pt + line-color)
  #v(7mm)
  #grid(
    columns: (1fr, 1fr, 1fr),
    column-gutter: 5mm,
    [
      #text(size: 7.5pt, weight: "bold", fill: cyan)[研究方法]
      #v(4pt)
      #text(size: 9.2pt, fill: ink)[公开资料与交叉验证]
    ],
    [
      #text(size: 7.5pt, weight: "bold", fill: cyan)[报告类型]
      #v(4pt)
      #text(size: 9.2pt, fill: ink)[结构化深度研究]
    ],
    [
      #text(size: 7.5pt, weight: "bold", fill: cyan)[发布日期]
      #v(4pt)
      #text(size: 9.2pt, fill: ink)[$if(date)$$date$$else$研究报告$endif$]
    ],
  )
  #v(1fr)
  #block(width: 100%, inset: (x: 12pt, y: 9pt), fill: paper-blue, radius: 4pt)[
    #grid(columns: (1fr, auto),
      [#text(size: 8pt, fill: muted)[独立研究 · 证据可追溯 · 结论有边界]],
      [#text(size: 8pt, weight: "bold", fill: navy)[DEEPRESEARCH CLI]],
    )
  ]
]
$endif$

$if(toc)$
#page[
  #v(5mm)
  #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 9pt, weight: "bold", tracking: 0.12em, fill: cyan)[CONTENTS]
  #v(4mm)
  #text(font: ("Heiti SC", "STHeiti", "Arial Unicode MS"), size: 24pt, weight: "bold", fill: navy)[目录]
  #v(8mm)
  #show outline.entry.where(level: 1): set text(size: 13.5pt, weight: "bold")
  #show outline.entry.where(level: 1): set block(above: 0.72em, below: 0.22em)
  #show outline.entry.where(level: 2): set text(size: 11.2pt)
  #outline(title: none, depth: 1, indent: 1.2em)
]
$endif$

$body$
