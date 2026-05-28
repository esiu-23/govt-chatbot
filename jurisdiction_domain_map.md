# Jurisdiction Domain Map — Chicago / Cook County / Illinois

Reference document for building map layers. Each domain shows what is owned/operated at city, county, and state level. Independent authorities are folded into the appropriate jurisdiction column based on governance (see mapping table at bottom).

---

## Independent Authorities → Jurisdiction Mapping

| Authority | Assigned To | Rationale |
|---|---|---|
| Chicago Public Schools (CPS) | City | State-created special district; board mayor-appointed; city-funded |
| Chicago Park District | City | State-created; board mayor-appointed; city-only footprint |
| CTA | City | RTA subsidiary; majority city-appointed board; city-only service area |
| Metra | State/Regional | RTA subsidiary; multi-county board; collar county + city service |
| Pace | State/Regional | RTA subsidiary; multi-county suburban bus service |
| Forest Preserves of Cook County | County | County-operated; 69,000 acres across Cook County |
| Cook County Health (Stroger, Provident, Oak Forest) | County | County-operated hospital + clinic network |

---

## Domain Map

### 1. Public Safety / Law Enforcement

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Police Department (CPD) | Municipal law enforcement within city limits |
| **City** | Chicago Fire Department (CFD) | Fire suppression, EMS within city |
| **County** | Cook County Sheriff's Office | Law enforcement in unincorporated Cook County; also patrols some forest preserves |
| **County** | Cook County Jail (Dept. of Corrections) | Largest single-site jail in the US; pre-trial and sentenced county detainees |
| **County** | Cook County Animal & Rabies Control | Countywide |
| **State** | Illinois State Police (ISP) | State highways, statewide investigations, FOID/background checks |
| **State** | Illinois Dept. of Corrections (IDOC) | State prisons (sentenced felons; separate from county jail) |

**Map layer assets:** Police districts (city), Sheriff patrol zones (unincorporated county), ISP district offices

---

### 2. Courts & Justice

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Department of Administrative Hearings | City ordinance violations (parking, building code) |
| **County** | Circuit Court of Cook County | Trial court of general jurisdiction; 1.2M+ cases/year |
| **County** | Clerk of the Circuit Court | Maintains all court records |
| **County** | State's Attorney | Criminal prosecution at county level |
| **County** | Public Defender | Court-appointed defense; largest in the nation |
| **County** | Adult Probation Department | County probation supervision |
| **County** | Public Guardian | Protects elders and dependent adults |
| **State** | Illinois Appellate Court (1st District) | Hears appeals from Cook County Circuit Court |
| **State** | Illinois Supreme Court | Final state appellate authority |
| **State** | Attorney General | Statewide law enforcement; consumer protection |

**Map layer assets:** Courthouse locations (county), City hearing office locations

---

### 3. Health Services

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Dept. of Public Health (CDPH) | City public health programs, inspections, clinics |
| **City** | Chicago DFSS social/health services | Delegate agency network |
| **County** | Cook County Health — Stroger Hospital | Level 1 trauma center, public hospital |
| **County** | Cook County Health — Provident Hospital | South Side public hospital |
| **County** | Cook County Health — Oak Forest Hospital | South suburban public hospital |
| **County** | Cook County Health — 30+ primary care clinics | Scattered across county |
| **County** | Medical Examiner's Office | Countywide death investigations |
| **State** | Illinois Dept. of Public Health (IDPH) | Statewide public health regulation, vital stats, food inspections |
| **State** | Illinois Dept. of Healthcare & Family Services (HFS) | Medicaid administration |
| **State** | Illinois Dept. of Human Services (IDHS) | Medicaid/SNAP enrollment, mental health, substance abuse |

**Map layer assets:** CDPH clinics (city), Cook County Health clinics/hospitals (county), IDPH-licensed facilities (state)

---

### 4. Education

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Public Schools (CPS) | Mayor-appointed board; ~600 schools within city limits |
| **City** | City Colleges of Chicago (CCC) | Mayor-appointed board; 7 community colleges |
| **County** | None directly | No county-run schools in Illinois |
| **State** | Illinois State Board of Education (ISBE) | Sets standards, funding formula, accreditation |
| **State** | Illinois Community College Board (ICCB) | Oversees community college system statewide |
| **State** | University of Illinois system (UIC, UIUC, etc.) | State-funded public universities |

