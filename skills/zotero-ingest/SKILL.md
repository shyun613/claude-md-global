---
name: zotero-ingest
description: Zotero에 추가된 논문을 PaperArchive wiki로 자동 처리하는 스킬. "새 논문 정리해줘", "zotero 논문 처리해줘", "ingest", "논문 추가됐어", "zotero sync해줘", "새로 추가한 논문 wiki에 넣어줘", "paper ingest", "논문 요약해서 저장해줘" 등 Zotero 논문을 wiki로 옮기고 싶을 때 항상 사용. 논문 추가·정리 맥락이 보이면 이 스킬 실행을 제안만 하고, 사용자가 지시하면 실행.
---

# Zotero → PaperArchive Ingest

Zotero에 저장된 논문 PDF를 NotebookLM으로 요약해 `01_PAPERS/`에 노트를 만들고
`03_WIKI/`의 MOC를 갱신하는 파이프라인.

## 스크립트

```
<볼트 루트>/scripts/zotero-to-wiki.py
```

## 상황별 명령어

| 상황 | 플래그 |
|------|--------|
| 미처리 논문 전체 정리 (기본 권장) | `--sync` |
| 방금 추가한 논문 1편만 | `--latest` |
| 목록 보고 직접 고르기 | `--list` |
| 노트북 유지해서 추가 질의 | `--keep-notebook` |
| 완료 후 Obsidian 바로 열기 | `--open` |

## 실행 흐름

1. **의도 파악** — 특정 논문을 언급했으면 `--latest`/`--list`,
   "쌓였다"는 뉘앙스면 `--sync`
2. **기본값은 `--sync`** — 이미 처리된 논문은 자동으로 건너뛰므로 중복이 없다
3. **실행** (볼트 루트에서):

```bash
python3 scripts/zotero-to-wiki.py --sync      # 미처리 전체
python3 scripts/zotero-to-wiki.py --latest    # 방금 추가한 1편
python3 scripts/zotero-to-wiki.py --list      # 보고 선택
```

4. **완료 후 안내** — 생성된 노트 경로를 알려주고,
   **"Relevance to My Work 섹션은 직접 채워주세요"** 를 반드시 덧붙인다.
   그 칸은 자동 요약이 채우면 안 되는 칸이다.

## 전제 조건

- `nlm login` 완료 (최초 1회) — `pip install notebooklm-mcp-cli`
- Zotero가 PDF를 저장 중 (`archive.config.yaml`의 `paths.zotero_storage`)
- 토픽 자동 분류는 `archive.config.yaml`의 `topics[].keywords`가 정한다.
  키워드가 없으면 전부 `_toread`로 떨어지고, 나중에 옮기면 된다.

## 문제가 생기면

| 증상 | 조치 |
|---|---|
| 인증 오류 | `nlm login` 재실행. 브라우저가 막히면 `nlm login --manual -f <cookies>` |
| `Zotero storage 없음` | `archive.config.yaml`의 `paths.zotero_storage` 확인 |
| `nlm` 명령 없음 | `pip install notebooklm-mcp-cli --break-system-packages` |
| 전부 `_toread`로 감 | 정상 — 키워드 미설정 상태. 원하면 config에 키워드를 채운다 |

> NotebookLM을 쓰지 않는 구성이라면 이 스킬 대신 `paper-archive` 스킬의
> quick 노트 경로(arXiv 직접 읽기)를 쓴다.
