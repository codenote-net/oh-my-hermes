# `omh-issue-loop` Workflow Map

Use this map to understand the complete workflow before executing the detailed instructions in
`SKILL.md`. The diagram is an overview, while `SKILL.md` is authoritative when details differ.

## Role legend

- **Human**: supplies the issue and makes the final merge or recovery decision.
- **Hermes orchestrator**: owns Git, GitHub, CI inspection, review orchestration, and handoffs.
- **Codex worker**: changes the working tree and runs local validation only.
- **Claude/Codex reviewers**: review without changing the implementation.

## End-to-end flowchart

```mermaid
flowchart TD
    H0["Human supplies one GitHub issue URL"]:::human
    O0["Hermes preflight<br/>Validate tools, auth, repository, and clean tree"]:::orchestrator
    O1["Fetch issue exactly once<br/>Save immutable issue snapshot"]:::orchestrator
    O2["Create issue branch from latest default branch"]:::orchestrator
    W0["Codex implementation worker<br/>Snapshot in prompt; local changes and validation only"]:::worker
    O3{"Post-worker side-effect<br/>safety check passed?"}:::decision
    S0["Stop: orchestration failure<br/>Preserve and report evidence"]:::stop

    R0["Three local reviews on exact diff<br/>Codex review + Claude code + security"]:::reviewer
    R1{"Any high-priority finding?"}:::decision
    L0{"Shared fix count<br/>below 10?"}:::decision
    W1["Codex restricted local fix worker"]:::worker
    O4["Safety check and affected local validation"]:::orchestrator
    F0["Fix-limit at-mention progress comment<br/>PR if created; otherwise issue"]:::stop
    H1["Human decides whether to continue manually<br/>or start a new run"]:::human

    O5["Full validation and signing preflight"]:::orchestrator
    O6["Hermes creates signed commit<br/>and pushes exact HEAD"]:::orchestrator
    G0{"Signoff required<br/>by this repository?"}:::decision
    G1["Sign off and verify<br/>exact pushed HEAD"]:::orchestrator
    O11["Open draft PR"]:::orchestrator
    R2["Five reviews on exact PR head<br/>3 local + PR review + fresh-worktree behavior"]:::reviewer
    R3{"All five reviews complete<br/>with zero high findings?"}:::decision
    L1{"Shared fix count<br/>below 10?"}:::decision
    W2["Codex restricted PR or CI fix worker"]:::worker
    O9["Safety check and validation<br/>Hermes signs, commits, and pushes"]:::orchestrator
    G2{"Signoff required<br/>by this repository?"}:::decision
    G3["Re-sign off and verify<br/>new pushed HEAD"]:::orchestrator

    C0["Inspect every PR CI check<br/>for the reviewed head SHA"]:::orchestrator
    C1{"CI state?"}:::decision
    C2["Continue background monitoring<br/>then read complete check set again"]:::orchestrator
    C3{"Red failure actionable<br/>from repository?"}:::decision
    S1["Stop: preserve ready PR<br/>Report CI or infrastructure blocker"]:::stop
    C4{"PR head still equals<br/>reviewed and green-CI SHA?"}:::decision

    O7["Immediately update and verify PR body<br/>Mark PR ready"]:::orchestrator
    O8["At-mention Human<br/>Five reviews passed; CI monitoring continues"]:::orchestrator
    H2["Human begins review<br/>Waits for final green-CI handoff before merge"]:::human
    O10["Update CI result and at-mention Human again<br/>All applicable CI is green"]:::orchestrator
    H3["Human performs final review<br/>and decides whether to merge"]:::human

    H0 --> O0 --> O1 --> O2 --> W0 --> O3
    O3 -- "No" --> S0
    O3 -- "Yes" --> R0 --> R1
    R1 -- "Yes" --> L0
    L0 -- "Yes: increment" --> W1 --> O4 --> R0
    L0 -- "No: 10 fixes used" --> F0 --> H1
    R1 -- "No" --> O5 --> O6 --> G0
    G0 -- "No" --> O11 --> R2 --> R3
    G0 -- "Yes" --> G1 --> O11
    R3 -- "No" --> L1
    L1 -- "Yes: increment" --> W2 --> O9 --> G2
    G2 -- "No" --> R2
    G2 -- "Yes" --> G3 --> R2
    L1 -- "No: 10 fixes used" --> F0
    R3 -- "Yes" --> O7 --> O8
    O8 -- "Human review" --> H2
    O8 -- "Background CI monitor" --> C0 --> C1
    C1 -- "Pending" --> C2 --> C0
    C1 -- "Red" --> C3
    C3 -- "No" --> S1
    C3 -- "Yes" --> L1
    C1 -- "Green" --> C4
    C4 -- "No: head changed" --> R2
    C4 -- "Yes" --> O10 --> H3

    classDef human fill:#fff2cc,stroke:#8a6d1d,color:#332800,stroke-width:2px;
    classDef orchestrator fill:#d9eaff,stroke:#245a9a,color:#102a43,stroke-width:2px;
    classDef worker fill:#dff4df,stroke:#317a31,color:#173b17,stroke-width:2px;
    classDef reviewer fill:#eee2ff,stroke:#7048a8,color:#2f1c47,stroke-width:2px;
    classDef decision fill:#ffffff,stroke:#555555,color:#222222,stroke-width:2px;
    classDef stop fill:#ffe0e0,stroke:#a33a3a,color:#551b1b,stroke-width:2px;
```

## Invariants visible in the map

1. Only Hermes mutates Git history, remotes, PRs, issues, or PR readiness.
2. Every child receives the same immutable issue snapshot; no child fetches the issue.
3. Every Codex implementation or fix is followed by the mandatory side-effect check.
4. A changed head SHA invalidates earlier review and CI evidence.
5. Signoff is skipped unless repository evidence sets `signoff_required=true`; installation alone
   is not evidence.
6. In required mode, every pushed commit is signed off again before reviews run for that HEAD.
7. CI repairs pass through the same conditional publication path.
8. Human review starts immediately after five clean reviews; CI monitoring continues in parallel.
9. The Human receives a second at-mention only when CI is green for that same reviewed SHA.
10. The workflow never merges or closes the issue; the final decision belongs to the Human.
