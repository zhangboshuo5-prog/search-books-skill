---
name: search-books
description: Search a fixed Z-Library route by exact book title, accept only original PDFs, and save validated bytes to a local Obsidian vault. Use only when the user explicitly requests a Z-Library PDF for a supplied title; do not use for scheduled reading workflows, general web search, EPUB conversion, or other mirrors.
---

# Search Books / 搜书 Skills

## Overview

Enter through `https://z-library.bz/`, use only the search/detail route it
declares on `https://z-library.im/`, select the first exact-title record whose
original format is PDF, and save its unmodified bytes into the local `My-wiki`
Obsidian Vault. `OBSIDIAN_VAULT_PATH` may override that path.

The final transfer may use the public HTTPS file URL returned by a verified
`z-library.im/dl/` entry. The skill deliberately excludes NotebookLM, EPUB
conversion, summaries, general web search, and any other mirror.

## Safety Boundaries

- Use the skill only for public-domain works or content the user owns or is
  authorized to access.
- Require one explicit title request for every download. Never schedule or
  batch the workflow unattended.
- Stop on an origin, DNS, title, format, or validation mismatch. Never expand
  the search to another site.
- Never request passwords in chat or expose stored browser cookies.

## Runtime Paths

Resolve the absolute directory containing this `SKILL.md` and assign it to
`SKILL_DIR` before running a bundled command.

- In Codex, install globally at `$HOME/.agents/skills/search-books`, or install
  for one repository at `<repo>/.agents/skills/search-books`.
- In Hermes, use the installed Skill directory exposed by the runtime.
- In Codex, set `SEARCH_BOOKS_STATE_DIR` to
  `$HOME/.codex/state/search-books` so browser state remains under Codex. In
  Hermes, leave it unset to use `HERMES_HOME`.

## Fixed Contract

| Item | Rule |
|---|---|
| Search scope | `z-library.bz` entry plus its confirmed `z-library.im` search/detail route; block every other page origin |
| Input | One book title supplied by the user |
| Selection | Normalized title must match exactly and source format must be PDF |
| Output | One original PDF; no Markdown, summary, or sidecar file |
| Destination | `My-wiki/raw/sources/hermes-inbox/books/<title>.pdf` |
| Existing file | Never overwrite; identical content is reported as a duplicate |
| Conversion | Never click or call PDF conversion controls |
| File redirect | Only a public HTTPS URL attested by `z-library.im/dl/`; strip site cookies and forbid further redirects during curl transfer |
| Approval | The user's title request authorizes creation of that one PDF |

## Workflow

### 1. Extract the title

Take the title inside Chinese or Western book-title marks. If the request does
not contain a usable title, ask for the title and stop.

Completion criterion: one non-empty title string is available.

### 2. Check setup when needed

Run the doctor command on first use or after a setup/network failure:

```bash
python3 "${SKILL_DIR}/scripts/search_books.py" doctor --json
```

The script never installs missing dependencies automatically. Install the
pinned `@playwright/cli@0.1.17` manually if doctor reports it missing.

Interpret these fields:

- `playwright_cli`: Playwright CLI is callable.
- `homepage`: the fixed `.bz` entry homepage opened on its HTTPS origin.
- `search_backend`: that entry page currently declares the exact authorized
  `https://z-library.im/s` search action. Doctor does not consume a search
  request before the real download.
- `vault`: the configured path exists and contains `.obsidian/`.
- `session`: optional; true when protected `.im` login state contains both
  `remix_userid` and `remix_userkey`. Public downloads can run anonymously.
- `dns`: both confirmed site hosts pass the public-address or the machine's
  dedicated `198.18.0.0/15` proxy Fake-IP policy.

Completion criterion: `playwright_cli`, `homepage`, `search_backend`, `vault`,
and `dns` are all `true`. A false `session` is informational. If a particular
book requires authentication, run the one-time login below and retry; never
switch to another mirror.

### 3. Perform one-time login

Never request or store the user's password in chat. Ask the user to run this
command in a local interactive terminal:

```bash
python3 "${SKILL_DIR}/scripts/search_books.py" login
```

The command opens the confirmed `.im` login page in a headed browser, waits for
the user to finish login, and stores browser state with owner-only permissions
under the configured runtime state directory.

Completion criterion: the command reports the saved
`storage-state-z-library.json` path.

### 4. Search and save

Run:

```bash
python3 "${SKILL_DIR}/scripts/search_books.py" download "<书名>" --json
```

The command reruns doctor as a hard preflight, then uses the `.im` search and
detail pages once. It reads the result card's `slot="title"` and
`extension="pdf"`, resolves the verified `.im/dl/` response without following
it in the browser, validates the returned HTTPS/DNS target, and transfers with
system curl without forwarding cookies or following another redirect. It
rejects EPUB records, converted-PDF links, unapproved page origins, non-PDF
payloads, title mismatches, and destination conflicts.

Completion criterion: JSON status is `saved` or `duplicate`.

### 5. Report the result

- `saved`: report the absolute PDF path, byte size, and SHA-256.
- `duplicate`: report the existing PDF path; do not claim a new file was made.
- `conflict`: explain that a different file already owns the title filename.
- Setup/network/search error: report the exact error and the next safe action.

Never claim success from a browser click alone. Success requires a validated
PDF on disk at the reported destination.

## One-Shot Recipe

For `下载《如何阅读一本书》`:

```bash
python3 "${SKILL_DIR}/scripts/search_books.py" download "如何阅读一本书" --json
```

Then report only the structured result. Do not create notes under `wiki/` and
do not invoke LLM Wiki or NotebookLM.

## Common Pitfalls

1. **Treating a conversion option as an original PDF.** Reject
   `data-convert_to="pdf"` and URLs containing `convertedTo=pdf`.
2. **Searching the wider web.** A DNS or HTTP failure is an error, not permission
   to try another domain or search engine.
3. **Trusting a `.pdf` suffix.** The script must validate the PDF magic bytes.
4. **Overwriting an existing book.** Existing paths are immutable; return
   `duplicate` or `conflict`.
5. **Running login non-interactively.** The first login requires the local
   headed browser and terminal input.
6. **Assuming the historical upstream selectors still work.** The current
   result cards expose the title through `slot="title"` and format through the
   `extension` attribute.
7. **Calling the broken `.bz/api/search` endpoint.** The current `.bz` page
   declares `.im/s`; a `504` from the obsolete API is not the download route.
8. **Bypassing the preflight because a state file exists.** `download` must stop
   unless the live entry/search-route, DNS, Playwright, and Vault checks pass.
   Session status is optional until the chosen book requires login.
9. **Opening the browser before validating DNS/IP.** Every browser entry point
   must pass the same public-address or dedicated proxy Fake-IP policy first.

## Verification Checklist

- [ ] Entry stayed on `z-library.bz`; search/detail stayed on `z-library.im`
- [ ] Normalized result title exactly matched the requested title
- [ ] Source record declared original extension `pdf`
- [ ] No conversion link was used
- [ ] Any external file URL was attested by `.im/dl/`, HTTPS/DNS-validated, and received no site Cookie
- [ ] Saved bytes passed PDF magic-byte validation
- [ ] SHA-256 was computed after download
- [ ] Existing files were not overwritten
- [ ] Final path is inside `raw/sources/hermes-inbox/books/`
- [ ] User received the actual status and path
