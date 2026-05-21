# Repository Agent Instructions

## Language

- Always answer the user in Korean unless the user explicitly requests another language.

## Observer Server Process Management

- The Observer browser port is fixed at `56755`. Always use `http://127.0.0.1:56755` for browser checks.

## Working Tree Safety

- Do not revert or overwrite user changes unless the user explicitly requests it.
- Keep changes narrowly scoped to the user's approved request.
- Prefer `rg` for searching files and text.
