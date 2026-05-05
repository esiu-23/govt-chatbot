---
title: Who Controls Chicago? — Analysis Overview
file: who-controls-chicago.html
last-reviewed: 2026-05-02
---

# Who Controls Chicago? — How the Analysis Is Built

## Purpose

Maps Chicago's decision-making structure: who holds power (elected vs. appointed), how much money flows through each office/department, and where that data does (and doesn't) exist in the Chicago Open Data Portal.

---

## Three Sections

### 1. "Where the Money Goes" — D3 Treemap

**What it shows:** Every major city office and department as a rectangle sized by spending.

**How it's built:**

- A hardcoded `GOV_TREE` object defines the government hierarchy (Mayor → departments, City Clerk, City Treasurer, City Council, City-wide Obligations). Each node carries `deptKeys` — the exact strings used in the open-data portal to match that department.
- On page load, six Socrata API calls run in parallel (`Promise.allSettled`):

  | Data | Dataset | API Query |
  |---|---|---|
  | 2026 budget appropriations | `6694-f78c` | `sum(ordinance_amount)` grouped by `department_description` |
  | Contract award amounts | `rsxa-ify5` | `sum(award_amount)` grouped by `department` |
  | Vendor payments | `pkr3-4xv7` | `sum(amount)` grouped by `department_name` |
  | Employee headcount | `xzkq-xp2w` | `count(*)` grouped by `department` |
  | Executive officeholders | `xzkq-xp2w` | filter on `COMMISSIONER`, `SUPERINTENDENT`, `MAYOR`, `DIRECTOR`, `TREASURER`, `CLERK` in job title |
  | City-wide direct voucher total | `pkr3-4xv7` | filter `contract_number = 'DV'`, `sum(amount)` |

- Each result is attached to its matching `GOV_TREE` node via `deptMap` (a lookup built from `deptKeys`).
- Size priority: `budget` → `contracts` → `vendorPayments`, with a $5M floor so tiny departments stay visible.
- Color encodes appointment type: yellow border = elected, blue border = appointed by Mayor Johnson, grey border = appointed (appointer not in open data), purple = city-wide obligations.
- Clicking a leaf opens a **detail panel** showing role, officeholder, salary, budget, contracts, vendor payments, and headcount.

### 2. "How Money Flows Through City Government" — Static Table

**What it shows:** Ten spending channels (budget appropriations, contracts, vendor payments, TIF creation, TIF spending, delegate agency contracts, SSAs, CHA, CTA, bonds) with who controls each, whether the approver is elected or appointed, and open-data availability.

**How it's built:** Fully static HTML — no API calls. Data sourced from public record and dataset knowledge. Dataset IDs are cited inline.

### 3. "Who Represents Your Neighborhood?" — Leaflet Ward Map

**What it shows:** Chicago's 50 wards as a choropleth. An optional overlay shows TIF district boundaries shaded by approved project spending.

**How it's built:**

- Ward boundaries fetched from Socrata GeoJSON export, trying two dataset IDs in sequence: `igwz-8jzy`, then fallback `sp34-6z76`.
- TIF district boundaries fetched from `fz5x-7zak` GeoJSON export.
- TIF spending per district fetched from dataset `mex4-ppfc` (`sum(approved_amount)` grouped by `tif_district`). Opacity of each TIF polygon scales linearly up to a $50M cap.
- Clicking a ward or TIF polygon opens a popup. Ward alderman names are noted as not available in open data; a link to the City Clerk ward lookup is shown instead.
- The TIF overlay is toggled via a checkbox (hidden by default).

---

## Data Sources Summary

| Dataset ID | Name | Used For |
|---|---|---|
| `6694-f78c` | Budget 2026 Appropriations | Treemap sizing (primary) |
| `rsxa-ify5` | Contracts | Treemap sizing (fallback); spending streams table |
| `pkr3-4xv7` | Vendor Payments | Treemap sizing (fallback); city-wide DV obligations |
| `xzkq-xp2w` | Current Employee Salaries | Headcount and executive officeholder lookup |
| `fz5x-7zak` | TIF District Boundaries | Ward map TIF overlay |
| `mex4-ppfc` | TIF Projects | TIF polygon spending opacity |
| `igwz-8jzy` / `sp34-6z76` | Ward Boundaries | Ward map base layer |
| `umwj-yc4m` | TIF Annual Expenditures | Cited in spending streams table only (not fetched at runtime) |

---

## What Is Hardcoded vs. Live

| Element | Source |
|---|---|
| Government hierarchy structure | Hardcoded `GOV_TREE` in JS |
| Elected/appointed status | Hardcoded per node |
| Officeholder names (Mayor, Clerk, Treasurer, Council) | Hardcoded |
| Officeholder names for department heads | Pulled live from `xzkq-xp2w` by job title keyword |
| Dollar amounts | Pulled live from Socrata APIs |
| Ward and TIF boundaries | Pulled live from Socrata GeoJSON exports |

---

## Known Gaps / "Not in Open Data"

- Alderman names per ward (linked to City Clerk instead)
- Special Service Area (SSA) spending
- Chicago Housing Authority budget
- Chicago Transit Authority budget
- Bond proceeds detail
- Appointment dates for commissioners

---

## How to Add More Data

- **New department:** Add a node to `GOV_TREE._children` (or as a child of `mayor._children`) with `id`, `name`, `role`, `type`, and `deptKeys` matching the portal's department name strings exactly.
- **New financial metric:** Add a Socrata fetch to `loadFinancialData()`, call `attach()` with the right key/value fields and a new property name, then surface it in `showDetailPanel()`.
- **New map layer:** Add a `socrataFetch` in `initMap()` and create a new `L.geoJSON` layer, following the TIF toggle pattern.
- **Update officeholders:** Either update the hardcoded `officeholder` field on a node, or rely on the live `xzkq-xp2w` salary lookup (which finds the first matching title).
