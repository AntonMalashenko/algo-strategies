---
name: confluence-docs
description: >-
  Where and how ALL project documentation is written: directly in Confluence, under the
  "Algo" page tree (https://anton-mal-slb.atlassian.net/wiki/spaces/~5c584b39876a6c0cdbaee8fc/pages/425985/Algo),
  never as new local markdown docs. Covers the page-tree structure and how to keep it
  (edit existing strategy subpages, create a new subpage under "Strategies" for a new
  strategy, create missing subsections when needed), the dev-work journal page
  ("Апдейты (журнал работ)", id 10584066) for development/work updates, the Jira loop
  (working a Jira ticket ⇒ comment the ticket AND document in Confluence), the style rule
  for future-research plans (plain language + include the ready-to-use prompts), and the
  access path: Atlassian MCP connector when available, otherwise the Confluence/Jira REST
  API over HTTPS with credentials from the repo `.env`. Use this skill whenever the task
  involves writing/updating documentation, "задокументируй", "запиши в конфлюенс",
  strategy docs, research plans, work-journal updates, or commenting/closing a Jira
  ticket — even if the request only says "add docs" or "опиши что сделали".
---

# confluence-docs — all documentation lives in Confluence, in one tree

Everything documentation-shaped goes **straight into Confluence**, not into new local
markdown files. The single root is the **Algo** page:

- Space: personal space `~5c584b39876a6c0cdbaee8fc`
- Root page: **Algo**, id `425985`
  <https://anton-mal-slb.atlassian.net/wiki/spaces/~5c584b39876a6c0cdbaee8fc/pages/425985/Algo>

`docs/` in the repo stays for *technical* artifacts only (data schemas, engineering
plans, handoffs); strategy research narratives and all new documentation land in
Confluence (see `strategy-lifecycle`). Existing `docs/` stubs point at their Confluence
pages — keep it that way.

## Page tree (verified 2026-09-03) — respect it

```
Algo (425985)
├── Strategies (524289)              ← index page + one subpage per strategy
│   ├── S007 — GER40 London×Frankfurt (622593)
│   ├── S011 — Larry Connors EOD mean-reversion (3833857)
│   ├── S016 — MTF top-down (8519681)
│   ├── S012 — Кросс-секционный крипто-моментум (10944513)
│   ├── S004 — H4 FVG bounce, тихие часы (10944530)
│   ├── S019 — интрадей gap-fade на US-акциях (11042817)
│   └── S014 — Интрадей-моментум SPY/ES (12779545)
├── Тестовое покрытие (15302657)      ← what's covered by automated tests, per
│   │                                   strategy, and why it matters for real money
│   └── S007 — тест-кейсы (15335425)  ← full e2e case catalog (tests/e2e/)
├── Глоссарий: термины бэктестов, метрик и мониторинга (4587521)
├── Проп-фирмы: алго/API-трейдинг для S007 (6094849)
├── S009 / логирование ботов — инцидент DOGE (10616833)
└── Апдейты (журнал работ) (10584066)   ← dev-work journal
```

Rules for placing content:

- **Existing strategy doc changes** → edit that strategy's existing subpage under
  *Strategies*. Do not fork a second page for the same strategy.
- **New strategy documentation** → create a **new subpage under Strategies (524289)**,
  titled `SNNN — <short name>` (match the existing naming), and add/refresh the row on
  the *Strategies* index page.
- **Test coverage documentation** (what an automated test suite covers for a strategy,
  and why) → create/edit that strategy's subpage under **Тестовое покрытие (15302657)**,
  titled `SNNN — тест-кейсы` (match `S007 — тест-кейсы`'s naming) — a sibling section to
  *Strategies*, not nested under it.
- **Missing subsection** → if the content genuinely has no home (new topic, not a
  strategy), create the subpage in the right place under *Algo* — don't dump unrelated
  content onto an existing page just to avoid creating one.
- **Development / engineering work updates** (what was built, fixed, deployed, refactored)
  → document on **"Апдейты (журнал работ)", id `10584066`**
  <https://anton-mal-slb.atlassian.net/wiki/spaces/~5c584b39876a6c0cdbaee8fc/pages/10584066>.
  Append a dated entry; keep the page's existing entry format.

## Jira loop

When the task being done comes from a Jira ticket (`ALGODEV-*`, host from
`JIRA_BASE_HOST` in `.env`):

1. **Comment the ticket** with what was done / decided / measured (short, factual,
   links to Confluence page versions and commits).
2. **Document in Confluence** in the right place per the tree rules above (strategy
   subpage, work journal, or a new subsection).

Both, not one or the other — Jira holds the task trail, Confluence holds the knowledge.

## Style rules

- **Future-research plans** ("что исследуем дальше", roadmap items, hypotheses to test):
  write them in **plain language** a non-specialist can follow (per the two-register rule
  in `orchestrator`), and **include the ready-to-use prompts** — the exact prompt text a
  future session can paste to start that research. Plan = plain-language "what and why"
  + the prompt block.
- Confluence pages are for the maintainer and may be in Russian; the English-only rule in
  `AGENTS.md` applies to repo artifacts, not Confluence content.
- When editing a page, preserve its structure and add to it; don't rewrite unrelated
  sections. Mention the page version you produced when logging the change.

## Access: connector first, REST API otherwise

1. **Atlassian MCP connector available** (Jira/Confluence tools in the session) → use it.
   Note (found 2026-09-03, a background/forked session): the connector's own cloud-access
   grant can cover ONLY the Jira cloud (`getAccessibleAtlassianResources` returning just
   `anton-mal-slb-jira.atlassian.net`) even when Confluence tools are present and callable
   — calling a Confluence tool then fails with `"Cloud id: ... isn't explicitly granted by
   the user"`. That is not "no connector" — it is a real per-session grant gap. Don't loop
   on re-trying the connector call; drop straight to the REST API fallback below, which
   uses the same `.env` credentials regardless of what the connector's OAuth grant covers.
2. **No connector (or the grant gap above)** → go over the web REST API with
   `curl`/`python`, using credentials from the repo **`.env`** (never hardcode or print
   them):
   - `JIRA_EMAIL` + `JIRA_API_TOKEN` — one Atlassian API token, works for both Jira and
     Confluence Cloud via basic auth (`-u "$JIRA_EMAIL:$JIRA_API_TOKEN"`).
   - `JIRA_BASE_HOST` — the Jira `*.atlassian.net` host, for Jira endpoints only.
     Confluence is a **different host** (`anton-mal-slb.atlassian.net`, see
     `CONFLUENCE_PROJECT_URL`) — do not reuse `JIRA_BASE_HOST` for `/wiki/api/v2/...`
     calls, it will 404.

Useful endpoints (base `https://anton-mal-slb.atlassian.net` for `/wiki/...` Confluence
calls; base `https://<JIRA_BASE_HOST>` for Jira's `/rest/api/3/...` calls):

```bash
# read a page (storage format + version)
GET /wiki/api/v2/pages/<id>?body-format=storage

# list children of a page
GET /wiki/api/v2/pages/<id>/children?limit=50

# update a page: PUT /wiki/api/v2/pages/<id>
# body: {"id","status":"current","title","version":{"number": <current+1>},
#        "body":{"representation":"storage","value":"<xhtml>"}}

# create a subpage: POST /wiki/api/v2/pages
# body: {"spaceId": <spaceId>, "parentId": "<parent id>", "title", "body": {...}}
# (get spaceId from GET /wiki/api/v2/pages/425985)

# comment a Jira issue: POST /rest/api/3/issue/<KEY>/comment  (ADF body)
```

Always GET the page first to read the current `version.number` and existing body —
Confluence updates are full-body replaces, so merge your addition into the fetched
storage XHTML rather than overwriting the page.

## After documenting

Log the work in `.claude/change-log/` per `code-change-log` (Confluence page ids +
versions touched, Jira tickets commented), so the next session can find what was
written and where.
