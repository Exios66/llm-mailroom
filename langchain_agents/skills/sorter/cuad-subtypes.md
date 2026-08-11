# CUAD Agreement Families — Sorter Skill Reference

The sorter classifies contracts into 25 CUAD agreement families (plus
`other`). These notes encode the evaluation-derived judgement calls.

## Family equivalence classes

Some families are semantically interchangeable in practice. A classification
into ANY member of the same equivalence class is a correct routing decision:

- `reseller` ↔ `distributor` — a "Reseller Agreement" often defines itself as
  a "Distribution Agreement" (pure resale-channel synonymy).
- `maintenance` ↔ `license` — software "License and Maintenance" hybrids; the
  license grant is the operative core either way.
- `development` ↔ `license` — development agreements whose operative
  mechanism is an IP/brand license (e.g. royalty structures).
- `affiliate` ↔ `joint_venture` — an "Affiliate Agreement" whose operative
  clause declares the parties joint venturers.

## Distinguishing signals (title vs operatives)

Titles lie more often than operatives. When they conflict, trust the operative
clauses:

| Ambiguous title | Operative signal → family |
|---|---|
| "Promotion and Distribution Agreement" | grant of resale rights → `distributor` |
| "Marketing and Servicing Agreement" | servicing duties dominate → `service` |
| "License, Development and Commercialization" | IP license grant + royalties → `license` |
| "Sponsorship and Development Agreement" | sponsorship consideration → `sponsorship` |
| "Site Development and Hosting Agreement" | hosting duties dominate → `hosting` |

## CUAD paper counts (folder taxonomy ground truth)

Affiliate 10 · Agency 13 · Collaboration 26 · Co-Branding 22 · Consulting 11 ·
Development 29 · Distributor 32 · Endorsement 24 · Franchise 15 · Hosting 20 ·
IP 17 · Joint Venture 23 · License 33 · Maintenance 34 · Manufacturing 17 ·
Marketing 17 · Non-Compete 3 · Outsourcing 18 · Promotion 12 · Reseller 12 ·
Service 28 · Sponsorship 31 · Supply 18 · Strategic Alliance 32 ·
Transportation 13. (Total 510 — every contract belongs to one family.)
