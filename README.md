# Harness KB

**A blueprint for building a knowledge base that maintains itself.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/version-1.6.3-blue.svg)](./CHANGELOG.md)
[![Docs](https://img.shields.io/badge/docs-blueprint-blue.svg)](./docs/blueprint.md)
[![Dependencies](https://img.shields.io/badge/deps-zero-brightgreen.svg)](#)

> Most "second brains" rot. When an AI agent relies on your knowledge base, rot becomes
> a correctness problem: stale indexes, contradictory rules, and broken links quietly make
> the agent miss data. At scale, you cannot hand-maintain your way out of this.
>
> **Harness KB** is a small, dependency-free blueprint — plus reference artifacts — for a KB
> that keeps *itself* correct, fresh, and fast to retrieve. It is the architecture, not an app.

_Provenance: this blueprint reflects a real working system as of **2026-07-25**. The concepts are
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
│   ├── scripts/auto_fix.py       Fixes the one safe class of drift — backup, gate, rollback (H2)
│   ├── scripts/test_auto_fix.py  Break-the-fixer tests, including a forced rollback
│   ├── scripts/generate_catalog.py  Triage catalog for agent retrieval — `--check` gates staleness
│   ├── scripts/claim.py          Per-file lock so parallel agents can't clobber each other (H4)
│   ├── scripts/test_claim.py     Break-the-lock tests for the claim lock
│   ├── scripts/tooling_selfcheck.py  Runs your tooling's test suites — the gate that runs the gates
│   ├── scripts/test_tooling_selfcheck.py  Break-the-gate tests for that runner
│   ├── hooks/settings.json       Example hooks: activity log, claim lock, tooling gate
│   ├── routines/kb-audit-daily.SKILL.md  Template for a scheduled daily audit agent
│   └── routines/kb-autofix-daily.SKILL.md  Template for the auto-fix run that follows it
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

# ...and gate it: exit 1 when the catalog no longer matches the notes
python examples/scripts/generate_catalog.py /path/to/your/vault --out catalog.json --check

# 4. See which agent stream currently holds which file (multi-agent lock, H4)
python examples/scripts/claim.py status --vault /path/to/your/vault

# 5. Run the test suites that guard your own in-vault tooling
python examples/scripts/tooling_selfcheck.py run --vault /path/to/your/vault

# 6. Let the machine fix the one class of drift it cannot get wrong (dry run first)
python examples/scripts/auto_fix.py /path/to/your/vault --rules examples/rules/rules.example.json
python examples/scripts/auto_fix.py /path/to/your/vault --rules examples/rules/rules.example.json --apply
```

> **Put these scripts inside the vault they serve.** A machine with the notes but without the tools
> cannot verify or regenerate anything, and a second copy kept in per-machine config drifts from the
> first with no audit able to see it. That failure grows with every machine you add — blueprint §5.

Wire `claim.py` into a `PreToolUse` hook ([examples/hooks](./examples/hooks)) and the lock stops
being advice: a write to a file another stream is editing is **blocked**, not merely discouraged.
It claims free files silently, so a single agent never notices it. Wire `tooling_selfcheck.py` into
the `Stop` hook and the same becomes true of your tests: edit a tool, and its suite runs before the
turn is allowed to end — because a gate nobody runs is not a gate.

`auto_fix.py` is the only script here that writes to your notes, and it is deliberately the
narrowest one: it rewrites a **number on a line that carries a marker**, nothing else. It refuses
the fix whenever the checker's report and its own re-read of that line disagree, backs up every
file it touches, re-runs both gates afterwards, and rolls the whole run back if either goes red.
Incomplete enumerations, removed markers and anything semantic stay in the report for a human —
widening that list should be a decision, not a flag.

One rule the marker pattern depends on: **at most one marker per unit per line.** The drift checker
matches its patterns across the whole line, so two markers of the same unit both get handed the
leftmost number — the report is wrong before the fixer ever sees it. The fixer detects that overlap
and refuses the line instead of guessing, but the real fix is to keep each claim on its own line.

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

**Khi nhiều agent dùng chung một vault:** đừng dựa vào luật "nhớ kiểm tra trước khi sửa" — đó là
khế ước xã hội, chỉ đúng tới lần đầu tiên một model quên. `claim.py` biến nó thành **khoá cơ học**:
hook chặn thẳng lệnh ghi khi stream khác đang giữ file, và im lặng khi bạn làm một mình.

**Một luật kiến trúc đáng nhớ:** script kiểm tra / sinh dữ liệu phải nằm **trong chính vault**, không
để trong thư mục cấu hình của từng máy. Máy có note mà thiếu công cụ thì không verify hay regen được
gì; bản sao thứ hai ở máy khác thì tự do lệch đi mà audit không thấy — vì audit chỉ quét vault. Hai
máy còn vá tay được; nhiều máy thì số bản sao tăng tuyến tính còn khả năng chúng khớp nhau thì
không. Phép thử: **clone vault sang máy trắng — chạy gate được không?**

Chi tiết đầy đủ: **[docs/blueprint.md](./docs/blueprint.md)** (tiếng Anh).

---

## License

[MIT](./LICENSE) © 2026 Chuong Phan
