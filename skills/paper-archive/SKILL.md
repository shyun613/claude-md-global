---
name: paper-archive
description: PaperArchive(~/workspace/PaperArchive) 논문 아카이브 조회/저장 스킬. 어느 프로젝트에서든 논문이 언급되면 사용 — "이 논문 아카이브에 있어?", "논문 찾아줘", "bib 가져와줘", "이 논문 저장/아카이브해줘", "citation 필요해", 새 논문을 웹에서 읽고 참조하게 된 모든 상황. 논문 제목/arXiv ID가 대화에 등장하면 웹 검색 전에 반드시 이 스킬로 아카이브를 먼저 조회.
---

# paper-archive — 논문 저장소 인터페이스

아카이브 루트: `~/workspace/PaperArchive` (이하 `$PA`)
> 다른 경로에 뒀다면 이 파일의 `$PA`를 그 경로로 바꿀 것.

## 1. LOOKUP — 항상 먼저

```bash
python3 $PA/scripts/paper_lookup.py search "<arXiv ID | 제목 | citekey | 키워드>"
```

- **히트** (exit 0, `bm25=` 없음) → 그 노트를 읽고 인용해 답한다. BibTeX는 노트의
  `## BibTeX` 섹션 또는 `$PA/01_PAPERS/references.bib`에서.
- **미스** (exit 1) → 웹 검색/WebFetch로 진행. 실제로 읽고 참조했다면 §2로 저장.
- 정확한 제목/ID를 모르는 탐색적 질의는 exact 티어 미스 시 BM25 폴백이 자동 작동.
  `search --bm25`로 랭킹 모드 강제 가능.

**exit code = 판정이다. 이걸로 갈린다:**

| exit | 뜻 | 할 일 |
|---|---|---|
| `0`, `bm25=` 없음 | exact 히트 (arXiv/citekey/제목/토큰) | 그대로 신뢰, 인용 |
| `0`, `bm25=` 있음 | 후보 — 제목·태그·토픽이 걸림 | **제목·arXiv ID 대조 후 채택** |
| `3` (줄 끝 `weak` 표시) | 약한 후보 — 본문 어휘만 겹침 | **미스로 취급, §2 INGEST로 진행** |
| `1` | 미스 (`not found`) | 웹 검색 → §2 |
| `2` | 카탈로그 없음 | `paper_lookup.py rebuild` 후 재시도 |

> [!warning] `bm25=` 가 붙은 줄은 **히트가 아니라 후보다**
> 실측(2026-08-11): 볼트에 없는 `"Attention Is All You Need"`를 검색하자
> **Far3D 노트를 `bm25=1.73`으로 반환**했다. 같은 분야 논문일수록 어휘가 겹쳐
> 오답 확률이 높다. 이 사례는 이제 **exit 3 + `| weak`**로 걸러진다 —
> 본문에서만 겹쳤을 뿐 제목·태그에는 없기 때문이다.
>
> exit 3은 미스와 같이 취급한다(경고 헤더 `# 약한 후보 ...`가 함께 출력된다).
> exit 0 + `bm25=`도 자동 채택은 금지 — 반환된 노트의 제목·arXiv ID가 찾던 논문과
> 실제로 같은지 대조하고, 아니면 §2 INGEST로 간다. **점수 크기는 신뢰도가 아니다.**
> (`--bm25`를 직접 부른 경우는 랭킹 도구이므로 weak이어도 exit 0이다 — ` | weak`
> 표시로 판단할 것.)

**status = 그 노트를 인용해도 되는지 가른다. exit code만큼 중요하다.**
조회 출력의 마지막 칸이 노트의 `status`다:

| status | 뜻 | 할 일 |
|---|---|---|
| `summarized` · `quick` | **정독본** — 원문을 읽고 쓴 노트 | 인용 가능 (수치는 §1.5로 원문 재확인) |
| `metadata` | **리스트업본** — 서지정보와 초록만. TL;DR·Method는 비어 있다 | **인용 근거로 쓰지 마라.** "이런 논문이 있다"는 포인터로만 쓰고, 내용이 필요하면 §2.5로 승격한다 |

> [!warning] `metadata` 노트를 읽고 요약하지 마라
> 초록만 보고 방법·기여를 서술하면 그건 정독이 아니라 추측이다. 조회는 성공했어도
> **알맹이가 없는 상태**다. 리스트업은 "또 찾는 낭비"를 막으려고 해둔 것이지
> 읽었다는 뜻이 아니다. 내용이 필요하면 §2.5로 승격한 뒤에 인용한다.

## 1.5 VERIFY — 수치·인용의 원문 검증 (집필 시 필수)

노트는 요약이다. 요약을 다시 인용하면 압축이 두 번 걸린다.
**타 논문의 수치·표·문장을 인용할 때는 노트가 아니라 전문에서 확인한다:**

```bash
python3 $PA/scripts/paper_lookup.py fulltext "<정규식>" [--citekey <key>] [-C 2] [-i]
```

