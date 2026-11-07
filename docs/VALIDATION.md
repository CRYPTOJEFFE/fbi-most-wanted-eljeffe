# Validation record

**Date:** November 7, 2026, America/New_York (ET)

This record captures the tests and live-source checks completed during development.
It contains aggregate measurements and checksums. It does not publish downloaded
person records, portraits, or posters.

## Execution environment

| Component | Observed value |
|---|---|
| Operating system | Linux |
| Architecture | x86_64 |
| Kernel | 6.18.57 |
| Python | 3.12.14 |
| SQLite | 3.53.1 |
| Logical CPUs visible to the process | 9 |

The application uses Python's standard library at runtime. No external validation
library was installed for the checks below.

## Automated checks

The final local unittest run passed **65 tests in 5.746 seconds**:

- **36 ingestion/network tests:** source discovery, parsing, pagination, integrity,
  attachment handling and controlled failure cases.
- **29 CLI, store and local HTTP API tests:** command behavior, source isolation,
  transactional imports, queries, exports and API behavior.

The suite is run with:

```sh
python -m unittest discover -s tests -v
```

Additional completed checks:

- A source-scoped CLI export of **479 records** revalidated with **zero
  schema/property errors**.
- The portable Python `.pyz` build completed.
- Demo loading and searching from the built `.pyz` completed successfully.
- Both the portable `.pyz` and an installed wheel were launched from outside the
  source checkout. Each served its bundled `/`, `/app.js` and `/styles.css`
  assets, and `/api/stats` reported the five explicitly loaded demo records.
- The bundled FollowTheMoney model was independently checked against its upstream
  Git blob hash.

The suite exercises meaningful failure cases, including malformed records and
duplicate IDs, interrupted or inconsistent snapshots, checksum/count mismatches,
URL and redirect restrictions, media-type rejection, source-specific retirement,
and preservation of existing data when an import fails.

## Live source ingestion

All three sources below completed a live download and database import.

| Source | Version / retrieval | Imported source observations | Completeness |
|---|---|---:|---|
| OpenSanctions FBI Most Wanted | `20261107172904-jav` | 479 | Complete; publisher count, size and SHA1 matched |
| OpenSanctions OFAC SDN | `20261107181005-hak` | 72,987 | Complete; publisher count, size and SHA1 matched |
| Direct FBI Wanted API | November 7, 2026, 1:52:12–1:54:02 p.m. ET | 1,269 | Complete; 26 pages, reported total 1,269, no warnings |

The direct FBI API initially returned HTTP 403. A later ordinary request to the
same official endpoint succeeded, followed by the complete 26-page run. The
successful run used no alternate endpoint or access-control workaround.

The local database contains **74,735 source observations** across these three
sources. This is a count of records, including notices, people, organizations,
sanctions and relationship entities. It is not a count of unique wanted people.
The FBI API includes wanted, missing-person and information notices.

### OpenSanctions file integrity

| Dataset | File size | Publisher entity count | Parsed records | Unique IDs within source |
|---|---:|---:|---:|---:|
| `us_fbi_most_wanted` | 652,779 bytes | 479 | 479 | 479 |
| `us_ofac_sdn` | 53,581,274 bytes | 72,987 | 72,987 | 72,987 |

**FBI Most Wanted**

```text
SHA1:   fa52c248da07a86b5eab53b3ba534176bc72d184
SHA256: e965cfbd5df8973efc9c0140220e13b1e98617ab247dc4a9e9ad95afc519b2d7
```

**OFAC SDN**

```text
SHA1:   8dab7e5bbb9a675ef44db3c038de88fee062cfce
SHA256: 19a67666da71f73064ca78a771b3eafa1b24dd0b516ce0823be4c3d3c1c4088c
```

Both SHA1 digests match the publisher's resource metadata. Independent local
reads reproduced both SHA1 and SHA256 values, record counts and byte counts.

### Source dates

