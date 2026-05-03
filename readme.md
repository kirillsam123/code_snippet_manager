# Snippet Manager

CLI-менеджер для хранения и поиска фрагментов кода.
SQLite + FTS5 (русский язык), индексы, транзакции, история поиска, сортировка.

## Features

- Add snippets (language, tags, description)
- Full-text search by title/description/code
- Filter by language or tag
- View all / specific snippet
- Delete with confirmation
- **Sorting** (by date, title, language)
- **Search history** (recent queries, top queries)

## Installation

```bash
git clone <repo>
python snippet_manager2.py list
```

DB path via `SNIPPET_DB` env variable.

## Commands

| Command | Example | Description |
|--------|---------|-------------|
| `add` | `-t "CSV Reader" -l python -g "csv,files"` | Add (multiline code, empty to finish) |
| `search` | `search "csv dictionary"` | Full-text search |
| `list` | `list --sort title --desc` | List snippets with sorting |
| `show` | `show 1` | Show snippet |
| `tag` | `tag python` | Find by tag |
| `lang` | `lang javascript` | Find by language |
| `delete` | `delete 1` | Delete (with confirmation) |
| `history` | `history` | Search history |
| `history --top 5` | | Top 5 queries |
| `history --clear` | | Clear history |

## Sorting Options

```bash
--sort date|title|lang   # default: date
--asc                    # ascending
--desc                   # descending (default)
```

## Examples

```bash
python snippet_manager2.py add -t "CSV Reader" -l python -g "csv,files"
python snippet_manager2.py search "csv" --sort date --desc
python snippet_manager2.py list --sort title --asc
python snippet_manager2.py history --top 10
```

## Database Schema

**SQLite** with `PRAGMA foreign_keys = ON`.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SCHEMA                                     │
└─────────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐          ┌─────────────────────────────────┐
    │  languages   │          │            snippets             │
    ├──────────────┤          ├─────────────────────────────────┤
    │ id (PK)      │◄──────── │ id (PK)                         │
    │ name (UNIQUE)           │ title                           │
    └──────────────┘          │ description                     │
                              │ code                            │
                              │ language_id (FK → languages.id) │
                              │ created_at                      │
                              │ updated_at                      │
                              └─────────────────────────────────┘
                                              │
                                              │ (one-to-many via snippet_tags)
                                              ▼
                               ┌─────────────────────────────────┐
                               │          snippet_tags           │
                               ├─────────────────────────────────┤
                               │ snippet_id (FK → snippets.id)   │
                               │ tag_id (FK → tags.id)           │
                               │ PRIMARY KEY (snippet_id, tag_id)│
                               └─────────────────────────────────┘
                                              │
                                              ▼
                               ┌─────────────────────────────────┐
                               │              tags               │
                               ├─────────────────────────────────┤
                               │ id (PK)                         │
                               │ name (UNIQUE)                   │
                               └─────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────┐
    │                    search_history                               │
    ├─────────────────────────────────────────────────────────────────┤
    │ id (PK)                                                         │
    │ query (TEXT)                                                    │
    │ result_count                                                    │
    │ created_at                                                      │
    └─────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────┐
    │                      snippets_fts (FTS5)                    │
    ├─────────────────────────────────────────────────────────────┤
    │ virtual table synced by triggers:                           │
    │ - rowid (maps to snippets.id)                               │
    │ - title, description, code (search fields)                  │
    │ tokenize = unicode61 (Russian support, case-insensitive)    │
    └─────────────────────────────────────────────────────────────┘
```

**FTS5**: virtual table `snippets_fts` with `unicode61` tokenizer (Russian support). Synced via triggers.

**Indexes**: on `language_id` in snippets and on `tag_id` in snippet_tags.

**Tags**: normalized (separate `tags` table, many-to-many via `snippet_tags`). Batch insert with `executemany`.

**Transactions**: add snippet in explicit transaction (BEGIN/COMMIT).

**Search validation**: blocks special FTS5 chars at query start.

## License