- 출력 `citekey p.N: <줄>` — 페이지 앵커 원문 (pdftotext 텍스트 레이어)
- 최종 원고에 들어갈 수치는 p.N으로 원 PDF를 눈으로 한 번 더 확인
- 전문이 없으면 아카이브 세션에서 `python3 scripts/build_fulltext.py`로 보충

## 2. INGEST — 아카이브에 없는 논문을 읽었을 때 (quick 노트)

1. arXiv abs 페이지(또는 ar5iv/HTML)를 WebFetch로 **실제로 읽는다** (초록만으로 요약 금지)
2. bib 확보:
   `python3 $PA/scripts/paper_bib.py --arxiv <ID> --title "<제목>" --authors "<A, B>" --year <Y>`
3. 노트 작성: `$PA/01_PAPERS/<topic>/<YYYY>-<성>-<제목슬러그20자>.md`
   - topic 목록 확인: `python3 -c "import sys; sys.path.insert(0,'$PA/scripts'); import config; print(config.topics())"`
   - 어디에 넣을지 애매하면 `_toread`. 나중에 옮기는 게 잘못 분류하는 것보다 싸다.
4. `python3 $PA/scripts/paper_sync.py --note $PA/01_PAPERS/<topic>/<파일명>.md`
   → PDF 확보 · 클라우드 push · Zotero 등재가 단계별 ✅/❌로 출력된다.
   **그 출력을 사용자에게 그대로 보고**할 것 — 미완 단계가 있으면 재시도 명령까지.
   (실패해도 exit 0이며 노트는 이미 저장된 상태다. 미완 단계는 pending 큐에 남아
   `--retry-pending`으로 회수된다. 설정이 없는 기능(rclone·Zotero 키)은 실패가 아니라
   "안 쓰는 구성"으로 표시된다)
5. `python3 $PA/scripts/paper_lookup.py rebuild`
5.5 **MOC 편입 (빠뜨리면 lint가 반드시 걸린다)** — `paper_sync.py`는 MOC를 건드리지 않는데
   `lint_wiki.py`의 `check_moc_coverage`는 `_toread` 밖의 모든 노트가 어느 MOC엔가
   `[[위키링크]]`로 걸려 있기를 요구한다. 그러니 이 단계를 직접 한다:
   `$PA/03_WIKI/moc-<topic>.md`의 `## 신규 유입 (미분류)` 아래에 한 줄 append —
   `- [[<노트파일명 stem (.md 제외)>]] — <한 줄 요약>`
   (`_toread`에 넣은 노트는 예외이므로 생략한다. §3의 "위키 편집은 아카이브 세션의 몫"
   금지 조항은 기존 위키 페이지 개편에 대한 것이고, 이 append와 step 6의 `_LOG.md`
   기록은 ingest 절차의 일부다.)
6. `$PA/03_WIKI/_LOG.md`에 append:
   `## [YYYY-MM-DD] ingest(quick) | <제목>` + Source/Note/Next 3줄
7. 사용자에게 안내: "quick 노트로 저장했고 PDF·클라우드·Zotero까지 반영했습니다 —
   아카이브 세션에서 `python3 scripts/quick_upgrade.py`를 돌리면 정식 요약으로
   승격됩니다" (NotebookLM을 안 쓰면 이 단계는 건너뛰어도 된다)
8. **관련성을 묻는다** — *"이 논문이 지금 하시는 일과 어떻게 걸리는지 한 줄 주시면
   `## Relevance to My Work`에 기록하겠습니다 (건너뛰셔도 됩니다)"*
   - 답을 주면 **그 말을 그대로** 그 섹션에 적는다. 다듬거나 살을 붙이지 마라
   - 답이 없으면 **비워둔 채로 둔다.** 빈 칸은 "저장했지만 아직 안 읽음"이라는 정보다
   - ⛔ **네가 판단해서 채우는 것은 여전히 금지.** 묻고 받아적는 것만 허용된다 —
     기준은 타이핑을 누가 하느냐가 아니라 **판단의 주체가 누구냐**다
   > 사용자 방침(2026-08-11): 기존 리스트업본 105편은 비워둔 채로 두고,
   > **새로 추가되는 논문만** 하나하나 채운다. 그래서 이 단계가 신규 ingest에만 있다.
   > `lint_wiki.py`의 `Relevance 공백(정보)` 항목은 **위반이 아니라 집계**다 —
   > 정독본 기준 개수만 찍고 노트를 나열하지 않으며, `status: metadata` 노트는 제외한다.

### 노트 서술 규약 (quick·summarized 공통)

- **본문 산문은 한국어로 쓴다.** Problem Statement / Method Overview / Key Contributions /
  Results / Limitations 등 설명 문장은 한국어 기술문.
- **논문 직접 인용은 영어 원문 그대로 둔다.** 큰따옴표로 감싼 구절은 번역하지 않는다 —
  인용 정확성이 가독성보다 우선이며, `$PA/fulltext/<citekey>.md` 로 대조 가능해야 한다.
