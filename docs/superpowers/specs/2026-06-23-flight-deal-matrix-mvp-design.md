# Flight Deal Matrix MVP Design

> **Recovery & reconciliation note (added 2026-06-23):** This spec was recovered
> verbatim from the unrelated `master` branch and preserved here unchanged. One
> point must be reconciled before it guides implementation: the "Core User Flow"
> and "MVP Success Criteria" describe user-facing matrices built from **mock
> provider data**. That conflicts with the repository's **real-data-only
> guardrail** (see `CONTRIBUTING.md` and `docs/ARCHITECTURE.md`): any user-facing
> pricing path must hit a real flight API, and mock/fixture fares are confined to
> tests and a clearly-labeled non-user-facing demo mode — never published as
> flight intelligence. Read every "mock provider data" success criterion below as
> "real provider data (mock only in tests/demo)."

## Purpose

Build an MVP service that helps travelers find the best flight deals without manually running repeated searches for each destination city and date combination.

The MVP focuses on price intelligence, not ticketing. A user enters an origin, a destination country or city, flexible departure dates, and return-date rules. The service expands the destination country into likely arrival-city candidates, searches the date combinations through a provider adapter, normalizes results, ranks the best options, and renders an interactive matrix plus PDF/CSV exports.

The reference artifact is `china-flight-matrix.pdf`: a PDF report with destination summary cards and one departure-date x return-date matrix per arrival city.

## MVP Scope

In scope:

- One trip origin, usually a city such as Toronto.
- Destination city or destination country.
- Destination country expansion into arrival-city candidates, such as China to Beijing, Shanghai, Guangzhou, and other configured candidates.
- Departure date range, such as Dec 13-17.
- Return dates from either a fixed date, a return-date range, or a trip-length rule such as about 2 weeks.
- Passenger configuration for adults, children, cabin, and currency.
- Stop preference, including any stops, prefer simpler, max one stop, or nonstop only where available.
- One matrix section per arrival city: departure dates as rows and return dates as columns.
- Highlight all useful winners, not just a single best answer.
- Show booking-site or provider handoff links when available.
- Export PDF similar to the sample report and CSV for spreadsheet review.
- Provider-agnostic backend with a mock provider for MVP development and future real adapters.

Out of scope for MVP:

- Selling tickets, payment collection, PNR creation, cancellation, or ticket servicing.
- True multi-leg itineraries such as Toronto to Beijing to Shanghai to Toronto.
- Open-jaw trips where outbound arrival city and return departure city intentionally differ.
- Multiple origin cities searched together for one traveler.
- User accounts, saved profiles, alerts, or scheduled monitoring.
- Guaranteed bookability. Prices are indicative until confirmed on the provider or booking site.

## Core User Flow

1. User enters origin city/country, destination city/country, departure date range, return-date rule, passengers, cabin, currency, and stop preference.
2. The backend resolves the origin to candidate airports.
3. If the destination is a country, the backend selects arrival-city candidates based on deal-search value: direct-route likelihood, international flight supply, major airport coverage, and configurable country seeds.
4. The backend expands the request into search tasks:
   - origin airport group x arrival city airport group x departure date x return date.
5. A matrix job runs those tasks through the active provider adapter.
6. Provider responses are normalized into a common offer shape.
7. The ranker computes highlights across all cells and city sections.
8. The GUI renders summary winner cards, arrival-city matrix sections, and offer detail panels.
9. The user can open booking handoff links from a populated cell or export the analysis to PDF/CSV.

## Arrival City Candidate Selection

The MVP uses a configurable destination-country seed list rather than trying to infer every city dynamically.

Each country configuration contains:

- Country code and display name.
- Candidate arrival cities.
- Airport groups per city.
- Whether direct or strong one-stop service is expected from common origins.
- Priority score for deal-search usefulness.
- Optional notes, such as seasonal availability or limited provider coverage.

For China, the MVP seed list starts with:

- Beijing: PEK, PKX.
- Shanghai: PVG, SHA.
- Guangzhou: CAN.
- Shenzhen: SZX.
- Chengdu: TFU, CTU where supported.
- Xiamen: XMN.
- Haikou: HAK and Sanya: SYX as optional leisure/island candidates.
- Shenyang: SHE as optional family/region-specific candidate.

The GUI should let the user include or exclude suggested cities before running a large search.

## Date Matrix Model

The primary visual and data unit is:

`arrival city -> departure date x return date matrix`

Rows are departure dates. Columns are return dates. Each populated cell represents the best normalized offer for that origin, arrival city, departure date, and return date after basic filters.

Each cell displays:

- Total price in selected currency.
- Stop pattern, such as `out 1 stop · back 2 stops`.
- Highlight label when applicable.
- Optional compact provider freshness marker.

Clicking a cell opens an offer detail panel with:

- Airline or provider summary.
- Outbound and return stop counts.
- Total duration and layover summary when available.
- Price freshness timestamp.
- Booking handoff link.
- Explanation for any highlight assigned to that cell.

## Highlight Labels

The MVP highlights all useful winners:

- Cheapest: lowest total fare after basic filters.
- Recommended: best overall value after price, stops, duration, layover quality, and availability.
- Low-stop winner: cheapest option matching the stop preference.
- Simple upgrade: nonstop or simpler route when the premium is within the configured threshold.
- Best arrival city: city with the strongest deal profile across date cells.
- Best date combo: globally best departure-return pair across all arrival cities.

The UI must avoid implying that only one cell matters. Multiple labels can apply to one cell.

## Recommendation Explanation

Every highlighted option must explain why it was highlighted.

Examples:

- Cheapest: "Lowest total fare across all searched China arrival cities and date combinations."
- Recommended: "Costs $380 more than the cheapest option but avoids a 2-stop return and saves about 5 hours total travel time."
- Low-stop winner: "Cheapest option with at most 1 stop each way."
- Simple upgrade: "Nonstop itinerary is within the 25% premium threshold over the cheapest valid option."

The ranker should produce structured reason codes and human-readable explanation text so the same reasoning can appear in the GUI and PDF.

## Provider Architecture

The backend uses a provider interface so the MVP can be built without immediate API credentials.

Provider adapter contract:

- Search request: origin airport group, destination airport group, departure date, return date, passengers, cabin, currency, stop preference.
- Search response: normalized offers, provider raw payload reference, booking link if available, freshness timestamp, provider warnings.
- Capabilities: supports booking links, supports live polling, supports max-stop filter, supports duration, supports baggage, supports multi-airport search.

Initial adapters:

- Mock provider: deterministic fixture data shaped like real responses. Used for development, tests, demos, and PDF generation.
- Skyscanner adapter stub: preferred first real adapter when partner access is available.
- Amadeus adapter stub: possible alternate, but Self-Service portal decommissioning on July 17, 2026 makes it a migration risk.
- Duffel adapter stub: possible future option if bookable offers or order flow become desirable.

The backend must surface partial provider failures. If some date/city searches fail, the matrix should render available cells and show warnings rather than failing the whole report.

## Booking Handoff

The MVP does not book flights. It links users to provider or booking-site pages when the provider returns a supported link.

Each booking link should be displayed with:

- Provider name.
- Price freshness timestamp.
- Note that the price may change on the booking site.
- Expiry or refresh state if known.

If a provider does not return a deep link, the cell can show "search on provider" using a generated query URL only if that provider's terms allow it. Otherwise, show price intelligence without a link.

## Backend Components

- API server: exposes endpoints for search jobs, job status, matrix results, exports, and provider health.
- Candidate resolver: resolves origin and destination inputs into airport groups and arrival-city candidates.
- Matrix job orchestrator: expands date/city combinations and runs provider searches with concurrency limits.
- Provider adapters: mock and real connector implementations behind a common interface.
- Normalizer: maps provider-specific flight data into the common offer model.
- Ranker: computes labels, scores, and explanations.
- Storage: persists search jobs, tasks, normalized offers, raw response references, and generated export metadata.
- Exporter: produces PDF and CSV from normalized matrix results.

## GUI Components

- Search form:
  - Origin input.
  - Destination input.
  - Departure date range.
  - Return-date range or trip-length rule.
  - Passenger and cabin controls.
  - Currency and stop preference.
  - Arrival-city candidate checklist for country destinations.
- Job progress view:
  - Shows total searches, completed searches, provider warnings, and partial failures.
- Results summary:
  - Cards for cheapest, recommended, low-stop winner, simple upgrade, best arrival city, and best date combo.
- Matrix sections:
  - One section per arrival city.
  - Departure dates as rows.
  - Return dates as columns.
  - Cell content includes price, stop pattern, labels, and link state.
- Offer detail panel:
  - Explains the selected cell and provides booking handoff links.
- Export controls:
  - Download PDF.
  - Download CSV.

## PDF Output

The PDF mirrors the sample report:

- Title summarizing origin, destination, date window, passenger group, cabin, currency, and freshness.
- Search settings and legend.
- Summary cards for destination-country and arrival-city winners.
- One departure-date x return-date matrix per arrival city.
- Cell content with price, stop pattern, and highlight label.
- Footer with provider coverage, stale-price warning, and booking-link limitations.

PDF generation should use HTML-to-PDF rendering if the app already uses a web frontend, because the reference PDF was generated from Chromium and the GUI/PDF should share visual components where practical.

## Data Model

Core entities:

- SearchJob: user inputs, status, active provider, created timestamp.
- CandidateCity: country, city, airport group, priority, inclusion state.
- SearchTask: one origin airport group, destination airport group, departure date, return date, status.
- Offer: normalized price, currency, provider, airline summary, stops, duration, layovers, booking link, freshness, raw reference.
- MatrixCell: task reference, best offer, labels, explanation text, warnings.
- MatrixSection: arrival city, airport group, cells, city-level highlights.
- ExportArtifact: job reference, type, file path or blob reference, generated timestamp.

## Error Handling

- Missing provider credentials: allow mock mode; real provider endpoints show a clear configuration error.
- Provider rate limit: pause or retry affected tasks; show partial matrix warnings.
- No results for a cell: render `n/a`, not an error.
- Expired booking link: mark as expired and offer to refresh if the provider supports it.
- Country with no configured candidate cities: prompt the user to enter destination cities manually.
- Large search size: warn the user and require reducing cities/dates or confirming a longer job.

## Testing Strategy

Tests should cover:

- Country destination expands to configured arrival cities.
- Departure range and return rules expand into expected date combinations.
- Matrix cells normalize mock provider results correctly.
- Highlighting handles cheapest, recommended, low-stop, simple upgrade, and ties.
- Recommendation explanations include price, stops, and tradeoff reasoning.
- Partial provider failures still produce a usable matrix.
- PDF/CSV exporters receive stable normalized data.

Implementation should use test-driven development for behavior changes. The mock provider makes the MVP testable before real credentials exist.

## MVP Success Criteria

- A user can enter Toronto to China, a Dec 13-17 departure window, and a return rule.
- The app suggests arrival cities such as Beijing, Shanghai, and Guangzhou.
- The backend generates a date matrix for each selected arrival city using mock provider data.
- The GUI highlights cheapest, recommended, low-stop, simple upgrade, best arrival city, and best date combo.
- Each highlighted option shows stops and explains why it was recommended.
- Each populated cell can show a booking handoff link when present in the provider data.
- The app exports a PDF similar to the reference report and a CSV of normalized matrix rows.
