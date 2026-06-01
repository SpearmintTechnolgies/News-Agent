---
name: chart-generator
description: Generates a factual crypto price chart PNG from CoinGecko live data.
user-invocable: false
disable-model-invocation: false
metadata.openclaw: '{"os": ["linux", "darwin"], "requires": {"bins": ["python3"]}}'
---

You are executing the chart-generator skill.

Step 1: Run the chart script using exec:
  python3 {baseDir}/scripts/generate_chart.py --coin {CHART_COIN} --days {CHART_DAYS} --output {CHART_OUTPUT}

Step 2: Read the script's stdout to confirm success or capture the error.

Step 3: Report the result back to the caller.
