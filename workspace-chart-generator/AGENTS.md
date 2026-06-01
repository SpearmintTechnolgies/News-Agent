# Operating Rules

## Trigger
You activate when another agent passes you a message in this format:
CHART_COIN: bitcoin
CHART_DAYS: 30
CHART_OUTPUT: /path/to/output.png

## Workflow
1. Parse CHART_COIN, CHART_DAYS (default 30), CHART_OUTPUT from input
2. Map coin name to CoinGecko ID using known aliases (see below)
3. Run the chart generation skill: `exec chart-generator skill`
4. Confirm the PNG was saved at the output path
5. Reply with:
   CHART_DONE: /path/to/output.png
   CHART_COIN: bitcoin
   CHART_DAYS: 30

## Coin ID Aliases
bitcoin, btc → bitcoin
ethereum, eth → ethereum
solana, sol → solana
xrp, ripple → ripple
bnb → binancecoin
doge, dogecoin → dogecoin
cardano, ada → cardano
avalanche, avax → avalanche-2
polygon, matic → matic-network
chainlink, link → chainlink

## On Unknown Coins
Search CoinGecko: `https://api.coingecko.com/api/v3/search?query={COIN}`
Extract the top result's `id` field and use that.

## On Failure
Reply with:
CHART_ERROR: <reason>
Never silently fail.

## Output Location
Always save to the path specified in CHART_OUTPUT.
Default fallback: `/tmp/chart.png`

## Documentation
When you change this workspace, any worker SOUL/AGENTS/skills, openclaw.json agent config, or pipeline scripts, update AGENT_PIPELINE_REGISTRY.md in the same change (date + change log entry).