| Source | Export time, ET | Source `last_change`, UTC |
|---|---|---|
| OpenSanctions FBI | November 7, 2026, 12:29:04 p.m. | `2026-10-30T17:38:13` |
| OpenSanctions OFAC | November 7, 2026, 1:10:05 p.m. | `2026-11-06T20:10:04` |

These are data-processing observations. They are distinct from an authority's
designation, listing or effective date.

## FollowTheMoney validation

Validation used the official **FollowTheMoney 4.11.3** ontology snapshot bundled
with this project:

- Upstream repository: https://github.com/opensanctions/followthemoney
- Upstream file: `js/src/defaultModel.json`
- Source Git blob: `d8cc0d21637fdc9414077d997201b980c2e0b43e`
- Verified local size: **242,321 bytes**
- Schema definitions: **71**
- License and regeneration details: [FTM_MODEL.md](FTM_MODEL.md)

The two complete OpenSanctions entity files were independently checked for:

- Known schemas and inherited property definitions.
- Required-property presence.
- String-array property representation.
- Duplicate entity IDs.
- Declared property length limits.
- Enumerated values, including countries, genders and topics.
- Valid partial ISO dates and preservation of supplied date precision.
- URL structure.
- Entity-reference resolution within the corresponding source.

**Result: zero errors across all 73,466 OpenSanctions observations.**

The **1,269 directly retrieved FBI notices** were also checked after mapping:
**zero unknown schema/property errors**. The source-specific CLI export of the
479-record FBI dataset likewise produced zero schema/property errors.

These are explicit ontology-based and structural checks. They do not assert that
every normalization algorithm from the upstream Python package was executed.
The upstream `ftm validate` command can normalize or drop values; its successful
exit alone would not establish a lossless round trip.

### Categories and graph integrity

The OpenSanctions FBI file contains **479 Person records**, all with the
`wanted` topic.

The OFAC file contains **18 schema types**, including **7,516 Person records**
and **19,563 Sanction records**. No record in that source carries the `wanted`
topic. All 19,563 sanction records identify the **Office of Foreign Assets
Control** as the authority.

OFAC contains **62,796 entity-reference values across 48,344 records**. Every
referenced ID resolves inside that source snapshot: **zero unresolved references**.
Relationships are derived only from properties the ontology marks as
`type: "entity"`. A name or note equal to an entity ID does not create a link.

Across the two OpenSanctions sources:

- **73,466 source observations**
- **73,336 unique upstream IDs**
- **130 upstream canonical IDs shared between sources**

Source assertions are stored separately. An export selection containing
conflicting duplicate IDs requires selection of one source; records are not
implicitly combined by name or ID.

### Authority dates and incomplete source facts

Only **31 OFAC Sanction records** supply `listingDate` and `startDate`. Their
published values are:

```text
listingDate: 2022-02-24
startDate:   2022-03-26
```

Other sanction records omit these dates. Their crawl/export timestamps were not
used to invent missing designation dates.

The OpenSanctions FBI file contains **574 birth-date values across 454 records**:
**565** have day precision and **9** have year-only precision. Missing months and
days were not filled.

## Live attachment verification

One live official FBI notice was used to exercise file discovery, download,
local storage and hash verification. The resulting six source links returned:

| Media type | Files |
|---|---:|
| JPEG | 3 |
| GIF | 1 |
| PDF | 2 |
| **Total** | **6** |

Verification results:

- **6 of 6** stored files had independently verified SHA256 hashes.
- **525,790 bytes** were downloaded.
- The links represented **5 unique content hashes** and **296,818 unique-content
  bytes**; multiple source URLs can return identical content.
- All stored paths remained inside the configured workspace.
- All generated local file URLs pointed to the corresponding stored files.

Representative verified PDF SHA256:

```text
c0b1c7df23e910c55f510660a597f0edaf7ce0e644b879f1381008bb6e39b7c2
```

