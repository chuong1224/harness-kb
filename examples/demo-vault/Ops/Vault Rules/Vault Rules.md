---
title: Vault Rules
aliases: ["rules summary", "vocabulary", "conventions"]
summary: "Human-readable summary of the vault conventions: tag vocabulary, areas, index count. Every number here is derived from the rules file and checked by check_rules_drift.py."
date: 2026-07-25
type: reference
tags: [ops]
---

# Vault Rules

> This document is a **projection** of the single source of truth, not the source itself.
> The numbers below carry marker comments so `check_rules_drift.py` fails loudly when they
> stop matching `examples/rules/rules.example.json`. To change a rule, edit the rules file
> first, then run the checker - it will point at every line that still needs updating.

## Tag vocabulary (7 tags) <!-- rules:tag_count --><!-- rules:tag_list -->

`research` · `project` · `skill` · `ops` · `personal` · `reference` · `index`

Index files carry the `index` tag and nothing else.
Retired and never to come back: `dashboard`, `guide`, `moc`.

## Areas (5 areas) <!-- rules:area_count -->

`Research` · `Projects` · `Skills` · `Ops` · `Personal` - an area may be declared before its
folder exists, which the checker reports as a warning rather than an error.

## Indexes (2 indexes) <!-- rules:index_count -->

Counted from the filesystem, not from memory: one index per folder that holds two or more
notes, named `Index - <Folder Name>`.

Back to [[Index - Ops]].
