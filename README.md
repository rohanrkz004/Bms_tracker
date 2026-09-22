# AMB Cinemas BMS Tracker

Tracks every movie and showtime listed by BookMyShow for **AMB Cinemas: Gachibowli** on **25 September 2026**.

Current stage:
- Scrapes the rendered BookMyShow cinema page with Playwright/Firefox.
- Tracks movie, language/format, showtime, screen label and availability status when exposed.
- Saves a baseline and reports additions, removals and status changes in GitHub Actions logs.
- Refuses to overwrite state when the BMS page is blocked or parsing fails.
- Runs every 5 minutes and can also be run manually.

Discord is intentionally not connected yet. It will be added after the scraper output is verified.
