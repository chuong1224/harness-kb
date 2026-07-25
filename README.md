# Harness KB

**A blueprint for building a knowledge base that maintains itself.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/version-1.2.0-blue.svg)](./CHANGELOG.md)
[![Docs](https://img.shields.io/badge/docs-blueprint-blue.svg)](./docs/blueprint.md)
[![Dependencies](https://img.shields.io/badge/deps-zero-brightgreen.svg)](#)

> Most "second brains" rot. When an AI agent relies on your knowledge base, rot becomes
> a correctness problem: stale indexes, contradictory rules, and broken links quietly make
> the agent miss data. At scale, you cannot hand-maintain your way out of this.
>
> **Harness KB** is a small, dependency-free blueprint — plus reference artifacts — for a KB
> that keeps *itself* correct, fresh, and fast to retrieve. It is the architecture, not an app.

_Provenance: this blueprint reflects a real working system as of **2026-07-24**. The concepts are
evergreen; any specific counts are illustrative snapshots, not live values._

---

## The one idea

A pile of dashboards where a human still fixes everything by hand is a **cockpit**, not a harness.

A real harness is a **closed control loop**:

```
sense deviation  →  decide  →  correct  →  verify  →  keep on rails
                         (with minimal human intervention)
```

The goal is to move the human from *operator* to *supervisor* — someone who approves the
high-risk changes and lets the system handle the mechanical, reversible ones on its own.

Read the full argument: **[docs/blueprint.md](./docs/blueprint.md)**.

---

## The 7-property scorecard

Use this to score any knowledge base honestly. Instrumentation and guardrails are the *easy*
half; the closed loop is what most systems never reach.

| Property | Question it answers |
|---|---|
| **Actuation** | Can the agent act on the KB at all? |
| **Observability** | Is every action logged and visible? |
| **Guardrails** | Is damage prevented before it happens? |
| **Determinism / self-healing** | Do entry points recover instead of corrupt? |
| **Automation** | Does routine work run without a human? |
| **Multi-agent coordination** | Do multiple agents share state safely? |
| **Closed control loop** | Does the system correct itself, or only report? |

---

## What's inside

```
harness-kb/
├── docs/
│   └── blueprint.md              The architecture: scorecard, loop diagnosis, roadmap, safety
├── examples/
│   ├── rules/rules.example.json  Single source of truth for tags/areas (the H1 pattern)
│   ├── scripts/verify_kb.py      Integrity gate — the "verify" step of the loop (zero deps)
│   ├── scripts/check_rules_drift.py  Documents vs. the source of truth — kills rule drift (H1)
│   ├── scripts/test_drift_check.py   Break-the-gate tests for the drift checker
│   ├── scripts/generate_catalog.py  Triage catalog generator for agent retrieval (zero deps)
│   ├── hooks/settings.json       Example activity-logging hook (Claude Code PostToolUse)
│   └── routines/kb-audit-daily.SKILL.md  Template for a scheduled daily audit agent
└── LICENSE
```

The example scripts are **reference implementations** — dependency-free, standard-library only —
that make the blueprint concrete. They are meant to be read and adapted, not vendored blindly.

## Quickstart

The scripts run on any folder of Markdown notes (a "vault"). Python 3.8+, no packages required.

```bash
# 1. Check integrity — the verify gate of the control loop
python examples/scripts/verify_kb.py /path/to/your/vault --rules examples/rules/rules.example.json

# 2. Check that your documents still agree with the source of truth (no rule drift)
python examples/scripts/check_rules_drift.py /path/to/your/vault --rules examples/rules/rules.example.json

# 3. Generate a triage catalog agents can read before searching
python examples/scripts/generate_catalog.py /path/to/your/vault --out catalog.json
```

Both checkers exit `0` when clean and `1` when they find problems — so you can wire them into a
commit hook or a scheduled job as a hard gate. Point them at `examples/demo-vault` to see real
output immediately: zero errors, plus a few warnings, because the example rules deliberately declare
more areas and tags than the six-note demo actually uses — which is exactly the signal you want when
a vocabulary entry stops being used.

---

## Tiếng Việt

**Bản thiết kế cho một knowledge base tự bảo trì.**

Phần lớn "bộ não thứ hai" đều mục theo thời gian. Khi một AI agent dựa vào KB của bạn, sự mục
nát trở thành vấn đề *tính đúng*: index lỗi thời, quy tắc mâu thuẫn, link gãy — âm thầm khiến
agent **sót dữ liệu**. Ở quy mô lớn, không thể bảo trì tay mãi được.

**Harness KB** là một blueprint nhỏ, không phụ thuộc thư viện, kèm các artifact mẫu, cho một KB
tự giữ mình **đúng — tươi — truy xuất nhanh**. Đây là *kiến trúc*, không phải một app.

**Ý tưởng cốt lõi:** một đống dashboard mà người vẫn phải tự tay sửa mọi thứ = *buồng lái*,
chưa phải harness. Harness thật là một **vòng điều khiển khép kín**: cảm biến độ lệch → quyết
định → tự chỉnh → xác minh → giữ trên đường ray, với can thiệp người tối thiểu. Mục tiêu: đưa
con người từ *người vận hành* thành *người giám sát* chỉ phê duyệt thay đổi rủi ro cao.

Chi tiết đầy đủ: **[docs/blueprint.md](./docs/blueprint.md)** (tiếng Anh).

---

## License

[MIT](./LICENSE) © 2026 Chuong Phan
