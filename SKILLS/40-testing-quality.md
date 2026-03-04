# Testing & Quality
## Strategy
- Unit tests for logic (pytest, Jasmine/Karma).
- Integration tests for API boundaries (httpx AsyncClient).
- E2E tests for critical user flows (Playwright).
## Minimum for every PR
- A test plan in the PR summary.
- Run DoD.
## Coverage targets
- API: 80%+ line coverage
- Scenario engine: 90%+ (safety-critical)
