# Part 2 — Data queries and reliable tool use

Status: **planned, not implemented.** Part 1 (autodiscover: `discovery_overview`, `save_descriptions`, `autodiscover` prompt) is done. Today the server only reads catalog metadata; `validate_sql` prepares statements but never runs them.

Part 2 has two goals:

1. Let the AI run read-only queries to look at real data (samples, checking a hypothesis, answering a question).
2. Make clients call the tools reliably. Today the model sometimes answers from memory or guesses instead of calling them.

---

## 2A. Read-only data queries

### Tools

| Tool | Purpose |
|------|---------|
| `run_query(database, sql, max_rows=50)` | Execute one read-only `SELECT` and return rows |
| `sample_values(database, table, column?, limit=20)` | Most frequent distinct values of a column (or a few sample rows of a table). Meant for autodiscover: status codes like `P/S/X`, flags `S/N`, code formats |
| `count_rows(database, table, where?)` | Cheap row count with an optional filter, to size a question before fetching |

### Safety (must-have before enabling)

- **Off by default.** Add a config flag `allow_data_queries: false`, optionally a per-database allowlist. When it is off, the tools are not registered at all, so the model doesn't see them.
- **Read-only database user.** Document that the configured user should hold only `CONNECT` plus `SELECT`. This is the real guarantee; the checks below are only defence in depth.
- **Reuse `check_read_only_sql`.** One statement, `SELECT`/`WITH` only, no `INTO` of any kind. Keep `validate_sql` as the dry run.
- **No locks.** Run `SET ISOLATION TO DIRTY READ` (or `COMMITTED READ LAST COMMITTED` where the instance supports it) and `SET LOCK MODE TO NOT WAIT` on the session before executing.
- **Row cap.** Fetch at most `max_rows + 1` rows, then stop and free the cursor, and report `truncated`. Prefer not rewriting the SQL. The hard ceiling (e.g. 500) comes from config.
- **Time cap (open question).** Informix has no per-statement timeout, and IfxPy has no cancel call. Options:
  1. Run the query in a worker subprocess and kill it after N seconds. The server notices the client disconnect and drops the session. This is the most robust option.
  2. Refuse queries whose `SET EXPLAIN` estimated cost exceeds a threshold. Rejected for now: it writes `sqexplain.out` on the server.
  3. Accept the risk on small databases.

  Recommendation: option 1, measured on a real instance first.
- **Sensitive data.** Add a config list `masked_columns` (per db/table/column, glob patterns) whose values are replaced by `***`. Fields like DNI/NIF, IBAN, phone and email should be masked by default once they are documented. Never return BYTE/TEXT/BLOB/CLOB contents; return their length only.
- **Audit.** Log every executed statement (database, SQL, row count, duration) to stderr, plus an optional audit file.

### Output format

Return `{"columns": [{"name", "type"}], "rows": [[...]], "row_count", "truncated", "elapsed_ms"}`:

- Rows are arrays, not dicts, to save tokens.
- Decimal values become strings so precision is kept; dates and datetimes use ISO 8601.
- Strings are trimmed, because legacy CHAR columns are padded with spaces, and cut at around 500 characters.

### Autodiscover integration

When data queries are enabled, step 2b of the `autodiscover` prompt uses `sample_values` for medium- and low-confidence columns before asking the user. Example: showing that `estado` contains `P`, `S` and `X` with their counts.

Keep the prompt working without data access: it already works from metadata only.

---

## 2B. Make the model actually use the tools

### Likely causes

- **No signal.** The user's question ("¿cuántos albaranes hubo ayer?") gives the model nothing that points at this server, so it answers from memory or writes SQL against an imagined schema.
- **Tool search hides the tools.** Clients with many tools, Claude Code among them, load MCP tools on demand by keyword. If our descriptions lack the words the user writes (Spanish/Catalan business terms, "Informix", "ERP", database names), the tools are never surfaced.
- **Generic names and descriptions.** Names like `list_tables` compete with other servers' tools.
- **Too many small steps.** Several small tools (`describe_table`, `list_indexes`, `list_foreign_keys`) make the model stop early.
- **Permission prompts.** Each prompt discourages the model from calling tools again.

### Actions

1. **Server `instructions`.** State *when* to use the server: "Any question about data, tables, SQL or reports of the company's Informix/ERP databases (<list of database names and what they hold>) must go through these tools; never assume table or column names." Generate the database list dynamically from the documented `_description` values at startup.
2. **Tool descriptions.**
   - The first sentence says when to use the tool.
   - Include Spanish keywords (tabla, columna, base de datos, consulta, campo, albarán, factura, cliente…) so keyword-based tool search finds them.
   - Keep the rest short.
3. **Single entry point.** Add `database_guide(database)`, returning the database description, the documented tables with their one-line descriptions, and the key relationships. The model calls one tool and gets the map. Consider folding indexes and FKs into `describe_table` (`include=["indexes", "foreign_keys"]`).
4. **Question prompt.** Add `/mcp__jurassic-mcp__ask <database> <question>`, which wraps the question with the workflow: guide → search → describe → validate → (run).
5. **Client-side nudges (documented in the README):**
   - A `CLAUDE.md` / Copilot instructions snippet for projects that use these databases.
   - Allow-listing the read-only tools so they don't trigger permission prompts (`.claude/settings.json` → `permissions.allow: ["mcp__jurassic-mcp__*"]`, minus `save_descriptions`/`run_query` if those should still prompt).
6. **Useful errors.** Errors must say what to do next (already partly done with `ToolError` and SQLCODE hints), so the model retries instead of giving up.
7. **Measure.**
   - Keep ~20 real questions (Spanish and Catalan, vague and precise) in `docs/eval-questions.md`.
   - For each client/model, record whether the right tools were called.
   - Run before and after each change. This can be scripted with the Claude Agent SDK against the STDIO server.

---

## Suggested order

1. 2B items 1–3 and 7 (cheap, and they help with part 1 too).
2. `sample_values` and `count_rows` behind `allow_data_queries`, plus the autodiscover integration.
3. `run_query` with all of the 2A safety measures, after testing the time cap on a real instance.
4. `ask` prompt and the client nudges.
