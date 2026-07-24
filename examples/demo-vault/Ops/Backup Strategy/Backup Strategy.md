---
title: Backup Strategy
aliases: ["backups", "backup policy"]
summary: "How the knowledge base is backed up: history repo kept outside any cloud-synced folder."
date: 2026-01-03
type: reference
tags: [ops]
---

# Backup Strategy

Keep the version-history repository **outside** any cloud-synced folder to avoid a half-synced
`.git` corrupting the database. Export the full history to a single bundle file for safe syncing.

Back to [[Index - Ops]].