**Map layer assets:** CPS school buildings (city), City Colleges campuses (city), UIC campus (state)

---

### 5. Transportation & Infrastructure (Roads)

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | CDOT — Chicago Dept. of Transportation | City streets, alleys, sidewalks, traffic signals within Chicago |
| **City** | City-owned bridges | Chicago River bridges and others within city |
| **County** | Cook County Dept. of Transportation & Highways | Roads in unincorporated Cook County only; does not maintain roads inside municipalities |
| **State** | Illinois Dept. of Transportation (IDOT) | State highways (US/IL routes), interstates passing through Chicago (I-90, I-94, I-290, etc.) |
| **State** | Illinois Tollway (ISTHA) | Tollway system (I-88, I-90/94 toll segments) |

**Map layer assets:** Street centerlines tagged by jurisdiction, IDOT highway segments, tollway segments, county road network (unincorporated areas)

---

### 6. Transit

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | CTA (Chicago Transit Authority) | Bus + L rail within city and near suburbs; majority city-appointed board |
| **State/Regional** | Metra | Commuter rail; multi-county RTA subsidiary; board appointed by suburban county officials |
| **State/Regional** | Pace | Suburban bus; multi-county RTA subsidiary |
| **State** | Regional Transportation Authority (RTA) | Oversight body funding CTA/Metra/Pace; state-created |

**Map layer assets:** CTA rail stations + bus routes (city), Metra stations/lines (state/regional), Pace stops (state/regional)

---

### 7. Housing & Buildings

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Dept. of Buildings (DOB) | Building permits, inspections, code enforcement within city |
| **City** | Chicago Dept. of Housing (DOH) | Affordable housing programs, housing vouchers (city portion) |
| **City** | Chicago Housing Authority (CHA) | Public housing within city limits; mayor-appointed board |
| **County** | None directly | County does not regulate buildings inside municipalities |
| **State** | Illinois Housing Development Authority (IHDA) | State housing finance, low-income housing tax credits |
| **State** | Illinois Dept. of Commerce & Economic Opportunity (DCEO) | Community development block grants |

**Map layer assets:** Building permit addresses (city Socrata `ydr8-5enu`), CHA properties (city), IHDA-funded developments

---

### 8. Parks & Open Space

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Park District | 580+ parks within city limits; mayor-appointed board |
| **City** | Chicago Dept. of Cultural Affairs (DCASE) | Millennium Park, Navy Pier programming |
| **County** | Forest Preserves of Cook County | 69,000 acres of forest preserves across all of Cook County (including within Chicago) |
| **State** | Illinois Dept. of Natural Resources (IDNR) | State parks, nature preserves |

**Map layer assets:** Chicago Park District park polygons (city), Forest Preserve district polygons (county), IDNR state park boundaries

---

### 9. Environmental Services

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | CDOT / Streets & Sanitation | Garbage collection, street sweeping, recycling within city |
| **City** | Chicago Dept. of Water Management | City water supply and sewer within city |
| **County** | Metropolitan Water Reclamation District (MWRD) | Wastewater treatment for Cook County; special district, separate elected board |
| **County** | Cook County Bureau of Environmental Sustainability | County sustainability programs |
| **State** | Illinois Environmental Protection Agency (IEPA) | Statewide air/water/land regulation, permits, cleanups |
| **State** | Illinois Dept. of Natural Resources (IDNR) | Wildlife, wetlands, natural areas |

**Map layer assets:** Water main infrastructure (city), MWRD treatment plant locations (county/regional), IEPA-regulated sites

---

### 10. Property / Land Records & Taxation

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | None directly | City does not assess property |
| **County** | Cook County Assessor's Office | Assesses property value for all Cook County parcels |
| **County** | Cook County Treasurer | Sends tax bills, collects property taxes |
| **County** | Cook County Board of Review | Property tax assessment appeals |
| **County** | Cook County Clerk — Tax Extension Unit | Calculates tax rates for all taxing districts |
| **State** | Illinois Dept. of Revenue (IDOR) | Sets assessment standards; homestead exemption oversight |
| **State** | Illinois Property Tax Code (ILCS) | Statutory framework for all property taxation |

**Map layer assets:** County parcel boundaries (Cook County Assessor GIS), TIF district boundaries (city `fz5x-7zak`), city-owned parcels (city Socrata `aksk-kvfp`)