This verifies the exercised notice and supported media path. It does not establish
that every agency provides downloadable media, or that every future source URL
will remain available.

## Observed local query timings

These were single measurements on this development environment before the
complete direct FBI API import, with **73,516 local source observations**.
The process and filesystem caches had already been used; these should be treated
as warmish, run-specific observations rather than controlled cold-start
benchmarks.

| Query | Observed elapsed time |
|---|---:|
| Wanted Person records | 43.03 ms |
| Sanctions category | 318.20 ms |
| Full-text query `united states` | 219.60 ms |
| OFAC source with Sanction schema | 36.72 ms |

These figures include the behavior measured in this particular run and do not
constitute latency guarantees, percentiles, concurrent-load measurements, or
performance measurements on macOS or Windows.

## Chromium browser verification

The self-contained browser smoke test passed against a real local Python server,
using Playwright 1.62.1 and Chrome Headless Shell 157.0.8103.38. It starts and stops
its own server and uses an isolated temporary workspace. It requires no live data
downloads.

Nine workflow groups passed:

1. Fresh installation starts empty; demo records never load automatically.
2. Explicit demo action loads five prominently labeled fictional records.
3. Wanted, sanctions and PEP filters preserve their different meanings.
4. Search, empty results, details and raw FtM inspection work.
5. Sources and snapshot history are displayed.
6. Downloaded ZIP contains FtM JSONL and a matching provenance/checksum manifest.
7. Imported hostile markup remains inert text and unsafe URLs are not executable links.
8. An invalid import shows its error and preserves the prior database.
9. Desktop and 390-pixel mobile layouts have no document-level horizontal overflow.

No page errors or external asset requests were recorded. Desktop and mobile
screenshots were visually inspected; [workbench-preview.png](workbench-preview.png)
shows the actual application with fictional demo records. Screenshot dates use
America/New_York.

## Operating-system coverage

In addition to the local Linux checks, every job passed in
[GitHub Actions run 42405101079](https://github.com/CRYPTOJEFFE/fbi-most-wanted-eljeffe/actions/runs/42405101079),
testing commit `df2808525610e6c067b0301c5ee49c991b5bed42`:

| Runner | Result | Work exercised |
|---|---|---|
| Linux (`ubuntu-latest`) | Passed | 65 tests, portable package build/demo, installed CLI |
| Windows (`windows-latest`) | Passed | 65 tests, portable package build/demo, installed CLI |
| Intel Mac (`macos-15-intel`) | Passed | 65 tests, portable package build/demo, installed CLI |
| Apple silicon Mac (`macos-latest`) | Passed | 65 tests, portable package build/demo, installed CLI |
| Chromium on Linux | Passed | All nine browser workflow groups |

The native OS jobs used Python 3.12 and uploaded portable `.pyz` artifacts.
The initial matrix exposed a test that compared an already resolved attachment
path with an unresolved temporary-directory alias. The corrected assertion
resolves both existing paths before checking containment; it continues to reject
files outside the destination. The successful run above includes that correction.
Browser automation was executed on Linux; this does not claim separate GUI
automation on every OS or on every Chromium-branded browser.

## Primary references

- FBI Wanted API: https://www.fbi.gov/wanted/api
- Live FBI endpoint: https://api.fbi.gov/wanted/v1/list
- OpenSanctions downloads and update semantics:
  https://www.opensanctions.org/docs/bulk/updates/
- OpenSanctions metadata:
  https://www.opensanctions.org/docs/metadata/
- OpenSanctions entity structure:
  https://www.opensanctions.org/docs/entities/
- FBI dataset:
  https://www.opensanctions.org/datasets/us_fbi_most_wanted/
- OFAC SDN dataset:
  https://www.opensanctions.org/datasets/us_ofac_sdn/
- FollowTheMoney schema reference:
  https://followthemoney.tech/explorer/schemata/
- FollowTheMoney command-line validation:
  https://followthemoney.tech/docs/cli/
