"""Where this project lives on the web, in one place.

The update check, the user-agent sent to Reddit and StockTwits, and the issue and release files
under .github all name this address; tests/test_project_address.py checks that they agree.
The original TradingAgents project is credited and linked, never contacted.
"""

OWNER = "NormanSisk"
REPO = "analyst-consortium"

HOME_URL = f"https://github.com/{OWNER}/{REPO}"
ISSUES_URL = f"{HOME_URL}/issues"
DISCUSSIONS_URL = f"{HOME_URL}/discussions"
SECURITY_REPORT_URL = f"{HOME_URL}/security/advisories/new"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

# Reddit and StockTwits ask for a descriptive user-agent that says who is calling.
USER_AGENT = f"analyst-consortium (+{HOME_URL})"

# The original project: credit and links only.
UPSTREAM_URL = "https://github.com/TauricResearch/TradingAgents"
