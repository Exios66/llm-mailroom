# Classes and strata

The canonical surface is **five document classes** and **55 class-by-subclass strata**. Subclasses are second-level gold (`expected_subclass`); they are not extra top-level classes. Counts below are from Mailroom-Corpus-EDA `reports/tables/strata_counts.csv` and `imbalance_metrics.json` (P2, 2026-09-13), matching Hub `mailroom-dataset` v1 / corpus-family v9.

Type entropy is **2.09 bits**. Type-level max/min imbalance is **7.2×** (1,100 insurance vs 152 merger). Stratum-level max/min is **557×** (557 correspondence `email` vs 1 merger `mixed_cash_stock_election`).

† = zero test rows under the family split. ‡ = minority stratum (< 10 rows).

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/23_imbalance_treemap.png" alt="Treemap of mailroom-dataset class and subclass imbalance"><figcaption><p>Imbalance treemap (figure 23). Interactive: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/figures_interactive/23_imbalance_treemap.html">Plotly HTML</a>.</p></figcaption></figure>

## `insurance_claim` — 1,100 rows (33.3%)

Six lines of business. Largest class. Train 986 / test 114.

| Subclass     | Rows | Train | Test | Share | Test share |
| ------------ | ---: | ----: | ---: | ----: | ---------: |
| `auto`       |  300 |   266 |   34 | 9.09% |      11.3% |
| `property`   |  200 |   184 |   16 | 6.06% |       8.0% |
| `carrier`    |  150 |   130 |   20 | 4.54% |      13.3% |
| `inpatient`  |  150 |   135 |   15 | 4.54% |      10.0% |
| `outpatient` |  150 |   135 |   15 | 4.54% |      10.0% |
| `pde`        |  150 |   136 |   14 | 4.54% |       9.3% |

Source card: [CMS insurance claims](source-corpora/cms-insurance-claims.md).

## `correspondence` — 1,000 rows (30.3%)

Eight mail subtypes drawn from the Enron dedup pool. Train 915 / test 85. `email` alone is 16.9% of the whole corpus.

| Subclass            | Rows | Train | Test |  Share | Test share |
| ------------------- | ---: | ----: | ---: | -----: | ---------: |
| `email`             |  557 |   518 |   39 | 16.87% |       7.0% |
| `memo`              |   83 |    74 |    9 |  2.51% |      10.8% |
| `notice`            |   81 |    77 |    4 |  2.45% |       4.9% |
| `letter`            |   79 |    65 |   14 |  2.39% |      17.7% |
| `press_release`     |   78 |    70 |    8 |  2.36% |      10.3% |
| `demand`            |   66 |    60 |    6 |  2.00% |       9.1% |
| `meeting_request`   |   53 |    48 |    5 |  1.61% |       9.4% |
| `attorney_demand` † |    3 |     3 |    0 |  0.09% |       0.0% |

Source card: [Enron correspondence](source-corpora/enron-correspondence.md).

## `contract` — 600 rows (18.2%)

