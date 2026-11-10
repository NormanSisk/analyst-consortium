# Security policy

## Reporting a vulnerability

Please report it privately, not in a public issue or discussion:

**https://github.com/NormanSisk/analyst-consortium/security/advisories/new**

This opens a private report that only you and the maintainer can read. If you cannot use that page, open a discussion that says only "I have a security report" and gives no details, and you will be given a private way to send it.

Please include:

- what you found and what an attacker could do with it,
- the steps to reproduce it, and the release number you ran,
- anything you have already tried, such as a fix.

## What to expect

One person maintains this project, so replies are best effort. You can expect an answer within about a week. A confirmed problem is fixed in the next release, and the release notes say what was fixed. You will be credited by name if you want to be, and not if you would rather stay anonymous. There is no bounty.

## Which versions are covered

The newest release only. Fixes are not carried back to older releases; update to the newest one.

## What counts

- The local web page (`tradingagents ui`): the checks that keep other web sites and other computers out, the per-session token, the file and folder actions, the saved-report and CSV routes.
- Where your keys, holdings file and results are kept, and anything that could print, upload or expose them. They are kept out of the repository and out of the Docker image on purpose.
- The update check and the Windows shortcut, which run on your own computer.
- A library we depend on that has a published advisory and that we have not yet updated.

## What does not count

- A rating, a price or a report that turns out to be wrong. That is a quality problem, not a security one, and the README says plainly what has not been shown. Open an issue or a discussion for it.
- A problem in code that comes unchanged from the original TradingAgents project by Tauric Research. Please report it to [them](https://github.com/TauricResearch/TradingAgents) as well; if you tell us too, we will pick up their fix.
- A problem inside an AI provider or a data source. Report it to that company.
- Anything that needs someone to already control your computer or your keys.

This is research software, not investment advice. Nothing in this policy changes that.
