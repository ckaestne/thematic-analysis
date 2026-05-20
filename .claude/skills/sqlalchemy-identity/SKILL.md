---
name: sqlalchemy-identity
description: How to compare and dedup SQLAlchemy/SQLModel ORM objects in this project. Prefer `is` and sets of ORM objects over PK comparisons; the design keeps ORM instances scoped to a single session so the identity map guarantees object identity. Trigger when writing dedup logic, equality checks, or set/dict membership over Code/Quote/Codebook/Segment/Theme instances.
---

# Comparing ORM objects in this project

When comparing or deduplicating SQLAlchemy/SQLModel instances (`Code`,
`Quote`, `Codebook`, `Segment`, `Theme`, etc.), prefer identity-based
forms over PK-based ones:

- Comparison: `obj1 is obj2`, not `obj1.id == obj2.id`.
- Dedup: `{x.related for x in items}` (set of ORM objects), not
  `{x.related_id: x.related for x in items}.values()`.

## Why

SQLAlchemy's per-session identity map (`{(class, pk): instance}`)
guarantees that any query or relationship traversal resolving to the
same row returns the same Python object. Within a single session, two
references to the same row are the same object — so `is` works and a
set of ORM objects deduplicates correctly.

This project uses many short-lived `with session() as s:` blocks per
CLI call, but the design keeps ORM instances within the scope of a
single session — they are not passed across session boundaries. Treat
"what if it crosses sessions" as a non-concern; if a future change
starts passing ORM objects across sessions, push back on that change
rather than weakening the comparison.

## Caveat: SQLModel disables `__hash__`

SQLModel instances are not hashable by default, so a `set` over them
raises `TypeError`. When you need set semantics over ORM rows, key on
the PK in a dict and read `.values()`:

```python
# segment.codes may have many rows per codebook revision
codebooks = {c.codebook_used_id: c.codebook_used for c in segment.codes}
for cb in codebooks.values():
    ...
```

The identity check inside the loop still uses `is`:

```python
inputs = [c for c in segment.codes if c.codebook_used is codebook]
```

## How to apply

- Comparing two ORM references known to be from the same session: use `is`.
- Deduping ORM relationships (hashable case, e.g. `Code`): use a set.
- Deduping SQLModel rows (unhashable): use a dict keyed on the PK,
  iterate `.values()` — the values are still the canonical instances.
- Don't add defensive PK fallbacks "just in case sessions cross." If
  they do, that's a design bug to fix elsewhere.