---

### 11. Social Services & Human Services

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | DFSS — Dept. of Family & Support Services | City-funded social services, delegate agency network |
| **City** | Chicago Commission on Human Relations | City civil rights enforcement |
| **County** | Cook County Dept. of Community & Family Support | County social services |
| **County** | Cook County Dept. of Veteran Affairs | County veteran services |
| **State** | IDHS — Dept. of Human Services | Medicaid/SNAP/TANF enrollment (dhs.state.il.us) |
| **State** | IDES — Dept. of Employment Security | Unemployment insurance (ides.illinois.gov) |
| **State** | Illinois Dept. on Aging | Senior services, elder abuse prevention |
| **State** | Illinois Dept. of Children & Family Services (DCFS) | Child protective services |

**Map layer assets:** DFSS service centers (city), IDHS benefit offices (state)

---

### 12. Business & Licensing

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | BACP — Dept. of Business Affairs & Consumer Protection | Business licenses within city (Socrata `r5kz-chrr`) |
| **City** | City Clerk | Tobacco licenses, various city permits |
| **County** | Cook County Bureau of Economic Development | County business development programs |
| **County** | Cook County permits (unincorporated only) | Building/zoning permits in unincorporated Cook County |
| **State** | Illinois Dept. of Financial & Professional Regulation (IDFPR) | Professional licenses statewide (doctors, lawyers, contractors) |
| **State** | Illinois Secretary of State | Business entity registration (LLCs, corporations) |
| **State** | Illinois Dept. of Revenue (IDOR) | Sales tax, employer registration |

**Map layer assets:** Active business license locations (city Socrata `r5kz-chrr`), IDFPR-licensed facilities

---

### 13. Vital Records & Elections

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Board of Election Commissioners | Runs elections within Chicago; separate from county |
| **County** | Cook County Clerk | Vital records (birth, death, marriage certificates) for suburban Cook County; election administration for suburban Cook |
| **County** | Cook County Clerk — Elections Division | Suburban Cook County elections |
| **State** | Illinois State Board of Elections | Statewide election oversight |
| **State** | Secretary of State | Driver's licenses, vehicle registration, state ID (ilsos.gov) |
| **State** | Illinois Dept. of Public Health (IDPH) | Statewide vital records registry |

**Map layer assets:** Election precinct boundaries (city/county), polling place locations

---

### 14. Emergency Management

| Jurisdiction | Agency / Service | Notes |
|---|---|---|
| **City** | Chicago Office of Emergency Management & Communications (OEMC) | City 911, emergency coordination, city cameras |
| **County** | Cook County Dept. of Emergency Management & Regional Security | County-level preparedness, coordination across municipalities |
| **State** | Illinois Emergency Management Agency (IEMA) | Statewide disaster response, federal liaison |
| **State** | Illinois National Guard | State military force, activated for emergencies |

**Map layer assets:** OEMC camera locations (city), emergency shelter locations

---

## Quick Reference Cheat Sheet

| If it's on a city block and it's a... | Check |
|---|---|
| Street / alley / sidewalk | **City** (CDOT) |
| Highway / interstate | **State** (IDOT) |
| School building | **City** (CPS) — unless private/charter |
| Park | **City** (Park District) or **County** (Forest Preserve) |
| Police presence | **City** (CPD) in Chicago; **County** (Sheriff) outside |
| Hospital / clinic | Could be **City** (CDPH clinic), **County** (Cook County Health), or **State** (UI Health) |
| Property tax parcel | **County** (Assessor) assesses; multiple taxing districts collect |
| Business license | **City** (BACP) for city address; state professional license (IDFPR) for the practitioner |
| Building permit | **City** (DOB) inside Chicago; **County** in unincorporated areas |
| Transit stop | **City** (CTA) for bus/L; **State/Regional** (Metra/Pace) for commuter rail |
| Water/sewer | **City** water supply; **County/Regional** (MWRD) for wastewater treatment |
| Vacant lot / city-owned land | **City** parcels dataset `aksk-kvfp` |
| Forest / nature area | **County** (Forest Preserves) or **State** (IDNR) |

---

*Sources: chicago.gov, cookcountyil.gov, illinois.gov, ides.illinois.gov, dhs.state.il.us, app/prompts.py `_SCOPE_CONTEXT`. Last updated 2026-05-11.*