- **TL;DR은 한/영 병기**(`**한국어** — …` / `**English** — …`) 를 유지한다.
- **수식·기호는 LaTeX `$...$`(인라인) / `$$...$$`(디스플레이) 로 쓴다.**
  유니코드 첨자·기호를 쓰지 않는다: `ℒ_ord` → `$\mathcal{L}_{\mathrm{ord}}$`,
  `σ_k²` → `$\sigma_k^2$`, `z_H` → `$z_H$`, `ℝ^{K×D}` → `$\mathbb{R}^{K \times D}$`.
  Obsidian이 `$` 수식을 네이티브로 렌더하고, HTML export(`scripts/export_html.py`)도
  KaTeX로 같은 표기를 렌더한다. 코드블록 안은 변환하지 않는다.
- 헤딩 제목은 영어 그대로 둔다(섹션 구조가 도구·스크립트의 계약이다).

### quick 노트 템플릿

````markdown
---
title: "<제목 그대로>"
authors: [<저자 전체>]
year: <YYYY>
venue: <학회/저널 또는 arXiv>
arxiv: "<ID, 버전 제거>"
doi: "<있으면>"
citekey: <paper_bib 출력의 citekey>
topics: [<topic>]
tags: [<소문자 키워드 3-6개>]
status: quick
summary_by: claude
added: <YYYY-MM-DD>
---

# <제목>

> [arXiv](https://arxiv.org/abs/<ID>) · quick 노트 (웹 기반) — 정식 요약으로 승격 예정

## TL;DR

**한국어** — 3문장: 문제, 방법, 결과.

**English** — 3 sentences: problem, method, results.

## Problem Statement

(무엇이 왜 문제인가 — 논문의 주장 기준으로)

## Method Overview

(핵심 메커니즘 — 수식/모듈 이름 포함)

## Key Contributions

1. ...
2. ...

## Relevance to My Work

<!-- ✍️ 사용자가 직접 작성하는 칸. 에이전트는 비워둔다. -->

> **내 문제/프로젝트와의 연결점:**

## Open Questions

- [ ]

## Related Papers

- [[]]

## BibTeX

```bibtex
<paper_bib 출력 붙여넣기 (% source 줄 포함)>
```
````

## 2.5 UPGRADE — 리스트업본을 정독본으로 승격

`status: metadata` 노트가 **실제로 필요해졌을 때만** 한다. 미리 다 읽어둘 필요 없다 —
리스트업의 목적은 재검색 낭비를 막는 것이고, 정독은 쓸 때 한다.

1. 원문을 **실제로 읽는다** — arXiv abs/HTML(또는 ar5iv), PDF가 있으면 `pdftotext`
2. 기존 노트의 사실값(frontmatter · `## Abstract` · `## Survey Context` · `## BibTeX`)은
   **보존**하고, 비어 있는 요약 섹션을 채운다:
   TL;DR / Problem Statement / Method Overview / Key Contributions (필요하면 Results·Limitations)
3. frontmatter 갱신: `status: metadata` → **`summarized`**,
   `summary_by:` 를 출처로 (`claude` = 직접 읽음 / `user-pdf` = 사용자 정리본 기반)
4. `## Related Papers`의 `- [[]]` 를 **실재하는 노트 stem**으로 채운다 (근거 있는 관계만).
   `ls $PA/01_PAPERS/*/` 로 확인하고 쓸 것 — dangling 링크는 만들지 않는다
5. `$PA/03_WIKI/moc-<topic>.md`의 그 논문 한 줄 요약을 **정독 기반으로 개선**
   (초록 발췌 → 메커니즘·수치)
6. `python3 $PA/scripts/paper_lookup.py rebuild` →
   `$PA/03_WIKI/_LOG.md`에 `## [YYYY-MM-DD] upgrade | <제목>` append
7. `## Relevance to My Work`는 **네가 채우지 않는다** — 사용자의 칸이다.
   다만 §2 step 8처럼 **묻고 답을 받으면 그대로 기록**할 수 있다. 답이 없으면 비워둔다

## 3. 규칙

- **조회 전 웹 검색 금지** — 아카이브에 있으면 웹에 나갈 이유가 없다
- INGEST 전 반드시 LOOKUP (중복 노트 방지)
- 이 스킬은 **신규 quick 노트 추가만** 한다. 기존 노트/위키 페이지 수정은
  아카이브 세션의 몫
- **특정 프로젝트와의 연관(가져올 아이디어·적용 분석)은 노트가 아니라
  `$PA/05_PROJECTS/<프로젝트>/` 원장에 쌓는다.** 관련성은 논문의 속성이 아니라 논문×프로젝트의
  관계이기 때문이다. 이 스킬은 논문 정리·위키에 집중하며, 원장은 **명시 요청이 있을 때만**
  편집한다 (규약: `$PA/05_PROJECTS/README.md`). `Relevance to My Work`는 별개의
  사용자 개인 칸으로 현행 유지
- `00_RAW/`, `01_PAPERS/` 기존 파일은 읽기 전용
- **본문 산문은 한국어, 논문 직접 인용은 영어 원문 유지, TL;DR은 한/영 병기,
  수식은 `$...$` LaTeX** — 상세는 §2 「노트 서술 규약」
- **논문을 읽지 않았으면 노트를 만들지 않는다** — 요약 환각 금지
- `Relevance to My Work`는 절대 자동으로 채우지 않는다 — 사용자의 칸이다