Twenty-six CUAD commercial-contract groups (26 of CUAD's 28-group taxonomy appear). Train 540 / test 60. 509 rows carry CUAD clause spans; 91 v9 EX-10 exhibits do not.

| Subclass                    | Rows | Train | Test | Share | Test share |
| --------------------------- | ---: | ----: | ---: | ----: | ---------: |
| `Supply`                    |   47 |    42 |    5 | 1.42% |      10.6% |
| `License_Agreements`        |   43 |    41 |    2 | 1.30% |       4.7% |
| `Consulting Agreements`     |   38 |    37 |    1 | 1.15% |       2.6% |
| `IP`                        |   35 |    31 |    4 | 1.06% |      11.4% |
| `Maintenance`               |   34 |    30 |    4 | 1.03% |      11.8% |
| `Service`                   |   34 |    26 |    8 | 1.03% |      23.5% |
| `Distributor`               |   32 |    27 |    5 | 0.97% |      15.6% |
| `Strategic Alliance`        |   32 |    28 |    4 | 0.97% |      12.5% |
| `Sponsorship`               |   31 |    28 |    3 | 0.94% |       9.7% |
| `Development`               |   28 |    24 |    4 | 0.85% |      14.3% |
| `Collaboration`             |   26 |    24 |    2 | 0.79% |       7.7% |
| `Endorsement` †             |   24 |    24 |    0 | 0.73% |       0.0% |
| `Co_Branding`               |   22 |    17 |    5 | 0.67% |      22.7% |
| `Hosting`                   |   20 |    18 |    2 | 0.61% |      10.0% |
| `Outsourcing` †             |   18 |    18 |    0 | 0.55% |       0.0% |
| `Manufacturing`             |   17 |    15 |    2 | 0.51% |      11.8% |
| `Marketing`                 |   17 |    15 |    2 | 0.51% |      11.8% |
| `Franchise`                 |   15 |    14 |    1 | 0.45% |       6.7% |
| `Joint Venture _ Filing` †  |   14 |    14 |    0 | 0.42% |       0.0% |
| `Agency Agreements` †       |   13 |    13 |    0 | 0.39% |       0.0% |
| `Transportation`            |   13 |    11 |    2 | 0.39% |      15.4% |
| `Promotion` †               |   12 |    12 |    0 | 0.36% |       0.0% |
| `Reseller` †                |   12 |    12 |    0 | 0.36% |       0.0% |
| `Affiliate_Agreements`      |   11 |     9 |    2 | 0.33% |      18.2% |
| `Joint Venture` ‡           |    9 |     7 |    2 | 0.27% |      22.2% |
| `Non_Compete_Non_Solicit` † |    3 |     3 |    0 | 0.09% |       0.0% |

Source card: [CUAD contracts](source-corpora/cuad-contracts.md).

## `corporate_record` — 450 rows (13.6%)

Ten governance-document subclasses from SEC EDGAR exhibits. Train 403 / test 47.

| Subclass                    | Rows | Train | Test | Share | Test share |
| --------------------------- | ---: | ----: | ---: | ----: | ---------: |
| `charter_amendment`         |   80 |    71 |    9 | 2.42% |      11.2% |
| `articles_of_incorporation` |   62 |    57 |    5 | 1.88% |       8.1% |
| `officer_certificate`       |   61 |    54 |    7 | 1.85% |      11.5% |
| `indenture`                 |   57 |    53 |    4 | 1.73% |       7.0% |
| `subsidiary_list`           |   53 |    43 |   10 | 1.61% |      18.9% |
| `rights_instrument`         |   46 |    43 |    3 | 1.39% |       6.5% |
| `board_resolution`          |   32 |    27 |    5 | 0.97% |      15.6% |
| `bylaws`                    |   28 |    27 |    1 | 0.85% |       3.6% |
| `powers_of_attorney`        |   28 |    25 |    3 | 0.85% |      10.7% |
| `other` †                   |    3 |     3 |    0 | 0.09% |       0.0% |

Source card: [SEC corporate records](source-corpora/edgar-corporate-records.md).

## `merger_agreement` — 152 rows (4.6%)

Five consideration-type subclasses from MAUD. Train 135 / test 17. Smallest class and the long-document stress test.

| Subclass                      | Rows | Train | Test | Share | Test share |
| ----------------------------- | ---: | ----: | ---: | ----: | ---------: |
| `all_cash`                    |   57 |    51 |    6 | 1.73% |      10.5% |
| `other`                       |   57 |    49 |    8 | 1.73% |      14.0% |
| `all_stock`                   |   24 |    22 |    2 | 0.73% |       8.3% |
| `mixed_cash_stock`            |   13 |    12 |    1 | 0.39% |       7.7% |
| `mixed_cash_stock_election` † |    1 |     1 |    0 | 0.03% |       0.0% |

Source card: [MAUD merger agreements](source-corpora/maud-merger-agreements.md).

## Split integrity and minority cells

The family split is `md5(filename utf-8) % 10 == 0 → test` (2,979 / 323). Per-stratum test shares still deviate from 10%. Chi-squared split homogeneity across the five types: χ² = 2.88, p = 0.58 (4 d.f.) — type mix is compatible with the hash split; **stratum** mix is not.

**Ten strata have zero test rows:**

| Class              | Subclass                    | Rows |
| ------------------ | --------------------------- | ---: |
| `contract`         | `Endorsement`               |   24 |
| `contract`         | `Outsourcing`               |   18 |
| `contract`         | `Joint Venture _ Filing`    |   14 |
| `contract`         | `Agency Agreements`         |   13 |
| `contract`         | `Promotion`                 |   12 |
| `contract`         | `Reseller`                  |   12 |
| `contract`         | `Non_Compete_Non_Solicit`   |    3 |
| `corporate_record` | `other`                     |    3 |
| `correspondence`   | `attorney_demand`           |    3 |
| `merger_agreement` | `mixed_cash_stock_election` |    1 |

**Five minority strata** (< 10 rows, 19 rows total): `mixed_cash_stock_election` (1), `Non_Compete_Non_Solicit` (3), `corporate_record/other` (3), `attorney_demand` (3), `Joint Venture` (9). Source: [`minority_strata_report.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/minority_strata_report.csv).

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/02_strata_train_test.png" alt="Train versus test counts for each mailroom-dataset stratum"><figcaption><p>Strata train/test (figure 02).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/24_strata_imbalance_ratio.png" alt="Stratum imbalance ratios relative to the majority cell"><figcaption><p>Strata imbalance ratios (figure 24). Interactive: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/figures_interactive/24_strata_ratio.html">Plotly HTML</a>.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/25_minority_strata.png" alt="Minority strata with fewer than ten rows"><figcaption><p>Minority strata (figure 25).</p></figcaption></figure>

The EDA ML-readiness notes recommend stratification-aware sampling or subclass rollups for the five minority cells, and a per-stratum test floor in a later corpus revision. That is analysis, not a pipeline change.

Raw table: [`strata_counts.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/strata_counts.csv). All charts: [Visualizations](visualizations.md).
